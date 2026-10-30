"""Run with: python -m unittest discover tests"""
import tempfile
import unittest
from pathlib import Path

from drac_usage_reporter import cache, config, fetch, flags, render, sacct, slurm, tables

# Two jobs in the sacct.FIELDS layout: a 4x H100 job whose main step finished but the job is
# FAILED, and a CPU job. The first job is repeated, as happens when it spans two date windows.
SACCT = """\
10000001|alice|llm_batch|rrg-pi_gpu|FAILED|2:0|2026-10-01T09:00:00|2026-10-01T10:00:00|2026-10-01T12:00:00|7200|03:00:00|1|16|billing=48800,cpu=16,gres/gpu:h100=4,gres/gpu=4,mem=64G,node=1|billing=48800,cpu=16,gres/gpu:h100=4,gres/gpu=4,mem=64G,node=1|08:00:00||/home/alice/x
10000001.batch||batch|rrg-pi_gpu|FAILED|2:0||||7200||1|16||cpu=16,gres/gpu:h100=4,gres/gpu=4,mem=64G,node=1|00:10.5|612348K|
10000001.0||bash|rrg-pi_gpu|COMPLETED|0:0||||7200||1|16||cpu=16,gres/gpu:h100=4,gres/gpu=4,mem=64G,node=1|07:59:00|7G|
10000001|alice|llm_batch|rrg-pi_gpu|FAILED|2:0|2026-10-01T09:00:00|2026-10-01T10:00:00|2026-10-01T12:00:00|7200|03:00:00|1|16|billing=48800,cpu=16,gres/gpu:h100=4,gres/gpu=4,mem=64G,node=1|billing=48800,cpu=16,gres/gpu:h100=4,gres/gpu=4,mem=64G,node=1|08:00:00||/home/alice/x
70000001_3|bob|run|rrg-pi_cpu|CANCELLED by 123|0:15|2026-10-02T00:00:00|2026-10-02T00:00:00|2026-10-02T01:00:00|3600|1-00:00:00|1|192|billing=192,cpu=192,mem=384G,node=1|billing=192,cpu=192,mem=384G,node=1|2-16:00:00||/home/bob
"""


class SlurmParsing(unittest.TestCase):
    def test_seconds(self):
        self.assertEqual(slurm.seconds("1-02:03:04"), 93784)
        self.assertEqual(slurm.seconds("00:10.5"), 10.5)
        self.assertEqual(slurm.seconds(""), 0)
        self.assertEqual(slurm.seconds("UNLIMITED"), 0)

    def test_gib(self):
        self.assertAlmostEqual(slurm.gib("1048576K"), 1.0)
        self.assertEqual(slurm.gib("64G"), 64)
        self.assertEqual(slurm.gib("1.5T"), 1536)

    def test_gpu(self):
        t = slurm.tres("billing=12200,cpu=12,gres/gpu:nvidia_h100_80gb_hbm3_2g.20gb=1,gres/gpu=1,mem=3G")
        self.assertEqual(slurm.gpu_label(t), "2g.20gb")
        self.assertEqual(slurm.rgu_per_hour(t), 12.2)
        self.assertEqual(slurm.gpu_label(slurm.tres("cpu=4,mem=8G")), "cpu")


    def test_billing_weights(self):
        text = ("PartitionName=cpubase_bycore_b1 TRESBillingWeights=CPU=1000.0,Mem=250G\n"
                "PartitionName=gpubase_bygpu_b1 TRESBillingWeights=CPU=1016.67,Mem=42.36G,"
                "GRES/gpu:nvidia_h100_80gb_hbm3_3g.40gb=5228.57,GRES/gpu:h100=12200\n")
        core, gb, gpus = slurm.billing_weights(text)
        self.assertAlmostEqual(core, 1.01667)
        self.assertAlmostEqual(gb, 0.04236)
        self.assertEqual(set(gpus), {"gres/gpu:nvidia_h100_80gb_hbm3_3g.40gb", "gres/gpu:h100"})
        self.assertAlmostEqual(gpus["gres/gpu:nvidia_h100_80gb_hbm3_3g.40gb"], 5.22857)
        self.assertEqual(slurm.billing_weights("PartitionName=x TRESBillingWeights=CPU=1000")[2], {})


