import math
from multiprocessing import cpu_count
from pathlib import Path
from random import random
from functools import partial
from collections import namedtuple

import torch
import torch.nn.functional as F
from torch import nn, einsum
from torch.optim import Adam
from torch.utils.data import Dataset, DataLoader

import torchaudio

from einops import rearrange, reduce, repeat
from einops.layers.torch import Rearrange

from audiolm_pytorch import SoundStream, EncodecWrapper
from audiolm_pytorch.data import SoundDataset, get_dataloader

from beartype import beartype
from beartype.typing import Tuple, Union, Optional

from naturalspeech2_pytorch.attend import Attend

from accelerate import Accelerator
from ema_pytorch import EMA

from tqdm.auto import tqdm

# constants

mlist = nn.ModuleList

def Sequential(*mods):
    return nn.Sequential(*filter(exists, mods))

# helpers functions

def exists(x):
    return x is not None

def default(val, d):
    if exists(val):
        return val
    return d() if callable(d) else d

def identity(t, *args, **kwargs):
    return t

def has_int_squareroot(num):
    return (math.sqrt(num) ** 2) == num

# sinusoidal positional embeds

class LearnedSinusoidalPosEmb(nn.Module):
    def __init__(self, dim):
        super().__init__()
        assert (dim % 2) == 0
        half_dim = dim // 2
        self.weights = nn.Parameter(torch.randn(half_dim))

    def forward(self, x):
        x = rearrange(x, 'b -> b 1')
        freqs = x * rearrange(self.weights, 'd -> 1 d') * 2 * math.pi
        fouriered = torch.cat((freqs.sin(), freqs.cos()), dim = -1)
        fouriered = torch.cat((x, fouriered), dim = -1)
        return fouriered

# model, which is wavenet + transformer

class CausalConv1d(nn.Conv1d):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        kernel_size, = self.kernel_size
        dilation, = self.dilation
        stride, = self.stride

        assert stride == 1
        self.causal_padding = dilation * (kernel_size - 1)

    def forward(self, x):
        causal_padded_x = F.pad(x, (self.causal_padding, 0), value = 0.)
        return super().forward(causal_padded_x)

class WavenetResBlock(nn.Module):
    def __init__(
        self,
        dim,
        *,
        dilation,
        kernel_size = 3,
        skip_conv = False,
        dim_time_mult = None
    ):
        super().__init__()

        self.cond_time = exists(dim_time_mult)
        self.to_time_cond = None

        if self.cond_time:
            self.to_time_cond = nn.Linear(dim * dim_time_mult, dim * 2)

        self.conv = CausalConv1d(dim, dim, kernel_size, dilation = dilation)
        self.res_conv = CausalConv1d(dim, dim, 1)
        self.skip_conv = CausalConv1d(dim, dim, 1) if skip_conv else None

    def forward(self, x, t = None):

        if self.cond_time:
            assert exists(t)
            t = self.to_time_cond(t)
            t = rearrange(t, 'b c -> b c 1')
            t_gamma, t_beta = t.chunk(2, dim = -2)

        res = self.res_conv(x)

        x = self.conv(x)

        if self.cond_time:
            x = x * t_gamma + t_beta

        x = x.tanh() * x.sigmoid()

        x = x + res

        skip = None
        if exists(self.skip_conv):
            skip = self.skip_conv(x)

        return x, skip


class WavenetStack(nn.Module):
    def __init__(
        self,
        dim,
        *,
        layers,
        kernel_size = 3,
        has_skip = False,
        dim_time_mult = None
    ):
        super().__init__()
        dilations = 2 ** torch.arange(layers)

        self.has_skip = has_skip
        self.blocks = mlist([])

        for dilation in dilations.tolist():
            block = WavenetResBlock(
                dim = dim,
                kernel_size = kernel_size,
                dilation = dilation,
                skip_conv = has_skip,
                dim_time_mult = dim_time_mult
            )

            self.blocks.append(block)

    def forward(self, x, t):
        residuals = []
        skips = []

        if isinstance(x, torch.Tensor):
            x = (x,) * len(self.blocks)

        for block_input, block in zip(x, self.blocks):
            residual, skip = block(block_input, t)

            residuals.append(residual)
            skips.append(skip)

        if self.has_skip:
            return torch.stack(skips)

        return residuals

