"""Slurm usage reports for a research group on Digital Research Alliance (DRAC) clusters.

    drac_usage_reporter                        # do it all: fetch, report and queue, report saved to reports/auto/
    drac_usage_reporter check                  # config, ssh, account membership, billing rates (all clusters)
    drac_usage_reporter fetch                  # bring the local cache up to date (only what is new)
    drac_usage_reporter report                 # flags, usage tables, job breakdown, from the cache
    drac_usage_reporter report --user alice    # one user: flags, request shapes, programs, job outcomes
    drac_usage_reporter queue                  # live queue: running and pending CPU and GPU jobs

Every command takes --cluster (a name, a comma-separated list, or `all`). The default is
[report] clusters, except for check, which checks every configured cluster.
Everything else is in config.ini (start from config.example.ini), or the file given by --config
or $DRAC_USAGE_CONFIG. Output is pretty-printed in a terminal and markdown when piped or
redirected; set NO_COLOR=1 to drop colours.
"""
import argparse
import contextlib
import datetime as dt
import io
import os
import shlex
import sys
from dataclasses import dataclass

from . import cache, config, fetch, flags, render, slurm, tables


@dataclass
class Raw:
    """A cluster's data for one report: sreport sums per window and the jobs of the longest window."""
    cluster: config.Cluster
    last_day: dt.date
    windows: list  # [(days, sreport dict)]
    jobs: dict
    queue: tuple = ("", None)  # (squeue snapshot, time taken) from the last fetch


def each_cluster(clusters, fn):
    """Run fn(cluster) for every cluster; a cluster that fails is reported and skipped."""
    done = 0
    for c in clusters:
        try:
            fn(c)
            done += 1
        except SystemExit as e:
            sys.stdout.flush()
            print(f"[{c.name}] skipped: {e}", file=sys.stderr)
    if not done:
        raise SystemExit(1)


def load_raw(cfg, clusters, date=None):
    """Each cluster's cached data for windows ending on `date` (default: the newest cached day)."""
    out = []
    for c in clusters:
        store = cache.Store(cfg.data_dir, c)
        days = store.sreport_days()
        if not days:
            print(f"[{c.name}] nothing cached in {store.dir}; run fetch first", file=sys.stderr)
            continue
        last = dt.date.fromisoformat(date) if date else days[-1]
        windows = []
        for w in cfg.windows:
            usage, missing = store.sreport(last - dt.timedelta(days=w - 1), last)
            if missing:
                print(f"[{c.name}] {len(missing)} of the last {w} days to {last} are not cached "
                      f"({missing[0]} .. {missing[-1]}); run fetch", file=sys.stderr)
            windows.append((w, usage))
        jobs = store.jobs(last - dt.timedelta(days=cfg.windows[-1] - 1), last)
        out.append(Raw(c, last, windows, jobs, store.queue()))
    if not out:
        raise SystemExit(1)
    return out


def cmd_check(a, cfg):
    print("Config: " + ", ".join(str(f) for f in cfg.files))
    print(f"Cache: {cfg.data_dir} (keeps {cfg.cache_days} days)   windows: {', '.join(map(str, cfg.windows))} days")
    print("Flags: " + ", ".join(f"{k}={v:g}" for k, v in cfg.flags.items()))
    print("Clusters: " + ", ".join(f"{n} (default)" if n in cfg.default_clusters else n for n in cfg.clusters))

    def one(c):
        print(f"\n[{c.name}]\n  cache: {cache.Store(cfg.data_dir, c).summary()}")
        print(f"  ssh: {shlex.join(c.ssh_command('<command>'))}")
        out = fetch.check(c).split()
        print(f"  connected: {out[0] if out else '?'}")
        is_open, msg = fetch.shared_connection(c)
        if c in fetch.CONNECTIONS.opened:
            print("  shared connection: opened by this run; closed again when it ends")
        elif is_open:
            print(f"  shared connection: open ({msg.removeprefix('Master running ').strip('()')})")
            print(f"  close it with: {shlex.join(c.control_command('exit'))}")
        else:
            print("  shared connection: none (no ControlMaster set up; each command logs in separately)")
        mine = set(out[1:])
        for acct in c.accounts:
            print(f"  account {acct}: {'member' if acct in mine else 'NOT a member (or no such account)'}")
        if mine - set(c.accounts):
            print(f"  other accounts you belong to: {', '.join(sorted(mine - set(c.accounts)))}")
        core, gb, gpus = slurm.billing_weights(fetch.partitions(c))
        live = {g.rsplit(":", 1)[-1].rsplit("_", 1)[-1]: (rgu, g) for g, rgu in gpus.items()}
        same = (abs(core - c.rgu_per_core) < 1e-4 and abs(gb - c.rgu_per_gb) < 1e-4
                and set(live) == set(c.gpus) and all(abs(live[t][0] - c.gpus[t].rgu) < 1e-4 for t in live))
        if same:
            print(f"  billing rates: match Slurm ({len(live)} GPU types, {core:g} RGU/h per core, {gb:g} per GB)")
            return
        print("  billing rates: config.ini differs from Slurm's TRESBillingWeights. Put these lines in "
              f"[cluster {c.name}]:")
        print(f"    rgu_per_core = {core:.6g}\n    rgu_per_gb = {gb:.6g}")
        for t, (rgu, gres) in sorted(live.items(), key=lambda kv: -kv[1][0]):
            print(f"    gpu {t} = {rgu:.6g}, {gres}")
    each_cluster(cfg.select(a.cluster or "all"), one)


