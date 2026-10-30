"""Pull sreport / sacct / squeue output from a cluster login node over ssh."""
import datetime as dt
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import sacct


def _ssh_hint(cluster, err):
    """What to do about a failed ssh, from its error text."""
    if "Host key verification failed" in err:
        return f"accept the host key once: run `ssh {cluster.host}` in a terminal and answer yes"
    if "Could not resolve hostname" in err:
        return f"unknown host {cluster.host!r}: set `host =` in [cluster {cluster.name}] or add an ~/.ssh/config alias"
    if "Permission denied" in err or "timed out" in err:
        return (f"log in once with `ssh {cluster.host}` (approve MFA) and keep that connection open "
                "with ssh ControlMaster")
    return "check `ssh` to this host works without prompts"


class Spinner:
    """`[fir] job records /  3s` on stderr while waiting; only in a terminal, cleared when done."""

    FRAMES = "|/-\\"

    def __init__(self, label):
        self.label = label
        self.done = threading.Event()
        self.thread = None

    def __enter__(self):
        if sys.stderr.isatty():
            self.thread = threading.Thread(target=self._spin, daemon=True)
            self.thread.start()
        return self

    def _spin(self):
        start, i = time.monotonic(), 0
        while not self.done.wait(0.1):
            sys.stderr.write(f"\r{self.label} {self.FRAMES[i % 4]} {time.monotonic() - start:3.0f}s")
            sys.stderr.flush()
            i += 1

    def __exit__(self, *exc):
        if self.thread:
            self.done.set()
            self.thread.join()
            sys.stderr.write("\r\033[K")  # clear the spinner line
            sys.stderr.flush()


class Connections:
    """Shared ssh connections: reuse an open one, else open one interactively and close it at exit.

    Only connections this program opened are closed; one the user opened is left alone.
    """

    def __init__(self):
        self.auto = False
        self.ready = set()   # cluster names checked this run
        self.opened = []     # clusters whose connection this run opened

    def ensure(self, cluster):
        if cluster.name in self.ready:
            return
        self.ready.add(cluster.name)
        if not self.auto or shared_connection(cluster)[0]:
            return
        if not (sys.stdin.isatty() and sys.stderr.isatty()):
            return  # cannot answer MFA or host key prompts; plain ssh will fail with a hint
        path = str(Path("~/.ssh").expanduser() / f"cm-drac-{cluster.name}-%r@%h:%p")
        sys.stdout.flush()
        print(f"[{cluster.name}] opening an ssh connection to {cluster.host} "
              "(answer any MFA or host key prompt)...", file=sys.stderr)
        p = subprocess.run(cluster.open_command(path))
        if p.returncode != 0:
            raise SystemExit(f"could not open an ssh connection (exit {p.returncode}). "
                             f"Try `ssh {cluster.host}` in a terminal to see why.")
        cluster.control_path = path
        self.opened.append(cluster)

    def close_all(self):
        for c in self.opened:
            subprocess.run(c.control_command("exit"), capture_output=True)
            print(f"[{c.name}] closed the ssh connection this run opened", file=sys.stderr)
        self.opened = []


CONNECTIONS = Connections()


def ssh(cluster, cmd, what="working"):
    """Run cmd on the cluster's login node; return stdout, echo stderr as a warning.

    `what` labels the spinner shown while waiting.
    """
    CONNECTIONS.ensure(cluster)
    with Spinner(f"[{cluster.name}] {what}"):
        p = subprocess.run(cluster.ssh_command(cmd), capture_output=True, text=True)
    err = p.stderr.strip()
    if p.returncode != 0 and not p.stdout:
        tip = (" Or run this command in a terminal: it then opens the connection itself, asking for MFA or "
               "the host key there." if CONNECTIONS.auto and not sys.stdin.isatty() else "")
        raise SystemExit(f"ssh failed ({err.splitlines()[-1] if err else f'exit {p.returncode}'}). "
                         f"To fix: {_ssh_hint(cluster, err)}.{tip}")
    if err:
        sys.stdout.flush()
        print(f"[{cluster.name}] {err}", file=sys.stderr)
    return p.stdout


def shared_connection(cluster):
    """Status of the ssh ControlMaster connection: (open?, ssh's message)."""
    try:
        p = subprocess.run(cluster.control_command("check"), capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        return False, "no answer"
    return p.returncode == 0, (p.stderr or p.stdout).strip()


def check(cluster):
    """Login node name and the Slurm accounts the login user belongs to."""
    return ssh(cluster, "hostname; sacctmgr -nP show assoc user=$USER format=account | sort -u", "accounts")


def partitions(cluster):
    """`scontrol show partition -o`: one line per partition, including TRESBillingWeights."""
    return ssh(cluster, "scontrol show partition -o", "billing rates")


def cluster_now(cluster):
    """The cluster's local time: sacct and sreport take times in it, so the cache keeps to it."""
    return dt.datetime.strptime(ssh(cluster, "date +%Y-%m-%dT%H:%M:%S", "connecting").strip(), "%Y-%m-%dT%H:%M:%S")


def sreport_days(cluster, accounts, days):
    """{day: per-user usage on that day in minutes (Account|Login|TresName|Used)}, one ssh call for all days.

    Minutes, not hours: summed over many days, values rounded to whole hours would drift.
    """
    tres = ["cpu", "billing", "gres/gpu"] + [g.gres for g in cluster.gpus.values()]
    cmds = [f"echo '@@ {d}'; sreport -n -P -t minutes cluster AccountUtilizationByUser "
            f"accounts={','.join(accounts)} start={d} end={d + dt.timedelta(days=1)} "
            f"-T {','.join(tres)} format=Account,Login,TresName,Used" for d in days]
    out, day = {}, None
    for line in ssh(cluster, "; ".join(cmds), f"usage, {len(days)} day{'s' * (len(days) != 1)}").splitlines(keepends=True):
        if line.startswith("@@ "):
            day = dt.date.fromisoformat(line[3:].strip())
            out[day] = ""
        elif day:
            out[day] += line
    return out


def date_chunks(start, end, days):
    """Split [start, end) into windows of at most `days` days."""
    out, s = [], start
    while s < end:
        e = min(s + dt.timedelta(days=days), end)
        out.append((s, e))
        s = e
    return out


def jobs(cluster, accounts, ranges):
    """sacct records of every job and step in the accounts for the given (start, end) datetimes,
    asked for in sacct_chunk_days windows (fir refuses wide ranges)."""
    cmds = [f"sacct -a -A {','.join(accounts)} -S {s:%Y-%m-%dT%H:%M:%S} -E {e:%Y-%m-%dT%H:%M:%S} -n -P "
            f"--format={','.join(sacct.FIELDS)}"
            for start, end in ranges for s, e in date_chunks(start, end, cluster.sacct_chunk_days)]
    return ssh(cluster, "; ".join(cmds), "job records") if cmds else ""


def queue(cluster, accounts):
    """Live queue, one line per array task:
    User|Account|State|TRES per node|TRES per job|Nodes|TRES (allocated, or requested if pending)|CPUs|Reason."""
    fmt = "UserName:|,Account:|,State:|,tres-per-node:|,tres-per-job:|,NumNodes:|,tres-alloc:|,NumCPUs:|,Reason:"
    return ssh(cluster, f"squeue -h -r -A {','.join(accounts)} -O {shlex.quote(fmt)}", "queue")