class Wavenet(nn.Module):
    def __init__(
        self,
        dim,
        *,
        stacks,
        layers,
        init_conv_kernel = 3,
        dim_time_mult = None
    ):
        super().__init__()
        self.init_conv = CausalConv1d(dim, dim, init_conv_kernel)
        self.stacks = mlist([])

        for ind in range(stacks):
            is_last = ind == (stacks - 1)

            stack = WavenetStack(
                dim,
                layers = layers,
                dim_time_mult = dim_time_mult,
                has_skip = is_last
            )

            self.stacks.append(stack)

        self.final_conv = CausalConv1d(dim, dim, 1)

    def forward(self, x, t = None):

        x = self.init_conv(x)

        for stack in self.stacks:
            x = stack(x, t)

        return self.final_conv(x.sum(dim = 0))

class RMSNorm(nn.Module):
    def __init__(self, dim, scale = True):
        super().__init__()
        self.scale = dim ** 0.5
        self.gamma = nn.Parameter(torch.ones(dim)) if scale else None

    def forward(self, x):
        gamma = default(self.gamma, 1)
        return F.normalize(x, dim = -1) * self.scale * gamma

class Transformer(nn.Module):
    def __init__(
        self,
        dim,
        *,
        depth,
        dim_head = 64,
        heads = 8,
        ff_mult = 4,
        ff_causal_conv = False,
        dim_time_mult = None,
        use_flash = False
    ):
        super().__init__()
        self.dim = dim
        self.layers = mlist([])

        cond_time = exists(dim_time_mult)

        self.to_time_cond = None
        self.cond_time = cond_time

        if cond_time:
            self.to_time_cond = nn.Linear(dim * dim_time_mult, dim * 4)

        for _ in range(depth):
            self.layers.append(mlist([
                RMSNorm(dim, scale = not cond_time),
                Attention(dim = dim, dim_head = dim_head, heads = heads, use_flash = use_flash),
                RMSNorm(dim, scale = not cond_time),
                FeedForward(dim = dim, mult = ff_mult, causal_conv = ff_causal_conv)
            ]))

        self.to_pred = nn.Sequential(
            RMSNorm(dim),
            nn.Linear(dim, dim, bias = False)
        )

    def forward(
        self,
        x,
        times = None
    ):
        if self.cond_time:
            assert exists(times)
            t = self.to_time_cond(times)
            t = rearrange(t, 'b d -> b 1 d')
            t_attn_gamma, t_attn_beta, t_ff_gamma, t_ff_beta = t.chunk(4, dim = -1)

        for attn_norm, attn, ff_norm, ff in self.layers:
            res = x
            x = attn_norm(x)

            if self.cond_time:
                x = x * t_attn_gamma + t_attn_beta

            x = attn(x) + res

            res = x
            x = ff_norm(x)

            if self.cond_time:
                x = x * t_ff_gamma + t_ff_beta

            x = ff(x) + res

        return self.to_pred(x)

class Model(nn.Module):
    def __init__(
        self,
        dim,
        *,
        depth,
        dim_head = 64,
        heads = 8,
        ff_mult = 4,
        wavenet_layers = 8,
        wavenet_stacks = 4,
        dim_time_mult = 4,
        use_flash_attn = True
    ):
        super().__init__()
        self.dim = dim

        # time condition

        dim_time = dim * dim_time_mult

        self.to_time_cond = Sequential(
            LearnedSinusoidalPosEmb(dim),
            nn.Linear(dim + 1, dim_time),
            nn.SiLU()
        )

        # wavenet

        self.wavenet = Wavenet(
            dim = dim,
            stacks = wavenet_stacks,
            layers = wavenet_layers,
            dim_time_mult = dim_time_mult
        )

        # transformer

        self.transformer = Transformer(
            dim = dim,
            depth = depth,
            dim_head = dim_head,
            heads = heads,
            ff_mult = ff_mult,
            ff_causal_conv = True,
            dim_time_mult = dim_time_mult,
            use_flash = use_flash_attn,
        )

    def forward(
        self,
        x,
        times
    ):
        t = self.to_time_cond(times)

        x = rearrange(x, 'b n d -> b d n')
        x = self.wavenet(x, t)
        x = rearrange(x, 'b d n -> b n d')

        x = self.transformer(x, t)
        return x

