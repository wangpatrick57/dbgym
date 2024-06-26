import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
import click
import yaml
import redis

from util.shell import subprocess_run

# Relative paths of different folders in the codebase
DBMS_PATH = Path("dbms")
POSTGRES_PATH = DBMS_PATH / "postgres"
TUNE_PATH = Path("tune")
PROTOX_PATH = TUNE_PATH / "protox"
PROTOX_EMBEDDING_PATH = PROTOX_PATH / "embedding"
PROTOX_AGENT_PATH = PROTOX_PATH / "agent"
PROTOX_WOLP_PATH = PROTOX_AGENT_PATH / "wolp"

# Paths of different parts of the workspace
# I made these Path objects even though they're not real paths just so they can work correctly with my other helper functions
WORKSPACE_PATH_PLACEHOLDER = Path("[workspace]")


# Helper functions that both this file and other files use
def get_symlinks_path_from_workspace_path(workspace_path):
    return workspace_path / "symlinks"
def get_tmp_path_from_workspace_path(workspace_path):
    return workspace_path / "tmp"

def get_scale_factor_string(scale_factor: float | str) -> str:
    assert type(scale_factor) is float or type(scale_factor) is str
    if scale_factor == SCALE_FACTOR_PLACEHOLDER:
        return scale_factor
    else:
        if float(int(scale_factor)) == scale_factor:
            return str(int(scale_factor))
        else:
            return str(scale_factor).replace(".", "point")
    
def get_pgdata_tgz_name(benchmark_name: str, scale_factor: float) -> str:
    return f"{benchmark_name}_sf{get_scale_factor_string(scale_factor)}_pristine_pgdata.tgz"


# Other parameters
BENCHMARK_NAME_PLACEHOLDER = "[benchmark_name]"
WORKLOAD_NAME_PLACEHOLDER = "[workload_name]"
SCALE_FACTOR_PLACEHOLDER = "[scale_factor]"

# Paths of config files in the codebase. These are always relative paths.
# The reason these can be relative paths instead of functions taking in codebase_path as input is because relative paths are relative to the codebase root
DEFAULT_HPO_SPACE_PATH = PROTOX_EMBEDDING_PATH / "default_hpo_space.json"
DEFAULT_SYSKNOBS_PATH = PROTOX_AGENT_PATH / "default_sysknobs.yaml"
DEFAULT_BOOT_CONFIG_FPATH = POSTGRES_PATH / "default_boot_config.yaml"
default_benchmark_config_path = (
    lambda benchmark_name: PROTOX_PATH
    / f"default_{benchmark_name}_benchmark_config.yaml"
)
default_benchbase_config_path = (
    lambda benchmark_name: PROTOX_PATH
    / f"default_{benchmark_name}_benchbase_config.xml"
)

