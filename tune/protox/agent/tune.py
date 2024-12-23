import json
import os
import shutil
import time
from pathlib import Path
from typing import Optional

import click
import pandas as pd

from benchmark.constants import DEFAULT_SCALE_FACTOR
from tune.protox.agent.coerce_config import coerce_config
from tune.protox.agent.hpo import TuneTrial, build_space
from util.workspace import (
    BENCHMARK_NAME_PLACEHOLDER,
    DEFAULT_BOOT_CONFIG_FPATH,
    WORKLOAD_NAME_PLACEHOLDER,
    WORKSPACE_PATH_PLACEHOLDER,
    DBGymConfig,
    TuningMode,
    fully_resolve_path,
    get_default_hpoed_agent_params_path,
    get_default_tuning_steps_dname,
    get_default_workload_name_suffix,
    get_workload_name,
    link_result,
    open_and_save,
)


# This is used when you already have a good set of HPOs and just want to tune the DBMS
@click.command()
@click.pass_obj
@click.argument("benchmark-name")
@click.option(
    "--workload-name-suffix",
    type=str,
    default=None,
    help=f"The suffix of the workload name (the part after the scale factor).",
)
@click.option(
    "--scale-factor",
    type=float,
    default=DEFAULT_SCALE_FACTOR,
    help=f"The scale factor used when generating the data of the benchmark.",
)
@click.option(
    "--hpoed-agent-params-path",
    default=None,
    type=Path,
    help=f"The path to best params found by the agent HPO process. The default is {get_default_hpoed_agent_params_path(WORKSPACE_PATH_PLACEHOLDER, BENCHMARK_NAME_PLACEHOLDER, WORKLOAD_NAME_PLACEHOLDER)}",
)
@click.option(
    "--enable-boot-during-tune",
    is_flag=True,
    help="Whether to enable the Boot query accelerator during the tuning process. Deciding to use Boot during tuning is separate from deciding to use Boot during HPO.",
)
@click.option(
    "--boot-config-fpath-during-tune",
    default=DEFAULT_BOOT_CONFIG_FPATH,
    type=Path,
    help="The path to the file configuring Boot when tuning. This may be a different Boot config than the one used for HPO.",
)
@click.option(
    "--tune-duration-during-tune",
    default=None,
    type=float,
    help="The number of hours to run the tuning agent for. If you do not specify this argument, it will be the same as --tune-duration-during-hpo.",
)
def tune(
    dbgym_cfg: DBGymConfig,
    benchmark_name: str,
    workload_name_suffix: Optional[str],
    scale_factor: float,
    hpoed_agent_params_path: Path,
    enable_boot_during_tune: bool,
    boot_config_fpath_during_tune: Path,
    tune_duration_during_tune: float,
) -> None:
    """IMPORTANT: The "tune" here is the one in "tune a DBMS". This is *different* from the "tune" in ray.tune.TuneConfig, which means to "tune hyperparameters"."""
    # Set args to defaults programmatically (do this before doing anything else in the function)
    if workload_name_suffix is None:
        workload_name_suffix = get_default_workload_name_suffix(benchmark_name)
    workload_name = get_workload_name(scale_factor, workload_name_suffix)
    if hpoed_agent_params_path is None:
        hpoed_agent_params_path = get_default_hpoed_agent_params_path(
            dbgym_cfg.dbgym_workspace_path, benchmark_name, workload_name
        )

    # Fully resolve all input paths.
    hpoed_agent_params_path = fully_resolve_path(dbgym_cfg, hpoed_agent_params_path)
    boot_config_fpath_during_tune = fully_resolve_path(
        dbgym_cfg, boot_config_fpath_during_tune
    )

    # Tune
    with open_and_save(dbgym_cfg, hpoed_agent_params_path, "r") as f:
        hpo_params = json.load(f)

    # Coerce using a dummy space.
    hpo_params = coerce_config(
        dbgym_cfg,
        build_space(
            sysknobs={},
            benchmark_config={},
            workload_path=Path(),
            embedder_path=[],
            pgconn_info={},
        ),
        hpo_params,
    )

    # Set defaults that depend on hpo_params
    if tune_duration_during_tune is None:
        tune_duration_during_tune = hpo_params["tune_duration"][str(TuningMode.HPO)]

    # Set the hpo_params that are allowed to differ between HPO, tuning, and replay.
    # In general, for configs that can differ between HPO, tuning, and replay I chose to name
    #   them "*tune*" and "*hpo*" to the end of them instead of naming them the same
    #   and overriding the config during tuning. It's just much less confusing if we
    #   make sure to never override any configs in hpo_params.
    # Note that while we currently do not persist the hpo_params used during *tuning* back to
    #   a file, this is entirely possible to do in the future if needed.
    hpo_params["enable_boot"][str(TuningMode.TUNE)] = enable_boot_during_tune
    hpo_params["boot_config_fpath"][
        str(TuningMode.TUNE)
    ] = boot_config_fpath_during_tune
    hpo_params["tune_duration"][str(TuningMode.TUNE)] = tune_duration_during_tune
    hpo_params["workload_timeout"][str(TuningMode.TUNE)] = hpo_params[
        "workload_timeout"
    ][str(TuningMode.HPO)]

    # Piggyback off the HPO magic.
    tune_trial = TuneTrial(dbgym_cfg, TuningMode.TUNE)
    tune_trial.setup(hpo_params)
    start = time.time()

    data = []
    step_data_fpath = dbgym_cfg.cur_task_runs_data_path(mkdir=True) / "step_data.csv"
    while (time.time() - start) < tune_duration_during_tune * 3600:
        data.append(tune_trial.step())

        # Continuously write the file out.
        pd.DataFrame(data).to_csv(step_data_fpath, index=False)

    tune_trial.cleanup()

    # Output the step data.
    pd.DataFrame(data).to_csv(step_data_fpath, index=False)

    # Link the tuning steps data (this directory allows you to replay the tuning run).
    tuning_steps_dpath = dbgym_cfg.cur_task_runs_artifacts_path("tuning_steps")
    # Replaying requires params.json, so we also copy it into the tuning_steps/ directory.
    # We copy hpoed_agent_params_path instead of moving it because hpoed_agent_params_path was generated in another task run
    # We copy instead of just symlinking so that tuning_steps/ is a fully self-contained directory.
    hpoed_agent_params_copy_fpath = tuning_steps_dpath / "params.json"
    shutil.copy(hpoed_agent_params_path, hpoed_agent_params_copy_fpath)
    tuning_steps_link_dname = get_default_tuning_steps_dname(
        benchmark_name, workload_name, enable_boot_during_tune
    )
    link_result(
        dbgym_cfg,
        tuning_steps_dpath,
        custom_result_name=tuning_steps_link_dname + ".link",
    )
    # We also create a link to hpoed_agent_params_path. This is useful when we are _manually_ looking through
    #   run_*/ and want to see which other run_*/ was responsible for creating params.json
    hpoed_agent_params_link_fpath = tuning_steps_dpath / "params.json.link"
    os.symlink(hpoed_agent_params_path, hpoed_agent_params_link_fpath)
