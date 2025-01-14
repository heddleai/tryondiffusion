import copy
import logging
import os

import torch
from torch.utils.data import DataLoader
from torch import optim
import torch.nn as nn
from torch.nn import functional as F
import cv2

from network import UNet64, UNet128
from utils.utils import mk_folders, GaussianSmoothing
from utils.dataloader_train import UNetDataset
from ema import EMA


def smoothen_image(img, sigma):

    # As suggested in:
    # https://jmlr.csail.mit.edu/papers/volume23/21-0635/21-0635.pdf Section 4.4

    smoothing2d = GaussianSmoothing(channels=3,
                                    kernel_size=3,
                                    sigma=sigma,
                                    conv_dim=2)

    img = F.pad(img, (1, 1, 1, 1), mode='reflect')
    img = smoothing2d(img)

    return img


class Diffusion:

    def __init__(self,
                 device,
                 pose_embed_dim,
                 time_steps=256,
                 beta_start=1e-4,
                 beta_end=0.02,
                 time_dim=256,
                 unet_dim=64,
                 noise_input_channel=3,
                 beta_ema=0.995):
        self.time_steps = time_steps
        self.beta_start = beta_start
        self.beta_end = beta_end

        self.beta = self.linear_beta_scheduler().to(device)
        self.alpha = 1 - self.beta
        self.alpha_cumprod = torch.cumprod(self.alpha, dim=0)

        self.noise_input_channel = noise_input_channel
        self.unet_dim = unet_dim
        if unet_dim == 128:
            self.net = UNet128(pose_embed_dim, time_dim).to(device)
        elif unet_dim == 64:
            self.net = UNet64(pose_embed_dim, time_dim).to(device)

        self.ema_net = copy.deepcopy(self.net).eval().requires_grad_(False)
        self.beta_ema = beta_ema

        self.device = device

    def linear_beta_scheduler(self):
        return torch.linspace(self.beta_start, self.beta_end, self.time_steps)

    def sample_time_steps(self, batch_size):
        return torch.randint(low=1, high=self.time_steps, size=(batch_size, ))

    def add_noise_to_img(self, img, t):
        sqrt_alpha_timestep = torch.sqrt(self.alpha_cumprod[t])[:, None, None, None]
        sqrt_one_minus_alpha_timestep = torch.sqrt(1 - self.alpha_cumprod[t])[:, None, None, None]
        epsilon = torch.randn_like(img)
        return (sqrt_alpha_timestep * epsilon) + (sqrt_one_minus_alpha_timestep * epsilon), epsilon

    @torch.inference_mode()
    def sample(self, use_ema, conditional_inputs):
        model = self.ema_net if use_ema else self.net
        ic, jp, jg, ia = conditional_inputs

        batch_size = len(ic)
        logging.info(f"Running inference for {batch_size} images")

        model.eval()
        with torch.inference_mode():

            # noise augmentation during testing as suggested in paper
            sigma = float(torch.FloatTensor(1).uniform_(0.4, 0.6))
            ia = smoothen_image(ia, sigma)
            ic = smoothen_image(ic, sigma)

            x = torch.randn(batch_size, self.noise_input_channel, self.unet_dim, self.unet_dim).to(self.device)

            # paper says to add noise augmentation to input noise during inference
            x = smoothen_image(x, sigma)

            # concatenating noise with rgb agnostic image across channels
            # corrupt -> concatenate -> predict
            x = torch.cat((x, ia), dim=1)

            for i in reversed(range(1, self.time_steps)):
                t = (torch.ones(batch_size) * i).long().to(self.device)
                predicted_noise = model(x, ic, jp, jg, t)
                # ToDo: Add Classifier-Free Guidance with guidance weight 2
                alpha = self.alpha[t][:, None, None, None]
                alpha_cumprod = self.alpha_cumprod[t][:, None, None, None]
                beta = self.beta[t][:, None, None, None]
                if i > 1:
                    noise = torch.randn_like(x)
                else:
                    noise = torch.zeros_like(x)

                x = 1 / torch.sqrt(alpha) * (x - ((1 - alpha) / (torch.sqrt(1 - alpha_cumprod))) * predicted_noise) + torch.sqrt(beta) * noise
        x = (x.clamp(-1, 1) + 1) / 2
        x = (x * 255).type(torch.uint8)
        return x

    def prepare(self, args):
        mk_folders(args.run_name)
        train_dataset = UNetDataset(ip_dir=args.train_ip_folder,
                                         jp_dir=args.train_jp_folder,
                                         jg_dir=args.train_jg_folder,
                                         ia_dir=args.train_ia_folder,
                                         ic_dir=args.train_ic_folder,
                                         unet_size=self.unet_dim)

        validation_dataset = UNetDataset(ip_dir=args.validation_ip_folder,
                                              jp_dir=args.validation_jp_folder,
                                              jg_dir=args.validation_jg_folder,
                                              ia_dir=args.validation_ia_folder,
                                              ic_dir=args.validation_ic_folder,
                                              unet_size=self.unet_dim)

        self.train_dataloader = DataLoader(train_dataset, args.batch_size_train, shuffle=True)
        # give args.batch_size_validation 1 while training
        self.val_dataloader = DataLoader(validation_dataset, args.batch_size_validation, shuffle=True)

        self.optimizer = optim.AdamW(self.net.parameters(), lr=args.lr, eps=1e-5)
        self.scheduler = optim.lr_scheduler.OneCycleLR(self.optimizer, max_lr=args.lr,
                                                       steps_per_epoch=len(self.train_dataloader), epochs=args.epochs)
        self.mse = nn.MSELoss()
        self.ema = EMA(self.beta_ema)
        self.scaler = torch.cuda.amp.GradScaler()

    def train_step(self, loss):
        self.optimizer.zero_grad()
        self.scaler.scale(loss).backward()
        self.scaler.step(self.optimizer)
        self.scaler.update()
        self.ema.step_ema(self.ema_net, self.net)
        self.scheduler.step()

    def single_epoch(self, train=True):
        avg_loss = 0.
        if train:
            self.net.train()
        else:
            self.net.eval()

        for ip, jp, jg, ia, ic in self.train_dataloader:

            # noise augmentation
            sigma = float(torch.FloatTensor(1).uniform_(0.4, 0.6))
            ia = smoothen_image(ia, sigma)
            ic = smoothen_image(ic, sigma)

            with torch.autocast(self.device) and (torch.inference_mode() if not train else torch.enable_grad()):
                ip = ip.to(self.device)
                jp = jp.to(self.device)
                jg = jg.to(self.device)
                ia = ia.to(self.device)
                ic = ic.to(self.device)
                t = self.sample_time_steps(ip.shape[0]).to(self.device)

                # corrupt -> concatenate -> predict
                zt, noise_epsilon = self.add_noise_to_img(ip, t)

                zt = torch.cat((zt, ia), dim=1)

                # ToDO: Make conditional inputs null, at 10% of the training time,
                # ToDo: for classifier-free guidance(GitHub Issue #21), with guidance weight 2.

                predicted_noise = self.net(zt, ic, jp, jg, t, sigma)
                loss = self.mse(noise_epsilon, predicted_noise)
                avg_loss += loss

            if train:
                self.train_step(loss)
                # ToDo: Add logs to tensorboard as well
                logging.info(f"train_mse_loss: {loss.item():2.3f}, learning_rate: {self.scheduler.get_last_lr()[0]}")

        return avg_loss.mean().item()

    def logging_images(self, epoch, run_name):

        for idx, ip, jp, jg, ia, ic in enumerate(self.val_dataloader[:4]):

            # sampled image
            sampled_image = self.sample(use_ema=False, conditional_inputs=(ic, jp, jg, ia))
            sampled_image = sampled_image[0].permute(1, 2, 0).squeeze().cpu().numpy()

            # ema sampled image
            ema_sampled_image = self.sample(use_ema=True, conditional_inputs=(ic, jp, jg, ia))
            ema_sampled_image = ema_sampled_image[0].permute(1, 2, 0).squeeze().cpu().numpy()

            # base images
            ip_np = ip[0].permute(1, 2, 0).squeeze().cpu().numpy()
            ic_np = ic[0].permute(1, 2, 0).squeeze().cpu().numpy()
            ia_np = ia[0].permute(1, 2, 0).squeeze().cpu().numpy()

            # make to folders
            os.makedirs(os.path.join("results", run_name, "images", f"{idx}_E{epoch}"), exist_ok=True)

            # define folder paths
            images_folder = os.path.join("results", run_name, "images", f"{idx}_E{epoch}")

            # save base images
            cv2.imwrite(os.path.join(images_folder, "ground_truth.png"), ip_np)
            cv2.imwrite(os.path.join(images_folder, "segmented_garment.png"), ic_np)
            cv2.imwrite(os.path.join(images_folder, "cloth_agnostic_rgb.png"), ia_np)

            # save sampled image
            cv2.imwrite(os.path.join(images_folder, "sampled_image"), sampled_image)

            # save ema sampled image
            cv2.imwrite(os.path.join(images_folder, "ema_sampled_image"), ema_sampled_image)

    def save_models(self, run_name, epoch=-1):

        torch.save(self.net.state_dict(), os.path.join("models", run_name, f"ckpt_{epoch}.pt"))
        torch.save(self.ema_net.state_dict(), os.path.join("models", run_name, f"ema_ckpt_{epoch}.pt"))
        torch.save(self.optimizer.state_dict(), os.path.join("models", run_name, f"optim_{epoch}.pt"))

    def fit(self, args):

        logging.info(f"Starting training")
        for epoch in args.epochs:
            logging.info(f"Starting Epoch: {epoch+1}")
            _ = self.single_epoch(train=True)

            if epoch % args.calculate_loss_frequency == 0:
                avg_loss = self.single_epoch(train=False)
                logging.info(f"Average Loss: {avg_loss}")

            if epoch % args.image_logging_frequency == 0:
                self.logging_images(epoch, args.run_name)

            if epoch % args.model_saving_frequency == 0:
                self.save_models(args.run_name, epoch)

        logging.info(f"Training Done Successfully! Yayyy! Now let's hope for good results")
