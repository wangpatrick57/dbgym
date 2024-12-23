import logging
from typing import Any, Optional, TypeVar, cast

import numpy as np
import torch

from tune.protox.env.artifact_manager import ArtifactManager
from tune.protox.env.types import ProtoAction
from util.log import DBGYM_LOGGER_NAME

T = TypeVar("T", torch.Tensor, np.typing.NDArray[np.float32])


class LSC(object):
    def __init__(
        self,
        horizon: int,
        lsc_parameters: dict[str, Any],
        vae_config: dict[str, Any],
        artifact_manager: Optional[ArtifactManager],
    ):
        self.frozen = False
        self.horizon = horizon
        self.num_steps = 0
        self.num_episodes = 0
        self.vae_configuration = vae_config
        self.enabled = lsc_parameters["enabled"]

        lsc_splits = lsc_parameters["initial"].split(",")
        lsc_increments = lsc_parameters["increment"].split(",")
        lsc_max = lsc_parameters["max"].split(",")
        if len(lsc_splits) == 1:
            lsc_splits = [float(lsc_splits[0])] * horizon
        else:
            assert len(lsc_splits) == horizon
            lsc_splits = [float(f) for f in lsc_splits]

        if len(lsc_increments) == 1:
            lsc_increments = [float(lsc_increments[0])] * horizon
        else:
            assert len(lsc_increments) == horizon
            lsc_increments = [float(f) for f in lsc_increments]

        if len(lsc_max) == 1:
            lsc_max = [float(lsc_max[0])] * horizon
        else:
            assert len(lsc_max) == horizon
            lsc_max = [float(f) for f in lsc_max]

        self.shift_eps_freq = lsc_parameters["shift_eps_freq"]
        self.lsc_shift = np.array(lsc_splits)
        self.increment = np.array(lsc_increments)
        self.max = np.array(lsc_max)
        self.shift_after = lsc_parameters["shift_after"]
        self.artifact_manager = artifact_manager

        logging.getLogger(DBGYM_LOGGER_NAME).info("LSC Shift: %s", self.lsc_shift)
        logging.getLogger(DBGYM_LOGGER_NAME).info(
            "LSC Shift Increment: %s", self.increment
        )
        logging.getLogger(DBGYM_LOGGER_NAME).info("LSC Shift Max: %s", self.max)

    def apply_bias(self, action: ProtoAction) -> ProtoAction:
        if not self.enabled:
            return action

        assert (
            action.shape[-1] == self.vae_configuration["latent_dim"]
        ), f"{action.shape} {self.vae_configuration['latent_dim']}"

        # Get the LSC shift associated with the current episode.
        lsc_shift = self.lsc_shift[(self.num_steps % self.horizon)]
        lsc_shift = lsc_shift * self.vae_configuration["output_scale"]
        return ProtoAction(action + lsc_shift)

    def current_bias(self) -> float:
        if not self.enabled:
            return 0.0

        # Get the LSC shift associated with the current episode.
        lsc_shift = self.lsc_shift[(self.num_steps % self.horizon)]
        lsc_shift = lsc_shift * self.vae_configuration["output_scale"]
        return cast(float, lsc_shift)

    def current_scale(self) -> np.typing.NDArray[np.float32]:
        if not self.enabled:
            return np.array([-1.0], dtype=np.float32)

        lsc_shift = self.lsc_shift[(self.num_steps % self.horizon)]
        lsc_max = self.max[(self.num_steps % self.horizon)]
        rel = lsc_shift / lsc_max
        return np.array([(rel * 2.0) - 1], dtype=np.float32)

    def inverse_scale(self, value: torch.Tensor) -> torch.Tensor:
        if not self.enabled:
            return torch.zeros_like(value).float()

        lsc_max = self.max[0]
        lsc_shift = ((value + 1) / 2.0) * lsc_max
        return cast(torch.Tensor, lsc_shift * self.vae_configuration["output_scale"])

    def advance(self) -> None:
        if self.frozen or (not self.enabled):
            return

        self.num_steps += 1

    def freeze(self) -> None:
        self.frozen = True

    def unfreeze(self) -> None:
        self.frozen = False

    def reset(self) -> None:
        if self.frozen or (not self.enabled):
            return

        # Advance the episode count.
        self.num_episodes += 1
        if (self.num_episodes <= self.shift_after) or (
            (self.num_episodes - self.shift_after) % self.shift_eps_freq != 0
        ):
            # Reset the number of steps we've taken.
            self.num_steps = 0
        else:
            # Get how many steps to make the update on.
            bound = self.horizon
            self.num_steps = 0

            # Now try to perform the LSC shifts.
            # Increment the current bias with the increment.
            self.lsc_shift[:bound] += self.increment[:bound]
            self.lsc_shift = self.lsc_shift % self.max
            logging.getLogger(DBGYM_LOGGER_NAME).info(
                "LSC Bias Update: %s", self.lsc_shift
            )
