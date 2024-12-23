import logging
import random
from typing import Any, Optional, Tuple, cast

import gymnasium as gym

from tune.protox.env.artifact_manager import ArtifactManager
from tune.protox.env.pg_env import PostgresEnv
from tune.protox.env.types import EnvInfoDict, HolonStateContainer, TargetResetConfig
from tune.protox.env.util.reward import RewardUtility
from util.log import DBGYM_LOGGER_NAME


class TargetResetWrapper(gym.core.Wrapper[Any, Any, Any, Any]):
    def __init__(
        self,
        env: gym.Env[Any, Any],
        maximize_state: bool,
        reward_utility: RewardUtility,
        start_reset: bool,
        artifact_manager: Optional[ArtifactManager],
    ):
        super().__init__(env)
        self.maximize_state = maximize_state
        self.start_reset = start_reset
        self.reward_utility = reward_utility
        self.tracked_states: list[TargetResetConfig] = []
        self.best_metric = None
        self.real_best_metric = None
        self.artifact_manager = artifact_manager

    def _get_state(self) -> HolonStateContainer:
        # There is a state_container at the bottom.
        assert isinstance(self.unwrapped, PostgresEnv)
        sc = self.unwrapped.state_container
        assert sc
        return sc

    def step(  # type: ignore
        self, *args: Any, **kwargs: Any
    ) -> tuple[Any, float, bool, bool, EnvInfoDict]:
        """Steps through the environment, normalizing the rewards returned."""
        obs, rews, terms, truncs, infos = self.env.step(*args, **kwargs)
        query_metric_data = infos.get("query_metric_data", None)
        assert self.best_metric is not None
        did_anything_time_out = infos.get("did_anything_time_out", False)

        metric = infos["metric"]
        if self.reward_utility.is_perf_better(metric, self.best_metric):
            self.best_metric = infos["metric"]
            if not did_anything_time_out:
                self.real_best_metric = self.best_metric

            if self.maximize_state:
                logging.getLogger(DBGYM_LOGGER_NAME).info(
                    f"Found new maximal state with {metric}."
                )
                assert len(self.tracked_states) > 0
                state = self._get_state()
                if self.start_reset:
                    self.tracked_states = [
                        self.tracked_states[0],
                        TargetResetConfig(
                            {
                                "metric": metric,
                                "env_state": obs,
                                "config": state,
                                "query_metric_data": query_metric_data,
                            }
                        ),
                    ]
                else:
                    self.tracked_states = [
                        TargetResetConfig(
                            {
                                "metric": metric,
                                "env_state": obs,
                                "config": state,
                                "query_metric_data": query_metric_data,
                            }
                        ),
                    ]
        return obs, rews, terms, truncs, infos

    def reset(self, **kwargs: Any) -> tuple[Any, dict[str, Any]]:
        if len(self.tracked_states) == 0:
            # First time.
            state, info = self.env.reset(**kwargs)
            assert "baseline_metric" in info
            self.best_metric = info["baseline_metric"]
            self.real_best_metric = self.best_metric

            self.tracked_states = [
                TargetResetConfig(
                    {
                        "metric": self.best_metric,
                        "env_state": state.copy(),
                        "config": self._get_state(),
                        "query_metric_data": info.get("query_metric_data", None),
                    }
                )
            ]
        else:
            reset_config = random.choice(self.tracked_states)
            if kwargs is None or "options" not in kwargs or kwargs["options"] is None:
                kwargs = {}
                kwargs["options"] = {}
            kwargs["options"].update(reset_config)
            state, info = self.env.reset(**kwargs)
        return state, info
