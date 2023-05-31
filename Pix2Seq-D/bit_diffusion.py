import math
from random import random
from functools import partial

from config import CFG
from utils import default, exists

import torch
from torch import nn, einsum
from torch.special import expm1
from torchvision import transforms as T
import torchvision.models as M
from einops import rearrange, reduce, repeat
from tqdm.auto import tqdm


# constants


BITS = CFG["bits"]

# small helper modules


class Residual(nn.Module):
    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def forward(self, x, *args, **kwargs):
        return self.fn(x, *args, **kwargs) + x


def Upsample(dim, dim_out=None):
    return nn.Sequential(
        nn.Upsample(scale_factor=2, mode='nearest'),
        nn.Conv2d(dim, default(dim_out, dim), 3, padding=1)
    )


def Downsample(dim, dim_out=None):
    return nn.Conv2d(dim, default(dim_out, dim), 4, 2, 1)


class LayerNorm(nn.Module):
    def __init__(self, dim):
        super().__init__()
        self.g = nn.Parameter(torch.ones(1, dim, 1, 1))

    def forward(self, x):
        eps = 1e-5 if x.dtype == torch.float32 else 1e-3
        var = torch.var(x, dim=1, unbiased=False, keepdim=True)
        mean = torch.mean(x, dim=1, keepdim=True)
        return (x - mean) * (var + eps).rsqrt() * self.g


class PreNorm(nn.Module):
    def __init__(self, dim, fn):
        super().__init__()
        self.fn = fn
        self.norm = LayerNorm(dim)

    def forward(self, x, context=None):
        x = self.norm(x)
        if exists(context):
            context = self.norm(context)
        return self.fn(x, context)

# positional embeds


class LearnedSinusoidalPosEmb(nn.Module):
    """ following @crowsonkb 's lead with learned sinusoidal pos emb """
    """ https://github.com/crowsonkb/v-diffusion-jax/blob/master/diffusion/models/danbooru_128.py#L8 """

    def __init__(self, dim):
        super().__init__()
        assert (dim % 2) == 0
        half_dim = dim // 2
        self.weights = nn.Parameter(torch.randn(half_dim))

    def forward(self, x):
        x = rearrange(x, 'b -> b 1')
        freqs = x * rearrange(self.weights, 'd -> 1 d') * 2 * math.pi
        fouriered = torch.cat((freqs.sin(), freqs.cos()), dim=-1)
        fouriered = torch.cat((x, fouriered), dim=-1)
        return fouriered

# building block modules


class Block(nn.Module):
    def __init__(self, dim, dim_out, groups=8):
        super().__init__()
        self.proj = nn.Conv2d(dim, dim_out, 3, padding=1)
        self.norm = nn.GroupNorm(groups, dim_out)
        self.act = nn.SiLU()

    def forward(self, x, scale_shift=None):
        x = self.proj(x)
        x = self.norm(x)

        if exists(scale_shift):
            scale, shift = scale_shift
            x = x * (scale + 1) + shift

        x = self.act(x)
        return x


class ResnetBlock(nn.Module):
    def __init__(self, dim, dim_out, *, time_emb_dim=None, groups=8):
        super().__init__()
        self.mlp = nn.Sequential(
            nn.SiLU(),
            nn.Linear(time_emb_dim, dim_out * 2)
        ) if exists(time_emb_dim) else None

        self.block1 = Block(dim, dim_out, groups=groups)
        self.block2 = Block(dim_out, dim_out, groups=groups)
        self.res_conv = nn.Conv2d(
            dim, dim_out, 1) if dim != dim_out else nn.Identity()

    def forward(self, x, time_emb=None):

        scale_shift = None
        if exists(self.mlp) and exists(time_emb):
            time_emb = self.mlp(time_emb)
            time_emb = rearrange(time_emb, 'b c -> b c 1 1')
            scale_shift = time_emb.chunk(2, dim=1)

        h = self.block1(x, scale_shift=scale_shift)

        h = self.block2(h)

        return h + self.res_conv(x)


