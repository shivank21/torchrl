# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.
# import tree
from __future__ import annotations

import typing
from typing import Any, Callable, Union

import numpy as np
import torch
from torch import Tensor

INT_CLASSES_TYPING = Union[int, np.integer]
if hasattr(typing, "get_args"):
    INT_CLASSES = typing.get_args(INT_CLASSES_TYPING)
else:
    # python 3.7
    INT_CLASSES = (int, np.integer)


def _to_numpy(data: Tensor) -> np.ndarray:
    return data.detach().cpu().numpy() if isinstance(data, torch.Tensor) else data


def _to_torch(
    data: Tensor, device, pin_memory: bool = False, non_blocking: bool = False
) -> torch.Tensor:
    if isinstance(data, np.generic):
        return torch.tensor(data, device=device)
    elif isinstance(data, np.ndarray):
        data = torch.from_numpy(data)
    elif not isinstance(data, Tensor):
        data = torch.tensor(data, device=device)

    if pin_memory:
        data = data.pin_memory()
    if device is not None:
        data = data.to(device, non_blocking=non_blocking)

    return data


def pin_memory_output(fun) -> Callable:
    """Calls pin_memory on outputs of decorated function if they have such method."""

    def decorated_fun(self, *args, **kwargs):
        output = fun(self, *args, **kwargs)
        if self._pin_memory:
            _tuple_out = True
            if not isinstance(output, tuple):
                _tuple_out = False
                output = (output,)
            output = tuple(_pin_memory(_output) for _output in output)
            if _tuple_out:
                return output
            return output[0]
        return output

    return decorated_fun


def _pin_memory(output: Any) -> Any:
    if hasattr(output, "pin_memory") and output.device == torch.device("cpu"):
        return output.pin_memory()
    else:
        return output


def _reduce(
    tensor: torch.Tensor, reduction: str, dim: int | None = None
) -> Union[float, torch.Tensor]:
    """Reduces a tensor given the reduction method."""
    if reduction == "max":
        result = tensor.max(dim=dim)
    elif reduction == "min":
        result = tensor.min(dim=dim)
    elif reduction == "mean":
        result = tensor.mean(dim=dim)
    elif reduction == "median":
        result = tensor.median(dim=dim)
    elif reduction == "sum":
        result = tensor.sum(dim=dim)
    else:
        raise NotImplementedError(f"Unknown reduction method {reduction}")
    if isinstance(result, tuple):
        result = result[0]
    return result.item() if dim is None else result