# Paths of dependencies in the workspace. These are named "*_path" because they will be an absolute path
# The reason these _cannot_ be relative paths is because relative paths are relative to the codebase root, not the workspace root
# Note that it's okay to hardcode the codebase paths (like dbgym_dbms_postgres) here. In the worst case, we'll just break an
#   integration test. The "source of truth" of codebase paths is based on DBGymConfig.cur_source_path(), which will always
#   reflect the actual codebase structure. As long as we automatically enforce getting the right codebase paths when writing, it's
#   ok to have to hardcode them when reading.
# Details
#  - If a name already has the workload_name, I omit scale factor. This is because the workload_name includes the scale factor
traindata_fname = (
    lambda benchmark_name, workload_name: f"{benchmark_name}_{workload_name}_embedding_traindata.parquet"
)
default_traindata_path = (
    lambda workspace_path, benchmark_name, workload_name: get_symlinks_path_from_workspace_path(
        workspace_path
    )
    / "dbgym_tune_protox_embedding"
    / "data"
    / traindata_fname(benchmark_name, workload_name)
)
default_embedder_dname = (
    lambda benchmark_name, workload_name: f"{benchmark_name}_{workload_name}_embedder"
)
default_embedder_path = (
    lambda workspace_path, benchmark_name, workload_name: get_symlinks_path_from_workspace_path(
        workspace_path
    )
    / "dbgym_tune_protox_embedding"
    / "data"
    / default_embedder_dname(benchmark_name, workload_name)
)
default_hpoed_agent_params_path = (
    lambda workspace_path, benchmark_name, workload_name: get_symlinks_path_from_workspace_path(workspace_path)
    / "dbgym_tune_protox_agent"
    / "data"
    / f"{benchmark_name}_{workload_name}_hpoed_agent_params.json"
)
workload_name_fn = (
    lambda scale_factor, seed_start, seed_end, query_subset : f"workload_sf{get_scale_factor_string(scale_factor)}_{seed_start}_{seed_end}_{query_subset}"
)
default_workload_path = (
    lambda workspace_path, benchmark_name, workload_name: get_symlinks_path_from_workspace_path(
        workspace_path
    )
    / f"dbgym_benchmark_{benchmark_name}"
    / "data"
    / workload_name
)
default_pristine_pgdata_snapshot_path = (
    lambda workspace_path, benchmark_name, scale_factor: get_symlinks_path_from_workspace_path(
        workspace_path
    )
    / f"dbgym_dbms_postgres"
    / "data"
    / get_pgdata_tgz_name(benchmark_name, scale_factor)
)
default_pgdata_parent_dpath = (
    lambda workspace_path: get_tmp_path_from_workspace_path(
        workspace_path
    )
)
default_pgbin_path = (
    lambda workspace_path: get_symlinks_path_from_workspace_path(
        workspace_path
    )
    / f"dbgym_dbms_postgres" / "build" / "repo" / "boot"/ "build" / "postgres" / "bin"
)