class LinearAttention(nn.Module):
    def __init__(self, dim, heads=4, dim_head=32):
        super().__init__()
        self.scale = dim_head ** -0.5
        self.heads = heads
        hidden_dim = dim_head * heads
        self.to_q = nn.Conv2d(dim, hidden_dim * 1, 1, bias=False)
        self.to_kv = nn.Conv2d(dim, hidden_dim * 2, 1, bias=False)

        self.to_out = nn.Sequential(
            nn.Conv2d(hidden_dim, dim, 1),
            LayerNorm(dim)
        )

    def forward(self, x, context=None):
        b, c, h, w = x.shape
        context = default(context, x)
        qkv = (self.to_q(x), *self.to_kv(context).chunk(2, dim=1))
        q, k, v = map(lambda t: rearrange(
            t, 'b (h c) x y -> b h c (x y)', h=self.heads), qkv)

        q = q.softmax(dim=-2)
        k = k.softmax(dim=-1)

        q = q * self.scale
        v = v / (h * w)

        context = torch.einsum('b h d n, b h e n -> b h d e', k, v)

        out = torch.einsum('b h d e, b h d n -> b h e n', context, q)
        out = rearrange(out, 'b h c (x y) -> b (h c) x y',
                        h=self.heads, x=h, y=w)
        return self.to_out(out)


class Attention(nn.Module):
    def __init__(self, dim, heads=4, dim_head=32):
        super().__init__()
        self.scale = dim_head ** -0.5
        self.heads = heads
        hidden_dim = dim_head * heads
        self.to_q = nn.Conv2d(dim, hidden_dim * 1, 1, bias=False)
        self.to_kv = nn.Conv2d(dim, hidden_dim * 2, 1, bias=False)
        self.to_out = nn.Conv2d(hidden_dim, dim, 1)

    def forward(self, x, context=None):
        b, c, h, w = x.shape

        context = default(context, x)

        qkv = (self.to_q(x), *self.to_kv(context).chunk(2, dim=1))
        q, k, v = map(lambda t: rearrange(
            t, 'b (h c) x y -> b h c (x y)', h=self.heads), qkv)

        q = q * self.scale

        sim = einsum('b h d i, b h d j -> b h i j', q, k)
        attn = sim.softmax(dim=-1)
        out = einsum('b h i j, b h d j -> b h i d', attn, v)
        out = rearrange(out, 'b h (x y) d -> b (h d) x y', x=h, y=w)
        return self.to_out(out)

# model


class Unet(nn.Module):
    def __init__(
        self,
        dim,
        init_dim=None,
        dim_mults=(1, 2, 4, 8),
        channels=3,
        embedding_channels=24,
        bits=BITS,
        resnet_block_groups=8,
        learned_sinusoidal_dim=16
    ):
        super().__init__()

        # determine dimensions

        channels *= bits
        self.channels = channels

        input_channels = channels * (2 + CFG["misc"]["frames_before"]) + embedding_channels
        init_dim = default(init_dim, dim)
        self.init_conv = nn.Conv2d(input_channels, init_dim, 7, padding=3)

        dims = [init_dim, *map(lambda m: dim * m, dim_mults)]
        in_out = list(zip(dims[:-1], dims[1:]))

        block_klass = partial(ResnetBlock, groups=resnet_block_groups)

        # time embeddings

        time_dim = dim * 4

        sinu_pos_emb = LearnedSinusoidalPosEmb(learned_sinusoidal_dim)
        fourier_dim = learned_sinusoidal_dim + 1

        self.time_mlp = nn.Sequential(
            sinu_pos_emb,
            nn.Linear(fourier_dim, time_dim),
            nn.GELU(),
            nn.Linear(time_dim, time_dim)
        )

        # layers

        self.downs = nn.ModuleList([])
        self.ups = nn.ModuleList([])
        num_resolutions = len(in_out)

        for ind, (dim_in, dim_out) in enumerate(in_out):
            is_last = ind >= (num_resolutions - 1)

            self.downs.append(nn.ModuleList([
                block_klass(dim_in, dim_in, time_emb_dim=time_dim),
                block_klass(dim_in, dim_in, time_emb_dim=time_dim),
                Residual(PreNorm(dim_in, LinearAttention(dim_in))),
                Downsample(dim_in, dim_out) if not is_last else nn.Conv2d(
                    dim_in, dim_out, 3, padding=1)
            ]))

        mid_dim = dims[-1]
        self.mid_block1 = block_klass(mid_dim, mid_dim, time_emb_dim=time_dim)
        self.mid_attn = Residual(PreNorm(mid_dim, Attention(mid_dim)))
        self.mid_block2 = block_klass(mid_dim, mid_dim, time_emb_dim=time_dim)

        for ind, (dim_in, dim_out) in enumerate(reversed(in_out)):
            is_last = ind == (len(in_out) - 1)

            self.ups.append(nn.ModuleList([
                block_klass(dim_out + dim_in, dim_out, time_emb_dim=time_dim),
                block_klass(dim_out + dim_in, dim_out, time_emb_dim=time_dim),
                Residual(PreNorm(dim_out, LinearAttention(dim_out))),
                Upsample(dim_out, dim_in) if not is_last else nn.Conv2d(
                    dim_out, dim_in, 3, padding=1)
            ]))

        self.final_res_block = block_klass(dim * 2, dim, time_emb_dim=time_dim)
        self.final_conv = nn.Conv2d(dim, channels, 1)

    def forward(self, x, time, x_self_cond, embedding, conditioning=None, fpn=None):
        
        x_self_cond = default(
            x_self_cond, lambda: torch.zeros_like(x))
        conditioning = default(
            conditioning,
            lambda: torch.zeros(x.shape[0], CFG["misc"]["frames_before"] * self.channels, *embedding.shape[2:]).to(CFG["device"])
        )
        
        x = torch.cat((conditioning, x_self_cond, embedding, x), dim=1)

        x = self.init_conv(x)
        r = x.clone()

        t = self.time_mlp(time)

        h = []

        for i, (block1, block2, attn, downsample) in enumerate(self.downs):
            x = block1(x, t)
            h.append(x)

            x = block2(x, t)

            x = attn(x, fpn[i])
            x = attn(x)
            h.append(x)

            x = downsample(x)

        x = self.mid_block1(x, t)
        x = self.mid_attn(x, fpn[-1])
        x = self.mid_block2(x, t)
        
        for block1, block2, attn, upsample in self.ups:
            x = torch.cat((x, h.pop()), dim=1)
            x = block1(x, t)

            x = torch.cat((x, h.pop()), dim=1)
            x = block2(x, t)
            x = attn(x)
            
            x = upsample(x)

        x = torch.cat((x, r), dim=1)

        x = self.final_res_block(x, t)
        return self.final_conv(x)