# feedforward

class GEGLU(nn.Module):
    def forward(self, x):
        x, gate = x.chunk(2, dim = -1)
        return F.gelu(gate) * x

def FeedForward(dim, mult = 4, causal_conv = False):
    dim_inner = int(dim * mult * 2 / 3)

    conv = None
    if causal_conv:
        conv = nn.Sequential(
            Rearrange('b n d -> b d n'),
            CausalConv1d(dim_inner, dim_inner, 3),
            Rearrange('b d n -> b n d'),
        )

    return Sequential(
        nn.Linear(dim, dim_inner * 2),
        GEGLU(),
        conv,
        nn.Linear(dim_inner, dim)
    )

# attention

class Attention(nn.Module):
    def __init__(
        self,
        dim,
        *,
        causal = False,
        dim_head = 64,
        heads = 8,
        dropout = 0.,
        use_flash = False
    ):
        super().__init__()
        self.scale = dim_head ** -0.5
        self.heads = heads

        dim_inner = dim_head * heads

        self.attend = Attend(causal = causal, dropout = dropout, use_flash = use_flash)
        self.to_qkv = nn.Linear(dim, dim_inner * 3, bias = False)
        self.to_out = nn.Linear(dim_inner, dim, bias = False)

    def forward(self, x):
        h = self.heads

        q, k, v = self.to_qkv(x).chunk(3, dim = -1)
        q, k, v = map(lambda t: rearrange(t, 'b n (h d) -> b h n d', h = h), (q, k, v))

        q = q * self.scale

        sim = einsum('b h i d, b h j d -> b h i j', q, k)

        attn = sim.softmax(dim = -1)

        out = einsum('b h i j, b h j d -> b h i d', attn, v)

        out = rearrange(out, 'b h n d -> b n (h d)')
        return self.to_out(out)

# tensor helper functions

def log(t, eps = 1e-20):
    return torch.log(t.clamp(min = eps))

def safe_div(numer, denom):
    return numer / denom.clamp(min = 1e-10)

def right_pad_dims_to(x, t):
    padding_dims = x.ndim - t.ndim
    if padding_dims <= 0:
        return t
    return t.view(*t.shape, *((1,) * padding_dims))

# noise schedules

def simple_linear_schedule(t, clip_min = 1e-9):
    return (1 - t).clamp(min = clip_min)

def cosine_schedule(t, start = 0, end = 1, tau = 1, clip_min = 1e-9):
    power = 2 * tau
    v_start = math.cos(start * math.pi / 2) ** power
    v_end = math.cos(end * math.pi / 2) ** power
    output = math.cos((t * (end - start) + start) * math.pi / 2) ** power
    output = (v_end - output) / (v_end - v_start)
    return output.clamp(min = clip_min)

def sigmoid_schedule(t, start = -3, end = 3, tau = 1, clamp_min = 1e-9):
    v_start = torch.tensor(start / tau).sigmoid()
    v_end = torch.tensor(end / tau).sigmoid()
    gamma = (-((t * (end - start) + start) / tau).sigmoid() + v_end) / (v_end - v_start)
    return gamma.clamp_(min = clamp_min, max = 1.)

# converting gamma to alpha, sigma or logsnr

def gamma_to_alpha_sigma(gamma, scale = 1):
    return torch.sqrt(gamma) * scale, torch.sqrt(1 - gamma)

