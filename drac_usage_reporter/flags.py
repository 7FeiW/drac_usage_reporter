"""Rule-based flags on sacct jobs: request shapes that waste allocation or hide problems.

Jobs are grouped by request shape (user, account, GPU, cores, memory). Each rule returns Flags with
an "at stake" number in RGU-h (GPU accounts) or core-h (CPU accounts); see docs/method.md for what
it means per rule. Thresholds come from the [flags] section of the config.
"""
import collections
from dataclasses import dataclass

from . import slurm

FAILED = ("FAILED", "OUT_OF_MEMORY", "NODE_FAIL")
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


@dataclass
class Flag:
    rule: str
    user: str
    account: str
    request: str
    jobs: int
    at_stake: float
    unit: str
    detail: str
    action: str
    max_severity: str = "high"  # cap for rules whose cost may be intended (e.g. checkpointed timeouts)
    severity: str = ""
    cluster: str = ""


def _shape(j):
    return j.user, j.account, j.gpu, j.ncpus, round(j.mem_gib)


def _request(gpu, cpus, mem):
    return f"{'' if gpu == 'cpu' else gpu + ', '}{cpus} CPUs, {mem}G"


def _cost(js):
    """RGU-h on GPU accounts, core-h on CPU accounts."""
    return sum(j.rgu_hours if j.account.endswith("_gpu") else j.core_hours for j in js)


def _unit(account):
    return "RGU-h" if account.endswith("_gpu") else "core-h"


def _cores(k):
    return f"~{k} core{'s' if k != 1 else ''}"


def _work_done(j):
    return bool(j.work_steps) and all(s.state == "COMPLETED" for s in j.work_steps.values())


def rate_above_gpu(key, js, cluster, lim):
    """GPU jobs billed above their GPU's price because they ask for more cores or memory than it covers."""
    user, account, gpu, cpus, mem = key
    typ, ngpu = slurm.gpu_type(js[0].alloc or js[0].req)
    g = cluster.gpus.get(typ)
    if not account.endswith("_gpu") or not g or not cluster.rgu_per_core:
        return []
    base = g.rgu * ngpu
    rate = max(slurm.rgu_per_hour(j.alloc) for j in js)
    if rate <= base * 1.01:
        return []
    extra = sum(max(0.0, slurm.rgu_per_hour(j.alloc) - base) * j.hours for j in js)
    hours = sum(j.hours for j in js)
    used = sum(j.total_cpu_s for j in js) / 3600 / hours if hours else 0
    cores, gb = cluster.covers(base)
    over_cores = cpus * cluster.rgu_per_core > base * 1.01
    over_mem = mem * cluster.rgu_per_gb > base * 1.01
    peak = max(j.max_rss_gib for j in js)
    causes = ([f"{cpus} cores (the GPU price covers {cores})"] if over_cores else []) + \
             ([f"{mem}G memory (covers {gb:.0f}G)"] if over_mem else [])
    detail = (f"billed {rate:.2f} RGU/h vs {base:.2f} for the GPU alone, because of {' and '.join(causes)}; "
              f"~{used:.1f} cores busy, peak memory {peak:.0f}G")
    actions = []
    if over_cores:
        big_name, big = max(cluster.gpus.items(), key=lambda kv: kv[1].rgu)
        if used >= 0.8 * cpus:
            act = "CPU-bound: run on CPU nodes if the GPU adds little"
            if typ != big_name and cpus * cluster.rgu_per_core <= big.rgu * 1.01:
                act += f", or ask for a full {big_name} at about the same price"
            actions.append(act)
        else:
            need = round(used * 1.25)
            actions.append(f"request at most {cores} cores (covered by the GPU price)" if need <= cores
                           else f"request {_cores(need)}")
    if over_mem:
        actions.append(f"request at most {gb:.0f}G" + (" (peak use is close: check first)" if peak > 0.9 * gb else ""))
    action = "; ".join(actions)
    return [Flag("rate_above_gpu", user, account, _request(gpu, cpus, mem), len(js), extra, "RGU-h", detail, action)]


def idle_cores(key, js, cluster, lim):
    """CPU-account jobs whose reserved cores mostly sit idle."""
    user, account, gpu, cpus, mem = key
    if not account.endswith("_cpu") or cpus < 2:
        return []
    core_h = sum(j.core_hours for j in js)
    used_h = sum(j.total_cpu_s for j in js) / 3600
    if not core_h or used_h / core_h >= lim["idle_cpu"]:
        return []
    busy = used_h / sum(j.hours for j in js)
    return [Flag("idle_cores", user, account, _request(gpu, cpus, mem), len(js), core_h - used_h, "core-h",
                 f"{used_h / core_h:.0%} CPU used (~{busy:.0f} of {cpus} cores busy)",
                 f"request {_cores(max(1, round(busy * 1.25)))}, or check that the program scales to {cpus}")]