# convert to bit representations and back


def decimal_to_bits(x, bits=BITS):
    """ expects image tensor ranging from 0 to 1, outputs bit tensor ranging from -1 to 1 """
    device = x.device

    x = (x * 255).int().clamp(0, 255)

    mask = 2 ** torch.arange(bits - 1, -1, -1, device=device)
    mask = rearrange(mask, 'd -> d 1 1')
    x = rearrange(x, 'b c h w -> b c 1 h w')

    bits = ((x & mask) != 0).float()
    bits = rearrange(bits, 'b c d h w -> b (c d) h w')
    bits = bits * 2 - 1
    return bits


def bits_to_decimal(x, bits=BITS):
    """ expects bits from -1 to 1, outputs image tensor from 0 to 1 """
    device = x.device

    x = (x > 0).int()
    mask = 2 ** torch.arange(bits - 1, -1, -1,
                             device=device, dtype=torch.int32)

    mask = rearrange(mask, 'd -> d 1 1')
    x = rearrange(x, 'b (c d) h w -> b c d h w', d=8)
    dec = reduce(x * mask, 'b c d h w -> b c h w', 'sum')
    return (dec / 255).clamp(0., 1.)

# bit diffusion class


def log(t, eps=1e-20):
    return torch.log(t.clamp(min=eps))


def right_pad_dims_to(x, t):
    padding_dims = x.ndim - t.ndim
    if padding_dims <= 0:
        return t
    return t.view(*t.shape, *((1,) * padding_dims))


def beta_linear_log_snr(t):
    return -torch.log(expm1(1e-4 + 10 * (t ** 2)))


def alpha_cosine_log_snr(t, s: float = 0.008):
    # not sure if this accounts for beta being clipped to 0.999 in discrete version
    return -log((torch.cos((t + s) / (1 + s) * math.pi * 0.5) ** -2) - 1, eps=1e-5)


def log_snr_to_alpha_sigma(log_snr):
    return torch.sqrt(torch.sigmoid(log_snr)), torch.sqrt(torch.sigmoid(-log_snr))


