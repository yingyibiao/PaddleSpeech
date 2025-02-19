# Copyright (c) 2023 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Diffusion denoising related modules for paddle"""
import math
from typing import Callable
from typing import Optional
from typing import Tuple

import paddle
import ppdiffusers
from paddle import nn
from ppdiffusers.models.embeddings import Timesteps
from ppdiffusers.schedulers import DDPMScheduler

from paddlespeech.t2s.modules.nets_utils import initialize
from paddlespeech.t2s.modules.residual_block import WaveNetResidualBlock


class WaveNetDenoiser(nn.Layer):
    """A Mel-Spectrogram Denoiser modified from WaveNet

    Args:
        in_channels (int, optional): 
            Number of channels of the input mel-spectrogram, by default 80
        out_channels (int, optional): 
            Number of channels of the output mel-spectrogram, by default 80
        kernel_size (int, optional): 
            Kernel size of the residual blocks inside, by default 3
        layers (int, optional): 
            Number of residual blocks inside, by default 20
        stacks (int, optional):
            The number of groups to split the residual blocks into, by default 5
            Within each group, the dilation of the residual block grows exponentially.
        residual_channels (int, optional): 
            Residual channel of the residual blocks, by default 256
        gate_channels (int, optional): 
            Gate channel of the residual blocks, by default 512
        skip_channels (int, optional): 
            Skip channel of the residual blocks, by default 256
        aux_channels (int, optional): 
            Auxiliary channel of the residual blocks, by default 256
        dropout (float, optional): 
            Dropout of the residual blocks, by default 0.
        bias (bool, optional): 
            Whether to use bias in residual blocks, by default True
        use_weight_norm (bool, optional): 
            Whether to use weight norm in all convolutions, by default False
    """

    def __init__(
            self,
            in_channels: int=80,
            out_channels: int=80,
            kernel_size: int=3,
            layers: int=20,
            stacks: int=5,
            residual_channels: int=256,
            gate_channels: int=512,
            skip_channels: int=256,
            aux_channels: int=256,
            dropout: float=0.,
            bias: bool=True,
            use_weight_norm: bool=False,
            init_type: str="kaiming_normal", ):
        super().__init__()

        # initialize parameters
        initialize(self, init_type)

        self.in_channels = in_channels
        self.out_channels = out_channels
        self.aux_channels = aux_channels
        self.layers = layers
        self.stacks = stacks
        self.kernel_size = kernel_size

        assert layers % stacks == 0
        layers_per_stack = layers // stacks

        self.first_t_emb = nn.Sequential(
            Timesteps(
                residual_channels,
                flip_sin_to_cos=False,
                downscale_freq_shift=1),
            nn.Linear(residual_channels, residual_channels * 4),
            nn.Mish(), nn.Linear(residual_channels * 4, residual_channels))
        self.t_emb_layers = nn.LayerList([
            nn.Linear(residual_channels, residual_channels)
            for _ in range(layers)
        ])

        self.first_conv = nn.Conv1D(
            in_channels, residual_channels, 1, bias_attr=True)
        self.first_act = nn.ReLU()

        self.conv_layers = nn.LayerList()
        for layer in range(layers):
            dilation = 2**(layer % layers_per_stack)
            conv = WaveNetResidualBlock(
                kernel_size=kernel_size,
                residual_channels=residual_channels,
                gate_channels=gate_channels,
                skip_channels=skip_channels,
                aux_channels=aux_channels,
                dilation=dilation,
                dropout=dropout,
                bias=bias)
            self.conv_layers.append(conv)

        final_conv = nn.Conv1D(skip_channels, out_channels, 1, bias_attr=True)
        nn.initializer.Constant(0.0)(final_conv.weight)
        self.last_conv_layers = nn.Sequential(nn.ReLU(),
                                              nn.Conv1D(
                                                  skip_channels,
                                                  skip_channels,
                                                  1,
                                                  bias_attr=True),
                                              nn.ReLU(), final_conv)

        if use_weight_norm:
            self.apply_weight_norm()

    def forward(self, x, t, c):
        """Denoise mel-spectrogram.

        Args:
            x(Tensor): 
                Shape (N, C_in, T), The input mel-spectrogram.
            t(Tensor): 
                Shape (N), The timestep input.
            c(Tensor): 
                Shape (N, C_aux, T'). The auxiliary input (e.g. fastspeech2 encoder output). 

        Returns:
            Tensor: Shape (N, C_out, T), the denoised mel-spectrogram.
        """
        assert c.shape[-1] == x.shape[-1]

        if t.shape[0] != x.shape[0]:
            t = t.tile([x.shape[0]])
        t_emb = self.first_t_emb(t)
        t_embs = [
            t_emb_layer(t_emb)[..., None] for t_emb_layer in self.t_emb_layers
        ]

        x = self.first_conv(x)
        x = self.first_act(x)
        skips = 0
        for f, t in zip(self.conv_layers, t_embs):
            x = x + t
            x, s = f(x, c)
            skips += s
        skips *= math.sqrt(1.0 / len(self.conv_layers))

        x = self.last_conv_layers(skips)
        return x

    def apply_weight_norm(self):
        """Recursively apply weight normalization to all the Convolution layers
        in the sublayers.
        """

        def _apply_weight_norm(layer):
            if isinstance(layer, (nn.Conv1D, nn.Conv2D)):
                nn.utils.weight_norm(layer)

        self.apply(_apply_weight_norm)

    def remove_weight_norm(self):
        """Recursively remove weight normalization from all the Convolution 
        layers in the sublayers.
        """

        def _remove_weight_norm(layer):
            try:
                nn.utils.remove_weight_norm(layer)
            except ValueError:
                pass

        self.apply(_remove_weight_norm)


