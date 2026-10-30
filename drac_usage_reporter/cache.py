"""Local store of fetched Slurm data, so each fetch only asks for what is new or may still change.

Layout, per cluster under <data_dir>/<cluster>/:

    index.json     accounts, sacct coverage, and when each sreport day was fetched
    sacct.txt      newest sacct record of every job and its steps (sacct.FIELDS layout)
    sreport.txt    sreport usage per day: day|Account|Login|TresName|minutes (cluster local time)
    queue.txt      squeue snapshot from the last fetch (time in index.json)

sacct: a job's record can change until it finishes, and sacct returns every job that was pending
or running at any time in the requested window. So once the store covers [from, to), asking for
[to - overlap, now) catches every job that changed since: new, started, finished or requeued.
Older history is added only when a longer window needs it.

sreport: usage is stored per day, so any window is a sum of days. A day is final once it was
fetched at least SETTLE after it ended (sreport's hourly rollups lag); until then it is fetched
again.

The cache rolls: prune() drops usage days and finished jobs older than a cutoff.
"""
import collections
import datetime as dt
import json
from pathlib import Path

from . import sacct

OVERLAP = dt.timedelta(hours=1)
SETTLE = dt.timedelta(days=1)
TS = "%Y-%m-%dT%H:%M:%S"


def _ts(t):
    return t.strftime(TS)


def _parse(s):
    return dt.datetime.strptime(s, TS)