def cmd_fetch(a, cfg):
    each_cluster(cfg.select(a.cluster), lambda c: fetch_cluster(c, cfg))


def fetch_cluster(c, cfg):
    """Bring one cluster's cache up to date."""
    store = cache.Store(cfg.data_dir, c)
    store.check_accounts(c.accounts)
    store.check_gpu_types([g.gres for g in c.gpus.values()])
    now = fetch.cluster_now(c)
    today = now.date()
    first = today - dt.timedelta(days=cfg.windows[-1] - 1)
    days = store.sreport_days_to_fetch(first, today, now)
    ranges = store.sacct_ranges(dt.datetime.combine(first, dt.time()), now)
    print(f"[{c.name}] usage: {len(days)} of {cfg.windows[-1]} days to fetch; jobs: "
          + ", ".join(f"{s:%Y-%m-%d %H:%M} .. {e:%Y-%m-%d %H:%M}" for s, e in ranges), file=sys.stderr)
    for day, text in fetch.sreport_days(c, c.accounts, days).items():
        store.put_sreport_day(day, text, now)
    store.put_sacct(fetch.jobs(c, c.accounts, ranges), ranges)
    store.put_queue(fetch.queue(c, c.accounts), now)
    n_days, n_jobs = store.prune(today - dt.timedelta(days=cfg.cache_days - 1))
    if n_days or n_jobs:
        print(f"[{c.name}] dropped {n_days} days of usage and {n_jobs} jobs older than "
              f"{cfg.cache_days} days (cache_days)", file=sys.stderr)
    store.save()
    print(f"[{c.name}] cache: {store.summary()}", file=sys.stderr)


def group_report(raws, cfg):
    many = len(raws) > 1
    h = "#" if many else ""  # one extra heading level per cluster when reporting several
    days = max(w for r in raws for w, _ in r.windows)
    print(f"# {cfg.group} usage on {', '.join(r.cluster.name for r in raws)} (to {max(r.last_day for r in raws)})\n")
    print("Generated by `drac_usage_reporter report`. See docs/method.md for how to read the columns.\n")
    print("Job tables and flags use lifetime job statistics. Historical windows include only jobs "
          "finished by the cutoff; earlier running-job snapshots are unavailable.\n")
    found = flags.sort([f for r in raws for f in flags.find(r.jobs, r.cluster, cfg.flags)])
    print(f"## Flags, last {days} days\n\n{flags.table(found, clusters=many)}\n")
    if many:
        print("## All clusters\n")
        for w in sorted(set.intersection(*({w for w, _ in r.windows} for r in raws))):
            per = [(r.cluster.name, dict(r.windows)[w]) for r in raws]
            print(f"### Last {w} days\n\n{tables.across_clusters(per)}\n")
    for r in raws:
        if many:
            print(f"## {r.cluster.name}\n")
        print(f"{h}## GPU usage (sreport)\n")
        for w, d in r.windows:
            print(f"{h}### Last {w} days\n\n{tables.gpu_usage(d, r.cluster)}\n")
        print(f"{h}## CPU usage, core-hours (sreport)\n\n{tables.cpu_usage([(f'{w} days', d) for w, d in r.windows])}\n")
        print(f"{h}## Jobs by user and account, last {r.windows[-1][0]} days (sacct)\n")
        print(f"{tables.by_user(r.jobs)}\n")
        for kind, label in (("gpu", "GPU jobs, by RGU-h"), ("cpu", "CPU-only jobs, by core-h")):
            t = tables.by_shape(r.jobs, kind, cfg.top)
            if t:
                print(f"{h}## Most expensive request shapes: {label}\n\n{t}\n")
        text, taken = r.queue
        if taken:
            print(f"{h}## Queue at {taken.replace('T', ' ')[:16]} (last fetch; cluster time)\n\n"
                  f"{tables.queue_summary(text)}\n")


