# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.
import warnings
from dataclasses import dataclass
from typing import Optional, Tuple, Union

import torch
from tensordict.nn import dispatch, TensorDictModule
from tensordict.tensordict import TensorDict, TensorDictBase
from tensordict.utils import NestedKey
from torch import Tensor
from torchrl.data.tensor_specs import TensorSpec
from torchrl.data.utils import _find_action_space

from torchrl.modules import ProbabilisticActor
from torchrl.objectives.common import LossModule
from torchrl.objectives.utils import (
    _GAMMA_LMBDA_DEPREC_WARNING,
    _vmap_func,
    default_value_kwargs,
    distance_loss,
    ValueEstimators,
)
from torchrl.objectives.value import TD0Estimator, TD1Estimator, TDLambdaEstimator


class IQLLoss(LossModule):
    r"""TorchRL implementation of the IQL loss.

    Presented in "Offline Reinforcement Learning with Implicit Q-Learning" https://arxiv.org/abs/2110.06169

    Args:
        actor_network (ProbabilisticActor): stochastic actor
        qvalue_network (TensorDictModule): Q(s, a) parametric model
        value_network (TensorDictModule, optional): V(s) parametric model.

    Keyword Args:
        num_qvalue_nets (integer, optional): number of Q-Value networks used.
            Defaults to ``2``.
        loss_function (str, optional): loss function to be used with
            the value function loss. Default is `"smooth_l1"`.
        temperature (float, optional):  Inverse temperature (beta).
            For smaller hyperparameter values, the objective behaves similarly to
            behavioral cloning, while for larger values, it attempts to recover the
            maximum of the Q-function.
        expectile (float, optional): expectile :math:`\tau`. A larger value of :math:`\tau` is crucial
            for antmaze tasks that require dynamical programming ("stichting").
        priority_key (str, optional): [Deprecated, use .set_keys(priority_key=priority_key) instead]
            tensordict key where to write the priority (for prioritized replay
            buffer usage). Default is `"td_error"`.
        separate_losses (bool, optional): if ``True``, shared parameters between
            policy and critic will only be trained on the policy loss.
            Defaults to ``False``, ie. gradients are propagated to shared
            parameters for both policy and critic losses.

    Examples:
        >>> import torch
        >>> from torch import nn
        >>> from torchrl.data import BoundedTensorSpec
        >>> from torchrl.modules.distributions.continuous import NormalParamWrapper, TanhNormal
        >>> from torchrl.modules.tensordict_module.actors import ProbabilisticActor, ValueOperator
        >>> from torchrl.modules.tensordict_module.common import SafeModule
        >>> from torchrl.objectives.iql import IQLLoss
        >>> from tensordict.tensordict import TensorDict
        >>> n_act, n_obs = 4, 3
        >>> spec = BoundedTensorSpec(-torch.ones(n_act), torch.ones(n_act), (n_act,))
        >>> net = NormalParamWrapper(nn.Linear(n_obs, 2 * n_act))
        >>> module = SafeModule(net, in_keys=["observation"], out_keys=["loc", "scale"])
        >>> actor = ProbabilisticActor(
        ...     module=module,
        ...     in_keys=["loc", "scale"],
        ...     spec=spec,
        ...     distribution_class=TanhNormal)
        >>> class ValueClass(nn.Module):
        ...     def __init__(self):
        ...         super().__init__()
        ...         self.linear = nn.Linear(n_obs + n_act, 1)
        ...     def forward(self, obs, act):
        ...         return self.linear(torch.cat([obs, act], -1))
        >>> module = ValueClass()
        >>> qvalue = ValueOperator(
        ...     module=module,
        ...     in_keys=['observation', 'action'])
        >>> module = nn.Linear(n_obs, 1)
        >>> value = ValueOperator(
        ...     module=module,
        ...     in_keys=["observation"])
        >>> loss = IQLLoss(actor, qvalue, value)
        >>> batch = [2, ]
        >>> action = spec.rand(batch)
        >>> data = TensorDict({
        ...         "observation": torch.randn(*batch, n_obs),
        ...         "action": action,
        ...         ("next", "done"): torch.zeros(*batch, 1, dtype=torch.bool),
        ...         ("next", "terminated"): torch.zeros(*batch, 1, dtype=torch.bool),
        ...         ("next", "reward"): torch.randn(*batch, 1),
        ...         ("next", "observation"): torch.randn(*batch, n_obs),
        ...     }, batch)
        >>> loss(data)
        TensorDict(
            fields={
                entropy: Tensor(shape=torch.Size([]), device=cpu, dtype=torch.float32, is_shared=False),
                loss_actor: Tensor(shape=torch.Size([]), device=cpu, dtype=torch.float32, is_shared=False),
                loss_qvalue: Tensor(shape=torch.Size([]), device=cpu, dtype=torch.float32, is_shared=False),
                loss_value: Tensor(shape=torch.Size([]), device=cpu, dtype=torch.float32, is_shared=False)},
            batch_size=torch.Size([]),
            device=None,
            is_shared=False)

    This class is compatible with non-tensordict based modules too and can be
    used without recurring to any tensordict-related primitive. In this case,
    the expected keyword arguments are:
    ``["action", "next_reward", "next_done", "next_terminated"]`` + in_keys of the actor, value, and qvalue network
    The return value is a tuple of tensors in the following order:
    ``["loss_actor", "loss_qvalue", "loss_value", "entropy"]``.

    Examples:
        >>> import torch
        >>> from torch import nn
        >>> from torchrl.data import BoundedTensorSpec
        >>> from torchrl.modules.distributions.continuous import NormalParamWrapper, TanhNormal
        >>> from torchrl.modules.tensordict_module.actors import ProbabilisticActor, ValueOperator
        >>> from torchrl.modules.tensordict_module.common import SafeModule
        >>> from torchrl.objectives.iql import IQLLoss
        >>> _ = torch.manual_seed(42)
        >>> n_act, n_obs = 4, 3
        >>> spec = BoundedTensorSpec(-torch.ones(n_act), torch.ones(n_act), (n_act,))
        >>> net = NormalParamWrapper(nn.Linear(n_obs, 2 * n_act))
        >>> module = SafeModule(net, in_keys=["observation"], out_keys=["loc", "scale"])
        >>> actor = ProbabilisticActor(
        ...     module=module,
        ...     in_keys=["loc", "scale"],
        ...     spec=spec,
        ...     distribution_class=TanhNormal)
        >>> class ValueClass(nn.Module):
        ...     def __init__(self):
        ...         super().__init__()
        ...         self.linear = nn.Linear(n_obs + n_act, 1)
        ...     def forward(self, obs, act):
        ...         return self.linear(torch.cat([obs, act], -1))
        >>> module = ValueClass()
        >>> qvalue = ValueOperator(
        ...     module=module,
        ...     in_keys=['observation', 'action'])
        >>> module = nn.Linear(n_obs, 1)
        >>> value = ValueOperator(
        ...     module=module,
        ...     in_keys=["observation"])
        >>> loss = IQLLoss(actor, qvalue, value)
        >>> batch = [2, ]
        >>> action = spec.rand(batch)
        >>> loss_actor, loss_qvalue, loss_value, entropy = loss(
        ...     observation=torch.randn(*batch, n_obs),
        ...     action=action,
        ...     next_done=torch.zeros(*batch, 1, dtype=torch.bool),
        ...     next_terminated=torch.zeros(*batch, 1, dtype=torch.bool),
        ...     next_observation=torch.zeros(*batch, n_obs),
        ...     next_reward=torch.randn(*batch, 1))
        >>> loss_actor.backward()


    The output keys can also be filtered using the :meth:`IQLLoss.select_out_keys`
    method.

    Examples:
        >>> loss.select_out_keys('loss_actor', 'loss_qvalue')
        >>> loss_actor, loss_qvalue = loss(
        ...     observation=torch.randn(*batch, n_obs),
        ...     action=action,
        ...     next_done=torch.zeros(*batch, 1, dtype=torch.bool),
        ...     next_terminated=torch.zeros(*batch, 1, dtype=torch.bool),
        ...     next_observation=torch.zeros(*batch, n_obs),
        ...     next_reward=torch.randn(*batch, 1))
        >>> loss_actor.backward()
    """

    @dataclass
    class _AcceptedKeys:
        """Maintains default values for all configurable tensordict keys.

        This class defines which tensordict keys can be set using '.set_keys(key_name=key_value)' and their
        default values

        Attributes:
            value (NestedKey): The input tensordict key where the state value is expected.
                Will be used for the underlying value estimator. Defaults to ``"state_value"``.
            action (NestedKey): The input tensordict key where the action is expected.
                Defaults to ``"action"``.
            log_prob (NestedKey): The input tensordict key where the log probability is expected.
                Defaults to ``"_log_prob"``.
            priority (NestedKey): The input tensordict key where the target priority is written to.
                Defaults to ``"td_error"``.
            state_action_value (NestedKey): The input tensordict key where the
                state action value is expected. Will be used for the underlying
                value estimator as value key. Defaults to ``"state_action_value"``.
            reward (NestedKey): The input tensordict key where the reward is expected.
                Will be used for the underlying value estimator. Defaults to ``"reward"``.
            done (NestedKey): The key in the input TensorDict that indicates
                whether a trajectory is done. Will be used for the underlying value estimator.
                Defaults to ``"done"``.
            terminated (NestedKey): The key in the input TensorDict that indicates
                whether a trajectory is terminated. Will be used for the underlying value estimator.
                Defaults to ``"terminated"``.
        """

        value: NestedKey = "state_value"
        action: NestedKey = "action"
        log_prob: NestedKey = "_log_prob"
        priority: NestedKey = "td_error"
        state_action_value: NestedKey = "state_action_value"
        reward: NestedKey = "reward"
        done: NestedKey = "done"
        terminated: NestedKey = "terminated"

    default_keys = _AcceptedKeys()
    default_value_estimator = ValueEstimators.TD0
    out_keys = [
        "loss_actor",
        "loss_qvalue",
        "loss_value",
        "entropy",
    ]

    def __init__(
        self,
        actor_network: ProbabilisticActor,
        qvalue_network: TensorDictModule,
        value_network: Optional[TensorDictModule],
        *,
        num_qvalue_nets: int = 2,
        loss_function: str = "smooth_l1",
        temperature: float = 1.0,
        expectile: float = 0.5,
        gamma: float = None,
        priority_key: str = None,
        separate_losses: bool = False,
    ) -> None:
        self._in_keys = None
        self._out_keys = None
        super().__init__()
        self._set_deprecated_ctor_keys(priority=priority_key)

        # IQL parameter
        self.temperature = temperature
        self.expectile = expectile

        # Actor Network
        self.convert_to_functional(
            actor_network,
            "actor_network",
            create_target_params=False,
        )
        if separate_losses:
            # we want to make sure there are no duplicates in the params: the
            # params of critic must be refs to actor if they're shared
            policy_params = list(actor_network.parameters())
        else:
            policy_params = None
        # Value Function Network
        self.convert_to_functional(
            value_network,
            "value_network",
            create_target_params=False,
            compare_against=policy_params,
        )

        # Q Function Network
        self.delay_qvalue = True
        self.num_qvalue_nets = num_qvalue_nets
        if separate_losses and policy_params is not None:
            qvalue_policy_params = list(actor_network.parameters()) + list(
                value_network.parameters()
            )
        else:
            qvalue_policy_params = None
        self.convert_to_functional(
            qvalue_network,
            "qvalue_network",
            num_qvalue_nets,
            create_target_params=True,
            compare_against=qvalue_policy_params,
        )

        self.loss_function = loss_function
        if gamma is not None:
            warnings.warn(_GAMMA_LMBDA_DEPREC_WARNING, category=DeprecationWarning)
            self.gamma = gamma
        self._vmap_qvalue_networkN0 = _vmap_func(
            self.qvalue_network, (None, 0), randomness=self.vmap_randomness
        )

    @property
    def device(self) -> torch.device:
        warnings.warn(
            "The device attributes of the looses will be deprecated in v0.3.",
            category=DeprecationWarning,
        )
        for p in self.parameters():
            return p.device
        raise RuntimeError(
            "At least one of the networks of SACLoss must have trainable " "parameters."
        )

    def _set_in_keys(self):
        keys = [
            self.tensor_keys.action,
            ("next", self.tensor_keys.reward),
            ("next", self.tensor_keys.done),
            ("next", self.tensor_keys.terminated),
            *self.actor_network.in_keys,
            *[("next", key) for key in self.actor_network.in_keys],
            *self.qvalue_network.in_keys,
            *self.value_network.in_keys,
        ]
        self._in_keys = list(set(keys))

    @property
    def in_keys(self):
        if self._in_keys is None:
            self._set_in_keys()
        return self._in_keys

    @in_keys.setter
    def in_keys(self, values):
        self._in_keys = values

    @staticmethod
    def loss_value_diff(diff, expectile=0.8):
        """Loss function for iql expectile value difference."""
        weight = torch.where(diff > 0, expectile, (1 - expectile))
        return weight * (diff**2)

    def _forward_value_estimator_keys(self, **kwargs) -> None:
        if self._value_estimator is not None:
            self._value_estimator.set_keys(
                value=self._tensor_keys.value,
                reward=self.tensor_keys.reward,
                done=self.tensor_keys.done,
                terminated=self.tensor_keys.terminated,
            )
        self._set_in_keys()

    @dispatch
    def forward(self, tensordict: TensorDictBase) -> TensorDictBase:
        shape = None
        if tensordict.ndimension() > 1:
            shape = tensordict.shape
            tensordict_reshape = tensordict.reshape(-1)
        else:
            tensordict_reshape = tensordict

        loss_actor, metadata = self.actor_loss(tensordict_reshape)
        loss_qvalue, metadata_qvalue = self.qvalue_loss(tensordict_reshape)
        loss_value, metadata_value = self.value_loss(tensordict_reshape)
        metadata.update(**metadata_qvalue, **metadata_value)

        if (loss_actor.shape != loss_qvalue.shape) or (
            loss_value is not None and loss_actor.shape != loss_value.shape
        ):
            raise RuntimeError(
                f"Losses shape mismatch: {loss_actor.shape}, {loss_qvalue.shape} and {loss_value.shape}"
            )
        tensordict_reshape.set(
            self.tensor_keys.priority, metadata.pop("td_error").detach().max(0).values
        )
        if shape:
            tensordict.update(tensordict_reshape.view(shape))

        entropy = -tensordict_reshape.get(self.tensor_keys.log_prob).detach()
        out = {
            "loss_actor": loss_actor.mean(),
            "loss_qvalue": loss_qvalue.mean(),
            "loss_value": loss_value.mean(),
            "entropy": entropy.mean(),
        }

        return TensorDict(
            out,
            [],
        )

    def actor_loss(self, tensordict: TensorDictBase) -> Tensor:
        # KL loss
        with self.actor_network_params.to_module(self.actor_network):
            dist = self.actor_network.get_dist(tensordict)

        log_prob = dist.log_prob(tensordict[self.tensor_keys.action])

        # Min Q value
        td_q = tensordict.select(*self.qvalue_network.in_keys)
        td_q = self._vmap_qvalue_networkN0(td_q, self.target_qvalue_network_params)
        min_q = td_q.get(self.tensor_keys.state_action_value).min(0)[0].squeeze(-1)

        if log_prob.shape != min_q.shape:
            raise RuntimeError(
                f"Losses shape mismatch: {log_prob.shape} and {min_q.shape}"
            )
        # state value
        with torch.no_grad():
            td_copy = tensordict.select(*self.value_network.in_keys).detach()
            with self.value_network_params.to_module(self.value_network):
                self.value_network(td_copy)
            value = td_copy.get(self.tensor_keys.value).squeeze(
                -1
            )  # assert has no gradient

        exp_a = torch.exp((min_q - value) * self.temperature)
        exp_a = torch.min(exp_a, torch.FloatTensor([100.0]).to(self.device))

        # write log_prob in tensordict for alpha loss
        tensordict.set(self.tensor_keys.log_prob, log_prob.detach())
        return -(exp_a * log_prob).mean(), {}

    def value_loss(self, tensordict: TensorDictBase) -> Tuple[Tensor, Tensor]:
        # Min Q value
        td_q = tensordict.select(*self.qvalue_network.in_keys)
        td_q = self._vmap_qvalue_networkN0(td_q, self.target_qvalue_network_params)
        min_q = td_q.get(self.tensor_keys.state_action_value).min(0)[0].squeeze(-1)
        # state value
        td_copy = tensordict.select(*self.value_network.in_keys)
        with self.value_network_params.to_module(self.value_network):
            self.value_network(td_copy)
        value = td_copy.get(self.tensor_keys.value).squeeze(-1)
        value_loss = self.loss_value_diff(min_q - value, self.expectile).mean()
        return value_loss, {}

    def qvalue_loss(self, tensordict: TensorDictBase) -> Tuple[Tensor, Tensor]:
        obs_keys = self.actor_network.in_keys
        tensordict = tensordict.select("next", *obs_keys, self.tensor_keys.action)

        target_value = self.value_estimator.value_estimate(
            tensordict, target_params=self.target_value_network_params
        ).squeeze(-1)
        tensordict_expand = self._vmap_qvalue_networkN0(
            tensordict.select(*self.qvalue_network.in_keys),
            self.qvalue_network_params,
        )
        pred_val = tensordict_expand.get(self.tensor_keys.state_action_value).squeeze(
            -1
        )
        td_error = (pred_val - target_value).pow(2)
        loss_qval = (
            distance_loss(
                pred_val,
                target_value.expand_as(pred_val),
                loss_function=self.loss_function,
            )
            .sum(0)
            .mean()
        )
        metadata = {"td_error": td_error.detach()}
        return loss_qval, metadata

    def make_value_estimator(self, value_type: ValueEstimators = None, **hyperparams):
        if value_type is None:
            value_type = self.default_value_estimator
        self.value_type = value_type
        value_net = self.value_network

        hp = dict(default_value_kwargs(value_type))
        if hasattr(self, "gamma"):
            hp["gamma"] = self.gamma
        hp.update(hyperparams)
        if value_type is ValueEstimators.TD1:
            self._value_estimator = TD1Estimator(
                **hp,
                value_network=value_net,
            )
        elif value_type is ValueEstimators.TD0:
            self._value_estimator = TD0Estimator(
                **hp,
                value_network=value_net,
            )
        elif value_type is ValueEstimators.GAE:
            raise NotImplementedError(
                f"Value type {value_type} it not implemented for loss {type(self)}."
            )
        elif value_type is ValueEstimators.TDLambda:
            self._value_estimator = TDLambdaEstimator(
                **hp,
                value_network=value_net,
            )
        else:
            raise NotImplementedError(f"Unknown value type {value_type}")

        tensor_keys = {
            "value_target": "value_target",
            "value": self.tensor_keys.value,
            "reward": self.tensor_keys.reward,
            "done": self.tensor_keys.done,
            "terminated": self.tensor_keys.terminated,
        }
        self._value_estimator.set_keys(**tensor_keys)