def gamma_to_log_snr(gamma, scale = 1, eps = 1e-5):
    return log(gamma * (scale ** 2) / (1 - gamma), eps = eps)

# gaussian diffusion

class NaturalSpeech2(nn.Module):

    @beartype
    def __init__(
        self,
        model: Model,
        codec: Optional[Union[SoundStream, EncodecWrapper]] = None,
        *,
        target_sample_hz = None,
        timesteps = 1000,
        use_ddim = True,
        noise_schedule = 'sigmoid',
        objective = 'v',
        schedule_kwargs: dict = dict(),
        time_difference = 0.,
        min_snr_loss_weight = True,
        min_snr_gamma = 5,
        train_prob_self_cond = 0.9,
        rvq_cross_entropy_loss_weight = 0.,    # default this to off until we are sure it is working. not totally sold that this is critical
        scale = 1.                             # this will be set to < 1. for better convergence when training on higher resolution images
    ):
        super().__init__()
        self.model = model
        self.codec = codec

        assert exists(codec) or exists(target_sample_hz)

        self.target_sample_hz = target_sample_hz
        self.seq_len_multiple_of = None

        if exists(codec):
            self.target_sample_hz = codec.target_sample_hz
            self.seq_len_multiple_of = codec.seq_len_multiple_of

        assert not exists(codec) or model.dim == codec.codebook_dim, f'transformer model dimension {model.dim} must be equal to codec dimension {codec.codebook_dim}'

        self.dim = codec.codebook_dim if exists(codec) else model.dim

        assert objective in {'x0', 'eps', 'v'}, 'objective must be either predict x0 or noise'
        self.objective = objective

        if noise_schedule == "linear":
            self.gamma_schedule = simple_linear_schedule
        elif noise_schedule == "cosine":
            self.gamma_schedule = cosine_schedule
        elif noise_schedule == "sigmoid":
            self.gamma_schedule = sigmoid_schedule
        else:
            raise ValueError(f'invalid noise schedule {noise_schedule}')

        # the main finding presented in Ting Chen's paper - that higher resolution images requires more noise for better training

        assert scale <= 1, 'scale must be less than or equal to 1'
        self.scale = scale

        # gamma schedules

        self.gamma_schedule = partial(self.gamma_schedule, **schedule_kwargs)

        self.timesteps = timesteps
        self.use_ddim = use_ddim

        # proposed in the paper, summed to time_next
        # as a way to fix a deficiency in self-conditioning and lower FID when the number of sampling timesteps is < 400

        self.time_difference = time_difference

        # probability for self conditioning during training

        self.train_prob_self_cond = train_prob_self_cond

        # min snr loss weight

        self.min_snr_loss_weight = min_snr_loss_weight
        self.min_snr_gamma = min_snr_gamma

        # weight of the cross entropy loss to residual vq codebooks

        self.rvq_cross_entropy_loss_weight = rvq_cross_entropy_loss_weight

    @property
    def device(self):
        return next(self.model.parameters()).device

    def print(self, s):
        return self.accelerator.print(s)

    def get_sampling_timesteps(self, batch, *, device):
        times = torch.linspace(1., 0., self.timesteps + 1, device = device)
        times = repeat(times, 't -> b t', b = batch)
        times = torch.stack((times[:, :-1], times[:, 1:]), dim = 0)
        times = times.unbind(dim = -1)
        return times

    @torch.no_grad()
    def ddpm_sample(self, shape, time_difference = None):
        batch, device = shape[0], self.device

        time_difference = default(time_difference, self.time_difference)

        time_pairs = self.get_sampling_timesteps(batch, device = device)

        audio = torch.randn(shape, device=device)

        x_start = None
        last_latents = None

        for time, time_next in tqdm(time_pairs, desc = 'sampling loop time step', total = self.timesteps):

            # add the time delay

            time_next = (time_next - self.time_difference).clamp(min = 0.)

            noise_cond = time

            # get predicted x0

            model_output = self.model(audio, noise_cond)

            # get log(snr)

            gamma = self.gamma_schedule(time)
            gamma_next = self.gamma_schedule(time_next)
            gamma, gamma_next = map(partial(right_pad_dims_to, audio), (gamma, gamma_next))

            # get alpha sigma of time and next time

            alpha, sigma = gamma_to_alpha_sigma(gamma, self.scale)
            alpha_next, sigma_next = gamma_to_alpha_sigma(gamma_next, self.scale)

            # calculate x0 and noise

            if self.objective == 'x0':
                x_start = model_output

            elif self.objective == 'eps':
                x_start = safe_div(audio - sigma * model_output, alpha)

            elif self.objective == 'v':
                x_start = alpha * audio - sigma * model_output

            # derive posterior mean and variance

            log_snr, log_snr_next = map(gamma_to_log_snr, (gamma, gamma_next))

            c = -expm1(log_snr - log_snr_next)

            mean = alpha_next * (audio * (1 - c) / alpha + c * x_start)
            variance = (sigma_next ** 2) * c
            log_variance = log(variance)

            # get noise

            noise = torch.where(
                rearrange(time_next > 0, 'b -> b 1 1 1'),
                torch.randn_like(audio),
                torch.zeros_like(audio)
            )

            audio = mean + (0.5 * log_variance).exp() * noise

        return audio

    @torch.no_grad()
    def ddim_sample(self, shape, time_difference = None):
        batch, device = shape[0], self.device

        time_difference = default(time_difference, self.time_difference)

        time_pairs = self.get_sampling_timesteps(batch, device = device)

        audio = torch.randn(shape, device = device)

        x_start = None
        last_latents = None

        for times, times_next in tqdm(time_pairs, desc = 'sampling loop time step'):

            # get times and noise levels

            gamma = self.gamma_schedule(times)
            gamma_next = self.gamma_schedule(times_next)

            padded_gamma, padded_gamma_next = map(partial(right_pad_dims_to, audio), (gamma, gamma_next))

            alpha, sigma = gamma_to_alpha_sigma(padded_gamma, self.scale)
            alpha_next, sigma_next = gamma_to_alpha_sigma(padded_gamma_next, self.scale)

            # add the time delay

            times_next = (times_next - time_difference).clamp(min = 0.)

            # predict x0

            model_output = self.model(audio, times)

            # calculate x0 and noise

            if self.objective == 'x0':
                x_start = model_output

            elif self.objective == 'eps':
                x_start = safe_div(audio - sigma * model_output, alpha)

            elif self.objective == 'v':
                x_start = alpha * audio - sigma * model_output

            # get predicted noise

            pred_noise = safe_div(audio - alpha * x_start, sigma)

            # calculate x next

            audio = x_start * alpha_next + pred_noise * sigma_next

        return audio

    @torch.no_grad()
    def sample(
        self,
        *,
        length,
        batch_size = 1
    ):
        sample_fn = self.ddpm_sample if not self.use_ddim else self.ddim_sample
        audio = sample_fn((batch_size, length, self.dim))

        if exists(self.codec):
            audio = self.codec.decode(audio)

            if audio.ndim == 3:
                audio = rearrange(audio, 'b 1 n -> b n')

        return audio

    def forward(
        self,
        audio,
        codes = None,
        *args,
        **kwargs
    ):
        is_raw_audio = audio.ndim == 2

        assert not (is_raw_audio and not exists(self.codec)), 'codec must be passed in if one were to train on raw audio'

        if is_raw_audio:
            with torch.no_grad():
                self.codec.eval()
                audio, codes, _ = self.codec(audio, return_encoded = True)

        batch, n, d, device = *audio.shape, self.device

        assert d == self.dim, f'codec codebook dimension {d} must match model dimensions {self.dim}'

        # sample random times

        times = torch.zeros((batch,), device = device).float().uniform_(0, 1.)

        # noise sample

        noise = torch.randn_like(audio)

        gamma = self.gamma_schedule(times)
        padded_gamma = right_pad_dims_to(audio, gamma)
        alpha, sigma =  gamma_to_alpha_sigma(padded_gamma, self.scale)

        noised_audio = alpha * audio + sigma * noise

        # predict and take gradient step

        pred = self.model(noised_audio, times)

        if self.objective == 'eps':
            target = noise

        elif self.objective == 'x0':
            target = audio

        elif self.objective == 'v':
            target = alpha * noise - sigma * audio

        loss = F.mse_loss(pred, target, reduction = 'none')
        loss = reduce(loss, 'b ... -> b', 'mean')

        # min snr loss weight

        snr = (alpha * alpha) / (sigma * sigma)
        maybe_clipped_snr = snr.clone()

        if self.min_snr_loss_weight:
            maybe_clipped_snr.clamp_(max = self.min_snr_gamma)

        if self.objective == 'eps':
            loss_weight = maybe_clipped_snr / snr

        elif self.objective == 'x0':
            loss_weight = maybe_clipped_snr

        elif self.objective == 'v':
            loss_weight = maybe_clipped_snr / (snr + 1)

        loss =  (loss * loss_weight).mean()

        # cross entropy loss to codebooks

        if self.rvq_cross_entropy_loss_weight == 0 or not exists(codes):
            return loss

        if self.objective == 'x0':
            x_start = pred

        elif self.objective == 'eps':
            x_start = safe_div(audio - sigma * pred, alpha)

        elif self.objective == 'v':
            x_start = alpha * audio - sigma * pred

        _, ce_loss = self.codec.rq(x_start, codes)

        return loss + self.rvq_cross_entropy_loss_weight * ce_loss