class DBGymConfig:
    """
    Global configurations that apply to all parts of DB-Gym
    """

    def __init__(self, config_path, startup_check=False):
        """
        Parameters
        ----------
        config_path : Path
        startup_check : bool
            True if startup_check shoul
        """
        assert is_base_git_dir(
            os.getcwd()
        ), "This script should be invoked from the root of the dbgym repo."

        # Parse the YAML file.
        contents: str = Path(config_path).read_text()
        yaml_config: dict = yaml.safe_load(contents)

        # Require dbgym_workspace_path to be absolute.
        # All future paths should be constructed from dbgym_workspace_path.
        dbgym_workspace_path = (
            Path(yaml_config["dbgym_workspace_path"]).resolve().absolute()
        )

        # Quickly display options.
        if startup_check:
            msg = (
                "💩💩💩 CMU-DB Database Gym: github.com/cmu-db/dbgym 💩💩💩\n"
                f"\tdbgym_workspace_path: {dbgym_workspace_path}\n"
                "\n"
                "Proceed?"
            )
            if not click.confirm(msg):
                print("Goodbye.")
                sys.exit(0)

        self.path: Path = config_path
        self.cur_path_list: list[str] = ["dbgym"]
        self.root_yaml: dict = yaml_config
        self.cur_yaml: dict = self.root_yaml

        # Set and create paths.
        self.dbgym_repo_path = Path(os.getcwd())
        self.dbgym_workspace_path = dbgym_workspace_path
        self.dbgym_workspace_path.mkdir(parents=True, exist_ok=True)
        self.dbgym_runs_path = self.dbgym_workspace_path / "task_runs"
        self.dbgym_runs_path.mkdir(parents=True, exist_ok=True)
        self.dbgym_symlinks_path = get_symlinks_path_from_workspace_path(
            self.dbgym_workspace_path
        )
        self.dbgym_symlinks_path.mkdir(parents=True, exist_ok=True)
        # tmp is a workspace for this run only
        # one use for it is to place the unzipped pgdata
        # there's no need to save the actual pgdata dir in run_*/ because we just save a symlink to
        #   the .tgz file we unzipped
        self.dbgym_tmp_path = get_tmp_path_from_workspace_path(self.dbgym_workspace_path)
        if self.dbgym_tmp_path.exists():
            shutil.rmtree(self.dbgym_tmp_path)
        self.dbgym_tmp_path.mkdir(parents=True, exist_ok=True)

        # Set the path for this task run's results.
        self.dbgym_this_run_path = (
            self.dbgym_runs_path / f"run_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}"
        )
        # exist_ok is False because we don't want to override a previous task run's data.
        self.dbgym_this_run_path.mkdir(parents=True, exist_ok=False)

    def __del__(self):
        if self.dbgym_tmp_path.exists():
            shutil.rmtree(self.dbgym_tmp_path)

    # append_group() is used to mark the "codebase path" of an invocation of the CLI. The "codebase path" is
    #   explained further in the documentation.
    def append_group(self, name) -> None:
        self.cur_path_list.append(name)
        self.cur_yaml = self.cur_yaml.get(name, {})

    def cur_source_path(self, *dirs) -> Path:
        cur_path = self.dbgym_repo_path
        assert self.cur_path_list[0] == "dbgym"
        for folder in self.cur_path_list[1:]:
            cur_path = cur_path / folder
        for dir in dirs:
            cur_path = cur_path / dir
        return cur_path

    def cur_symlinks_path(self, *dirs, mkdir=False) -> Path:
        flattened_structure = "_".join(self.cur_path_list)
        cur_path = self.dbgym_symlinks_path / flattened_structure
        for dir in dirs:
            cur_path = cur_path / dir
        if mkdir:
            cur_path.mkdir(parents=True, exist_ok=True)
        return cur_path

    def cur_task_runs_path(self, *dirs, mkdir=False) -> Path:
        flattened_structure = "_".join(self.cur_path_list)
        cur_path = self.dbgym_this_run_path / flattened_structure
        for dir in dirs:
            cur_path = cur_path / dir
        if mkdir:
            cur_path.mkdir(parents=True, exist_ok=True)
        return cur_path

    def cur_symlinks_bin_path(self, *dirs, mkdir=False) -> Path:
        return self.cur_symlinks_path("bin", *dirs, mkdir=mkdir)

    def cur_symlinks_build_path(self, *dirs, mkdir=False) -> Path:
        return self.cur_symlinks_path("build", *dirs, mkdir=mkdir)

    def cur_symlinks_data_path(self, *dirs, mkdir=False) -> Path:
        return self.cur_symlinks_path("data", *dirs, mkdir=mkdir)

    def cur_task_runs_build_path(self, *dirs, mkdir=False) -> Path:
        return self.cur_task_runs_path("build", *dirs, mkdir=mkdir)

    def cur_task_runs_data_path(self, *dirs, mkdir=False) -> Path:
        return self.cur_task_runs_path("data", *dirs, mkdir=mkdir)

    def cur_task_runs_artifacts_path(self, *dirs, mkdir=False) -> Path:
        return self.cur_task_runs_path("artifacts", *dirs, mkdir=mkdir)


def conv_inputpath_to_realabspath(dbgym_cfg: DBGymConfig, inputpath: os.PathLike) -> Path:
    """
    Convert any user inputted path to a real, absolute path
    For flexibility, we take in any os.PathLike. However, for consistency, we always output a Path object
    Whenever a path is required, the user is allowed to enter relative paths, absolute paths, or paths starting with ~
    Relative paths are relative to the base dbgym repo dir
    It *does not* check whether the path exists, since the user might be wanting to create a new file/dir
    Raises RuntimeError for errors
    """
    # for simplicity we only process Path objects
    realabspath = Path(inputpath)
    # expanduser() is always "ok" to call first
    realabspath = realabspath.expanduser()
    # the reason we don't call Path.absolute() is because the path should be relative to dbgym_cfg.dbgym_repo_path,
    #   which is not necessary where cwd() points at the time of calling this function
    if not realabspath.is_absolute():
        realabspath = dbgym_cfg.dbgym_repo_path / realabspath
    # resolve has two uses: normalize the path (remove ..) and resolve symlinks
    # I believe the pathlib library (https://docs.python.org/3/library/pathlib.html#pathlib.Path.resolve) does it this
    #   way to avoid an edge case related to symlinks and normalizing paths (footnote 1 of the linked docs)
    realabspath = realabspath.resolve()
    assert realabspath.is_absolute(), f"after being processed, realabspath ({realabspath}) is still not absolute"
    assert realabspath.exists(), f"after being processed, realabspath ({realabspath}) is still a non-existent path"
    return realabspath