def _write(path, text):
    """Write via a temporary file, so an interrupted fetch never leaves half a file."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text)
    tmp.replace(path)


class Store:
    def __init__(self, root, cluster):
        self.cluster = cluster
        self.dir = Path(root) / cluster.name
        self.index_path = self.dir / "index.json"
        self.index = json.loads(self.index_path.read_text()) if self.index_path.exists() else {}
        if self.index.get("sreport_unit") != "minutes":  # caches from before minutes were stored
            self.index["sreport"] = {}
            self.index["sreport_unit"] = "minutes"
        self._usage = None  # {day: [Account|Login|TresName|Used lines]}, loaded on first use

    def save(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        if self._usage is not None:
            _write(self.dir / "sreport.txt",
                   "".join(f"{d}|{line}\n" for d in sorted(self._usage) for line in self._usage[d]))
        self.index_path.write_text(json.dumps(self.index, indent=1, sort_keys=True) + "\n")

    def check_gpu_types(self, gres_names):
        """Fetch usage again when the configured GPU types changed: sreport only returns the
        per-type hours it was asked for."""
        if self.index.get("sreport_gpus", []) != sorted(gres_names):
            self.index["sreport"] = {}
            self.index["sreport_gpus"] = sorted(gres_names)

    def check_accounts(self, accounts):
        """Start over when the configured accounts changed: cached data would not match."""
        if self.index.get("accounts") not in (None, sorted(accounts)):
            for f in (self.dir / "sreport.txt", self.dir / "sacct.txt"):
                f.unlink(missing_ok=True)
            self.index, self._usage = {"sreport": {}, "sreport_unit": "minutes"}, {}
        self.index["accounts"] = sorted(accounts)

    # ---- sreport, per day -------------------------------------------------------------------

    def sreport_days_to_fetch(self, first, last, now):
        """Days in [first, last] that are missing or not final yet."""
        out, d = [], first
        while d <= last:
            got = self.index["sreport"].get(d.isoformat())
            day_end = dt.datetime.combine(d + dt.timedelta(days=1), dt.time())
            if not got or _parse(got) < day_end + SETTLE:
                out.append(d)
            d += dt.timedelta(days=1)
        return out

    def usage(self):
        if self._usage is None:
            self._usage = collections.defaultdict(list)
            f = self.dir / "sreport.txt"
            for line in f.read_text().splitlines() if f.exists() else []:
                day, _, rest = line.partition("|")
                self._usage[day].append(rest)
        return self._usage

    def put_sreport_day(self, day, text, fetched):
        self.usage()[day.isoformat()] = [ln.strip() for ln in text.splitlines()
                                         if ln.count("|") == 3 and ln.strip().split("|")[3].isdigit()]
        self.index["sreport"][day.isoformat()] = _ts(fetched)

    def sreport_days(self):
        return sorted(dt.date.fromisoformat(d) for d in self.index["sreport"])

    def sreport(self, first, last):
        """Usage summed over the days [first, last]: {(account, user): {tres: hours}}, and the missing days."""
        minutes, missing = self._sum_minutes(first, last)
        return {k: {t: round(m / 60) for t, m in v.items()} for k, v in minutes.items()}, missing

    def _sum_minutes(self, first, last):
        total = collections.defaultdict(lambda: collections.defaultdict(int))
        missing, d = [], first
        while d <= last:
            if d.isoformat() not in self.index["sreport"]:
                missing.append(d)
            for line in self.usage().get(d.isoformat(), []):
                acct, user, tres, used = line.split("|")
                total[(acct, user)][tres] += int(used)
            d += dt.timedelta(days=1)
        return {k: dict(v) for k, v in total.items()}, missing

    # ---- sacct, per job ---------------------------------------------------------------------

    def sacct_ranges(self, need_from, now):
        """Time ranges to ask sacct for so the store covers [need_from, now)."""
        cov = self.index.get("sacct")
        if not cov:
            return [(need_from, now)]
        c_from, c_to = _parse(cov["from"]), _parse(cov["to"])
        out = [(need_from, c_from)] if need_from < c_from else []
        return out + [(c_to - OVERLAP, now)]

    def _groups(self, lines):
        """{job id: {JobID of line: line}} - a job's own line plus its steps."""
        g = collections.defaultdict(dict)
        for line in lines:
            jid = line.split("|", 1)[0]
            if line.count("|") == len(sacct.FIELDS) - 1 and jid:
                g[jid.partition(".")[0]][jid] = line  # later lines (fresher windows) win
        return g

    def put_sacct(self, text, ranges):
        """Merge freshly fetched records; extend the coverage by `ranges`."""
        path = self.dir / "sacct.txt"
        stored = self._groups(path.read_text().splitlines()) if path.exists() else {}
        fresh = self._groups(text.splitlines())
        # Pending array tasks are listed as one placeholder (123_[5-9]); once any record of that
        # array comes back, the old placeholder is out of date.
        bases = {k.split("_", 1)[0] for k in fresh}
        stored = {k: v for k, v in stored.items() if not ("[" in k and k.split("_", 1)[0] in bases)}
        stored.update(fresh)
        self.dir.mkdir(parents=True, exist_ok=True)
        _write(path, "".join(line + "\n" for g in stored.values() for line in g.values()))
        cov = self.index.get("sacct")
        lo = min([r[0] for r in ranges] + ([_parse(cov["from"])] if cov else []))
        hi = max([r[1] for r in ranges] + ([_parse(cov["to"])] if cov else []))
        self.index["sacct"] = {"from": _ts(lo), "to": _ts(hi), "jobs": len(stored)}

    def jobs(self, first, last):
        """Jobs overlapping [first, last]. Historical windows include only jobs finished by
        their cutoff: the cache has latest records, not historical running-job snapshots.
        Costs remain lifetime job costs, rather than usage clipped to the window.
        """
        path = self.dir / "sacct.txt"
        if not path.exists():
            return {}
        lo, hi = first.isoformat(), (last + dt.timedelta(days=1)).isoformat()
        historical = hi + "T00:00:00" < self.index.get("sacct", {}).get("to", "")
        out = {}
        for k, j in sacct.parse(path.read_text().splitlines(), self.cluster.name).items():
            ended = j.end if j.end[:1].isdigit() else "9999"
            if historical and ended >= hi:
                continue
            if ended >= lo and j.submit < hi:
                out[k] = j
        return out

    def prune(self, cutoff):
        """Drop usage days before `cutoff` and jobs that ended before it; keep unfinished jobs.

        Returns (days dropped, jobs dropped).
        """
        old_days = [d for d in self.index["sreport"] if d < cutoff.isoformat()]
        for d in old_days:
            del self.index["sreport"][d]
            self.usage().pop(d, None)
        if old_days:
            self.usage()  # make sure save() rewrites sreport.txt
        dropped = 0
        path = self.dir / "sacct.txt"
        if path.exists():
            groups = self._groups(path.read_text().splitlines())
            keep = {}
            for k, g in groups.items():
                end = g.get(k, "").split("|")[sacct.FIELDS.index("End")] if k in g else ""
                if end[:1].isdigit() and end < cutoff.isoformat():
                    dropped += 1
                else:
                    keep[k] = g
            if dropped:
                _write(path, "".join(line + "\n" for g in keep.values() for line in g.values()))
            cov = self.index.get("sacct")
            if cov:
                start = dt.datetime.combine(cutoff, dt.time())
                cov["from"] = _ts(max(_parse(cov["from"]), start))
                cov["jobs"] = len(keep)
        return len(old_days), dropped

    def put_queue(self, text, fetched):
        self.dir.mkdir(parents=True, exist_ok=True)
        _write(self.dir / "queue.txt", text)
        self.index["queue"] = _ts(fetched)

    def queue(self):
        """(squeue snapshot, when it was taken) from the last fetch; ("", None) if none."""
        f = self.dir / "queue.txt"
        return (f.read_text(), self.index["queue"]) if f.exists() and "queue" in self.index else ("", None)

    def summary(self):
        cov = self.index.get("sacct")
        days = self.sreport_days()
        jobs = f"jobs {cov['from']} .. {cov['to']} ({cov['jobs']:,} jobs)" if cov else "no jobs"
        usage = f"usage {days[0]} .. {days[-1]} ({len(days)} days)" if days else "no usage"
        return f"{jobs}; {usage}"
