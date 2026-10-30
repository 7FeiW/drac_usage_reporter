"""Load cluster, ssh and flag settings from config.ini (start from config.example.ini)."""
import configparser
import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.ini"            # yours, git-ignored
EXAMPLE = ROOT / "config.example.ini"   # committed template

# Used for anything a config file leaves out of [report] and [flags].
DEFAULTS = {
    "report": {"group": "", "clusters": "all", "windows": "30, 90, 180", "data_dir": "data/cache",
               "cache_days": "365", "reports_dir": "reports/auto", "top": "25", "auto_connect": "yes"},
    "flags": {"min_at_stake": "200", "high": "10000", "medium": "1000", "idle_cpu": "0.6",
              "timeout_rate": "0.3", "fail_rate": "0.1", "mem_ceiling": "0.95", "masked_min_jobs": "5"},
}


@dataclass
class GpuType:
    rgu: float      # RGU per GPU-hour (Slurm billing weight / 1000)
    gres: str       # TRES name in sreport, e.g. gres/gpu:h100


@dataclass
class Cluster:
    name: str
    host: str
    user: str = ""
    identity_file: str = ""
    ssh_options: list = field(default_factory=list)
    accounts: list = field(default_factory=list)
    sacct_chunk_days: int = 10
    gpus: dict = field(default_factory=dict)  # short type -> GpuType
    rgu_per_core: float = 0.0  # billing weight of one core, in RGU/h (0 = unknown)
    rgu_per_gb: float = 0.0    # billing weight of one GB of memory, in RGU/h

    def covers(self, rgu):
        """(cores, GB) whose billing equals `rgu`: what a GPU's price already pays for."""
        cores = int(round(rgu / self.rgu_per_core, 2)) if self.rgu_per_core else 0
        return cores, rgu / self.rgu_per_gb if self.rgu_per_gb else 0.0

    control_path: str = ""     # set when this program opened the shared ssh connection itself

    def _options(self, keep=None, drop=()):
        """ssh_options as a list, keeping only `-o` options whose name is in `keep` (lowercase), or
        dropping those in `drop`. Other flags are kept unless `keep` is given."""
        out, it = [], iter(self.ssh_options)
        for o in it:
            pair = [o, next(it, "")] if o == "-o" else [o]
            name = (pair[-1] if o == "-o" else o[2:] if o.startswith("-o") else "").split("=")[0].lower()
            if (keep is None and name not in drop) or (keep is not None and name in keep):
                out += pair
        return out

    def _login(self):
        return (["-l", self.user] if self.user else []) + \
            (["-i", os.path.expanduser(self.identity_file)] if self.identity_file else [])

    def ssh_command(self, remote):
        # ssh keeps the first value it sees for an option, so the connection opened by this
        # program (control_path) goes before the configured options.
        own = ["-o", f"ControlPath={self.control_path}", "-o", "ControlMaster=no"] if self.control_path else []
        return ["ssh", *own, *self._options(), *self._login(), self.host, remote]

    def control_command(self, op):
        """ssh -O <op> (check, exit) for the shared ControlMaster connection.

        Only what locates the connection is kept: its ControlPath and the login name.
        """
        path = ["-o", f"ControlPath={self.control_path}"] if self.control_path else self._options(keep={"controlpath"})
        return ["ssh", *path, *(["-l", self.user] if self.user else []), "-O", op, self.host]

    def open_command(self, path):
        """Interactive ssh that authenticates (MFA, host key prompts) and then keeps a shared
        connection open in the background at `path` until `ssh -O exit`."""
        mine = self._options(drop={"batchmode", "controlmaster", "controlpath", "controlpersist"})
        return ["ssh", "-o", "ControlMaster=yes", "-o", f"ControlPath={path}", "-o", "ControlPersist=yes",
                *mine, *self._login(), "-f", "-N", self.host]


@dataclass
class Config:
    files: list
    group: str
    default_clusters: list
    clusters: dict
    flags: dict
    windows: list   # sreport windows in days, ascending
    data_dir: Path
    reports_dir: Path  # where the `all` command saves its reports
    top: int
    auto_connect: bool
    cache_days: int  # rolling cache: keep this many days (at least the longest window)

    def cluster(self, name):
        if name not in self.clusters:
            raise SystemExit(f"unknown cluster {name!r}; configured: {', '.join(self.clusters)}")
        return self.clusters[name]

    def select(self, spec=None):
        """Clusters named by 'fir,narval' or 'all'; default: [report] clusters."""
        names = [x.strip() for x in spec.split(",")] if spec else self.default_clusters
        if names == ["all"]:
            names = list(self.clusters)
        return [self.cluster(n) for n in names]


def _cluster(name, s):
    gpus = {}
    for k, v in s.items():
        if k.startswith("gpu "):
            rgu, gres = (x.strip() for x in v.split(","))
            gpus[k[4:].strip()] = GpuType(float(rgu), gres)
    return Cluster(name=name, host=s.get("host", name), user=s.get("user", ""),
                   identity_file=s.get("identity_file", ""), ssh_options=shlex.split(s.get("ssh_options", "")),
                   accounts=[a.strip() for a in s.get("accounts", "").split(",") if a.strip()],
                   sacct_chunk_days=int(s.get("sacct_chunk_days", 10)), gpus=gpus,
                   rgu_per_core=float(s.get("rgu_per_core", 0)), rgu_per_gb=float(s.get("rgu_per_gb", 0)))


def load(path=None):
    """Read `path`, else $DRAC_USAGE_CONFIG, else config.ini in the repo root, on top of DEFAULTS."""
    path = Path(path or os.environ.get("DRAC_USAGE_CONFIG") or CONFIG).expanduser()
    if not path.exists():
        if path == CONFIG:
            raise SystemExit(f"no {CONFIG}. Start from the example:\n  cp {EXAMPLE} {CONFIG}\n"
                             "then set your clusters, accounts and ssh settings in it.")
        raise SystemExit(f"config file not found: {path}")
    cp = configparser.ConfigParser(delimiters=("=",), interpolation=None)
    cp.read_dict(DEFAULTS)
    cp.read(path)
    clusters = {sec.split(None, 1)[1]: _cluster(sec.split(None, 1)[1], cp[sec])
                for sec in cp.sections() if sec.startswith("cluster ")}
    rep = cp["report"]
    data_dir = Path(rep["data_dir"]).expanduser()
    reports_dir = Path(rep["reports_dir"]).expanduser()
    windows = sorted(int(w) for w in rep["windows"].split(","))
    return Config(files=[path], group=rep["group"],
                  default_clusters=[x.strip() for x in rep["clusters"].split(",") if x.strip()],
                  clusters=clusters, flags={k: float(v) for k, v in cp["flags"].items()},
                  windows=windows,
                  data_dir=data_dir if data_dir.is_absolute() else ROOT / data_dir,
                  reports_dir=reports_dir if reports_dir.is_absolute() else ROOT / reports_dir,
                  top=int(rep["top"]),
                  auto_connect=rep["auto_connect"].strip().lower() in ("yes", "true", "on", "1"),
                  cache_days=max(int(rep["cache_days"]), windows[-1]))
