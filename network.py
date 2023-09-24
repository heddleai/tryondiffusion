import torch
import torch.nn as nn
import torch.nn.functional as F


class AttentionPool1D(nn.Module):

    def __init__(self, pose_embeb_dim: int, num_heads: int, output_dim: int = None):
        """
        Clip inspired 1D attention pooling
        :param pose_embeb_dim:
        :param num_heads:
        :param output_dim:
        """
        super().__init__()
        self.positional_embedding = nn.Parameter(torch.randn(2, pose_embeb_dim) / pose_embeb_dim ** 0.5)
        self.k_proj = nn.Linear(pose_embeb_dim, pose_embeb_dim)
        self.q_proj = nn.Linear(pose_embeb_dim, pose_embeb_dim)
        self.v_proj = nn.Linear(pose_embeb_dim, pose_embeb_dim)
        self.c_proj = nn.Linear(pose_embeb_dim, output_dim or pose_embeb_dim)
        self.num_heads = num_heads

    def forward(self, x):
        # if x in format NP
        # N - Batch Dimension, P - Pose Dimension
        x = x[None, :, :]  # NN -> 1NP
        x = torch.cat([x.mean(dim=0, keepdim=True), x], dim=0)  # 2NP
        x = x + self.positional_embedding[:, None, :].to(x.dtype)  # 2NP
        x, _ = F.multi_head_attention_forward(
            query=x[:1], key=x, value=x,
            embed_dim_to_check=8,
            num_heads=self.num_heads,
            q_proj_weight=self.q_proj.weight,
            k_proj_weight=self.k_proj.weight,
            v_proj_weight=self.v_proj.weight,
            in_proj_weight=None,
            in_proj_bias=torch.cat([self.q_proj.bias, self.k_proj.bias, self.v_proj.bias]),
            bias_k=None,
            bias_v=None,
            add_zero_attn=False,
            dropout_p=0,
            out_proj_weight=self.c_proj.weight,
            out_proj_bias=self.c_proj.bias,
            use_separate_proj_weight=True,
            training=self.training,
            need_weights=False
        )
        return x.squeeze(0)


class FiLM(nn.Module):

    def __init__(self, inp_dim):
        super().__init__()
        self.fc = nn.Linear(inp_dim, 2)
        self.activation = nn.ReLU()

    def forward(self, clip_embeddings):
        x = self.fc1(clip_embeddings)
        x = self.activation(x)
        return self.gamma * x + self.beta


class ResBlockNoAttention(nn.Module):

    def __init__(self, inp_channel, out_channel, clip_embeddings):
        super().__init__()
        self.gn1 = nn.GroupNorm(min(32, int(abs(len(inp_channel)/4))))
        self.swish1 = nn.SiLU(True)
        self.conv1 = nn.Conv2d(inp_channel, inp_channel)
        self.gn2 = nn.GroupNorm(min(32, int(abs(len(inp_channel) / 4))))
        self.swish2 = nn.SiLU(True)
        self.conv2 = nn.Conv2d(inp_channel, out_channel)

    def forward(self, x):
        residual = self.conv(x)

        x = self.gn1(x)
        x = self.swish1(x)
        x = self.conv1(x)
        x = self.gn2(x)
        x = self.swish2(x)
        x = self.conv2(x)

        x += residual

        return x