def user_report(raws, cfg, user):
    many = len(raws) > 1
    h = "#" if many else ""
    print(f"# {user} on {', '.join(r.cluster.name for r in raws)} (to {max(r.last_day for r in raws)})\n")
    print("Job tables and flags use lifetime job statistics. Historical windows include only jobs "
          "finished by the cutoff; earlier running-job snapshots are unavailable.\n")
    lim = dict(cfg.flags, min_at_stake=0, masked_min_jobs=1)  # show everything for one user
    found = flags.sort([f for r in raws for f in flags.find(r.jobs, r.cluster, lim, user)])
    print(f"## Flags\n\n{flags.table(found, clusters=many)}\n")
    for r in raws:
        mine = {k: j for k, j in r.jobs.items() if j.user == user}
        if many:
            print(f"## {r.cluster.name}\n")
        for kind, label in (("gpu", "GPU jobs"), ("cpu", "CPU-only jobs")):
            t = tables.by_shape(mine, kind, top=1000)
            if t:
                print(f"{h}## Request shapes: {label}\n\n{t}\n")
        print(f"{h}## GPU jobs by program\n\n{tables.by_program(mine, user)}\n")
        print(f"{h}## Job outcomes by job name\n\n{tables.step_outcomes(mine, user)}\n")


def cmd_report(a, cfg):
    raws = load_raw(cfg, cfg.select(a.cluster), a.date)
    if a.csv:
        sel = [j for r in raws for j in r.jobs.values() if j.elapsed_s and (not a.user or j.user == a.user)]
        with open(a.csv, "w", newline="") as fh:
            tables.write_jobs_csv(sel, fh)
        print(f"{len(sel)} jobs written to {a.csv}", file=sys.stderr)
    if a.user:
        user_report(raws, cfg, a.user)
    else:
        group_report(raws, cfg)


def cmd_queue(a, cfg):
    clusters = cfg.select(a.cluster)

    def one(c):
        text = tables.queue_summary(fetch.queue(c, c.accounts))
        print(f"## {c.name}\n\n{text}\n" if len(clusters) > 1 else text)
    each_cluster(clusters, one)


def cmd_all(a, cfg):
    """fetch, then the group report (with the queue as just fetched); the markdown is also saved to reports_dir."""
    clusters = cfg.select(a.cluster)
    each_cluster(clusters, lambda c: fetch_cluster(c, cfg))
    raws = load_raw(cfg, clusters)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        group_report(raws, cfg)  # includes the queue snapshot the fetch just took
    md = buf.getvalue()
    out = cfg.reports_dir / f"{max(r.last_day for r in raws)}_{'_'.join(r.cluster.name for r in raws)}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md)
    print(md, end="")
    sys.stdout.flush()
    print(f"saved {out}", file=sys.stderr)


def main():
    p = argparse.ArgumentParser(prog="drac_usage_reporter", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", help="config file to use instead of config.ini")
    sub = p.add_subparsers(dest="cmd")

    def add(name, fn, help):
        s = sub.add_parser(name, help=help, description=help)
        s.add_argument("--cluster", help="name, comma-separated list, or `all` (default: [report] clusters)")
        s.set_defaults(fn=fn)
        return s

    add("all", cmd_all, "do it all (the default): fetch, report and queue; also saves the report")
    add("check", cmd_check, "show settings; test ssh, account membership and billing rates (all clusters)")
    add("fetch", cmd_fetch, "bring the local cache up to date: fetch only what is new or may still change")
    s = add("report", cmd_report, "usage report for the group, or for one user with --user")
    s.add_argument("--user", help="report on one user")
    s.add_argument("--date", help="last day of the report windows, YYYY-MM-DD (default: newest cached day)")
    s.add_argument("--csv", help="also write the started jobs (of --user, if given) to this CSV file")
    add("queue", cmd_queue, "live queue of the group's accounts: running and pending CPU and GPU jobs")

    a = p.parse_args()
    if a.cmd is None:  # no command: do it all
        a = p.parse_args(sys.argv[1:] + ["all"])
    cfg = config.load(a.config)
    fetch.CONNECTIONS.auto = cfg.auto_connect
    try:
        if a.cmd not in ("all", "report", "queue") or not sys.stdout.isatty():
            a.fn(a, cfg)
            return
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            a.fn(a, cfg)
        print(render.pretty(buf.getvalue(), color=render.use_color("auto", sys.stdout)))
    finally:
        fetch.CONNECTIONS.close_all()


if __name__ == "__main__":
    try:
        main()
        sys.stdout.flush()
    except BrokenPipeError:  # output piped into head etc.: stop quietly (recipe from the Python docs)
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(1)
