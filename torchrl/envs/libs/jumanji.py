# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.
import importlib.util

from typing import Dict, Optional, Tuple, Union

import numpy as np
import torch
from tensordict.tensordict import TensorDict, TensorDictBase
from torchrl.envs.utils import _classproperty

_has_jumanji = importlib.util.find_spec("jumanji") is not None

from torchrl.data.tensor_specs import (
    BoundedTensorSpec,
    CompositeSpec,
    DEVICE_TYPING,
    DiscreteTensorSpec,
    OneHotDiscreteTensorSpec,
    TensorSpec,
    UnboundedContinuousTensorSpec,
    UnboundedDiscreteTensorSpec,
)
from torchrl.data.utils import numpy_to_torch_dtype_dict
from torchrl.envs.gym_like import GymLikeEnv

from torchrl.envs.libs.jax_utils import (
    _extract_spec,
    _ndarray_to_tensor,
    _object_to_tensordict,
    _tensordict_to_object,
    _tree_flatten,
    _tree_reshape,
)


def _get_envs():
    if not _has_jumanji:
        raise ImportError("Jumanji is not installed in your virtual environment.")
    import jumanji

    return jumanji.registered_environments()


def _jumanji_to_torchrl_spec_transform(
    spec,
    dtype: Optional[torch.dtype] = None,
    device: DEVICE_TYPING = None,
    categorical_action_encoding: bool = True,
) -> TensorSpec:
    import jumanji

    if isinstance(spec, jumanji.specs.DiscreteArray):
        action_space_cls = (
            DiscreteTensorSpec
            if categorical_action_encoding
            else OneHotDiscreteTensorSpec
        )
        if dtype is None:
            dtype = numpy_to_torch_dtype_dict[spec.dtype]
        return action_space_cls(spec.num_values, dtype=dtype, device=device)
    elif isinstance(spec, jumanji.specs.BoundedArray):
        shape = spec.shape
        if dtype is None:
            dtype = numpy_to_torch_dtype_dict[spec.dtype]
        return BoundedTensorSpec(
            shape=shape,
            low=np.asarray(spec.minimum),
            high=np.asarray(spec.maximum),
            dtype=dtype,
            device=device,
        )
    elif isinstance(spec, jumanji.specs.Array):
        shape = spec.shape
        if dtype is None:
            dtype = numpy_to_torch_dtype_dict[spec.dtype]
        if dtype in (torch.float, torch.double, torch.half):
            return UnboundedContinuousTensorSpec(
                shape=shape, dtype=dtype, device=device
            )
        else:
            return UnboundedDiscreteTensorSpec(shape=shape, dtype=dtype, device=device)
    elif isinstance(spec, jumanji.specs.Spec) and hasattr(spec, "__dict__"):
        new_spec = {}
        for key, value in spec.__dict__.items():
            if isinstance(value, jumanji.specs.Spec):
                if key.endswith("_obs"):
                    key = key[:-4]
                if key.endswith("_spec"):
                    key = key[:-5]
                new_spec[key] = _jumanji_to_torchrl_spec_transform(
                    value, dtype, device, categorical_action_encoding
                )
        return CompositeSpec(**new_spec)
    else:
        raise TypeError(f"Unsupported spec type {type(spec)}")


