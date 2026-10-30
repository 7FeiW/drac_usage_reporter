"""Build markdown tables from sreport and sacct dumps."""
import collections
import csv
import re

from . import slurm


def md(headers, rows):
    """Markdown table; numeric-looking columns are right-aligned."""
    def num(v):
        return bool(re.fullmatch(r"[-–\d.,%k ]+|\*\*[\d.,%k]+\*\*", str(v)))
    align = ["---:" if rows and all(num(r[i]) or r[i] in ("", "–") for r in rows) else "---"
             for i in range(len(headers))]
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(align) + " |"]
    lines += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(lines)


def n(v, digits=0):
    return "–" if not v else f"{v:,.{digits}f}"


def pct(a, b):
    return f"{a / b:.0%}" if b else "–"


# ---- sreport --------------------------------------------------------------------------------

def load_sreport(path):
    """{(account, user): {tres: hours}}; user '' holds the account total."""
    d = collections.defaultdict(dict)
    with open(path) as fh:
        for line in fh:
            f = line.strip().split("|")
            if len(f) == 4 and f[3].isdigit():
                d[(f[0], f[1])][f[2]] = int(f[3])
    return d


def gpu_usage(d, cluster):
    """Per user GPU-hours by type and RGU-hours billed, with each user's share of the account."""
    types = list(cluster.gpus)
    rows = []
    for acct in sorted({a for a, _ in d if a.endswith("_gpu")}):
        users = sorted(((u, v) for (a, u), v in d.items() if a == acct and u and v.get("billing")),
                       key=lambda uv: -uv[1]["billing"])
        total = sum(v["billing"] for _, v in users)
        for u, v in users:
            rows.append([u, acct] + [n(v.get(cluster.gpus[t].gres, 0)) for t in types]
                        + [n(v["billing"] / 1000), pct(v["billing"], total)])
        if len(users) > 1:
            rows.append(["**total**", f"**{acct}**"] + [""] * len(types)
                        + [f"**{n(total / 1000)}**", ""])
    return md(["User", "Account"] + [f"{t} h" for t in types] + ["RGU-h billed", "Share"], rows)


def cpu_usage(windows):
    """Core-hours per user on the CPU accounts, one column per (label, sreport dict) window."""
    keys = sorted({(a, u) for _, d in windows for a, u in d if a.endswith("_cpu") and u},
                  key=lambda k: (k[0], -windows[-1][1].get(k, {}).get("cpu", 0)))
    rows = [[u, a] + [n(d.get((a, u), {}).get("cpu", 0)) for _, d in windows]
            for a, u in keys if any(d.get((a, u), {}).get("cpu") for _, d in windows)]
    return md(["User", "Account"] + [f"{label}" for label, _ in windows], rows)


def across_clusters(per_cluster):
    """Per-user RGU-h billed (GPU accounts) and core-h (CPU accounts) side by side for several clusters.

    per_cluster: [(cluster name, sreport dict)], each for the same window.
    """
    names = [c for c, _ in per_cluster]
    gpu = collections.defaultdict(lambda: collections.defaultdict(float))
    cpu = collections.defaultdict(lambda: collections.defaultdict(float))
    for c, d in per_cluster:
        for (a, u), v in d.items():
            if u and a.endswith("_gpu"):
                gpu[u][c] += v.get("billing", 0) / 1000
            elif u and a.endswith("_cpu"):
                cpu[u][c] += v.get("cpu", 0)
    users = sorted(set(gpu) | set(cpu), key=lambda u: -(sum(gpu[u].values()) + sum(cpu[u].values()) / 10))
    rows = [[u] + [n(gpu[u][c]) for c in names] + [n(sum(gpu[u].values()))]
            + [n(cpu[u][c]) for c in names] + [n(sum(cpu[u].values()))] for u in users]
    total = ["**total**"] + [f"**{n(sum(gpu[u][c] for u in users))}**" for c in names] + \
        [f"**{n(sum(sum(gpu[u].values()) for u in users))}**"] + \
        [f"**{n(sum(cpu[u][c] for u in users))}**" for c in names] + [f"**{n(sum(sum(cpu[u].values()) for u in users))}**"]
    return md(["User"] + [f"{c} RGU-h" for c in names] + ["Total RGU-h"] + [f"{c} core-h" for c in names]
              + ["Total core-h"], rows + [total])