def is_base_git_dir(cwd) -> bool:
    """
    Returns whether we are in the base directory of some git repository
    """
    try:
        git_toplevel = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"], encoding="utf-8"
        ).strip()
        return git_toplevel == cwd
    except subprocess.CalledProcessError as e:
        # this means we are not in _any_ git repo
        return False


def parent_dir(dpath: os.PathLike) -> os.PathLike:
    """
    Return a path of the parent directory of a directory path
    Note that os.path.dirname() does not always return the parent directory (it only does when the path doesn't end with a '/')
    """
    assert os.path.isdir(dpath) and os.path.isabs(dpath)
    return os.path.abspath(os.path.join(dpath, os.pardir))


def dir_basename(dpath: os.PathLike) -> str:
    """
    Return the directory name of a directory path
    Note that os.path.basename() does not always return the directory name (it only does when the path doesn't end with a '/')
    """
    assert os.path.isdir(dpath) and os.path.isabs(dpath)
    dpath_dirname, dpath_basename = os.path.split(dpath)
    # this means the path ended with a '/' so all os.path.split() does is get rid of the slash
    if dpath_basename == "":
        return os.path.basename(dpath_dirname)
    else:
        return dpath_basename


def is_child_path(child_path: os.PathLike, parent_dpath: os.PathLike) -> bool:
    """
    Checks whether child_path refers to a file/dir/link that is a child of the dir referred to by parent_dpath
    """
    assert os.path.isdir(parent_dpath)
    return os.path.samefile(
        os.path.commonpath([parent_dpath, child_path]), parent_dpath
    )


def open_and_save(dbgym_cfg: DBGymConfig, open_fpath: os.PathLike, mode="r"):
    """
    Open a file and "save" it to [workspace]/task_runs/run_*/.
    It takes in a str | Path to match the interface of open().
    This file does not work if open_fpath is a symlink, to make its interface identical to that of open().
        Make sure to resolve all symlinks with conv_inputpath_to_realabspath().
    See the comment of save_file() for what "saving" means
    If you are generating a "result" for the run, _do not_ use this. Just use the normal open().
        This shouldn't be too hard to remember because this function crashes if open_fpath doesn't exist,
        and when you write results you're usually opening open_fpaths which do not exist.

    **Notable Behavior**
     - If you open the same "config" file twice in the same run, it'll only be saved the first time (even if the file has changed in between).
        - "Dependency" files should be immutable so there's no problem here.
     - If you open two "config" files of the same name but different paths, only the first open will be saved.
        - Opening two "dependency" files of the same name but different paths will lead to two different "base dirs" being symlinked.
    """
    # process/validate open_fpath
    assert os.path.isabs(
        open_fpath
    ), f"open_and_save(): open_fpath ({open_fpath}) should be an absolute path"
    assert not os.path.islink(open_fpath), f"open_fpath ({open_fpath}) should not be a symlink"
    assert os.path.exists(open_fpath), f"open_fpath ({open_fpath}) does not exist"
    # open_and_save *must* be called on files because it doesn't make sense to open a directory. note that this doesn't mean we'll always save
    #   a file though. we sometimes save a directory (see save_file() for details)
    assert os.path.isfile(open_fpath), f"open_fpath ({open_fpath}) is not a file"

    # save
    save_file(dbgym_cfg, open_fpath)

    # open
    return open(open_fpath, mode=mode)