class JumanjiWrapper(GymLikeEnv):
    """Jumanji environment wrapper.

    Jumanji offers a vectorized simulation framework based on Jax.
    TorchRL's wrapper incurs some overhead for the jax-to-torch conversion,
    but computational graphs can still be built on top of the simulated trajectories,
    allowing for backpropagation through the rollout.

    GitHub: https://github.com/instadeepai/jumanji

    Doc: https://instadeepai.github.io/jumanji/

    Paper: https://arxiv.org/abs/2306.09884

    Args:
        env (jumanji.env.Environment): the env to wrap.
        categorical_action_encoding (bool, optional): if ``True``, categorical
            specs will be converted to the TorchRL equivalent (:class:`torchrl.data.DiscreteTensorSpec`),
            otherwise a one-hot encoding will be used (:class:`torchrl.data.OneHotTensorSpec`).
            Defaults to ``False``.

    Keyword Args:
        from_pixels (bool, optional): Not yet supported.
        frame_skip (int, optional): if provided, indicates for how many steps the
            same action is to be repeated. The observation returned will be the
            last observation of the sequence, whereas the reward will be the sum
            of rewards across steps.
        device (torch.device, optional): if provided, the device on which the data
            is to be cast. Defaults to ``torch.device("cpu")``.
        batch_size (torch.Size, optional): the batch size of the environment.
            With ``jumanji``, this indicates the number of vectorized environments.
            Defaults to ``torch.Size([])``.
        allow_done_after_reset (bool, optional): if ``True``, it is tolerated
            for envs to be ``done`` just after :meth:`~.reset` is called.
            Defaults to ``False``.

    Attributes:
        available_envs: environments availalbe to build

    Examples:
    Examples:
        >>> import jumanji
        >>> from torchrl.envs import JumanjiWrapper
        >>> base_env = jumanji.make("Snake-v1")
        >>> env = JumanjiWrapper(base_env)
        >>> env.set_seed(0)
        >>> td = env.reset()
        >>> td["action"] = env.action_spec.rand()
        >>> td = env.step(td)
        >>> print(td)
        TensorDict(
            fields={
                action: Tensor(shape=torch.Size([]), device=cpu, dtype=torch.int32, is_shared=False),
                action_mask: Tensor(shape=torch.Size([4]), device=cpu, dtype=torch.bool, is_shared=False),
                done: Tensor(shape=torch.Size([1]), device=cpu, dtype=torch.bool, is_shared=False),
                grid: Tensor(shape=torch.Size([12, 12, 5]), device=cpu, dtype=torch.float32, is_shared=False),
                next: TensorDict(
                    fields={
                        action_mask: Tensor(shape=torch.Size([4]), device=cpu, dtype=torch.bool, is_shared=False),
                        done: Tensor(shape=torch.Size([1]), device=cpu, dtype=torch.bool, is_shared=False),
                        grid: Tensor(shape=torch.Size([12, 12, 5]), device=cpu, dtype=torch.float32, is_shared=False),
                        reward: Tensor(shape=torch.Size([1]), device=cpu, dtype=torch.float32, is_shared=False),
                        state: TensorDict(
                            fields={
                                action_mask: Tensor(shape=torch.Size([4]), device=cpu, dtype=torch.bool, is_shared=False),
                                body: Tensor(shape=torch.Size([12, 12]), device=cpu, dtype=torch.bool, is_shared=False),
                                body_state: Tensor(shape=torch.Size([12, 12]), device=cpu, dtype=torch.int32, is_shared=False),
                                fruit_position: TensorDict(
                                    fields={
                                        col: Tensor(shape=torch.Size([]), device=cpu, dtype=torch.int32, is_shared=False),
                                        row: Tensor(shape=torch.Size([]), device=cpu, dtype=torch.int32, is_shared=False)},
                                    batch_size=torch.Size([]),
                                    device=cpu,
                                    is_shared=False),
                                head_position: TensorDict(
                                    fields={
                                        col: Tensor(shape=torch.Size([]), device=cpu, dtype=torch.int32, is_shared=False),
                                        row: Tensor(shape=torch.Size([]), device=cpu, dtype=torch.int32, is_shared=False)},
                                    batch_size=torch.Size([]),
                                    device=cpu,
                                    is_shared=False),
                                key: Tensor(shape=torch.Size([2]), device=cpu, dtype=torch.int32, is_shared=False),
                                length: Tensor(shape=torch.Size([]), device=cpu, dtype=torch.int32, is_shared=False),
                                step_count: Tensor(shape=torch.Size([]), device=cpu, dtype=torch.int32, is_shared=False),
                                tail: Tensor(shape=torch.Size([12, 12]), device=cpu, dtype=torch.bool, is_shared=False)},
                            batch_size=torch.Size([]),
                            device=cpu,
                            is_shared=False),
                        step_count: Tensor(shape=torch.Size([]), device=cpu, dtype=torch.int32, is_shared=False),
                        terminated: Tensor(shape=torch.Size([1]), device=cpu, dtype=torch.bool, is_shared=False)},
                    batch_size=torch.Size([]),
                    device=cpu,
                    is_shared=False),
                state: TensorDict(
                    fields={
                        action_mask: Tensor(shape=torch.Size([4]), device=cpu, dtype=torch.bool, is_shared=False),
                        body: Tensor(shape=torch.Size([12, 12]), device=cpu, dtype=torch.bool, is_shared=False),
                        body_state: Tensor(shape=torch.Size([12, 12]), device=cpu, dtype=torch.int32, is_shared=False),
                        fruit_position: TensorDict(
                            fields={
                                col: Tensor(shape=torch.Size([]), device=cpu, dtype=torch.int32, is_shared=False),
                                row: Tensor(shape=torch.Size([]), device=cpu, dtype=torch.int32, is_shared=False)},
                            batch_size=torch.Size([]),
                            device=cpu,
                            is_shared=False),
                        head_position: TensorDict(
                            fields={
                                col: Tensor(shape=torch.Size([]), device=cpu, dtype=torch.int32, is_shared=False),
                                row: Tensor(shape=torch.Size([]), device=cpu, dtype=torch.int32, is_shared=False)},
                            batch_size=torch.Size([]),
                            device=cpu,
                            is_shared=False),
                        key: Tensor(shape=torch.Size([2]), device=cpu, dtype=torch.int32, is_shared=False),
                        length: Tensor(shape=torch.Size([]), device=cpu, dtype=torch.int32, is_shared=False),
                        step_count: Tensor(shape=torch.Size([]), device=cpu, dtype=torch.int32, is_shared=False),
                        tail: Tensor(shape=torch.Size([12, 12]), device=cpu, dtype=torch.bool, is_shared=False)},
                    batch_size=torch.Size([]),
                    device=cpu,
                    is_shared=False),
                step_count: Tensor(shape=torch.Size([]), device=cpu, dtype=torch.int32, is_shared=False),
                terminated: Tensor(shape=torch.Size([1]), device=cpu, dtype=torch.bool, is_shared=False)},
            batch_size=torch.Size([]),
            device=cpu,
            is_shared=False)
        >>> print(env.available_envs)
        ['Game2048-v1',
         'Maze-v0',
         'Cleaner-v0',
         'CVRP-v1',
         'MultiCVRP-v0',
         'Minesweeper-v0',
         'RubiksCube-v0',
         'Knapsack-v1',
         'Sudoku-v0',
         'Snake-v1',
         'TSP-v1',
         'Connector-v2',
         'MMST-v0',
         'GraphColoring-v0',
         'RubiksCube-partly-scrambled-v0',
         'RobotWarehouse-v0',
         'Tetris-v0',
         'BinPack-v2',
         'Sudoku-very-easy-v0',
         'JobShop-v0']

    To take advante of Jumanji, one usually executes multiple environments at the
    same time.

        >>> import jumanji
        >>> from torchrl.envs import JumanjiWrapper
        >>> base_env = jumanji.make("Snake-v1")
        >>> env = JumanjiWrapper(base_env, batch_size=[10])
        >>> env.set_seed(0)
        >>> td = env.reset()
        >>> td["action"] = env.action_spec.rand()
        >>> td = env.step(td)

    In the following example, we iteratively test different batch sizes
    and report the execution time for a short rollout:

    Examples:
        >>> from torch.utils.benchmark import Timer
        >>> for batch_size in [4, 16, 128]:
        ...     timer = Timer(
        ...     '''
        ... env.rollout(100)
        ... ''',
        ... setup=f'''
        ... from torchrl.envs import JumanjiWrapper
        ... import jumanji
        ... env = JumanjiWrapper(jumanji.make('Snake-v1'), batch_size=[{batch_size}])
        ... env.set_seed(0)
        ... env.rollout(2)
        ... ''')
        ...     print(batch_size, timer.timeit(number=10))
        4
        env.rollout(100)
        setup: [...]
        Median: 122.40 ms
        2 measurements, 1 runs per measurement, 1 thread

        16
        env.rollout(100)
        setup: [...]
        Median: 134.39 ms
        2 measurements, 1 runs per measurement, 1 thread

        128
        env.rollout(100)
        setup: [...]
        Median: 172.31 ms
        2 measurements, 1 runs per measurement, 1 thread

    """

    git_url = "https://github.com/instadeepai/jumanji"
    libname = "jumanji"

    @_classproperty
    def available_envs(cls):
        if not _has_jumanji:
            return []
        return list(_get_envs())

    @property
    def lib(self):
        import jumanji

        return jumanji

    def __init__(self, env: "jumanji.env.Environment" = None, **kwargs):  # noqa: F821
        if not _has_jumanji:
            raise ImportError(
                "jumanji is not installed or importing it failed. Consider checking your installation."
            )
        if env is not None:
            kwargs["env"] = env
        super().__init__(**kwargs)

    def _build_env(
        self,
        env,
        _seed: Optional[int] = None,
        from_pixels: bool = False,
        render_kwargs: Optional[dict] = None,
        pixels_only: bool = False,
        camera_id: Union[int, str] = 0,
        **kwargs,
    ):
        self.from_pixels = from_pixels
        self.pixels_only = pixels_only

        if from_pixels:
            raise NotImplementedError("TODO")
        return env

    def _make_state_example(self, env):
        import jax
        from jax import numpy as jnp

        key = jax.random.PRNGKey(0)
        keys = jax.random.split(key, self.batch_size.numel())
        state, _ = jax.vmap(env.reset)(jnp.stack(keys))
        state = _tree_reshape(state, self.batch_size)
        return state

    def _make_state_spec(self, env) -> TensorSpec:
        import jax

        key = jax.random.PRNGKey(0)
        state, _ = env.reset(key)
        state_dict = _object_to_tensordict(state, self.device, batch_size=())
        state_spec = _extract_spec(state_dict)
        return state_spec

    def _make_action_spec(self, env) -> TensorSpec:
        action_spec = _jumanji_to_torchrl_spec_transform(
            env.action_spec(), device=self.device
        )
        action_spec = action_spec.expand(*self.batch_size, *action_spec.shape)
        return action_spec

    def _make_observation_spec(self, env) -> TensorSpec:
        jumanji = self.lib

        spec = env.observation_spec()
        new_spec = _jumanji_to_torchrl_spec_transform(spec, device=self.device)
        if isinstance(spec, jumanji.specs.Array):
            return CompositeSpec(observation=new_spec).expand(self.batch_size)
        elif isinstance(spec, jumanji.specs.Spec):
            return CompositeSpec(**{k: v for k, v in new_spec.items()}).expand(
                self.batch_size
            )
        else:
            raise TypeError(f"Unsupported spec type {type(spec)}")

    def _make_reward_spec(self, env) -> TensorSpec:
        reward_spec = _jumanji_to_torchrl_spec_transform(
            env.reward_spec(), device=self.device
        )
        if not len(reward_spec.shape):
            reward_spec.shape = torch.Size([1])
        return reward_spec.expand([*self.batch_size, *reward_spec.shape])

    def _make_specs(self, env: "jumanji.env.Environment") -> None:  # noqa: F821

        # extract spec from jumanji definition
        self.action_spec = self._make_action_spec(env)
        self.observation_spec = self._make_observation_spec(env)
        self.reward_spec = self._make_reward_spec(env)

        # extract state spec from instance
        state_spec = self._make_state_spec(env).expand(self.batch_size)
        self.state_spec["state"] = state_spec
        self.observation_spec["state"] = state_spec.clone()

        # build state example for data conversion
        self._state_example = self._make_state_example(env)

    def _check_kwargs(self, kwargs: Dict):
        jumanji = self.lib
        if "env" not in kwargs:
            raise TypeError("Could not find environment key 'env' in kwargs.")
        env = kwargs["env"]
        if not isinstance(env, (jumanji.env.Environment,)):
            raise TypeError("env is not of type 'jumanji.env.Environment'.")

    def _init_env(self):
        pass

    @property
    def key(self):
        key = getattr(self, "_key", None)
        if key is None:
            raise RuntimeError(
                "the env.key attribute wasn't found. Make sure to call `env.set_seed(seed)` before any interaction."
            )
        return key

    @key.setter
    def key(self, value):
        self._key = value

    def _set_seed(self, seed):
        import jax

        if seed is None:
            raise Exception("Jumanji requires an integer seed.")
        self.key = jax.random.PRNGKey(seed)

    def read_state(self, state):
        state_dict = _object_to_tensordict(state, self.device, self.batch_size)
        return self.state_spec["state"].encode(state_dict)

    def read_obs(self, obs):
        from jax import numpy as jnp

        if isinstance(obs, (list, jnp.ndarray, np.ndarray)):
            obs_dict = _ndarray_to_tensor(obs).to(self.device)
        else:
            obs_dict = _object_to_tensordict(obs, self.device, self.batch_size)
        return super().read_obs(obs_dict)

    def _step(self, tensordict: TensorDictBase) -> TensorDictBase:
        import jax

        # prepare inputs
        state = _tensordict_to_object(tensordict.get("state"), self._state_example)
        action = self.read_action(tensordict.get("action"))

        # flatten batch size into vector
        state = _tree_flatten(state, self.batch_size)
        action = _tree_flatten(action, self.batch_size)

        # jax vectorizing map on env.step
        state, timestep = jax.vmap(self._env.step)(state, action)

        # reshape batch size from vector
        state = _tree_reshape(state, self.batch_size)
        timestep = _tree_reshape(timestep, self.batch_size)

        # collect outputs
        state_dict = self.read_state(state)
        obs_dict = self.read_obs(timestep.observation)
        reward = self.read_reward(np.asarray(timestep.reward))
        done = timestep.step_type == self.lib.types.StepType.LAST
        done = _ndarray_to_tensor(done).view(torch.bool).to(self.device)

        # build results
        tensordict_out = TensorDict(
            source=obs_dict,
            batch_size=tensordict.batch_size,
            device=self.device,
        )
        tensordict_out.set("reward", reward)
        tensordict_out.set("done", done)
        tensordict_out.set("terminated", done)
        # tensordict_out.set("terminated", done)
        tensordict_out["state"] = state_dict

        return tensordict_out

    def _reset(
        self, tensordict: Optional[TensorDictBase] = None, **kwargs
    ) -> TensorDictBase:
        import jax
        from jax import numpy as jnp

        # generate random keys
        self.key, *keys = jax.random.split(self.key, self.numel() + 1)

        # jax vectorizing map on env.reset
        state, timestep = jax.vmap(self._env.reset)(jnp.stack(keys))

        # reshape batch size from vector
        state = _tree_reshape(state, self.batch_size)
        timestep = _tree_reshape(timestep, self.batch_size)

        # collect outputs
        state_dict = self.read_state(state)
        obs_dict = self.read_obs(timestep.observation)
        done_td = self.full_done_spec.zero()

        # build results
        tensordict_out = TensorDict(
            source=obs_dict,
            batch_size=self.batch_size,
            device=self.device,
        )
        tensordict_out.update(done_td)
        tensordict_out["state"] = state_dict

        return tensordict_out

    def _output_transform(self, step_outputs_tuple: Tuple) -> Tuple:
        ...

    def _reset_output_transform(self, reset_outputs_tuple: Tuple) -> Tuple:
        ...