# ---- sacct ----------------------------------------------------------------------------------

class Agg:
    """Running totals over a group of jobs."""

    def __init__(self):
        self.jobs = 0
        self.states = collections.Counter()
        self.hours = self.core_h = self.rgu_h = self.cpu_used_h = self.mem_req_h = self.mem_used_h = 0.0
        self.max_rss = 0.0

    def add(self, j):
        self.jobs += 1
        self.states[j.state] += 1
        self.hours += j.hours
        self.core_h += j.core_hours
        self.rgu_h += j.rgu_hours
        self.cpu_used_h += j.total_cpu_s / 3600
        self.mem_req_h += j.mem_gib * j.hours
        self.mem_used_h += j.max_rss_gib * j.hours
        self.max_rss = max(self.max_rss, j.max_rss_gib)

    @property
    def outcome(self):
        s = self.states
        fail = s["FAILED"] + s["OUT_OF_MEMORY"] + s["NODE_FAIL"]
        return f"{s['COMPLETED']} / {fail} / {s['TIMEOUT']} / {s['CANCELLED']}"

    def cost_key(self):
        """Rough common scale for sorting: 1 RGU-h ~ 1 core-h."""
        return self.rgu_h if self.rgu_h else self.core_h


def group(jobs, key):
    g = collections.defaultdict(Agg)
    for j in jobs.values():
        g[key(j)].add(j)
    return g


def by_user(jobs):
    """Jobs, outcomes, hours, cost and efficiency per (user, account)."""
    g = group(jobs, lambda j: (j.user, j.account))
    rows = [[u, a, a_.jobs, a_.outcome, n(a_.hours), n(a_.core_h), n(a_.rgu_h),
             pct(a_.cpu_used_h, a_.core_h), pct(a_.mem_used_h, a_.mem_req_h)]
            for (u, a), a_ in sorted(g.items(), key=lambda kv: (kv[0][1].endswith("_cpu"), -kv[1].cost_key()))]
    return md(["User", "Account", "Jobs", "OK / fail / timeout / cancel", "Hours", "Core-h", "RGU-h",
               "CPU used", "Memory used"], rows)


def by_shape(jobs, kind, top=25):
    """Most expensive request shapes (user, account, GPU, CPUs, memory) of GPU jobs (by RGU-h) or
    CPU-only jobs (by core-h). Empty string when there are none."""
    gpu_jobs = kind == "gpu"
    sel = {k: j for k, j in jobs.items() if (j.gpu != "cpu") == gpu_jobs}
    g = group(sel, lambda j: (j.user, j.account, j.gpu, j.ncpus, round(j.mem_gib)))
    items = sorted(g.items(), key=lambda kv: -(kv[1].rgu_h if gpu_jobs else kv[1].core_h))[:top]
    if not items:
        return ""
    head = ["User", "Account"] + (["GPU"] if gpu_jobs else []) + ["CPUs", "Memory", "Jobs",
                                                                  "OK / fail / timeout / cancel", "Hours", "Core-h"]
    head += (["RGU-h"] if gpu_jobs else []) + ["CPU used", "Max memory"]
    rows = [[u, a] + ([gpu] if gpu_jobs else []) + [c, f"{m}G", a_.jobs, a_.outcome, n(a_.hours), n(a_.core_h)]
            + ([n(a_.rgu_h)] if gpu_jobs else []) + [pct(a_.cpu_used_h, a_.core_h), f"{a_.max_rss:.1f}G"]
            for (u, a, gpu, c, m), a_ in items]
    return md(head, rows)