# TODO(phw2): after merging agent-train, refactor some code in agent-train to use save_file() instead of open_and_save()
def save_file(dbgym_cfg: DBGymConfig, fpath: os.PathLike) -> Path:
    """
    If an external function takes in a file/directory as input, you will not be able to call open_and_save().
        In these situations, just call save_file().
    "Saving" can mean either copying the file or creating a symlink to it
    We copy the file if it is a "config", meaning it just exists without having been generated
    We create a symlink if it is a "dependency", meaning a task.py command was run to generate it
        In these cases we create a symlink so we have full provenance for how the dependency was created
    """
    # process fpath and ensure that it's a file at the end
    fpath = conv_inputpath_to_realabspath(dbgym_cfg, fpath)
    fpath = os.path.realpath(fpath)  # traverse symlinks
    assert not os.path.islink(fpath), f"fpath ({fpath}) should not be a symlink"
    assert os.path.exists(fpath), f"fpath ({fpath}) does not exist"
    assert os.path.isfile(fpath), f"fpath ({fpath}) is not a file"
    assert not is_child_path(
        fpath, dbgym_cfg.dbgym_this_run_path
    ), f"fpath ({fpath}) was generated in this task run ({dbgym_cfg.dbgym_this_run_path}). You do not need to save it"

    # save _something_ to dbgym_this_run_path
    # save a symlink if the opened file was generated by a run. this is for two reasons:
    #   1. files or dirs generated by a run are supposed to be immutable so saving a symlink is safe
    #   2. files or dirs generated by a run may be very large (up to 100s of GBs) so we don't want to copy them
    if is_child_path(fpath, dbgym_cfg.dbgym_runs_path):
        # get paths we'll need later.
        parent_dpath = os.path.dirname(fpath)
        assert not os.path.samefile(
            parent_dpath, dbgym_cfg.dbgym_runs_path
        ), f"fpath ({fpath}) should be inside a run_*/ dir instead of directly in dbgym_cfg.dbgym_runs_path ({dbgym_cfg.dbgym_runs_path})"
        assert not os.path.samefile(
            parent_dir(parent_dpath), dbgym_cfg.dbgym_runs_path
        ), f"fpath ({fpath}) should be inside a run_*/[codebase]/ dir instead of directly in run_*/ ({dbgym_cfg.dbgym_runs_path})"
        assert not os.path.samefile(
            parent_dir(parent_dir(parent_dpath)), dbgym_cfg.dbgym_runs_path
        ), f"fpath ({fpath}) should be inside a run_*/[codebase]/[organization]/ dir instead of directly in run_*/ ({dbgym_cfg.dbgym_runs_path})"
        # org_dpath is the run_*/[codebase]/[organization]/ dir that fpath is in
        org_dpath = parent_dpath
        while not os.path.samefile(
            parent_dir(parent_dir(parent_dir(org_dpath))), dbgym_cfg.dbgym_runs_path
        ):
            org_dpath = parent_dir(org_dpath)
        org_dname = dir_basename(org_dpath)
        codebase_dpath = parent_dir(org_dpath)
        codebase_dname = dir_basename(codebase_dpath)
        this_run_save_dpath = os.path.join(
            dbgym_cfg.dbgym_this_run_path, codebase_dname, org_dname
        )
        os.makedirs(this_run_save_dpath, exist_ok=True)

        # if the fpath file is directly in org_dpath, we symlink the file directly
        if os.path.samefile(parent_dpath, org_dpath):
            fname = os.path.basename(fpath)
            symlink_fpath = os.path.join(this_run_save_dpath, fname)
            try_create_symlink(fpath, symlink_fpath)
        # else, we know the fpath file is _not_ directly inside org_dpath dir
        # we go as far back as we can while still staying in org_dpath and symlink that "base" dir
        # this is because lots of runs create dirs within org_dpath and it's just a waste of space to symlink every individual file
        else:
            # set base_dpath such that its parent is org_dpath
            base_dpath = parent_dpath
            while not os.path.samefile(parent_dir(base_dpath), org_dpath):
                base_dpath = parent_dir(base_dpath)

            # create symlink
            open_base_dname = dir_basename(base_dpath)
            symlink_dpath = os.path.join(this_run_save_dpath, open_base_dname)
            try_create_symlink(base_dpath, symlink_dpath)
    # if it wasn't generated by a run
    else:
        # since we don't know where the file is at all, the location is "unknown" and the org is "all"
        this_run_save_dpath = os.path.join(
            dbgym_cfg.dbgym_this_run_path, "unknown", "all"
        )
        os.makedirs(this_run_save_dpath, exist_ok=True)
        fname = os.path.basename(fpath)
        # in this case, we want to copy instead of symlinking since it might disappear in the future
        copy_fpath = os.path.join(this_run_save_dpath, fname)
        shutil.copy(fpath, copy_fpath)