class BitDiffusion(nn.Module):
    def __init__(
        self,
        model,
        *,
        image_size,
        embedding_size,
        timesteps=1000,
        use_ddim=False,
        noise_schedule='cosine',
        time_difference=0.,
        bit_scale=1.
    ):
        super().__init__()
        self.model = model
        self.channels = self.model.channels

        self.image_size = image_size
        self.embedding_size = embedding_size

        if noise_schedule == "linear":
            self.log_snr = beta_linear_log_snr
        elif noise_schedule == "cosine":
            self.log_snr = alpha_cosine_log_snr
        else:
            raise ValueError(f'invalid noise schedule {noise_schedule}')

        self.bit_scale = bit_scale

        self.timesteps = timesteps
        self.use_ddim = use_ddim

        # proposed in the paper, summed to time_next
        # as a way to fix a deficiency in self-conditioning and lower FID when the number of sampling timesteps is < 400

        self.time_difference = time_difference

    @property
    def device(self):
        return next(self.model.parameters()).device

    def get_sampling_timesteps(self, batch, *, device):
        times = torch.linspace(1., 0., self.timesteps + 1, device=device)
        times = repeat(times, 't -> b t', b=batch)
        times = torch.stack((times[:, :-1], times[:, 1:]), dim=0)
        times = times.unbind(dim=-1)
        return times

    @torch.no_grad()
    def ddpm_sample(self, shape, embedding, time_difference=None, conditioning=None, fpn=None):
        batch, device = shape[0], self.device

        time_difference = default(time_difference, self.time_difference)

        time_pairs = self.get_sampling_timesteps(batch, device=device)

        img = torch.randn(shape, device=device)
                
        # downscale the image to match the embedding size

        img = T.Resize(embedding.shape[2:])(img)
        
        if conditioning is not None:
            conditioning = torch.cat(conditioning, dim=1)
            conditioning = decimal_to_bits(T.Resize(embedding.shape[2:])(conditioning)) * self.bit_scale

        x_start = None

        for time, time_next in tqdm(time_pairs, desc='sampling loop time step', total=self.timesteps):

            # add the time delay

            time_next = (time_next - self.time_difference).clamp(min=0.)

            noise_cond = self.log_snr(time)

            # get predicted x0

            x_start = self.model(img, noise_cond, x_start,
                                 embedding, conditioning, fpn)

            # clip x0

            x_start.clamp_(-self.bit_scale, self.bit_scale)

            # get log(snr)

            log_snr = self.log_snr(time)
            log_snr_next = self.log_snr(time_next)
            log_snr, log_snr_next = map(
                partial(right_pad_dims_to, img), (log_snr, log_snr_next))

            # get alpha sigma of time and next time

            alpha, sigma = log_snr_to_alpha_sigma(log_snr)
            alpha_next, sigma_next = log_snr_to_alpha_sigma(log_snr_next)

            # derive posterior mean and variance

            c = -expm1(log_snr - log_snr_next)

            mean = alpha_next * (img * (1 - c) / alpha + c * x_start)
            variance = (sigma_next ** 2) * c
            log_variance = log(variance)

            # get noise

            noise = torch.where(
                rearrange(time_next > 0, 'b -> b 1 1 1'),
                torch.randn_like(img),
                torch.zeros_like(img)
            )

            img = mean + (0.5 * log_variance).exp() * noise

        return img

    @torch.no_grad()
    def ddim_sample(self, shape, embedding, time_difference=None, conditioning=None, fpn=None):
        batch, device = shape[0], self.device

        time_difference = default(time_difference, self.time_difference)

        time_pairs = self.get_sampling_timesteps(batch, device=device)

        img = torch.randn(shape, device=device)
        
        # downscale the image to match the embedding size

        img = T.Resize(embedding.shape[2:])(img)
        
        if conditioning is not None:
            conditioning = torch.cat(conditioning, dim=1)
            conditioning = decimal_to_bits(T.Resize(embedding.shape[2:])(conditioning)) * self.bit_scale

        x_start = None

        for times, times_next in tqdm(time_pairs, desc='sampling loop time step'):

            # get times and noise levels

            log_snr = self.log_snr(times)
            log_snr_next = self.log_snr(times_next)

            padded_log_snr, padded_log_snr_next = map(
                partial(right_pad_dims_to, img), (log_snr, log_snr_next))

            alpha, sigma = log_snr_to_alpha_sigma(padded_log_snr)
            alpha_next, sigma_next = log_snr_to_alpha_sigma(
                padded_log_snr_next)

            # add the time delay

            times_next = (times_next - time_difference).clamp(min=0.)

            # predict x0

            x_start = self.model(img, log_snr, x_start,
                                 embedding, conditioning, fpn)

            # clip x0

            x_start.clamp_(-self.bit_scale, self.bit_scale)

            # get predicted noise

            pred_noise = (img - alpha * x_start) / sigma.clamp(min=1e-8)

            # calculate x next

            img = x_start * alpha_next + pred_noise * sigma_next

        return img

    @torch.no_grad()
    def sample(self, batch_size, embedding, conditioning, fpn):
        image_size, channels = self.embedding_size, self.channels
        sample_fn = self.ddpm_sample if not self.use_ddim else self.ddim_sample
        pred = sample_fn((batch_size, channels, image_size.height, image_size.width), embedding=embedding, conditioning=conditioning, fpn=fpn)
        return bits_to_decimal(pred), pred

    def forward(self, img, embedding, conditioning, fpn, *args, **kwargs):
        """
            img: (batch, channels, height, width) the panoptic mask to be denoised
            embedding: (batch, embedding_ch, embedding_dim, embedding_dim) the embedding to condition on
            conditioning: [(batch, channels, height, width), ] list of images/masks to condition on
            fpn: [(batch, embedding_dim), ] the fpn embeddings to condition on at each scale
        """

        batch, c, h, w, device, img_size, = *img.shape, img.device, self.image_size
        assert h == img_size.height and w == img_size.width, f'height and width of image must be {img_size}'

        # sample random times

        times = torch.zeros((batch,), device=device).float().uniform_(0, 1.)

        # downscale the image to match the embedding size

        img = T.Resize(embedding.shape[2:])(img)

        # convert image to bit representation

        img = decimal_to_bits(img) * self.bit_scale

        if conditioning is not None:
            conditioning = torch.cat(conditioning, dim=1)
            conditioning = decimal_to_bits(T.Resize(embedding.shape[2:])(conditioning)) * self.bit_scale
        
        # noise sample

        noise = torch.randn_like(img)

        noise_level = self.log_snr(times)
        padded_noise_level = right_pad_dims_to(img, noise_level)
        alpha, sigma = log_snr_to_alpha_sigma(padded_noise_level)

        noised_img = alpha * img + sigma * noise

        # if doing self-conditioning, 50% of the time, predict x_start from current set of times
        # and condition with unet with that
        # this technique will slow down training by 25%, but seems to lower FID significantly

        self_cond = None
        if random() < 0.5:
            with torch.no_grad():
                self_cond = self.model(
                    noised_img, noise_level, None, embedding, conditioning, fpn).detach_()

        # predict and take gradient step

        pred = self.model(noised_img, noise_level, self_cond,
                          embedding, conditioning, fpn)

        # return F.cross_entropy(pred, img)
        return CFG["train"]["loss"](pred, img)