# trainer

def cycle(dl):
    while True:
        for data in dl:
            yield data

class Trainer(object):
    def __init__(
        self,
        diffusion_model: NaturalSpeech2,
        *,
        dataset: Optional[Dataset] = None,
        folder = None,
        train_batch_size = 16,
        gradient_accumulate_every = 1,
        train_lr = 1e-4,
        train_num_steps = 100000,
        ema_update_every = 10,
        ema_decay = 0.995,
        adam_betas = (0.9, 0.99),
        save_and_sample_every = 1000,
        num_samples = 1,
        results_folder = './results',
        amp = False,
        fp16 = False,
        use_ema = True,
        split_batches = True,
        dataloader = None,
        data_max_length = None,
        data_max_length_seconds = 2,
        sample_length = None
    ):
        super().__init__()

        # accelerator

        self.accelerator = Accelerator(
            split_batches = split_batches,
            mixed_precision = 'fp16' if fp16 else 'no'
        )

        self.accelerator.native_amp = amp

        # model

        self.model = diffusion_model
        assert exists(diffusion_model.codec)

        self.dim = diffusion_model.dim

        # training hyperparameters

        self.batch_size = train_batch_size
        self.gradient_accumulate_every = gradient_accumulate_every
        self.train_num_steps = train_num_steps

        # dataset and dataloader

        dl = dataloader

        if not exists(dl):
            assert exists(dataset) or exists(folder)

            if exists(dataset):
                self.ds = dataset
            elif exists(folder):
                # create dataset

                if exists(data_max_length_seconds):
                    assert not exists(data_max_length)
                    data_max_length = int(data_max_length_seconds * diffusion_model.target_sample_hz)
                else:
                    assert exists(data_max_length)

                self.ds = SoundDataset(
                    folder,
                    max_length = data_max_length,
                    target_sample_hz = diffusion_model.target_sample_hz,
                    seq_len_multiple_of = diffusion_model.seq_len_multiple_of
                )

                dl = DataLoader(
                    self.ds,
                    batch_size = train_batch_size,
                    shuffle = True,
                    pin_memory = True,
                    num_workers = cpu_count()
                )

        dl = self.accelerator.prepare(dl)
        self.dl = cycle(dl)

        # optimizer

        self.opt = Adam(diffusion_model.parameters(), lr = train_lr, betas = adam_betas)

        # for logging results in a folder periodically

        self.use_ema = use_ema
        self.ema = None

        if self.accelerator.is_main_process and use_ema:
            # make sure codec is not part of the EMA
            # encodec seems to be not deepcopyable, so this is a necessary hack

            codec = diffusion_model.codec
            diffusion_model.codec = None

            self.ema = EMA(
                diffusion_model,
                beta = ema_decay,
                update_every = ema_update_every,
                ignore_startswith_names = set(['codec.'])
            ).to(self.device)

            diffusion_model.codec = codec
            self.ema.ema_model.codec = codec

        # sampling hyperparameters

        self.sample_length = default(sample_length, data_max_length)
        self.num_samples = num_samples
        self.save_and_sample_every = save_and_sample_every

        # results folder

        self.results_folder = Path(results_folder)
        self.results_folder.mkdir(exist_ok = True)

        # step counter state

        self.step = 0

        # prepare model, dataloader, optimizer with accelerator

        self.model, self.opt = self.accelerator.prepare(self.model, self.opt)

    @property
    def unwrapped_model(self):
        return self.accelerator.unwrap_model(self.model)
    
    @property
    def device(self):
        return self.accelerator.device

    def save(self, milestone):
        if not self.accelerator.is_local_main_process:
            return

        data = {
            'step': self.step,
            'model': self.accelerator.get_state_dict(self.model),
            'opt': self.opt.state_dict(),
            'ema': self.ema.state_dict(),
            'scaler': self.accelerator.scaler.state_dict() if exists(self.accelerator.scaler) else None,
            'version': __version__
        }

        torch.save(data, str(self.results_folder / f'model-{milestone}.pt'))

    def load(self, milestone):
        accelerator = self.accelerator
        device = accelerator.device

        data = torch.load(str(self.results_folder / f'model-{milestone}.pt'), map_location=device)

        model = self.accelerator.unwrap_model(self.model)
        model.load_state_dict(data['model'])

        self.step = data['step']
        self.opt.load_state_dict(data['opt'])
        if self.accelerator.is_main_process:
            self.ema.load_state_dict(data["ema"])

        if 'version' in data:
            print(f"loading from version {data['version']}")

        if exists(self.accelerator.scaler) and exists(data['scaler']):
            self.accelerator.scaler.load_state_dict(data['scaler'])

    def train(self):
        accelerator = self.accelerator
        device = accelerator.device

        with tqdm(initial = self.step, total = self.train_num_steps, disable = not accelerator.is_main_process) as pbar:

            while self.step < self.train_num_steps:

                total_loss = 0.

                for _ in range(self.gradient_accumulate_every):
                    data = next(self.dl).to(device)

                    with self.accelerator.autocast():
                        loss = self.model(data)
                        loss = loss / self.gradient_accumulate_every
                        total_loss += loss.item()

                    self.accelerator.backward(loss)

                accelerator.clip_grad_norm_(self.model.parameters(), 1.0)
                pbar.set_description(f'loss: {total_loss:.4f}')

                accelerator.wait_for_everyone()

                self.opt.step()
                self.opt.zero_grad()

                accelerator.wait_for_everyone()

                self.step += 1

                if accelerator.is_main_process:
                    self.ema.update()

                    if True or self.step % self.save_and_sample_every == 0:

                        models = [(self.unwrapped_model, str(self.step))]

                        if self.use_ema:
                            models.append((self.ema.ema_model, f'{self.step}.ema'))

                        for model, label in models:
                            model.eval()

                            with torch.no_grad():
                                generated = model.sample(
                                    batch_size = self.num_samples,
                                    length = self.sample_length
                                )

                            for ind, t in enumerate(generated):
                                filename = str(self.results_folder / f'sample_{label}.flac')
                                t = rearrange(t, 'n -> 1 n')
                                torchaudio.save(filename, t.cpu().detach(), self.unwrapped_model.target_sample_hz)

                        self.print(f'{steps}: saving to {str(self.results_folder)}')

                        self.save(milestone)

                pbar.update(1)

        self.print('training complete')
