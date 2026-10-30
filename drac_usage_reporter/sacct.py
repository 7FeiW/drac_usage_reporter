"""Load per-job sacct dumps (one line per job or step, '|'-separated) into Job objects."""
from dataclasses import dataclass, field

from . import slurm

# Order matters: WorkDir is last so a stray '|' in a path cannot shift other fields.
FIELDS = ["JobID", "User", "JobName", "Account", "State", "ExitCode", "Submit", "Start", "End",
          "ElapsedRaw", "Timelimit", "NNodes", "NCPUS", "ReqTRES", "AllocTRES", "TotalCPU",
          "MaxRSS", "WorkDir"]


@dataclass
class Step:
    name: str       # step program for numbered steps (gmx_mpi, bash, ...), else batch/extern
    state: str
    exit: str
    max_rss_gib: float


@dataclass
class Job:
    id: str
    user: str
    name: str
    account: str
    state: str
    exit: str
    submit: str
    start: str
    end: str
    elapsed_s: int
    limit: str
    nodes: int
    ncpus: int
    req: dict
    alloc: dict
    total_cpu_s: float
    workdir: str
    steps: dict = field(default_factory=dict)  # step id suffix ('batch', '0', ...) -> Step
    cluster: str = ""

    @property
    def hours(self):
        return self.elapsed_s / 3600

    @property
    def core_hours(self):
        return self.ncpus * self.hours

    @property
    def rgu_hours(self):
        """RGU-hours, counted only on GPU accounts (CPU accounts are charged in core-hours)."""
        return slurm.rgu_per_hour(self.alloc) * self.hours if self.account.endswith("_gpu") else 0.0

    @property
    def mem_gib(self):
        return slurm.gib(self.alloc.get("mem") or self.req.get("mem", ""))

    @property
    def max_rss_gib(self):
        return max((s.max_rss_gib for s in self.steps.values()), default=0.0)

    @property
    def gpu(self):
        return slurm.gpu_label(self.alloc or self.req)

    @property
    def main_steps(self):
        """srun steps (everything except batch and extern)."""
        return {k: s for k, s in self.steps.items() if k not in ("batch", "extern")}

    @property
    def work_steps(self):
        """Steps that did the work: the srun steps, or the batch script itself when there are none."""
        return self.main_steps or {k: s for k, s in self.steps.items() if k == "batch"}

    @property
    def work_state(self):
        return ",".join(sorted({s.state for s in self.work_steps.values()})) or "none"


def _int(s):
    return int(s) if s and s.isdigit() else 0


def load(path, cluster=""):
    """Read a sacct dump file into {job id: Job}."""
    with open(path) as fh:
        return parse(fh, cluster)


def parse(lines, cluster=""):
    """sacct lines into {job id: Job}. Jobs seen in several date windows are merged."""
    jobs, steps = {}, []
    for line in lines:
        f = line.rstrip("\n").split("|", len(FIELDS) - 1)
        if len(f) != len(FIELDS):
            continue
        r = dict(zip(FIELDS, f))
        jid, _, step = r["JobID"].partition(".")
        if step:
            steps.append((jid, step, Step(r["JobName"], r["State"].split()[0], r["ExitCode"],
                                          slurm.gib(r["MaxRSS"]))))
            continue
        jobs[jid] = Job(
            id=jid, user=r["User"], name=r["JobName"], account=r["Account"],
            state=r["State"].split()[0] if r["State"] else "", exit=r["ExitCode"],
            submit=r["Submit"], start=r["Start"], end=r["End"], elapsed_s=_int(r["ElapsedRaw"]),
            limit=r["Timelimit"], nodes=_int(r["NNodes"]), ncpus=_int(r["NCPUS"]),
            req=slurm.tres(r["ReqTRES"]), alloc=slurm.tres(r["AllocTRES"]),
            total_cpu_s=slurm.seconds(r["TotalCPU"]), workdir=r["WorkDir"], cluster=cluster)
    for jid, step, s in steps:
        if jid in jobs:
            jobs[jid].steps[step] = s
    return jobs