class Encoder(nn.Module):
    def __init__(self, freeze_backbone=True):
        super().__init__()
        self.model = M.detection.fasterrcnn_resnet50_fpn_v2(
            weights=M.detection.FasterRCNN_ResNet50_FPN_V2_Weights.DEFAULT).backbone
        self.conv = nn.ModuleList([
            nn.Conv2d(256, 32, kernel_size=5, padding=2),
            nn.Conv2d(256, 32, kernel_size=3, padding=1),
            nn.Conv2d(256, 64, kernel_size=3, padding=1),
            nn.Conv2d(256, 128, kernel_size=3, padding=1)
        ])
        
        for param in self.model.parameters():
            param.requires_grad = freeze_backbone

    def forward(self, x):
        features = list(self.model(x).values())[:-1]
        compressed_features = list(
            map(lambda x: x[0](x[1]), zip(self.conv, features))) + [features[-1]]
        return compressed_features


class EncoderDecoder(nn.Module):
    def __init__(self, encoder, decoder):
        super().__init__()
        self.encoder = encoder
        self.decoder = decoder
        self.bit_scale = decoder.bit_scale

    def forward(self, image_list, mask_list):
        # lists are ordered from oldest to newest frame
        original_image = image_list[-1]
        mask = mask_list[-1]

        fpn_embeddings = self.encoder(original_image)
        preds = self.decoder(mask, fpn_embeddings[0], mask_list[:-1], fpn_embeddings)
        return preds

    def sample(self, image, mask_list, ground_truth=None):
        # lists are ordered from oldest to newest frame
        
        fpn_embeddings = self.encoder(image)
        embedding = fpn_embeddings[0]
        preds, bits_pred = self.decoder.sample(
            image.shape[0], embedding, mask_list, fpn_embeddings)
        
        if ground_truth is not None:
            ground_truth = decimal_to_bits(T.Resize(embedding.shape[2:])(ground_truth)) * self.bit_scale
            return preds, CFG["train"]["loss"](bits_pred, ground_truth)
        return preds