class GaussianDiffusion(nn.Layer):
    """Common Gaussian Diffusion Denoising Model Module 

    Args:
        denoiser (Layer, optional): 
            The model used for denoising noises.
        num_train_timesteps (int, optional): 
            The number of timesteps between the noise and the real during training, by default 1000.
        beta_start (float, optional): 
            beta start parameter for the scheduler, by default 0.0001.
        beta_end (float, optional): 
            beta end parameter for the scheduler, by default 0.0001.
        beta_schedule (str, optional): 
            beta schedule parameter for the scheduler, by default 'squaredcos_cap_v2' (cosine schedule).
        num_max_timesteps (int, optional): 
            The max timestep transition from real to noise, by default None.
    
    Examples: 
        >>> import paddle
        >>> import paddle.nn.functional as F
        >>> from tqdm import tqdm
        >>> 
        >>> denoiser = WaveNetDenoiser()
        >>> diffusion = GaussianDiffusion(denoiser, num_train_timesteps=1000, num_max_timesteps=100)
        >>> x = paddle.ones([4, 80, 192]) # [B, mel_ch, T] # real mel input
        >>> c = paddle.randn([4, 256, 192]) # [B, fs2_encoder_out_ch, T] # fastspeech2 encoder output
        >>> loss = F.mse_loss(*diffusion(x, c))
        >>> loss.backward()
        >>> print('MSE Loss:', loss.item())
        MSE Loss: 1.6669728755950928 
        >>> def create_progress_callback():
        >>>     pbar = None
        >>>     def callback(index, timestep, num_timesteps, sample):
        >>>         nonlocal pbar
        >>>         if pbar is None:
        >>>             pbar = tqdm(total=num_timesteps)
        >>>             pbar.update(index)
        >>>         pbar.update()
        >>> 
        >>>     return callback
        >>> 
        >>> # ds=1000, K_step=60, scheduler=ddpm, from aux fs2 mel output
        >>> ds = 1000
        >>> infer_steps = 1000
        >>> K_step = 60
        >>> scheduler_type = 'ddpm'
        >>> x_in = x
        >>> diffusion = GaussianDiffusion(denoiser, num_train_timesteps=ds, num_max_timesteps=K_step)
        >>> with paddle.no_grad():
        >>>     sample = diffusion.inference(
        >>>         paddle.randn(x.shape), c, ref_x=x_in, 
        >>>         num_inference_steps=infer_steps,
        >>>         scheduler_type=scheduler_type,
        >>>         callback=create_progress_callback())
        100%|█████| 60/60 [00:03<00:00, 18.36it/s] 
        >>> 
        >>> # ds=100, K_step=100, scheduler=ddpm, from gaussian noise
        >>> ds = 100
        >>> infer_steps = 100
        >>> K_step = 100
        >>> scheduler_type = 'ddpm'
        >>> x_in = None
        >>> diffusion = GaussianDiffusion(denoiser, num_train_timesteps=ds, num_max_timesteps=K_step)
        >>> with paddle.no_grad():
        >>>     sample = diffusion.inference(
        >>>         paddle.randn(x.shape), c, ref_x=x_in, 
        >>>         num_inference_steps=infer_steps,
        >>>         scheduler_type=scheduler_type,
        >>>         callback=create_progress_callback())
        100%|█████| 100/100 [00:05<00:00, 18.29it/s] 
        >>> 
        >>> # ds=1000, K_step=1000, scheduler=pndm, infer_step=25, from gaussian noise
        >>> ds = 1000
        >>> infer_steps = 25
        >>> K_step = 1000
        >>> scheduler_type = 'pndm'
        >>> x_in = None
        >>> diffusion = GaussianDiffusion(denoiser, num_train_timesteps=ds, num_max_timesteps=K_step)
        >>> with paddle.no_grad():
        >>>     sample = diffusion.inference(
        >>>         paddle.randn(x.shape), c, ref_x=x_in, 
        >>>         num_inference_steps=infer_steps,
        >>>         scheduler_type=scheduler_type,
        >>>         callback=create_progress_callback())
        100%|█████| 34/34 [00:01<00:00, 19.75it/s]
        >>> 
        >>> # ds=1000, K_step=100, scheduler=pndm, infer_step=50, from aux fs2 mel output
        >>> ds = 1000
        >>> infer_steps = 50
        >>> K_step = 100
        >>> scheduler_type = 'pndm'
        >>> x_in = x
        >>> diffusion = GaussianDiffusion(denoiser, num_train_timesteps=ds, num_max_timesteps=K_step)
        >>> with paddle.no_grad():
        >>>     sample = diffusion.inference(
        >>>         paddle.randn(x.shape), c, ref_x=x_in, 
        >>>         num_inference_steps=infer_steps,
        >>>         scheduler_type=scheduler_type,
        >>>         callback=create_progress_callback())
        100%|█████| 14/14 [00:00<00:00, 23.80it/s]

    """

    def __init__(self,
                 denoiser: nn.Layer,
                 num_train_timesteps: Optional[int]=1000,
                 beta_start: Optional[float]=0.0001,
                 beta_end: Optional[float]=0.02,
                 beta_schedule: Optional[str]="squaredcos_cap_v2",
                 num_max_timesteps: Optional[int]=None):
        super().__init__()

        self.num_train_timesteps = num_train_timesteps
        self.beta_start = beta_start
        self.beta_end = beta_end
        self.beta_schedule = beta_schedule

        self.denoiser = denoiser
        self.noise_scheduler = DDPMScheduler(
            num_train_timesteps=num_train_timesteps,
            beta_start=beta_start,
            beta_end=beta_end,
            beta_schedule=beta_schedule)
        self.num_max_timesteps = num_max_timesteps

    def forward(self, x: paddle.Tensor, cond: Optional[paddle.Tensor]=None
                ) -> Tuple[paddle.Tensor, paddle.Tensor]:
        """Generate random timesteps noised x.

        Args:
            x (Tensor): 
                The input for adding noises.
            cond (Tensor, optional):
                Conditional input for compute noises.
          
        Returns: 
            y (Tensor): 
                The output with noises added in.
            target (Tensor):
                The noises which is added to the input.

        """
        noise_scheduler = self.noise_scheduler

        # Sample noise that we'll add to the mel-spectrograms
        target = noise = paddle.randn(x.shape)

        # Sample a random timestep for each mel-spectrogram
        num_timesteps = self.num_train_timesteps
        if self.num_max_timesteps is not None:
            num_timesteps = self.num_max_timesteps
        timesteps = paddle.randint(0, num_timesteps, (x.shape[0], ))

        # Add noise to the clean mel-spectrograms according to the noise magnitude at each timestep
        # (this is the forward diffusion process)
        noisy_images = noise_scheduler.add_noise(x, noise, timesteps)

        y = self.denoiser(noisy_images, timesteps, cond)

        # then compute loss use output y and noisy target for prediction_type == "epsilon"
        return y, target

    def inference(self,
                  noise: paddle.Tensor,
                  cond: Optional[paddle.Tensor]=None,
                  ref_x: Optional[paddle.Tensor]=None,
                  num_inference_steps: Optional[int]=1000,
                  strength: Optional[float]=None,
                  scheduler_type: Optional[str]="ddpm",
                  callback: Optional[Callable[[int, int, int, paddle.Tensor],
                                              None]]=None,
                  callback_steps: Optional[int]=1):
        """Denoising input from noises. Refer to ppdiffusers img2img pipeline.

        Args:
            noise (Tensor): 
                The input tensor as a starting point for denoising.
            cond (Tensor, optional):
                Conditional input for compute noises.
            ref_x (Tensor, optional):
                The real output for the denoising process to refer.
            num_inference_steps (int, optional):
                The number of timesteps between the noise and the real during inference, by default 1000.
            strength (float, optional):
                Mixing strength of ref_x with noise. The larger the value, the stronger the noise. 
                Range [0,1], by default None.
            scheduler_type (str, optional):
                Noise scheduler for generate noises. 
                Choose a great scheduler can skip many denoising step, by default 'ddpm'.
            callback (Callable[[int,int,int,Tensor], None], optional):
                Callback function during denoising steps.

                Args:
                    index (int):
                        Current denoising index.
                    timestep (int):
                        Current denoising timestep.
                    num_timesteps (int):
                        Number of the denoising timesteps.
                    denoised_output (Tensor):
                        Current intermediate result produced during denoising.

            callback_steps (int, optional):
                The step to call the callback function.
          
        Returns: 
            denoised_output (Tensor): 
                The denoised output tensor.

        """
        scheduler_cls = None
        for clsname in dir(ppdiffusers.schedulers):
            if clsname.lower() == scheduler_type + "scheduler":
                scheduler_cls = getattr(ppdiffusers.schedulers, clsname)
                break

        if scheduler_cls is None:
            raise ValueError(f"No such scheduler type named {scheduler_type}")

        scheduler = scheduler_cls(
            num_train_timesteps=self.num_train_timesteps,
            beta_start=self.beta_start,
            beta_end=self.beta_end,
            beta_schedule=self.beta_schedule)

        # set timesteps
        scheduler.set_timesteps(num_inference_steps)

        # prepare first noise variables
        noisy_input = noise
        timesteps = scheduler.timesteps
        if ref_x is not None:
            init_timestep = None
            if strength is None or strength < 0. or strength > 1.:
                strength = None
                if self.num_max_timesteps is not None:
                    strength = self.num_max_timesteps / self.num_train_timesteps
            if strength is not None:
                # get the original timestep using init_timestep
                init_timestep = min(
                    int(num_inference_steps * strength), num_inference_steps)
                t_start = max(num_inference_steps - init_timestep, 0)
                timesteps = scheduler.timesteps[t_start:]
                num_inference_steps = num_inference_steps - t_start
                noisy_input = scheduler.add_noise(
                    ref_x, noise, timesteps[:1].tile([noise.shape[0]]))

        # denoising loop
        denoised_output = noisy_input
        num_warmup_steps = len(
            timesteps) - num_inference_steps * scheduler.order
        for i, t in enumerate(timesteps):
            denoised_output = scheduler.scale_model_input(denoised_output, t)

            # predict the noise residual
            noise_pred = self.denoiser(denoised_output, t, cond)

            # compute the previous noisy sample x_t -> x_t-1
            denoised_output = scheduler.step(noise_pred, t,
                                             denoised_output).prev_sample

            # call the callback, if provided
            if i == len(timesteps) - 1 or ((i + 1) > num_warmup_steps and
                                           (i + 1) % scheduler.order == 0):
                if callback is not None and i % callback_steps == 0:
                    callback(i, t, len(timesteps), denoised_output)

        return denoised_output