class DiscreteIQLLoss(IQLLoss):
    r"""TorchRL implementation of the discrete IQL loss.

    Presented in "Offline Reinforcement Learning with Implicit Q-Learning" https://arxiv.org/abs/2110.06169

    Args:
        actor_network (ProbabilisticActor): stochastic actor
        qvalue_network (TensorDictModule): Q(s) parametric model
        value_network (TensorDictModule, optional): V(s) parametric model.

    Keyword Args:
        action_space (str or TensorSpec): Action space. Must be one of
                ``"one-hot"``, ``"mult_one_hot"``, ``"binary"`` or ``"categorical"``,
                or an instance of the corresponding specs (:class:`torchrl.data.OneHotDiscreteTensorSpec`,
                :class:`torchrl.data.MultiOneHotDiscreteTensorSpec`,
                :class:`torchrl.data.BinaryDiscreteTensorSpec` or :class:`torchrl.data.DiscreteTensorSpec`).
        num_qvalue_nets (integer, optional): number of Q-Value networks used.
            Defaults to ``2``.
        loss_function (str, optional): loss function to be used with
            the value function loss. Default is `"smooth_l1"`.
        temperature (float, optional):  Inverse temperature (beta).
            For smaller hyperparameter values, the objective behaves similarly to
            behavioral cloning, while for larger values, it attempts to recover the
            maximum of the Q-function.
        expectile (float, optional): expectile :math:`\tau`. A larger value of :math:`\tau` is crucial
            for antmaze tasks that require dynamical programming ("stichting").
        priority_key (str, optional): [Deprecated, use .set_keys(priority_key=priority_key) instead]
            tensordict key where to write the priority (for prioritized replay
            buffer usage). Default is `"td_error"`.
        separate_losses (bool, optional): if ``True``, shared parameters between
            policy and critic will only be trained on the policy loss.
            Defaults to ``False``, ie. gradients are propagated to shared
            parameters for both policy and critic losses.

    Examples:
        >>> import torch
        >>> from torch import nn
        >>> from torchrl.data.tensor_specs import OneHotDiscreteTensorSpec
        >>> from torchrl.modules.distributions.continuous import NormalParamWrapper
        >>> from torchrl.modules.distributions.discrete import OneHotCategorical
        >>> from torchrl.modules.tensordict_module.actors import ProbabilisticActor, ValueOperator
        >>> from torchrl.modules.tensordict_module.common import SafeModule
        >>> from torchrl.objectives.iql import DiscreteIQLLoss
        >>> from tensordict.tensordict import TensorDict
        >>> n_act, n_obs = 4, 3
        >>> spec = OneHotDiscreteTensorSpec(n_act)
        >>> module = TensorDictModule(nn.Linear(n_obs, n_act), in_keys=["observation"], out_keys=["logits"])
        >>> actor = ProbabilisticActor(
        ...     module=module,
        ...     in_keys=["logits"],
        ...     out_keys=["action"],
        ...     spec=spec,
        ...     distribution_class=OneHotCategorical)
        >>> qvalue = TensorDictModule(
        ...     nn.Linear(n_obs),
        ...     in_keys=["observation"],
        ...     out_keys=["state_action_value"],
        ... )
        >>> value = TensorDictModule(
        ...     nn.Linear(n_obs),
        ...     in_keys=["observation"],
        ...     out_keys=["state_value"],
        ... )
        >>> loss = DiscreteIQLLoss(actor, qvalue, value)
        >>> batch = [2, ]
        >>> action = spec.rand(batch)
        >>> data = TensorDict({
        ...         "observation": torch.randn(*batch, n_obs),
        ...         "action": action,
        ...         ("next", "done"): torch.zeros(*batch, 1, dtype=torch.bool),
        ...         ("next", "terminated"): torch.zeros(*batch, 1, dtype=torch.bool),
        ...         ("next", "reward"): torch.randn(*batch, 1),
        ...         ("next", "observation"): torch.randn(*batch, n_obs),
        ...     }, batch)
        >>> loss(data)
        TensorDict(
            fields={
                entropy: Tensor(shape=torch.Size([]), device=cpu, dtype=torch.float32, is_shared=False),
                loss_actor: Tensor(shape=torch.Size([]), device=cpu, dtype=torch.float32, is_shared=False),
                loss_qvalue: Tensor(shape=torch.Size([]), device=cpu, dtype=torch.float32, is_shared=False),
                loss_value: Tensor(shape=torch.Size([]), device=cpu, dtype=torch.float32, is_shared=False)},
            batch_size=torch.Size([]),
            device=None,
            is_shared=False)

    This class is compatible with non-tensordict based modules too and can be
    used without recurring to any tensordict-related primitive. In this case,
    the expected keyword arguments are:
    ``["action", "next_reward", "next_done", "next_terminated"]`` + in_keys of the actor, value, and qvalue network
    The return value is a tuple of tensors in the following order:
    ``["loss_actor", "loss_qvalue", "loss_value", "entropy"]``.

    Examples:
        >>> import torch
        >>> import torch
        >>> from torch import nn
        >>> from torchrl.data.tensor_specs import OneHotDiscreteTensorSpec
        >>> from torchrl.modules.distributions.continuous import NormalParamWrapper
        >>> from torchrl.modules.distributions.discrete import OneHotCategorical
        >>> from torchrl.modules.tensordict_module.actors import ProbabilisticActor, ValueOperator
        >>> from torchrl.modules.tensordict_module.common import SafeModule
        >>> from torchrl.objectives.iql import DiscreteIQLLoss
        >>> from tensordict.tensordict import TensorDict
        >>> _ = torch.manual_seed(42)
        >>> n_act, n_obs = 4, 3
        >>> spec = OneHotDiscreteTensorSpec(n_act)
        >>> net = NormalParamWrapper(nn.Linear(n_obs, 2 * n_act))
        >>> module = SafeModule(net, in_keys=["observation"], out_keys=["logits"])
        >>> actor = ProbabilisticActor(
        ...     module=module,
        ...     in_keys=["logits"],
        ...     out_keys=["action"],
        ...     spec=spec,
        ...     distribution_class=OneHotCategorical)
        >>> class ValueClass(nn.Module):
        ...     def __init__(self):
        ...         super().__init__()
        ...         self.linear = nn.Linear(n_obs, n_act)
        ...     def forward(self, obs):
        ...         return self.linear(obs)
        >>> module = ValueClass()
        >>> qvalue = ValueOperator(
        ...     module=module,
        ...     in_keys=['observation'])
        >>> module = nn.Linear(n_obs, 1)
        >>> value = ValueOperator(
        ...     module=module,
        ...     in_keys=["observation"])
        >>> loss = DiscreteIQLLoss(actor, qvalue, value)
        >>> batch = [2, ]
        >>> action = spec.rand(batch)
        >>> loss_actor, loss_qvalue, loss_value, entropy = loss(
        ...     observation=torch.randn(*batch, n_obs),
        ...     action=action,
        ...     next_done=torch.zeros(*batch, 1, dtype=torch.bool),
        ...     next_terminated=torch.zeros(*batch, 1, dtype=torch.bool),
        ...     next_observation=torch.zeros(*batch, n_obs),
        ...     next_reward=torch.randn(*batch, 1))
        >>> loss_actor.backward()


    The output keys can also be filtered using the :meth:`DiscreteIQLLoss.select_out_keys`
    method.

    Examples:
        >>> loss.select_out_keys('loss_actor', 'loss_qvalue', 'loss_value')
        >>> loss_actor, loss_qvalue, loss_value = loss(
        ...     observation=torch.randn(*batch, n_obs),
        ...     action=action,
        ...     next_done=torch.zeros(*batch, 1, dtype=torch.bool),
        ...     next_terminated=torch.zeros(*batch, 1, dtype=torch.bool),
        ...     next_observation=torch.zeros(*batch, n_obs),
        ...     next_reward=torch.randn(*batch, 1))
        >>> loss_actor.backward()
    """

    @dataclass
    class _AcceptedKeys:
        """Maintains default values for all configurable tensordict keys.

        This class defines which tensordict keys can be set using '.set_keys(key_name=key_value)' and their
        default values

        Attributes:
            value (NestedKey): The input tensordict key where the state value is expected.
                Will be used for the underlying value estimator. Defaults to ``"state_value"``.
            action (NestedKey): The input tensordict key where the action is expected.
                Defaults to ``"action"``.
            log_prob (NestedKey): The input tensordict key where the log probability is expected.
                Defaults to ``"_log_prob"``.
            priority (NestedKey): The input tensordict key where the target priority is written to.
                Defaults to ``"td_error"``.
            state_action_value (NestedKey): The input tensordict key where the
                state action value is expected. Will be used for the underlying
                value estimator as value key. Defaults to ``"state_action_value"``.
            reward (NestedKey): The input tensordict key where the reward is expected.
                Will be used for the underlying value estimator. Defaults to ``"reward"``.
            done (NestedKey): The key in the input TensorDict that indicates
                whether a trajectory is done. Will be used for the underlying value estimator.
                Defaults to ``"done"``.
            terminated (NestedKey): The key in the input TensorDict that indicates
                whether a trajectory is terminated. Will be used for the underlying value estimator.
                Defaults to ``"terminated"``.
        """

        value: NestedKey = "state_value"
        action: NestedKey = "action"
        log_prob: NestedKey = "_log_prob"
        priority: NestedKey = "td_error"
        state_action_value: NestedKey = "state_action_value"
        reward: NestedKey = "reward"
        done: NestedKey = "done"
        terminated: NestedKey = "terminated"

    default_keys = _AcceptedKeys()
    default_value_estimator = ValueEstimators.TD0
    out_keys = [
        "loss_actor",
        "loss_qvalue",
        "loss_value",
        "entropy",
    ]

    def __init__(
        self,
        actor_network: ProbabilisticActor,
        qvalue_network: TensorDictModule,
        value_network: Optional[TensorDictModule],
        *,
        action_space: Union[str, TensorSpec] = None,
        num_qvalue_nets: int = 2,
        loss_function: str = "smooth_l1",
        temperature: float = 1.0,
        expectile: float = 0.5,
        gamma: float = None,
        priority_key: str = None,
        separate_losses: bool = False,
    ) -> None:
        self._in_keys = None
        self._out_keys = None
        if expectile >= 1.0:
            raise ValueError(f"Expectile should be lower than 1.0 but is {expectile}")
        super().__init__(
            actor_network=actor_network,
            qvalue_network=qvalue_network,
            value_network=value_network,
            num_qvalue_nets=num_qvalue_nets,
            loss_function=loss_function,
            temperature=temperature,
            expectile=expectile,
            gamma=gamma,
            priority_key=priority_key,
            separate_losses=separate_losses,
        )
        if action_space is None:
            warnings.warn(
                "action_space was not specified. DiscreteIQLLoss will default to 'one-hot'."
                "This behaviour will be deprecated soon and a space will have to be passed."
                "Check the DiscreteIQLLoss documentation to see how to pass the action space. "
            )
            action_space = "one-hot"
        self.action_space = _find_action_space(action_space)

    def actor_loss(self, tensordict: TensorDictBase) -> Tensor:
        # KL loss
        with self.actor_network_params.to_module(self.actor_network):
            dist = self.actor_network.get_dist(tensordict)

        log_prob = dist.log_prob(tensordict[self.tensor_keys.action])

        # Min Q value
        td_q = tensordict.select(*self.qvalue_network.in_keys)
        td_q = self._vmap_qvalue_networkN0(td_q, self.target_qvalue_network_params)
        state_action_value = td_q.get(self.tensor_keys.state_action_value)
        action = tensordict.get(self.tensor_keys.action)
        if self.action_space == "categorical":
            if action.shape != state_action_value.shape:
                # unsqueeze the action if it lacks on trailing singleton dim
                action = action.unsqueeze(-1)
            chosen_state_action_value = torch.gather(
                state_action_value, -1, index=action
            ).squeeze(-1)
        else:
            action = action.to(torch.float)
            chosen_state_action_value = (state_action_value * action).sum(-1)
        min_Q, _ = torch.min(chosen_state_action_value, dim=0)
        if log_prob.shape != min_Q.shape:
            raise RuntimeError(
                f"Losses shape mismatch: {log_prob.shape} and {min_Q.shape}"
            )
        with torch.no_grad():
            # state value
            td_copy = tensordict.select(*self.value_network.in_keys).detach()
            with self.value_network_params.to_module(self.value_network):
                self.value_network(td_copy)
            value = td_copy.get(self.tensor_keys.value).squeeze(
                -1
            )  # assert has no gradient

        exp_a = torch.exp((min_Q - value) * self.temperature)
        exp_a = torch.min(exp_a, torch.FloatTensor([100.0]).to(self.device))

        # write log_prob in tensordict for alpha loss
        tensordict.set(self.tensor_keys.log_prob, log_prob.detach())
        return -(exp_a * log_prob).mean(), {}

    def value_loss(self, tensordict: TensorDictBase) -> Tuple[Tensor, Tensor]:
        # Min Q value
        with torch.no_grad():
            # Min Q value
            td_q = tensordict.select(*self.qvalue_network.in_keys)
            td_q = self._vmap_qvalue_networkN0(td_q, self.target_qvalue_network_params)
            state_action_value = td_q.get(self.tensor_keys.state_action_value)
            action = tensordict.get(self.tensor_keys.action)
            if self.action_space == "categorical":
                if action.shape != state_action_value.shape:
                    # unsqueeze the action if it lacks on trailing singleton dim
                    action = action.unsqueeze(-1)
                chosen_state_action_value = torch.gather(
                    state_action_value, -1, index=action
                ).squeeze(-1)
            else:
                action = action.to(torch.float)
                chosen_state_action_value = (state_action_value * action).sum(-1)
            min_Q, _ = torch.min(chosen_state_action_value, dim=0)
        # state value
        td_copy = tensordict.select(*self.value_network.in_keys)
        with self.value_network_params.to_module(self.value_network):
            self.value_network(td_copy)
        value = td_copy.get(self.tensor_keys.value).squeeze(-1)
        value_loss = self.loss_value_diff(min_Q - value, self.expectile).mean()
        return value_loss, {}

    def qvalue_loss(self, tensordict: TensorDictBase) -> Tuple[Tensor, Tensor]:
        obs_keys = self.actor_network.in_keys
        next_td = tensordict.select("next", *obs_keys, self.tensor_keys.action)
        with torch.no_grad():
            target_value = self.value_estimator.value_estimate(
                next_td, target_params=self.target_value_network_params
            ).squeeze(-1)

        # predict current Q value
        td_q = tensordict.select(*self.qvalue_network.in_keys)
        td_q = self._vmap_qvalue_networkN0(td_q, self.qvalue_network_params)
        state_action_value = td_q.get(self.tensor_keys.state_action_value)
        action = tensordict.get(self.tensor_keys.action)
        if self.action_space == "categorical":
            if action.shape != state_action_value.shape:
                # unsqueeze the action if it lacks on trailing singleton dim
                action = action.unsqueeze(-1)
            pred_val = torch.gather(state_action_value, -1, index=action).squeeze(-1)
        else:
            action = action.to(torch.float)
            pred_val = (state_action_value * action).sum(-1)

        td_error = (pred_val - target_value.expand_as(pred_val)).pow(2)
        loss_qval = (
            distance_loss(
                pred_val,
                target_value.expand_as(pred_val),
                loss_function=self.loss_function,
            )
            .sum(0)
            .mean()
        )
        metadata = {"td_error": td_error.detach()}
        return loss_qval, metadata
