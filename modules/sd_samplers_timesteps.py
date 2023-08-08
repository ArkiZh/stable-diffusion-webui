import torch
import inspect
from modules import devices, sd_samplers_common, sd_samplers_timesteps_impl
from modules.sd_samplers_cfg_denoiser import CFGDenoiser

from modules.shared import opts
import modules.shared as shared

samplers_timesteps = [
    ('k_DDIM', sd_samplers_timesteps_impl.ddim, ['k_ddim'], {}),
    ('k_PLMS', sd_samplers_timesteps_impl.plms, ['k_plms'], {}),
    ('k_UniPC', sd_samplers_timesteps_impl.unipc, ['k_unipc'], {}),
]


samplers_data_timesteps = [
    sd_samplers_common.SamplerData(label, lambda model, funcname=funcname: CompVisSampler(funcname, model), aliases, options)
    for label, funcname, aliases, options in samplers_timesteps
]


class CompVisTimestepsDenoiser(torch.nn.Module):
    def __init__(self, model, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.inner_model = model

    def forward(self, input, timesteps, **kwargs):
        return self.inner_model.apply_model(input, timesteps, **kwargs)


class CompVisTimestepsVDenoiser(torch.nn.Module):
    def __init__(self, model, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.inner_model = model

    def predict_eps_from_z_and_v(self, x_t, t, v):
        return self.inner_model.sqrt_alphas_cumprod[t.to(torch.int), None, None, None] * v + self.inner_model.sqrt_one_minus_alphas_cumprod[t.to(torch.int), None, None, None] * x_t

    def forward(self, input, timesteps, **kwargs):
        model_output = self.inner_model.apply_model(input, timesteps, **kwargs)
        e_t = self.predict_eps_from_z_and_v(input, timesteps, model_output)
        return e_t


class CFGDenoiserTimesteps(CFGDenoiser):

    def __init__(self, model, sampler):
        super().__init__(model, sampler)

        self.alphas = model.inner_model.alphas_cumprod

    def get_pred_x0(self, x_in, x_out, sigma):
        ts = int(sigma.item())

        s_in = x_in.new_ones([x_in.shape[0]])
        a_t = self.alphas[ts].item() * s_in
        sqrt_one_minus_at = (1 - a_t).sqrt()

        pred_x0 = (x_in - sqrt_one_minus_at * x_out) / a_t.sqrt()

        return pred_x0


class CompVisSampler(sd_samplers_common.Sampler):
    def __init__(self, funcname, sd_model):
        super().__init__(funcname)

        self.eta_option_field = 'eta_ddim'
        self.eta_infotext_field = 'Eta DDIM'

        denoiser = CompVisTimestepsVDenoiser if sd_model.parameterization == "v" else CompVisTimestepsDenoiser
        self.model_wrap = denoiser(sd_model)
        self.model_wrap_cfg = CFGDenoiserTimesteps(self.model_wrap, self)

    def get_timesteps(self, p, steps):
        discard_next_to_last_sigma = self.config is not None and self.config.options.get('discard_next_to_last_sigma', False)
        if opts.always_discard_next_to_last_sigma and not discard_next_to_last_sigma:
            discard_next_to_last_sigma = True
            p.extra_generation_params["Discard penultimate sigma"] = True

        steps += 1 if discard_next_to_last_sigma else 0

        timesteps = torch.clip(torch.asarray(list(range(0, 1000, 1000 // steps)), device=devices.device) + 1, 0, 999)

        return timesteps

    def sample_img2img(self, p, x, noise, conditioning, unconditional_conditioning, steps=None, image_conditioning=None):
        steps, t_enc = sd_samplers_common.setup_img2img_steps(p, steps)

        timesteps = self.get_timesteps(p, steps)
        timesteps_sched = timesteps[:t_enc]

        alphas_cumprod = shared.sd_model.alphas_cumprod
        sqrt_alpha_cumprod = torch.sqrt(alphas_cumprod[timesteps[t_enc]])
        sqrt_one_minus_alpha_cumprod = torch.sqrt(1 - alphas_cumprod[timesteps[t_enc]])

        xi = x * sqrt_alpha_cumprod + noise * sqrt_one_minus_alpha_cumprod

        extra_params_kwargs = self.initialize(p)
        parameters = inspect.signature(self.func).parameters

        if 'timesteps' in parameters:
            extra_params_kwargs['timesteps'] = timesteps_sched
        if 'is_img2img' in parameters:
            extra_params_kwargs['is_img2img'] = True

        self.model_wrap_cfg.init_latent = x
        self.last_latent = x
        extra_args = {
            'cond': conditioning,
            'image_cond': image_conditioning,
            'uncond': unconditional_conditioning,
            'cond_scale': p.cfg_scale,
            's_min_uncond': self.s_min_uncond
        }

        samples = self.launch_sampling(t_enc + 1, lambda: self.func(self.model_wrap_cfg, xi, extra_args=extra_args, disable=False, callback=self.callback_state, **extra_params_kwargs))

        if self.model_wrap_cfg.padded_cond_uncond:
            p.extra_generation_params["Pad conds"] = True

        return samples

    def sample(self, p, x, conditioning, unconditional_conditioning, steps=None, image_conditioning=None):
        steps = steps or p.steps
        timesteps = self.get_timesteps(p, steps)

        extra_params_kwargs = self.initialize(p)
        parameters = inspect.signature(self.func).parameters

        if 'timesteps' in parameters:
            extra_params_kwargs['timesteps'] = timesteps

        self.last_latent = x
        samples = self.launch_sampling(steps, lambda: self.func(self.model_wrap_cfg, x, extra_args={
            'cond': conditioning,
            'image_cond': image_conditioning,
            'uncond': unconditional_conditioning,
            'cond_scale': p.cfg_scale,
            's_min_uncond': self.s_min_uncond
        }, disable=False, callback=self.callback_state, **extra_params_kwargs))

        if self.model_wrap_cfg.padded_cond_uncond:
            p.extra_generation_params["Pad conds"] = True

        return samples