# TODO(phw2): refactor our manual symlinking in postgres/cli.py to use link_result() instead
def link_result(dbgym_cfg: DBGymConfig, result_path: Path, custom_result_name: str | None=None) -> Path:
    """
    result_path must be a "result", meaning it was generated inside dbgym_cfg.dbgym_this_run_path
    result_path itself can be a file or a dir but not a symlink
    Returns the symlink path.
    Create a symlink of the same name to result_path inside [workspace]/data/
    Will override the old symlink if there is one
    This is called so that [workspace]/data/ always contains the latest generated version of a file
    """
    result_path = conv_inputpath_to_realabspath(dbgym_cfg, result_path)
    assert is_child_path(result_path, dbgym_cfg.dbgym_this_run_path)
    assert not os.path.islink(result_path)

    if custom_result_name != None:
        result_name = custom_result_name
    else:
        if os.path.isfile(result_path):
            result_name = os.path.basename(result_path)
        elif os.path.isdir(result_path):
            result_name = dir_basename(result_path)
        else:
            raise AssertionError("result_path must be either a file or dir")
    symlink_path = dbgym_cfg.cur_symlinks_data_path(mkdir=True) / result_name

    # Remove the old symlink ("old" meaning created in an earlier run) if there is one
    # Note that in a multi-threaded setting, this might remove one created by a process in the same run,
    #   meaning it's not "old" by our definition of "old". However, we'll always end up with a symlink
    #   file of the current run regardless of the order of threads.
    try_remove_file(symlink_path)
    try_create_symlink(result_path, symlink_path)

    return symlink_path


def try_create_symlink(src_path: Path, dst_path: Path) -> None:
    '''
    Our functions that create symlinks might be called by multiple processes at once
    during HPO. Thus, this is a thread-safe way to create a symlink.
    '''
    try:
        os.symlink(src_path, dst_path)
    except FileExistsError:
        # it's ok if it exists
        pass


def try_remove_file(path: Path) -> None:
    '''
    Our functions that remove files might be called by multiple processes at once
    during HPO. Thus, this is a thread-safe way to remove a file.
    '''
    try:
        os.remove(path)
    except FileNotFoundError:
        # it's ok if it doesn't exist
        pass


def restart_ray(redis_port: int):
    """
    Stop and start Ray.
    This is good to do between each stage to avoid bugs from carrying over across stages
    """
    subprocess_run("ray stop -f")
    ncpu = os.cpu_count()
    # --disable-usage-stats avoids a Y/N prompt
    subprocess_run(
        f"OMP_NUM_THREADS={ncpu} ray start --head --port={redis_port} --num-cpus={ncpu} --disable-usage-stats"
    )


def make_redis_started(port: int):
    """
    Start Redis if it's not already started.
    Note that Ray uses Redis but does *not* use this function. It starts Redis on its own.
    One current use for this function to start/stop Redis for Boot.
    """
    try:
        r = redis.Redis(port=port)
        r.ping()
        # This means Redis is running, so we do nothing
        do_start_redis = False
    except (redis.ConnectionError, redis.TimeoutError):
        # This means Redis is not running, so we start it
        do_start_redis = True
    
    # I'm starting Redis outside of except so that errors in r.ping get propagated correctly
    if do_start_redis:
        subprocess_run(f"redis-server --port {port} --daemonize yes")
        # When you start Redis in daemon mode, it won't let you know if it's started, so we ping again to check
        r = redis.Redis(port=port)
        r.ping()