def by_program(jobs, user):
    """A user's GPU jobs grouped by request shape and the program its job steps ran."""
    def prog(j):
        names = sorted({s.name for s in j.main_steps.values()})
        return ",".join(names)[:20] or j.name[:20]
    sel = {k: j for k, j in jobs.items() if j.user == user and j.account.endswith("_gpu") and j.elapsed_s}
    g = group(sel, lambda j: (j.gpu, j.ncpus, round(j.mem_gib), prog(j)))
    rate = {k: slurm.rgu_per_hour(j.alloc) for j in sel.values()
            for k in [(j.gpu, j.ncpus, round(j.mem_gib), prog(j))]}
    rows = [[gpu, c, f"{m}G", p, a_.jobs, n(a_.hours), f"{rate[(gpu, c, m, p)]:.2f}", n(a_.rgu_h),
             pct(a_.cpu_used_h, a_.core_h), f"{a_.max_rss:.1f}G"]
            for (gpu, c, m, p), a_ in sorted(g.items(), key=lambda kv: -kv[1].rgu_h)]
    return md(["GPU", "CPUs", "Memory", "Program", "Jobs", "Hours", "RGU/h", "RGU-h", "CPU used", "Max memory"], rows)


def step_outcomes(jobs, user, top=20):
    """Per job name: job state vs. state of its work step(s). Shows jobs marked FAILED whose work finished."""
    c = collections.Counter()
    for j in jobs.values():
        if j.user == user:
            c[(j.name, j.state, j.work_state)] += 1
    return md(["Job name", "Job state", "Work step", "Jobs"], [[a, b, d, v] for (a, b, d), v in c.most_common(top)])


CSV_COLUMNS = ["cluster", "jobid", "user", "name", "account", "gpu", "nodes", "cpus", "mem_gib", "rgu_per_h", "state",
               "exit", "work_step", "submit", "start", "elapsed_h", "limit", "cpu_used", "max_rss_gib", "workdir"]


def write_jobs_csv(jobs, fh):
    w = csv.writer(fh)
    w.writerow(CSV_COLUMNS)
    for j in sorted(jobs, key=lambda j: j.submit):
        w.writerow([j.cluster, j.id, j.user, j.name, j.account, j.gpu, j.nodes, j.ncpus, f"{j.mem_gib:.0f}",
                    slurm.rgu_per_hour(j.alloc), j.state, j.exit,
                    j.work_state, j.submit, j.start,
                    f"{j.hours:.2f}", j.limit, pct(j.total_cpu_s / 3600, j.core_hours),
                    f"{j.max_rss_gib:.1f}", j.workdir])


def queue_summary(text):
    """Running and pending jobs per user and account, with their GPUs and cores, from fetch.queue output."""
    g = collections.defaultdict(lambda: {"jr": 0, "gr": 0, "cr": 0, "jp": 0, "gp": 0, "cp": 0,
                                         "why": collections.Counter()})
    for line in text.splitlines():
        f = [x.strip() for x in line.split("|")]
        if len(f) < 9:
            continue
        u, a, st, per_node, per_job, nn, alloc, cpus, reason = f[:9]
        m = re.search(r"gres/gpu=(\d+)", alloc)
        gpus = int(m.group(1)) if m else 0
        if not m:
            for spec, mul in ((per_node, int(nn or 1)), (per_job, 1)):
                gpus += sum(int(x) * mul for x in re.findall(r"gpu(?::[\w.]+)?:(\d+)", spec))
        row = g[(u, a)]
        cores = int(cpus) if cpus.isdigit() else 0
        if st == "RUNNING":
            row["jr"] += 1
            row["gr"] += gpus
            row["cr"] += cores
        else:
            row["jp"] += 1
            row["gp"] += gpus
            row["cp"] += cores
            row["why"][reason.split(",")[0]] += 1

    def why(c):
        return ", ".join(r if len(c) == 1 else f"{r} ({k:,})" for r, k in c.most_common(3))

    keys = sorted(g, key=lambda k: (not k[1].endswith("_gpu"), -(g[k]["gr"] + g[k]["gp"]), -(g[k]["cr"] + g[k]["cp"])))
    rows = [[u, a, n(r["jr"]), n(r["gr"]), n(r["cr"]), n(r["jp"]), n(r["gp"]), n(r["cp"]), why(r["why"])]
            for (u, a), r in ((k, g[k]) for k in keys)]
    return md(["User", "Account", "Jobs running", "GPUs running", "Cores running", "Jobs pending", "GPUs pending",
               "Cores pending", "Pending because"], rows)
