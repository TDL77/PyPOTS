# Created by Wenjie Du <wenjay.du@gmail.com>
# License: BSD-3-Clause

import numpy as np
import torch
import torch.nn as nn

from .submodules import DiffusionModel


class _CSDI(nn.Module):
    def __init__(
        self,
        n_layers,
        n_heads,
        n_channels,
        d_target,
        d_time_embedding,
        d_feature_embedding,
        d_diffusion_embedding,
        is_unconditional,
        target_strategy,
        n_diffusion_steps,
        schedule,
        beta_start,
        beta_end,
    ):
        super().__init__()

        self.d_target = d_target
        self.d_time_embedding = d_time_embedding
        self.d_feature_embedding = d_feature_embedding
        self.is_unconditional = is_unconditional
        self.target_strategy = target_strategy
        self.n_channels = n_channels
        self.n_diffusion_steps = n_diffusion_steps

        d_side = d_time_embedding + d_feature_embedding
        if self.is_unconditional:
            d_input = 1
        else:
            d_side += 1  # for conditional mask
            d_input = 2

        self.embed_layer = nn.Embedding(
            num_embeddings=self.d_target,
            embedding_dim=self.d_feature_embedding,
        )

        self.diff_model = DiffusionModel(
            n_diffusion_steps,
            d_diffusion_embedding,
            d_input,
            d_side,
            n_channels,
            n_heads,
            n_layers,
        )

        # parameters for diffusion models
        if schedule == "quad":
            self.beta = (
                np.linspace(beta_start**0.5, beta_end**0.5, self.n_diffusion_steps)
                ** 2
            )
        elif schedule == "linear":
            self.beta = np.linspace(beta_start, beta_end, self.n_diffusion_steps)

        self.alpha_hat = 1 - self.beta
        self.alpha = np.cumprod(self.alpha_hat)
        self.register_buffer(
            "alpha_torch", torch.tensor(self.alpha).float().unsqueeze(1).unsqueeze(1)
        )

    def time_embedding(self, pos, d_model=128):
        pe = torch.zeros(pos.shape[0], pos.shape[1], d_model).to(pos.device)
        position = pos.unsqueeze(2)
        div_term = 1 / torch.pow(
            10000.0, torch.arange(0, d_model, 2, device=pos.device) / d_model
        )
        pe[:, :, 0::2] = torch.sin(position * div_term)
        pe[:, :, 1::2] = torch.cos(position * div_term)
        return pe

    def get_randmask(self, observed_mask):
        rand_for_mask = torch.rand_like(observed_mask) * observed_mask
        rand_for_mask = rand_for_mask.reshape(len(rand_for_mask), -1)
        for i in range(len(observed_mask)):
            sample_ratio = np.random.rand()  # missing ratio
            num_observed = observed_mask[i].sum().item()
            num_masked = round(num_observed * sample_ratio)
            rand_for_mask[i][rand_for_mask[i].topk(num_masked).indices] = -1
        cond_mask = (rand_for_mask > 0).reshape(observed_mask.shape).float()
        return cond_mask

    def get_hist_mask(self, observed_mask, for_pattern_mask=None):
        if for_pattern_mask is None:
            for_pattern_mask = observed_mask
        if self.target_strategy == "mix":
            rand_mask = self.get_randmask(observed_mask)

        cond_mask = observed_mask.clone()
        for i in range(len(cond_mask)):
            mask_choice = np.random.rand()
            if self.target_strategy == "mix" and mask_choice > 0.5:
                cond_mask[i] = rand_mask[i]
            else:  # draw another sample for histmask (i-1 corresponds to another sample)
                cond_mask[i] = cond_mask[i] * for_pattern_mask[i - 1]
        return cond_mask

    def get_side_info(self, observed_tp, cond_mask):
        B, K, L = cond_mask.shape
        device = observed_tp.device
        time_embed = self.time_embedding(
            observed_tp, self.d_time_embedding
        )  # (B,L,emb)
        time_embed = time_embed.to(device)
        time_embed = time_embed.unsqueeze(2).expand(-1, -1, K, -1)
        feature_embed = self.embed_layer(
            torch.arange(self.d_target).to(device)
        )  # (K,emb)
        feature_embed = feature_embed.unsqueeze(0).unsqueeze(0).expand(B, L, -1, -1)

        side_info = torch.cat([time_embed, feature_embed], dim=-1)  # (B,L,K,*)
        side_info = side_info.permute(0, 3, 2, 1)  # (B,*,K,L)

        if not self.is_unconditional:
            side_mask = cond_mask.unsqueeze(1)  # (B,1,K,L)
            side_info = torch.cat([side_info, side_mask], dim=1)

        return side_info

    def calc_loss_valid(
        self, observed_data, cond_mask, observed_mask, side_info, is_train
    ):
        loss_sum = 0
        for t in range(self.n_diffusion_steps):  # calculate loss for all t
            loss = self.calc_loss(
                observed_data, cond_mask, observed_mask, side_info, is_train, set_t=t
            )
            loss_sum += loss.detach()
        return loss_sum / self.n_diffusion_steps

    def calc_loss(
        self, observed_data, cond_mask, observed_mask, side_info, is_train, set_t=-1
    ):
        B, K, L = observed_data.shape
        device = observed_data.device
        if is_train != 1:  # for validation
            t = (torch.ones(B) * set_t).long().to(device)
        else:
            t = torch.randint(0, self.n_diffusion_steps, [B]).to(device)

        current_alpha = self.alpha_torch[t]  # (B,1,1)
        noise = torch.randn_like(observed_data)
        noisy_data = (current_alpha**0.5) * observed_data + (
            1.0 - current_alpha
        ) ** 0.5 * noise

        total_input = self.set_input_to_diffmodel(noisy_data, observed_data, cond_mask)

        predicted = self.diff_model(total_input, side_info, t)  # (B,K,L)

        target_mask = observed_mask - cond_mask
        residual = (noise - predicted) * target_mask
        num_eval = target_mask.sum()
        loss = (residual**2).sum() / (num_eval if num_eval > 0 else 1)
        return loss

    def set_input_to_diffmodel(self, noisy_data, observed_data, cond_mask):
        if self.is_unconditional:
            total_input = noisy_data.unsqueeze(1)  # (B,1,K,L)
        else:
            cond_obs = (cond_mask * observed_data).unsqueeze(1)
            noisy_target = ((1 - cond_mask) * noisy_data).unsqueeze(1)
            total_input = torch.cat([cond_obs, noisy_target], dim=1)  # (B,2,K,L)

        return total_input

    def impute(self, observed_data, cond_mask, side_info, n_sampling_times):
        B, K, L = observed_data.shape
        device = observed_data.device
        imputed_samples = torch.zeros(B, n_sampling_times, K, L).to(device)

        for i in range(n_sampling_times):
            # generate noisy observation for unconditional model
            if self.is_unconditional:
                noisy_obs = observed_data
                noisy_cond_history = []
                for t in range(self.n_diffusion_steps):
                    noise = torch.randn_like(noisy_obs)
                    noisy_obs = (self.alpha_hat[t] ** 0.5) * noisy_obs + self.beta[
                        t
                    ] ** 0.5 * noise
                    noisy_cond_history.append(noisy_obs * cond_mask)

            current_sample = torch.randn_like(observed_data)

            for t in range(self.n_diffusion_steps - 1, -1, -1):
                if self.is_unconditional:
                    diff_input = (
                        cond_mask * noisy_cond_history[t]
                        + (1.0 - cond_mask) * current_sample
                    )
                    diff_input = diff_input.unsqueeze(1)  # (B,1,K,L)
                else:
                    cond_obs = (cond_mask * observed_data).unsqueeze(1)
                    noisy_target = ((1 - cond_mask) * current_sample).unsqueeze(1)
                    diff_input = torch.cat([cond_obs, noisy_target], dim=1)  # (B,2,K,L)
                predicted = self.diff_model(
                    diff_input, side_info, torch.tensor([t]).to(device)
                )

                coeff1 = 1 / self.alpha_hat[t] ** 0.5
                coeff2 = (1 - self.alpha_hat[t]) / (1 - self.alpha[t]) ** 0.5
                current_sample = coeff1 * (current_sample - coeff2 * predicted)

                if t > 0:
                    noise = torch.randn_like(current_sample)
                    sigma = (
                        (1.0 - self.alpha[t - 1]) / (1.0 - self.alpha[t]) * self.beta[t]
                    ) ** 0.5
                    current_sample += sigma * noise

            imputed_samples[:, i] = current_sample.detach()
        return imputed_samples

    def forward(self, inputs, training=True, n_sampling_times=1):
        (observed_data, observed_mask, observed_tp, gt_mask, for_pattern_mask,) = (
            inputs["observed_data"],
            inputs["observed_mask"],
            inputs["observed_tp"],
            inputs["gt_mask"],
            inputs["for_pattern_mask"],
        )

        if not training:
            cond_mask = gt_mask
        elif self.target_strategy != "random":
            cond_mask = self.get_hist_mask(
                observed_mask, for_pattern_mask=for_pattern_mask
            )
        else:
            cond_mask = self.get_randmask(observed_mask)

        side_info = self.get_side_info(observed_tp, cond_mask)

        loss_func = self.calc_loss if training == 1 else self.calc_loss_valid

        # `loss` is always the item for backward propagating to update the model
        loss = loss_func(observed_data, cond_mask, observed_mask, side_info, training)

        results = {
            "loss": loss,  # will be used for backward propagating to update the model
        }
        if not training:
            samples = self.impute(
                observed_data, cond_mask, side_info, n_sampling_times
            )  # (B,nsample,K,L)
            imputation = samples.mean(dim=1)  # (B,K,L)
            imputed_data = observed_data + imputation * (1 - gt_mask)
            results["imputed_data"] = imputed_data.permute(0, 2, 1)  # (B,L,K)
        return results