class JumanjiEnv(JumanjiWrapper):
    """Jumanji environment wrapper built with the environment name.

    Jumanji offers a vectorized simulation framework based on Jax.
    TorchRL's wrapper incurs some overhead for the jax-to-torch conversion,
    but computational graphs can still be built on top of the simulated trajectories,
    allowing for backpropagation through the rollout.

    GitHub: https://github.com/instadeepai/jumanji

    Doc: https://instadeepai.github.io/jumanji/

    Paper: https://arxiv.org/abs/2306.09884

    Args:
        env_name (str): the name of the environment to wrap. Must be part of :attr:`~.available_envs`.
        categorical_action_encoding (bool, optional): if ``True``, categorical
            specs will be converted to the TorchRL equivalent (:class:`torchrl.data.DiscreteTensorSpec`),
            otherwise a one-hot encoding will be used (:class:`torchrl.data.OneHotTensorSpec`).
            Defaults to ``False``.

    Keyword Args:
        from_pixels (bool, optional): Not yet supported.
        frame_skip (int, optional): if provided, indicates for how many steps the
            same action is to be repeated. The observation returned will be the
            last observation of the sequence, whereas the reward will be the sum
            of rewards across steps.
        device (torch.device, optional): if provided, the device on which the data
            is to be cast. Defaults to ``torch.device("cpu")``.
        batch_size (torch.Size, optional): the batch size of the environment.
            With ``jumanji``, this indicates the number of vectorized environments.
            Defaults to ``torch.Size([])``.
        allow_done_after_reset (bool, optional): if ``True``, it is tolerated
            for envs to be ``done`` just after :meth:`~.reset` is called.
            Defaults to ``False``.

    Attributes:
        available_envs: environments availalbe to build

    Examples:
        >>> from torchrl.envs import JumanjiEnv
        >>> env = JumanjiEnv("Snake-v1")
        >>> env.set_seed(0)
        >>> td = env.reset()
        >>> td["action"] = env.action_spec.rand()
        >>> td = env.step(td)
        >>> print(td)
        TensorDict(
            fields={
                action: Tensor(shape=torch.Size([]), device=cpu, dtype=torch.int32, is_shared=False),
                action_mask: Tensor(shape=torch.Size([4]), device=cpu, dtype=torch.bool, is_shared=False),
                done: Tensor(shape=torch.Size([1]), device=cpu, dtype=torch.bool, is_shared=False),
                grid: Tensor(shape=torch.Size([12, 12, 5]), device=cpu, dtype=torch.float32, is_shared=False),
                next: TensorDict(
                    fields={
                        action_mask: Tensor(shape=torch.Size([4]), device=cpu, dtype=torch.bool, is_shared=False),
                        done: Tensor(shape=torch.Size([1]), device=cpu, dtype=torch.bool, is_shared=False),
                        grid: Tensor(shape=torch.Size([12, 12, 5]), device=cpu, dtype=torch.float32, is_shared=False),
                        reward: Tensor(shape=torch.Size([1]), device=cpu, dtype=torch.float32, is_shared=False),
                        state: TensorDict(
                            fields={
                                action_mask: Tensor(shape=torch.Size([4]), device=cpu, dtype=torch.bool, is_shared=False),
                                body: Tensor(shape=torch.Size([12, 12]), device=cpu, dtype=torch.bool, is_shared=False),
                                body_state: Tensor(shape=torch.Size([12, 12]), device=cpu, dtype=torch.int32, is_shared=False),
                                fruit_position: TensorDict(
                                    fields={
                                        col: Tensor(shape=torch.Size([]), device=cpu, dtype=torch.int32, is_shared=False),
                                        row: Tensor(shape=torch.Size([]), device=cpu, dtype=torch.int32, is_shared=False)},
                                    batch_size=torch.Size([]),
                                    device=cpu,
                                    is_shared=False),
                                head_position: TensorDict(
                                    fields={
                                        col: Tensor(shape=torch.Size([]), device=cpu, dtype=torch.int32, is_shared=False),
                                        row: Tensor(shape=torch.Size([]), device=cpu, dtype=torch.int32, is_shared=False)},
                                    batch_size=torch.Size([]),
                                    device=cpu,
                                    is_shared=False),
                                key: Tensor(shape=torch.Size([2]), device=cpu, dtype=torch.int32, is_shared=False),
                                length: Tensor(shape=torch.Size([]), device=cpu, dtype=torch.int32, is_shared=False),
                                step_count: Tensor(shape=torch.Size([]), device=cpu, dtype=torch.int32, is_shared=False),
                                tail: Tensor(shape=torch.Size([12, 12]), device=cpu, dtype=torch.bool, is_shared=False)},
                            batch_size=torch.Size([]),
                            device=cpu,
                            is_shared=False),
                        step_count: Tensor(shape=torch.Size([]), device=cpu, dtype=torch.int32, is_shared=False),
                        terminated: Tensor(shape=torch.Size([1]), device=cpu, dtype=torch.bool, is_shared=False)},
                    batch_size=torch.Size([]),
                    device=cpu,
                    is_shared=False),
                state: TensorDict(
                    fields={
                        action_mask: Tensor(shape=torch.Size([4]), device=cpu, dtype=torch.bool, is_shared=False),
                        body: Tensor(shape=torch.Size([12, 12]), device=cpu, dtype=torch.bool, is_shared=False),
                        body_state: Tensor(shape=torch.Size([12, 12]), device=cpu, dtype=torch.int32, is_shared=False),
                        fruit_position: TensorDict(
                            fields={
                                col: Tensor(shape=torch.Size([]), device=cpu, dtype=torch.int32, is_shared=False),
                                row: Tensor(shape=torch.Size([]), device=cpu, dtype=torch.int32, is_shared=False)},
                            batch_size=torch.Size([]),
                            device=cpu,
                            is_shared=False),
                        head_position: TensorDict(
                            fields={
                                col: Tensor(shape=torch.Size([]), device=cpu, dtype=torch.int32, is_shared=False),
                                row: Tensor(shape=torch.Size([]), device=cpu, dtype=torch.int32, is_shared=False)},
                            batch_size=torch.Size([]),
                            device=cpu,
                            is_shared=False),
                        key: Tensor(shape=torch.Size([2]), device=cpu, dtype=torch.int32, is_shared=False),
                        length: Tensor(shape=torch.Size([]), device=cpu, dtype=torch.int32, is_shared=False),
                        step_count: Tensor(shape=torch.Size([]), device=cpu, dtype=torch.int32, is_shared=False),
                        tail: Tensor(shape=torch.Size([12, 12]), device=cpu, dtype=torch.bool, is_shared=False)},
                    batch_size=torch.Size([]),
                    device=cpu,
                    is_shared=False),
                step_count: Tensor(shape=torch.Size([]), device=cpu, dtype=torch.int32, is_shared=False),
                terminated: Tensor(shape=torch.Size([1]), device=cpu, dtype=torch.bool, is_shared=False)},
            batch_size=torch.Size([]),
            device=cpu,
            is_shared=False)
        >>> print(env.available_envs)
        ['Game2048-v1',
         'Maze-v0',
         'Cleaner-v0',
         'CVRP-v1',
         'MultiCVRP-v0',
         'Minesweeper-v0',
         'RubiksCube-v0',
         'Knapsack-v1',
         'Sudoku-v0',
         'Snake-v1',
         'TSP-v1',
         'Connector-v2',
         'MMST-v0',
         'GraphColoring-v0',
         'RubiksCube-partly-scrambled-v0',
         'RobotWarehouse-v0',
         'Tetris-v0',
         'BinPack-v2',
         'Sudoku-very-easy-v0',
         'JobShop-v0']

    To take advante of Jumanji, one usually executes multiple environments at the
    same time.

        >>> from torchrl.envs import JumanjiEnv
        >>> env = JumanjiEnv("Snake-v1", batch_size=[10])
        >>> env.set_seed(0)
        >>> td = env.reset()
        >>> td["action"] = env.action_spec.rand()
        >>> td = env.step(td)

    In the following example, we iteratively test different batch sizes
    and report the execution time for a short rollout:

    Examples:
        >>> from torch.utils.benchmark import Timer
        >>> for batch_size in [4, 16, 128]:
        ...     timer = Timer(
        ...     '''
        ... env.rollout(100)
        ... ''',
        ... setup=f'''
        ... from torchrl.envs import JumanjiEnv
        ... env = JumanjiEnv('Snake-v1', batch_size=[{batch_size}])
        ... env.set_seed(0)
        ... env.rollout(2)
        ... ''')
        ...     print(batch_size, timer.timeit(number=10))
        4 <torch.utils.benchmark.utils.common.Measurement object at 0x1fca91910>
        env.rollout(100)
        setup: [...]
          Median: 122.40 ms
          2 measurements, 1 runs per measurement, 1 thread
        16 <torch.utils.benchmark.utils.common.Measurement object at 0x1ff9baee0>
        env.rollout(100)
        setup: [...]
          Median: 134.39 ms
          2 measurements, 1 runs per measurement, 1 thread
        128 <torch.utils.benchmark.utils.common.Measurement object at 0x1ff9ba7c0>
        env.rollout(100)
        setup: [...]
          Median: 172.31 ms
          2 measurements, 1 runs per measurement, 1 thread
    """

    def __init__(self, env_name, **kwargs):
        kwargs["env_name"] = env_name
        super().__init__(**kwargs)

    def _build_env(
        self,
        env_name: str,
        **kwargs,
    ) -> "jumanji.env.Environment":  # noqa: F821
        if not _has_jumanji:
            raise ImportError(
                f"jumanji not found, unable to create {env_name}. "
                f"Consider installing jumanji. More info:"
                f" {self.git_url}."
            )
        from_pixels = kwargs.pop("from_pixels", False)
        pixels_only = kwargs.pop("pixels_only", True)
        if kwargs:
            raise ValueError(f"Extra kwargs are not supported by {type(self)}.")
        self.wrapper_frame_skip = 1
        env = self.lib.make(env_name, **kwargs)
        return super()._build_env(env, pixels_only=pixels_only, from_pixels=from_pixels)

    @property
    def env_name(self):
        return self._constructor_kwargs["env_name"]

    def _check_kwargs(self, kwargs: Dict):
        if "env_name" not in kwargs:
            raise TypeError("Expected 'env_name' to be part of kwargs")

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(env={self.env_name}, batch_size={self.batch_size}, device={self.device})"
