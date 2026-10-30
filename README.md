# Drac Usage Reporter

Usage audits of a research group's Slurm accounts on Digital Research Alliance of Canada (DRAC)
clusters: who uses how much GPU (RGU) and CPU time on each cluster, what jobs ask for, how well
they use it, and flags for request patterns that waste allocation or hide failures. Comes configured for fir and
narval; other clusters take a short config section.

**New here? Start with [docs/usage.md](docs/usage.md)**, a step-by-step guide covering
[ssh and MFA setup](docs/usage.md#2-set-up-ssh), configuration, fetching, reports, flags, adding
a cluster and troubleshooting.

Once ssh works, one command does everything:

```bash
python -m drac_usage_reporter
```

It updates the cache, prints the report (ending with the queue: running and pending CPU and GPU
jobs), and saves the report to
`reports/auto/<date>_<clusters>.md`.

## Commands

```bash
python -m drac_usage_reporter                        # do it all: fetch + report (with queue), saved to reports/auto/
python -m drac_usage_reporter check                  # every configured cluster: ssh, accounts, billing rates
python -m drac_usage_reporter fetch                  # update the local cache: only what is new or may still change
python -m drac_usage_reporter report                 # group report from the cache
python -m drac_usage_reporter report --user alice    # one user: flags, request shapes, programs, outcomes
python -m drac_usage_reporter queue                  # live queue: running and pending CPU and GPU jobs
```

| Option | Commands | Meaning |
|---|---|---|
| `--cluster C` | every command, including the bare do-it-all | a cluster, a comma-separated list, or `all` (default: `[report] clusters`; `check` defaults to `all`) |
| `--date DATE` | `report` | last day of the report windows (default: the newest cached day) |
| `--user U` | `report` | report on one user |
| `--csv FILE` | `report` | also write the started jobs (of `--user`, if given) as CSV |
| `--config FILE` | all | extra config file, overriding the others |

Everything else (accounts, ssh, windows, data folder, billing rates, flag thresholds) lives in
the config.

- **Output:** in a terminal, output is pretty-printed: boxed tables, colour, and flags as cards.
  Piped or redirected (`report > reports/2026-10-29_fir.md`), it is markdown.
  `NO_COLOR=1` turns colour off.
- **Several clusters:** with more than one cluster, `report` adds a Cluster column to the flags
  and an **All clusters** table of RGU-h and core-h per user, with totals. RGU is the same unit
  on every DRAC cluster, so the totals add up.
- **Cache:** `fetch` keeps everything it pulls in `data/cache/<cluster>/`, three files per cluster.
  Each run asks the cluster only for what's missing or may still change, so after the first fill
  (about 1.5 min for 180 days on fir) a fetch takes seconds. `report` reads only the cache, so
  `report --date` can show any past window you have cached without fetching again. The cache
  rolls: `cache_days` (default 365) sets how far back it keeps data.
- **Unreachable clusters:** a cluster that can't be reached, or has no fetched data, is skipped
  with a message. The others still run.

## Configuration

Settings live in `config.ini`, which is git-ignored. Start from the committed example, which has
fir and narval with their billing rates; set your login, key path and group account names:

```bash
cp config.example.ini config.ini
```

Anything you leave out of `[report]` and `[flags]` falls back to a built-in default, so a minimal
`config.ini` is just the `[cluster ...]` sections. To use a different file, pass `--config FILE`
or set `$DRAC_USAGE_CONFIG`. Without a config file, the tool says how to create one.

```ini
[report]
clusters = fir, narval      # default for --cluster; `all` = every [cluster] section
windows = 30, 90, 180       # sreport windows in days; sacct covers the longest
data_dir = data/cache       # local cache: fetch updates it, report reads it
cache_days = 365            # rolling cache: older usage and finished jobs are dropped
top = 25                    # request shapes listed per cluster

[cluster fir]
host = fir.alliancecan.ca   # direct SSH hostname
user = <ccdb_username>      # your CCDB login
identity_file = ~/.ssh/<ssh_key>
ssh_options = -o BatchMode=yes -o ControlMaster=auto -o ControlPath=~/.ssh/cm-%r@%h -o ControlPersist=12h
accounts = rrg-<pi>_gpu, def-<pi>_gpu, rrg-<pi>_cpu, def-<pi>_cpu
sacct_chunk_days = 10
rgu_per_core = 1.01667      # billing weights from Slurm's TRESBillingWeights (`check` verifies them)
rgu_per_gb = 0.04236
gpu h100 = 12.2, gres/gpu:h100

[flags]
min_at_stake = 200          # see docs/method.md#flags for every threshold
```

To add a cluster, add a `[cluster <name>]` section. `check` then tells you whether the accounts
are right, and prints the billing-rate lines to paste in if they're missing or out of date.

**ssh connections and MFA.** DRAC clusters ask for MFA on each new login. The tool logs in once
per cluster and runs everything through that one connection:

- **Already open:** if a shared connection (ssh ControlMaster) is open, for example one you
  opened yourself, the tool uses it and leaves it open.
- **Not open:** in a terminal, the tool opens one (`auto_connect = yes`, the default). ssh asks
  for MFA, or to accept a new host key, as usual, and the tool closes the connection when the
  command ends, even after an error or Ctrl-C.
- **No terminal** (cron, pipes): the tool can't answer prompts, so you need to keep a connection
  open yourself.

`check` shows each cluster's connection and how to close one you opened (`ssh -O exit <host>`).

## Flags

The report starts with flags. Started jobs are grouped by request shape, and these rules are
applied. Thresholds are in `[flags]`; details are in [docs/method.md](docs/method.md#flags).

| Rule | Flags | At stake |
|---|---|---|
| `rate_above_gpu` | GPU jobs billed above the GPU's price because of cores or memory beyond what it covers | extra RGU-h |
| `idle_cores` | CPU jobs using less than `idle_cpu` of their cores | idle core-h |
| `failures` | shapes where ≥ `fail_rate` of jobs really failed (flags memory-limit hits) | cost of failed jobs |
| `timeouts` | shapes where ≥ `timeout_rate` of jobs timed out (capped at medium: may be checkpointing) | cost of timed-out jobs |
| `multi_gpu` | jobs holding several GPUs, whose use sacct cannot confirm (capped at medium) | cost of all but one GPU |
| `masked_failures` | jobs marked FAILED although the work step completed (capped at low) | cost of those jobs |

`report --user U` shows every flag for that user, with no minimum.

## Layout

```
config.example.ini          settings template (copy to config.ini, which is git-ignored)
drac_usage_reporter/        Python package (standard library only, Python 3.10+)
  __main__.py               the four commands
  config.py                 load the config files, pick clusters
  fetch.py                  pull sreport / sacct / squeue / partition output over ssh
  cache.py                  local cache; works out what the next fetch needs
  sacct.py                  load sacct dumps into Job objects (one per job, with its steps)
  slurm.py                  parse durations, memory sizes, TRES strings, billing weights
  tables.py                 markdown tables and per-job CSVs
  flags.py                  rule-based flags
  render.py                 terminal rendering (boxed tables, flag cards, colour)
docs/
  usage.md                  step-by-step guide
  method.md                 data sources, RGU billing, flag rules, column definitions
reports/                    dated reports with findings; reports/auto/ holds the do-it-all ones
data/cache/<cluster>/       local cache: index.json, sacct.txt, sreport.txt (git-ignored)
tests/                      python -m unittest discover tests
```

## Not done yet

- Narval's rates in `config.example.ini` were inferred from job billing; `check --cluster narval`
  compares them with narval's Slurm config. (Rates come from each cluster's Slurm config, since the
  DRAC documentation site blocks automated fetches.)
- Flags on the live queue (catch an expensive request shape before it runs).

## License

MIT, see [LICENSE](LICENSE).