def timeouts(key, js, cluster, lim):
    """Shapes where many started jobs hit the time limit."""
    user, account, gpu, cpus, mem = key
    to = [j for j in js if j.state == "TIMEOUT"]
    if len(to) < 3 or len(to) / len(js) < lim["timeout_rate"]:
        return []
    return [Flag("timeouts", user, account, _request(gpu, cpus, mem), len(to), _cost(to), _unit(account),
                 f"{len(to)} of {len(js)} started jobs timed out",
                 "fine if the jobs checkpoint and resume; otherwise raise the limit or split the work",
                 max_severity="medium")]


def failures(key, js, cluster, lim):
    """Shapes where many started jobs really failed (work step did not complete)."""
    user, account, gpu, cpus, mem = key
    bad = [j for j in js if j.state in FAILED and not _work_done(j)]
    if len(bad) < 3 or len(bad) / len(js) < lim["fail_rate"]:
        return []
    oom = sum(1 for j in bad if j.mem_gib and j.max_rss_gib >= lim["mem_ceiling"] * j.mem_gib)
    detail = f"{len(bad)} of {len(js)} started jobs failed"
    action = "check the job logs"
    if oom:
        detail += f"; {oom} of them reached their memory limit"
        action = "raise --mem or reduce the batch size"
    return [Flag("failures", user, account, _request(gpu, cpus, mem), len(bad), _cost(bad), _unit(account),
                 detail, action)]


def multi_gpu(key, js, cluster, lim):
    """Jobs holding several GPUs; sacct cannot show whether all of them were used."""
    user, account, gpu, cpus, mem = key
    _, ngpu = slurm.gpu_type(js[0].alloc or js[0].req)
    if ngpu < 2:
        return []
    cost = _cost(js)
    cpu = sum(j.total_cpu_s for j in js) / 3600 / max(sum(j.core_hours for j in js), 1e-9)
    return [Flag("multi_gpu", user, account, _request(gpu, cpus, mem), len(js), cost * (ngpu - 1) / ngpu,
                 "RGU-h", f"{ngpu} GPUs per job, {cpu:.0%} CPU used; GPU use is not recorded",
                 "check the program really uses every GPU (tensor parallel size, nvidia-smi on the node)",
                 max_severity="medium")]


SHAPE_RULES = [rate_above_gpu, idle_cores, failures, timeouts, multi_gpu]


def masked_failures(jobs, lim):
    """Users with many jobs marked FAILED whose work step completed (a trailing script command errors)."""
    per_user = collections.defaultdict(list)
    for j in jobs.values():
        if j.state == "FAILED" and _work_done(j):
            per_user[(j.user, j.account)].append(j)
    return [Flag("masked_failures", u, a, "any", len(js), _cost(js), _unit(a),
                 f"{len(js)} jobs FAILED although their work step completed",
                 "fix the failing command at the end of the script so real failures stand out",
                 max_severity="low")
            for (u, a), js in per_user.items() if len(js) >= lim["masked_min_jobs"]]


def sort(flags):
    return sorted(flags, key=lambda f: (SEVERITY_ORDER[f.severity], -f.at_stake))


def find(jobs, cluster, lim, user=None):
    groups = collections.defaultdict(list)
    for j in jobs.values():
        if j.elapsed_s and (not user or j.user == user):
            groups[_shape(j)].append(j)
    out = [f for key, js in groups.items() for rule in SHAPE_RULES for f in rule(key, js, cluster, lim)]
    out = [f for f in out if f.at_stake >= lim["min_at_stake"]]
    out += masked_failures({k: j for k, j in jobs.items() if not user or j.user == user}, lim)
    for f in out:
        f.cluster = cluster.name
        sev = "high" if f.at_stake >= lim["high"] else "medium" if f.at_stake >= lim["medium"] else "low"
        f.severity = max(sev, f.max_severity, key=SEVERITY_ORDER.get)
    return sort(out)


def table(flags, clusters=False):
    """Markdown table of flags; `clusters` adds a Cluster column (set Flag.cluster first)."""
    from .tables import md
    head = ["Severity", "Rule"] + (["Cluster"] if clusters else []) + ["User", "Account", "Request", "Jobs",
                                                                       "At stake", "Detail", "Suggestion"]
    return md(head, [[f.severity, f.rule] + ([f.cluster] if clusters else [])
                     + [f.user, f.account, f.request, f.jobs, f"{f.at_stake:,.0f} {f.unit}",
                        f.detail, f.action] for f in flags])
