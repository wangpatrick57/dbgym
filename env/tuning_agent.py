import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, NewType, TypedDict

from util.workspace import DBGymConfig, is_fully_resolved

# PostgresConn doesn't use these types because PostgresConn is used internally by tuning agents.
# These types are only given as the outputs of tuning agents.
IndexesDelta = NewType("IndexesDelta", list[str])
SysKnobsDelta = NewType("SysKnobsDelta", dict[str, str])
QueryKnobsDelta = NewType("QueryKnobsDelta", dict[str, list[str]])


@dataclass
class TuningAgentMetadata:
    """Metadata for tuning agent"""

    workload_path: Path
    pristine_dbdata_snapshot_path: Path
    dbdata_parent_path: Path
    pgbin_path: Path

    def __post_init__(self) -> None:
        """
        Since the metadata needs to persist over time, we need to make sure that the paths are
        fully resolved.
        """
        assert is_fully_resolved(
            self.workload_path
        ), f"workload_path={self.workload_path}"
        assert is_fully_resolved(
            self.pristine_dbdata_snapshot_path
        ), f"pristine_dbdata_snapshot_path={self.pristine_dbdata_snapshot_path}"
        assert is_fully_resolved(
            self.dbdata_parent_path
        ), f"dbdata_parent_path={self.dbdata_parent_path}"
        assert is_fully_resolved(self.pgbin_path), f"pgbin_path={self.pgbin_path}"

    def asdict(self) -> dict[str, Any]:
        return {
            "workload_path": str(self.workload_path),
            "pristine_dbdata_snapshot_path": str(self.pristine_dbdata_snapshot_path),
            "dbdata_parent_path": str(self.dbdata_parent_path),
            "pgbin_path": str(self.pgbin_path),
        }


@dataclass
class DBMSConfigDelta:
    """
    This class represents a DBMS config delta. A "DBMS config" is the indexes, system knobs,
    and query knobs set by the tuning agent. A "delta" is the change from the prior config.

    `indexes` contains a list of SQL statements for creating indexes. Note that since it's a
    config delta, it might contain "DROP ..." statements.

    `sysknobs` contains a mapping from knob names to their values.

    `qknobs` contains a mapping from query IDs to a list of knobs. Each list contains knobs
    to prepend to the start of the query. The knobs are a list[str] instead of a dict[str, str]
    because knobs can be settings ("SET (enable_sort on)") or flags ("IndexOnlyScan(it)").
    """

    indexes: IndexesDelta
    sysknobs: SysKnobsDelta
    qknobs: QueryKnobsDelta


def get_step_delta_fpath(tuning_agent_artifacts_dpath: Path, step_num: int) -> Path:
    return tuning_agent_artifacts_dpath / f"step{step_num}_delta.json"


def get_metadata_fpath(tuning_agent_artifacts_dpath: Path) -> Path:
    return tuning_agent_artifacts_dpath / "metadata.json"


class TuningAgent:
    def __init__(self, dbgym_cfg: DBGymConfig) -> None:
        self.dbgym_cfg = dbgym_cfg
        self.tuning_agent_artifacts_dpath = self.dbgym_cfg.cur_task_runs_artifacts_path(
            "tuning_agent_artifacts", mkdir=True
        )
        assert is_fully_resolved(self.tuning_agent_artifacts_dpath)
        self.next_step_num = 0

        # Write metadata file
        metadata = self._get_metadata()
        with get_metadata_fpath(self.tuning_agent_artifacts_dpath).open("w") as f:
            json.dump(metadata.asdict(), f)

    def step(self) -> None:
        """
        This wraps _step() and saves the cfg to a file so that it can be replayed.
        """
        curr_step_num = self.next_step_num
        self.next_step_num += 1
        dbms_cfg_delta = self._step()
        with get_step_delta_fpath(
            self.tuning_agent_artifacts_dpath, curr_step_num
        ).open("w") as f:
            json.dump(asdict(dbms_cfg_delta), f)

    def _get_metadata(self) -> TuningAgentMetadata:
        """
        This should be overridden by subclasses.

        This should return the metadata of the tuning agent. This metadata is used for replay.
        """
        raise NotImplementedError

    def _step(self) -> DBMSConfigDelta:
        """
        This should be overridden by subclasses.

        This should return the delta in the config caused by this step.
        """
        raise NotImplementedError


class TuningAgentArtifactsReader:
    def __init__(self, tuning_agent_artifacts_dpath: Path) -> None:
        self.tuning_agent_artifacts_dpath = tuning_agent_artifacts_dpath
        assert is_fully_resolved(self.tuning_agent_artifacts_dpath)
        num_steps = 0
        while get_step_delta_fpath(
            self.tuning_agent_artifacts_dpath, num_steps
        ).exists():
            num_steps += 1
        self.num_steps = num_steps

    def get_metadata(self) -> TuningAgentMetadata:
        with get_metadata_fpath(self.tuning_agent_artifacts_dpath).open("r") as f:
            data = json.load(f)
            return TuningAgentMetadata(
                workload_path=Path(data["workload_path"]),
                pristine_dbdata_snapshot_path=Path(
                    data["pristine_dbdata_snapshot_path"]
                ),
                dbdata_parent_path=Path(data["dbdata_parent_path"]),
                pgbin_path=Path(data["pgbin_path"]),
            )

    def get_step_delta(self, step_num: int) -> DBMSConfigDelta:
        assert step_num >= 0 and step_num < self.num_steps
        with get_step_delta_fpath(self.tuning_agent_artifacts_dpath, step_num).open(
            "r"
        ) as f:
            data = json.load(f)
            return DBMSConfigDelta(
                indexes=data["indexes"],
                sysknobs=data["sysknobs"],
                qknobs=data["qknobs"],
            )

    def get_all_deltas(self) -> list[DBMSConfigDelta]:
        return [self.get_step_delta(step_num) for step_num in range(self.num_steps)]