class SacctLoading(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        p = Path(self.tmp.name) / "sacct.txt"
        p.write_text(SACCT)
        self.jobs = sacct.load(p)

    def tearDown(self):
        self.tmp.cleanup()

    def test_jobs_and_steps(self):
        self.assertEqual(set(self.jobs), {"10000001", "70000001_3"})
        j = self.jobs["10000001"]
        self.assertEqual(j.gpu, "4x h100")
        self.assertAlmostEqual(j.rgu_hours, 97.6)
        self.assertEqual(set(j.main_steps), {"0"})
        self.assertEqual(j.max_rss_gib, 7)
        cpu = self.jobs["70000001_3"]
        self.assertEqual(cpu.state, "CANCELLED")
        self.assertEqual(cpu.rgu_hours, 0)
        self.assertEqual(cpu.core_hours, 192)

    def test_flags(self):
        cfg = config.load(config.EXAMPLE)
        lim = dict(cfg.flags, min_at_stake=0, masked_min_jobs=1)
        found = {(f.rule, f.user) for f in flags.find(self.jobs, cfg.cluster("fir"), lim)}
        self.assertIn(("multi_gpu", "alice"), found)
        self.assertIn(("masked_failures", "alice"), found)
        self.assertIn(("idle_cores", "bob"), found)  # 192 cores, 64 core-h used of 192

    def test_tables(self):
        self.assertIn("| FAILED | COMPLETED | 1 |", tables.step_outcomes(self.jobs, "alice"))
        self.assertIn("| bob | rrg-pi_cpu | 1 | 0 / 0 / 0 / 1 |", tables.by_user(self.jobs))


class Config(unittest.TestCase):
    def test_fir_defaults(self):
        c = config.load(config.EXAMPLE).cluster("fir")
        self.assertEqual(c.covers(c.gpus["2g.20gb"].rgu)[0], 3)
        self.assertEqual(c.covers(c.gpus["h100"].rgu)[0], 12)
        self.assertEqual(c.gpus["h100"].gres, "gres/gpu:h100")
        self.assertIn("rrg-<pi>_gpu", c.accounts)  # placeholder the user fills in
        self.assertEqual(c.ssh_command("hostname")[-2:], [c.host, "hostname"])

    def test_control_command(self):
        c = config.Cluster("fir", "fir.example.org", user="me",
                           ssh_options=["-o", "BatchMode=yes", "-o", "ControlPath=~/.ssh/cm-%r@%h", "-o",
                                        "ControlPersist=12h"])
        self.assertEqual(c.control_command("exit"),
                         ["ssh", "-o", "ControlPath=~/.ssh/cm-%r@%h", "-l", "me", "-O", "exit", "fir.example.org"])
        self.assertEqual(config.Cluster("fir", "fir").control_command("check"), ["ssh", "-O", "check", "fir"])

    def test_select(self):
        cfg = config.load(config.EXAMPLE)
        self.assertEqual([c.name for c in cfg.select("fir,narval")], ["fir", "narval"])
        self.assertEqual([c.name for c in cfg.select("all")], list(cfg.clusters))
        self.assertEqual([c.name for c in cfg.select()], cfg.default_clusters)
        with self.assertRaises(SystemExit):
            cfg.select("nosuch")

    def test_across_clusters(self):
        a = {("rrg-x_gpu", "u1"): {"billing": 2000}, ("rrg-x_cpu", "u1"): {"cpu": 10}}
        b = {("rrg-x_gpu", "u1"): {"billing": 1000}}
        t = tables.across_clusters([("fir", a), ("narval", b)])
        self.assertIn("| u1 | 2 | 1 | 3 | 10 | – | 10 |", t)

    def test_minimal_config(self):
        with tempfile.NamedTemporaryFile("w", suffix=".ini") as fh:
            fh.write("[cluster fir]\nhost = fir.example.org\nuser = someone\naccounts = a_gpu\n"
                     "[flags]\nmin_at_stake = 5\n")
            fh.flush()
            cfg = config.load(fh.name)
        c = cfg.cluster("fir")
        self.assertEqual((c.host, c.user, cfg.flags["min_at_stake"]), ("fir.example.org", "someone", 5))
        self.assertEqual(cfg.flags["idle_cpu"], 0.6)  # left out: built-in default
        self.assertEqual((cfg.windows, cfg.top, cfg.default_clusters), ([30, 90, 180], 25, ["all"]))
        self.assertIn("-l", c.ssh_command("x"))

    def test_missing_config(self):
        with self.assertRaises(SystemExit):
            config.load("/nonexistent/config.ini")

    def test_example_matches_defaults(self):
        import configparser
        cp = configparser.ConfigParser(delimiters=("=",), interpolation=None)
        cp.read(config.EXAMPLE)
        self.assertEqual(dict(cp["flags"]), config.DEFAULTS["flags"])


class Render(unittest.TestCase):
    MD = "# Title\n\n| Name | Count |\n| --- | ---: |\n| a | 1 |\n| **total** | **12** |\n"

    def test_table(self):
        out = render.pretty(self.MD, width=80, color=False)
        self.assertIn("┌", out)
        self.assertIn("│ a     │     1 │", out)
        self.assertIn("│ total │    12 │", out)
        self.assertNotIn("\033[", out)
        self.assertIn("\033[", render.pretty(self.MD, width=80, color=True))

    def test_wrap_fits_width(self):
        md = "| A | B |\n| --- | --- |\n| x | " + "word " * 40 + "|\n"
        self.assertTrue(all(len(line) <= 40 for line in render.pretty(md, width=40).splitlines()))

    def test_empty_tables(self):
        self.assertEqual(render.pretty("| A | B |\n| --- | ---: |\n", width=80), "(none)")
        self.assertEqual(render.pretty(flags.table([]), width=80), "(none)")

    def test_flag_cards(self):
        md = ("| Severity | Rule | User | Jobs | Detail | Suggestion |\n| --- | --- | --- | ---: | --- | --- |\n"
              "| high | idle_cores | bob | 97 | 33% CPU used | request fewer cores |\n")
        out = render.pretty(md, width=80)
        self.assertIn("● HIGH   idle_cores · bob · 97 jobs", out)
        self.assertIn("→ request fewer cores", out)


class Connections(unittest.TestCase):
    def setUp(self):
        self.c = config.Cluster("narval", "narval", ssh_options=["-o", "BatchMode=yes", "-o", "ConnectTimeout=30"])
        self.conns = fetch.Connections()
        self.conns.auto = True

    def run_ensure(self, already_open, tty=True, rc=0):
        from unittest import mock
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return mock.Mock(returncode=rc, stdout="", stderr="")
        with mock.patch.object(fetch, "shared_connection", return_value=(already_open, "")), \
                mock.patch.object(fetch.subprocess, "run", side_effect=fake_run), \
                mock.patch.object(fetch.sys.stdin, "isatty", return_value=tty), \
                mock.patch.object(fetch.sys.stderr, "isatty", return_value=tty), \
                mock.patch.object(fetch.sys, "stderr"):
            self.conns.ensure(self.c)
            self.conns.ensure(self.c)  # second call is a no-op
            self.conns.close_all()
        return calls

    def test_reuses_open_connection(self):
        self.assertEqual(self.run_ensure(already_open=True), [])
        self.assertEqual(self.c.control_path, "")

    def test_opens_and_closes_its_own(self):
        calls = self.run_ensure(already_open=False)
        self.assertEqual(len(calls), 2)
        opener, closer = calls
        self.assertIn("ControlMaster=yes", opener)
        self.assertNotIn("BatchMode=yes", opener)  # must be able to prompt for MFA
        self.assertEqual(opener[-3:], ["-f", "-N", "narval"])
        self.assertEqual(closer[-3:], ["-O", "exit", "narval"])
        # later commands go through the new connection, ahead of the configured options
        self.assertEqual(self.c.ssh_command("hostname")[1:5],
                         ["-o", f"ControlPath={self.c.control_path}", "-o", "ControlMaster=no"])

    def test_no_terminal_no_prompt(self):
        self.assertEqual(self.run_ensure(already_open=False, tty=False), [])

    def test_open_failure(self):
        with self.assertRaises(SystemExit):
            self.run_ensure(already_open=False, rc=255)


class Cache(unittest.TestCase):
    def setUp(self):
        import datetime as dt
        self.dt = dt
        self.tmp = tempfile.TemporaryDirectory()
        self.c = config.Cluster("fir", "fir")
        self.store = cache.Store(self.tmp.name, self.c)
        self.store.check_accounts(["rrg-x_gpu"])

    def tearDown(self):
        self.tmp.cleanup()

    def test_sreport_days_and_sums(self):
        dt = self.dt
        d1, d2 = dt.date(2026, 10, 27), dt.date(2026, 10, 28)
        now = dt.datetime(2026, 10, 29, 12)
        self.assertEqual(self.store.sreport_days_to_fetch(d1, d2, now), [d1, d2])
        self.store.put_sreport_day(d1, "rrg-x_gpu|u1|billing|60000\nrrg-x_gpu|u1|cpu|90\n", now)
        self.store.put_sreport_day(d2, "rrg-x_gpu|u1|billing|60000\n", now)
        self.store.save()
        # d1 ended more than a day before it was fetched: final. d2 did not: fetch again.
        self.assertEqual(self.store.sreport_days_to_fetch(d1, d2, now), [d2])
        again = cache.Store(self.tmp.name, self.c)  # reload from disk
        usage, missing = again.sreport(d1, dt.date(2026, 10, 29))
        self.assertEqual(usage[("rrg-x_gpu", "u1")], {"billing": 2000, "cpu": 2})  # minutes -> hours
        self.assertEqual(missing, [dt.date(2026, 10, 29)])
        self.assertEqual(sorted(p.name for p in again.dir.iterdir()), ["index.json", "sreport.txt"])

    def test_sacct_ranges_and_merge(self):
        dt = self.dt
        t0, t1, t2 = dt.datetime(2026, 10, 1), dt.datetime(2026, 10, 20), dt.datetime(2026, 10, 21)
        self.assertEqual(self.store.sacct_ranges(t0, t1), [(t0, t1)])
        old = SACCT.replace("FAILED|2:0|2026-10-01", "RUNNING|0:0|2026-10-01")
        old += "77_[1-3]|bob|arr|rrg-pi_cpu|PENDING|0:0|2026-10-02T00:00:00|Unknown|Unknown|0|1:00:00|1|1|cpu=1||||\n"
        self.store.put_sacct(old, [(t0, t1)])
        self.assertEqual(self.store.sacct_ranges(t0, t2), [(t1 - cache.OVERLAP, t2)])
        self.assertEqual(self.store.sacct_ranges(dt.datetime(2026, 9, 1), t2)[0], (dt.datetime(2026, 9, 1), t0))
        # Refresh: job 10000001 finished, array 77 started task 1 and still has 2-3 pending.
        new = [line for line in SACCT.splitlines() if line.startswith("10000001")]
        new += ["77_1|bob|arr|rrg-pi_cpu|RUNNING|0:0|2026-10-02T00:00:00|2026-10-20T00:00:00|Unknown|60|1:00:00|1|1|cpu=1|cpu=1|||",
                "77_[2-3]|bob|arr|rrg-pi_cpu|PENDING|0:0|2026-10-02T00:00:00|Unknown|Unknown|0|1:00:00|1|1|cpu=1||||"]
        self.store.put_sacct("\n".join(new), [(t1 - cache.OVERLAP, t2)])
        jobs = self.store.jobs(dt.date(2026, 10, 1), dt.date(2026, 10, 21))
        self.assertEqual(jobs["10000001"].state, "FAILED")
        self.assertIn("77_1", jobs)
        self.assertIn("77_[2-3]", jobs)
        self.assertNotIn("77_[1-3]", jobs)  # stale placeholder dropped
        self.assertEqual(self.store.index["sacct"]["from"], "2026-10-01T00:00:00")

    def test_prune(self):
        dt = self.dt
        now = dt.datetime(2026, 10, 21)
        for d in (dt.date(2026, 9, 1), dt.date(2026, 10, 15)):
            self.store.put_sreport_day(d, "a|u|cpu|60\n", now)
        running = "77_1|bob|arr|rrg-pi_cpu|RUNNING|0:0|2026-09-01T00:00:00|2026-09-01T00:00:00|Unknown|60|1:00:00|1|1|cpu=1|cpu=1|||"
        self.store.put_sacct(SACCT + running + "\n", [(dt.datetime(2026, 9, 1), now)])
        days, jobs = self.store.prune(dt.date(2026, 10, 2))
        self.store.save()
        self.assertEqual((days, jobs), (1, 1))  # Sep 1, and job 10000001 (ended Oct 1)
        self.assertEqual(self.store.sreport_days(), [dt.date(2026, 10, 15)])
        left = self.store.jobs(dt.date(2026, 9, 1), dt.date(2026, 10, 20))
        self.assertIn("70000001_3", left)  # ended Oct 2 01:00, after the cutoff
        self.assertIn("77_1", left)  # unfinished jobs are always kept
        self.assertEqual(self.store.index["sacct"]["from"], "2026-10-02T00:00:00")

    def test_gpu_types_change_refetches_usage(self):
        dt = self.dt
        self.store.check_gpu_types(["gres/gpu:h100"])
        self.store.put_sreport_day(dt.date(2026, 10, 1), "a|u|cpu|60\n", dt.datetime(2026, 10, 5))
        self.store.check_gpu_types(["gres/gpu:h100"])
        self.assertEqual(len(self.store.sreport_days()), 1)
        self.store.check_gpu_types(["gres/gpu:a100", "gres/gpu:h100"])
        self.assertEqual(self.store.sreport_days(), [])

    def test_accounts_change_resets(self):
        dt = self.dt
        self.store.put_sreport_day(dt.date(2026, 10, 1), "a|u|cpu|60\n", dt.datetime(2026, 10, 5))
        self.store.save()
        s2 = cache.Store(self.tmp.name, self.c)
        s2.check_accounts(["other_gpu"])
        self.assertEqual(s2.sreport_days(), [])

    def test_historical_cutoff_excludes_later_statistics(self):
        dt = self.dt
        self.store.put_sacct(SACCT, [(dt.datetime(2026, 10, 1), dt.datetime(2026, 10, 5))])
        # Bob was pending on Oct 1, but his cached usage and outcome are from Oct 2.
        jobs = self.store.jobs(dt.date(2026, 10, 1), dt.date(2026, 10, 1))
        self.assertEqual(set(jobs), {"10000001"})
        running = SACCT.splitlines()[0].replace("2026-10-01T12:00:00", "Unknown")
        self.store.put_sacct(running, [(dt.datetime(2026, 10, 1), dt.datetime(2026, 10, 5))])
        self.assertEqual(self.store.jobs(dt.date(2026, 10, 1), dt.date(2026, 10, 1)), {})
        self.assertIn("10000001", self.store.jobs(dt.date(2026, 10, 1), dt.date(2026, 10, 5)))


class Queue(unittest.TestCase):
    def test_other_states_do_not_count_as_pending(self):
        text = "\n".join(f"u|a_cpu|{state}|N/A|N/A|1|cpu=4|4|None"
                         for state in ("COMPLETING", "SUSPENDED", "CONFIGURING"))
        self.assertNotIn("| u |", tables.queue_summary(text))

    def test_cpu_and_gpu(self):
        text = "\n".join([
            "a|rrg-x_gpu|RUNNING|gres/gpu:h100:1|N/A|1|cpu=12,gres/gpu=1|12|None",
            "a|rrg-x_gpu|PENDING|gres/gpu:h100:2|N/A|1|cpu=24,gres/gpu=2|24|ReqNodeNotAvail, May be reserved",
            "b|rrg-x_cpu|PENDING|N/A|N/A|1|cpu=96,mem=192G|96|Priority",
            "b|rrg-x_cpu|PENDING|N/A|N/A|1|cpu=96,mem=192G|96|Priority",
            "b|rrg-x_cpu|PENDING|N/A|N/A|1|cpu=2,mem=8G|2|Dependency",
        ])
        t = tables.queue_summary(text)
        self.assertIn("| a | rrg-x_gpu | 1 | 1 | 12 | 1 | 2 | 24 | ReqNodeNotAvail |", t)
        self.assertIn("| b | rrg-x_cpu | – | – | – | 3 | – | 194 | Priority (2), Dependency (1) |", t)


class Fetch(unittest.TestCase):
    def test_failure_with_stdout_is_rejected(self):
        from unittest import mock
        with mock.patch.object(fetch.CONNECTIONS, "ensure"), \
                mock.patch.object(fetch.subprocess, "run", return_value=mock.Mock(
                    returncode=1, stdout="partial records", stderr="database unavailable")):
            with self.assertRaises(SystemExit):
                fetch.ssh(config.Cluster("fir", "fir"), "sacct")

    def test_batched_commands_stop_on_first_failure(self):
        import datetime as dt
        from unittest import mock
        # Execute the actual generated shell batch locally. The first command fails;
        # a later successful command must not mask that failure.
        for command in ("sreport", "sacct"):
            with self.subTest(command=command):
                c = config.Cluster("fir", "fir")
                shell = f"n=0; {command}() {{ n=$((n+1)); [ $n -gt 1 ]; }}; "
                with mock.patch.object(fetch.CONNECTIONS, "ensure"), \
                        mock.patch.object(c, "ssh_command", side_effect=lambda cmd: ["sh", "-c", shell + cmd]):
                    with self.assertRaises(SystemExit):
                        if command == "sreport":
                            fetch.sreport_days(c, ["a"], [dt.date(2026, 10, 1), dt.date(2026, 10, 2)])
                        else:
                            fetch.jobs(c, ["a"], [(dt.datetime(2026, 10, 1), dt.datetime(2026, 10, 25))])

    def test_date_chunks(self):
        import datetime as dt
        c = fetch.date_chunks(dt.date(2026, 1, 1), dt.date(2026, 1, 25), 10)
        self.assertEqual([(str(a), str(b)) for a, b in c],
                         [("2026-01-01", "2026-01-11"), ("2026-01-11", "2026-01-21"), ("2026-01-21", "2026-01-25")])


if __name__ == "__main__":
    unittest.main()
