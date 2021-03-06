
import numpy as np
import torch
from collections import namedtuple

from rlpyt.algos.base import RlAlgorithm
from rlpyt.utils.quick_args import save__init__args
from rlpyt.utils.logging import logger
from rlpyt.replays.non_sequence.uniform import UniformReplayBuffer
from rlpyt.utils.collections import namedarraytuple
from rlpyt.utils.buffer import buffer_to
from rlpyt.distributions.gaussian import Gaussian
from rlpyt.distributions.gaussian import DistInfo as GaussianDistInfo
from rlpyt.utils.tensor import valid_mean
from rlpyt.algos.utils import valid_from_done


OptInfo = namedtuple("OptInfo",
    ["q1Loss", "q2Loss", "vLoss", "piLoss",
    "q1GradNorm", "q2GradNorm", "vGradNorm", "piGradNorm",
    "q1", "q2", "v", "piMu", "piLogStd", "qMeanDiff"])
SamplesToBuffer = namedarraytuple("SamplesToRepay",
    ["observation", "action", "reward", "done"])


class SAC(RlAlgorithm):

    opt_info_fields = tuple(f for f in OptInfo._fields)  # copy

    def __init__(
            self,
            discount=0.99,
            batch_size=256,
            min_steps_learn=int(1e4),
            replay_size=int(1e6),
            training_ratio=256,  # data_consumption / data_generation
            target_update_tau=0.005,  # tau=1 for hard update.
            target_update_interval=1,  # interval=1000 for hard update.
            learning_rate=3e-4,
            OptimCls=torch.optim.Adam,
            optim_kwargs=None,
            initial_optim_state_dict=None,
            action_prior="uniform",  # or "gaussian"
            reward_scale=1,
            reparameterize=True,
            clip_grad_norm=1e6,
            policy_output_regularization=0.001,
            n_step_return=1,
            ):
        if optim_kwargs is None:
            optim_kwargs = dict()
        assert action_prior in ["uniform", "gaussian"]
        save__init__args(locals())
        self.update_counter = 0

    def initialize(self, agent, n_itr, batch_spec, mid_batch_reset, examples):
        if agent.recurrent:
            raise NotImplementedError
        self.agent = agent
        self.n_itr = n_itr
        self.mid_batch_reset = mid_batch_reset
        self.optimizer = self.OptimCls(agent.parameters(),
            lr=self.learning_rate, **self.optim_kwargs)
        if self.initial_optim_state_dict is not None:
            self.optimizer.load_state_dict(self.initial_optim_state_dict)

        sample_bs = batch_spec.size
        train_bs = self.batch_size
        assert (self.training_ratio * sample_bs) % train_bs == 0
        self.updates_per_optimize = int((self.training_ratio * sample_bs) //
            train_bs)
        logger.log(f"From sampler batch size {sample_bs}, training "
            f"batch size {train_bs}, and training ratio "
            f"{self.training_ratio}, computed {self.updates_per_optimize} "
            f"updates per iteration.")
        self.min_itr_learn = self.min_steps_learn // sample_bs
        self.agent.give_min_itr_learn(self.min_itr_learn)

        example_to_buffer = SamplesToBuffer(
            observation=examples["observation"],
            action=examples["action"],
            reward=examples["reward"],
            done=examples["done"],
        )
        replay_kwargs = dict(
            example=example_to_buffer,
            size=self.replay_size,
            B=batch_spec.B,
            n_step_return=self.n_step_return,
        )
        self.replay_buffer = UniformReplayBuffer(**replay_kwargs)

        if self.action_prior == "gaussian":
            self.action_prior_distribution = Gaussian(
                dim=agent.env_spaces.action.size, std=1.)

    def optimize_agent(self, itr, samples=None):
        if samples is not None:
            samples_to_buffer = SamplesToBuffer(
                observation=samples.env.observation,
                action=samples.agent.action,
                reward=samples.env.reward,
                done=samples.env.done,
            )
            self.replay_buffer.append_samples(samples_to_buffer)
        opt_info = OptInfo(*([] for _ in range(len(OptInfo._fields))))
        if itr < self.min_itr_learn:
            return opt_info
        for _ in range(self.updates_per_optimize):
            self.update_counter += 1
            samples_from_replay = self.replay_buffer.sample_batch(self.batch_size)
            self.optimizer.zero_grad()
            losses, values = self.loss(samples_from_replay)
            for loss in losses:
                loss.backward()
            grad_norms = [torch.nn.utils.clip_grad_norm_(ps, self.clip_grad_norm)
                for ps in self.agent.parameters_by_model()]
            self.optimizer.step()
            self.append_opt_info_(opt_info, losses, grad_norms, values)
            if self.update_counter % self.target_update_interval == 0:
                self.agent.update_target(self.target_update_tau)
        return opt_info

    def loss(self, samples):
        """Samples have leading batch dimension [B,..] (but not time)."""
        agent_inputs, target_inputs, action = buffer_to(
            (samples.agent_inputs, samples.target_inputs, samples.action),
            device=self.agent.device)  # Move to device once, re-use.
        q1, q2 = self.agent.q(*agent_inputs, action)
        with torch.no_grad():
            target_v = self.agent.target_v(*target_inputs)
        disc = self.discount ** self.n_step_return
        y = (self.reward_scale * samples.return_ +
            (1 - samples.done_n.float()) * disc * target_v)
        if self.mid_batch_reset and not self.agent.recurrent:
            valid = None  # OR: torch.ones_like(samples.done, dtype=torch.float)
        else:
            valid = valid_from_done(samples.done)

        q1_loss = 0.5 * valid_mean((y - q1) ** 2, valid)
        q2_loss = 0.5 * valid_mean((y - q2) ** 2, valid)

        v = self.agent.v(*agent_inputs)
        new_action, log_pi, (pi_mean, pi_log_std) = self.agent.pi(*agent_inputs)
        if not self.reparameterize:
            new_action = new_action.detach()  # No grad.
        log_target1, log_target2 = self.agent.q(*agent_inputs, new_action)
        min_log_target = torch.min(log_target1, log_target2)
        prior_log_pi = self.get_action_prior(new_action.cpu())
        v_target = (min_log_target - log_pi + prior_log_pi).detach()  # No grad.
        v_loss = 0.5 * valid_mean((v - v_target) ** 2, valid)

        if self.reparameterize:
            pi_losses = log_pi - min_log_target
        else:
            pi_factor = (v - v_target).detach()  # No grad.
            pi_losses = log_pi * pi_factor
        if self.policy_output_regularization > 0:
            pi_losses += torch.sum(self.policy_output_regularization * 0.5 *
                pi_mean ** 2 + pi_log_std ** 2, dim=-1)
        pi_loss = valid_mean(pi_losses, valid)

        losses = (q1_loss, q2_loss, v_loss, pi_loss)
        values = tuple(val.detach() for val in (q1, q2, v, pi_mean, pi_log_std))
        return losses, values

    def get_action_prior(self, action):
        if self.action_prior == "uniform":
            prior_log_pi = 0.0
        elif self.action_prior == "gaussian":
            prior_log_pi = self.action_prior_distribution.log_likelihood(
                action, GaussianDistInfo(mean=torch.zeros_like(action)))
        return prior_log_pi

    def append_opt_info_(self, opt_info, losses, grad_norms, values):
        """In-place."""
        q1_loss, q2_loss, v_loss, pi_loss = losses
        q1_grad_norm, q2_grad_norm, v_grad_norm, pi_grad_norm = grad_norms
        q1, q2, v, pi_mean, pi_log_std = values
        opt_info.q1Loss.append(q1_loss.item())
        opt_info.q2Loss.append(q2_loss.item())
        opt_info.vLoss.append(v_loss.item())
        opt_info.piLoss.append(pi_loss.item())
        opt_info.q1GradNorm.append(q1_grad_norm)
        opt_info.q2GradNorm.append(q2_grad_norm)
        opt_info.vGradNorm.append(v_grad_norm)
        opt_info.piGradNorm.append(pi_grad_norm)
        opt_info.q1.extend(q1[::10].numpy())  # Downsample for stats.
        opt_info.q2.extend(q2[::10].numpy())
        opt_info.v.extend(v[::10].numpy())
        opt_info.piMu.extend(pi_mean[::10].numpy())
        opt_info.piLogStd.extend(pi_log_std[::10].numpy())
        opt_info.qMeanDiff.append(torch.mean(abs(q1 - q2)).item())
