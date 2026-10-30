# How to use drac_usage_reporter

This guide goes from a fresh checkout to a dated usage report, then covers adding a cluster and
common problems. There are four commands, `check`, `fetch`, `report` and `queue`. Running the
tool with no command does everything at once:

```bash
python -m drac_usage_reporter                    # or: ... --cluster fir,narval
```

This fetches (step 5), prints the report (step 6, ending with the queue as just fetched), and saves the
report as markdown to `reports/auto/<date>_<clusters>.md` (`[report] reports_dir`). Running it
again on the same day replaces that day's file. Hand-written reports go in `reports/` and are
never touched. The [README](../README.md) has the short command reference;
[method.md](method.md) explains where the numbers come from.

## 1. Requirements

- **Python 3.10 or newer.** Standard library only, so there is nothing to install. Run every
  command from the repo root as `python -m drac_usage_reporter ...`.
- **An account on the cluster that belongs to the group's Slurm accounts** (typically
  `rrg-<pi>_gpu`, `def-<pi>_gpu`, `rrg-<pi>_cpu`, `def-<pi>_cpu`, where `<pi>` is your PI's
  username; `check` lists the accounts you belong to). Slurm shows
  you every user's jobs in accounts you belong to, and no others.
- **ssh access to the clusters' login nodes** with a key registered in CCDB, and MFA set up
  (step 2).

## 2. Set up ssh

The tool runs Slurm commands on each cluster's login node over ssh. This is a one-time setup per
computer. If `ssh fir` already logs you in, skip to 2.4.

### 2.1 Create a key

```bash
ssh-keygen -t ed25519 -f ~/.ssh/<ssh_key> -C "<ccdb_username>@<this computer>"
```

Give it a passphrase. To avoid typing the passphrase for every connection, load the key into
`ssh-agent` (`ssh-add ~/.ssh/<ssh_key>`). The private key must be readable only by you:

```bash
chmod 700 ~/.ssh && chmod 600 ~/.ssh/<ssh_key>
```

On WSL, keep keys in the Linux home (`~/.ssh`), not under `/mnt/c/...`. Windows drives don't keep
Unix permissions, and ssh refuses keys that others can read.

### 2.2 Register the key and MFA in CCDB

Alliance clusters only accept keys registered in your CCDB account, and they require
multifactor authentication.

1. Log in to [CCDB](https://ccdb.alliancecan.ca). Under **My Account → SSH Keys**, add the
   contents of `~/.ssh/<ssh_key>.pub` (the `.pub` file, never the private key). It can take a
   few minutes to reach the clusters.
2. Under **My Account → Multifactor authentication management**, enroll a device (the Duo
   Mobile app or a hardware key) if you haven't yet.

### 2.3 Add the clusters to `~/.ssh/config`

```
Host fir
  HostName fir.alliancecan.ca
  User <ccdb_username>
  IdentityFile ~/.ssh/<ssh_key>
  IdentitiesOnly yes
  ServerAliveInterval 60

Host narval
  HostName narval.alliancecan.ca
  User <ccdb_username>
  IdentityFile ~/.ssh/<ssh_key>
  IdentitiesOnly yes
  ServerAliveInterval 60
```

The alias (`fir`, `narval`) is what `host =` in the tool's config refers to; by default the
tool uses the cluster's section name. If you'd rather not edit `~/.ssh/config`, set `host`,
`user` and `identity_file` in `config.ini` instead (step 3).

### 2.4 First login

Log in once by hand to each cluster:

```bash
ssh fir
```

- **Host key:** the first time, ssh asks *"Are you sure you want to continue connecting?"*.
  Answer `yes`, and ssh records the cluster's host key in `~/.ssh/known_hosts`.
- **MFA:** approve the request, for example with a push to your phone.
- You land on a login node (`login1`, `login2`, ...). Type `exit`.

If it fails, run `ssh -v fir` to see why. The usual causes are a key not (yet) registered in
CCDB, the wrong `User`, or key file permissions.

### 2.5 How the tool uses ssh

Because of MFA, the tool logs in once per cluster and sends every command through that one
shared connection (ssh ControlMaster).

- **You run a command in a terminal and no connection is open:** the tool opens one. You see
  ssh's usual prompts (host key the first time, then MFA). When the command ends, even after an
  error or Ctrl-C, it closes that connection again (`[narval] closed the ssh connection this run
  opened`).
- **A connection is already open:** the tool uses it and leaves it open. That's handy when you
  run several commands in a row: you approve MFA once. To keep connections open yourself, add
  this to each `Host` block:

  ```
    ControlMaster auto
    ControlPath ~/.ssh/cm-%r@%h
    ControlPersist 12h
  ```

  ```bash
  ssh fir exit          # log in once; the connection stays open for 12 h
  ssh -O check fir      # is it open?
  ssh -O exit fir       # close it now
  ```
- **No terminal** (cron jobs, scripts): the tool can't answer prompts, so keep a connection open
  as above. Set `auto_connect = no` in `[report]` to turn automatic connections off entirely.

### 2.6 Check it

```bash
python -m drac_usage_reporter check
```

For each cluster, this should print `connected: login…`, your account memberships, and the
state of the shared connection, including the command to close it (step 4).

## 3. Configure

Settings live in `config.ini` in the repo root, which is git-ignored. Start from the committed
example:

```bash
cp config.example.ini config.ini
```

[`config.example.ini`](../config.example.ini) explains every setting and has working sections for
fir and narval, including their billing rates. Replace `<pi>` in the account names with your
group's, and edit anything else that differs for you. Anything left out of `[report]` and `[flags]` falls back to a built-in default, so a
config can be as small as:

```ini
[report]
clusters = fir                  # what --cluster means when you leave it out (default: all)

[cluster fir]
host = fir.alliancecan.ca       # not needed if "fir" is an alias in ~/.ssh/config
user = <ccdb_username>
identity_file = ~/.ssh/<ssh_key>
accounts = rrg-<pi>_gpu, def-<pi>_gpu, rrg-<pi>_cpu, def-<pi>_cpu
```

`check` (step 4) then prints the billing-rate lines to add.

The `[report]` section also sets the sreport `windows` (default 30, 90, 180 days), the `data_dir`
for the local cache (default `data/cache`), `cache_days`, `reports_dir`, `top` (the number of
request shapes per table), and `auto_connect`. To use a different file, pass `--config FILE` or set
`$DRAC_USAGE_CONFIG`.

## 4. Check

```bash
python -m drac_usage_reporter check                    # every configured cluster
python -m drac_usage_reporter check --cluster narval   # just one
```

This prints which config files were read, the data folder, windows, flag thresholds, and the
configured clusters, marking the defaults that `fetch`, `report` and `queue` use. Then, for
each cluster, it:

- shows what the local cache holds (step 5);
- shows the exact ssh command and connects;
- marks each configured account `member` or `NOT a member`, and lists other accounts you belong
  to;
- says whether a shared ssh connection (ControlMaster) is open, and prints the command to close
  it, e.g. `ssh -O exit fir`;
- compares the billing rates in the config with the cluster's own Slurm configuration
  (`TRESBillingWeights`).

When a cluster can't be reached, `check` says why and how to fix it (for example, "accept the host
key once: run `ssh narval` in a terminal and answer yes"), then moves on to the next cluster.

If the rates are missing or out of date, `check` prints the lines to paste into the cluster's
section:

```ini
rgu_per_core = 1.01667
rgu_per_gb = 0.04236
gpu h100 = 12.2, gres/gpu:h100
gpu 3g.40gb = 5.22857, gres/gpu:nvidia_h100_80gb_hbm3_3g.40gb
...
```

Without rates, the report still shows billed RGU-h, but the GPU-type columns and the
`rate_above_gpu` flag are missing. Run `check` now and then to catch rate changes.

## 5. Fetch the data

```bash
python -m drac_usage_reporter fetch
python -m drac_usage_reporter fetch --cluster all
```

`fetch` keeps a local cache in `<data_dir>/<cluster>/` (default `data/cache/fir/`) with three
files:

| File | Content |
|---|---|
| `sacct.txt` | the newest record of every job and job step |
| `sreport.txt` | usage per day, user, account and resource (one line each) |
| `queue.txt` | the queue (running and pending jobs) at the last fetch |
| `index.json` | accounts, the time range the job records cover, and when each day and the queue were fetched |

Each run works out what is missing or may still have changed, and asks the cluster only for
that:

- **Job records:** everything since the previous fetch, plus an hour of overlap. Slurm returns
  every job that was pending or running in that time, so new, started, finished and requeued jobs
  are all caught. Older history is only fetched when a window reaches further back than the
  cache.
- **Usage:** the days in the longest window that are missing, or that were fetched less than a
  day after they ended. sreport's rollups lag a few hours, so in practice today and yesterday
  are fetched again.

The first fill of 180 days takes about 1.5 minutes on fir. After that, a fetch takes a few
seconds:

```
[fir] usage: 2 of 180 days to fetch; jobs: 2026-10-29 19:02 .. 2026-10-29 20:04
[fir] cache: jobs 2026-05-03T00:00:00 .. 2026-10-29T20:04:18 (4,987 jobs); usage 2026-05-03 .. 2026-10-29 (180 days)
```

The cache rolls. After each fetch, usage days and finished jobs older than `cache_days`
(`[report]`, default 365) are dropped; jobs still pending or running are kept. `cache_days` is
never less than the longest window, and `report --date` can only look back as far as it.

```
[fir] dropped 150 days of usage and 3137 jobs older than 30 days (cache_days)
```

Times are the cluster's local time. If you change a cluster's `accounts`, its cache starts over.
To force a complete refetch, delete `data/cache/<cluster>/`.

`data/` is git-ignored. It holds every user's job names and working directories, so keep it out
of any repo you push.

## 6. Make the report

```bash
python -m drac_usage_reporter report                                   # in the terminal
python -m drac_usage_reporter report > reports/2026-10-29_fir.md       # markdown file
python -m drac_usage_reporter report --cluster all                     # several clusters in one report
python -m drac_usage_reporter report --date 2026-09-30                 # windows ending on an earlier day
```

`report` reads only the cache. Its windows end on the newest cached day, or on `--date`, so you
can look at any earlier period the cache covers without fetching. In a terminal the
output is pretty-printed: boxed tables wrapped to the window width, colour, and flags as cards.
Redirected to a file it is markdown, ready to commit. `NO_COLOR=1` turns colour off.

The report has these sections:

1. **Flags:** request patterns worth a look, most serious first (see step 7).
2. **All clusters** (only with several clusters): RGU-h and core-h per user per cluster, with
   totals.
3. **Per cluster:**
   - GPU usage per window: GPU-hours by type, RGU-h billed, share of the account.
   - CPU core-hours per window.
   - Jobs by user and account: outcomes, hours, cost, how much of the reserved CPU and memory
     was used.
   - The most expensive request shapes, in two tables: GPU jobs ranked by RGU-h, and CPU-only
     jobs ranked by core-h (`top` of each, default 25).
   - The queue at the last fetch: running and pending jobs per user and account, with their
     GPUs and cores, and why the pending ones wait (`Priority`, `Dependency`,
     `ReqNodeNotAvail`, ...).

The generated tables are a starting point. Add a short **Findings** section that interprets them,
as in [2026-10-29_fir.md](../reports/2026-10-29_fir.md).

## 7. Read the flags

Jobs are grouped by request shape: user, account, GPU, cores and memory. Each flag says how much
is "at stake" (in RGU-h or core-h), what it saw, and what to try.

| Rule | What it means | What to do |
|---|---|---|
| `rate_above_gpu` | The job asks for more cores or memory than the GPU price covers, so it pays the core or memory charge. | Request no more cores and memory than the GPU price covers, or move CPU-bound work to CPU nodes. |
| `idle_cores` | Most reserved cores sat idle (CPU accounts). | Request fewer cores, or check that the program scales. |
| `failures` | Many jobs of this shape really failed. It notes jobs that reached their memory limit. | Read the job logs; raise `--mem` if jobs hit the limit. |
| `timeouts` | Many jobs hit the time limit. | Nothing, if they checkpoint and resume. Otherwise raise the limit or split the work. |
| `multi_gpu` | Jobs hold several GPUs; Slurm can't show whether they all work. | Check the program's GPU settings, or run `nvidia-smi` on the node during a run. |
| `masked_failures` | Jobs are marked FAILED although their work step finished. | Fix the failing command at the end of the batch script. |

Severity is high, medium or low depending on the amount at stake. `timeouts` and `multi_gpu` stop
at medium and `masked_failures` at low, because these can be intended. Thresholds are in the
`[flags]` section of the config.

Flags only point at places to look. Before you tell someone their jobs are wasteful, confirm it
with a user report (step 8), and with the user.

## 8. One user

```bash
python -m drac_usage_reporter report --user alice
python -m drac_usage_reporter report --user alice --csv alice_jobs.csv
```

A user report shows:

- **Flags:** every flag for that user, with no minimum.
- **Request shapes:** each combination of GPU, cores and memory, with outcomes, hours, cost and
  how much was used, in separate tables for GPU jobs and CPU-only jobs.
- **GPU jobs by program:** grouped by what the job steps ran (`gmx_mpi`, `python`, ...).
- **Job outcomes by job name:** the job's final state next to the state of the step that did the
  work. This catches jobs marked FAILED whose work actually finished.

`--csv` writes one row per started job: cluster, ID, request, billing rate, state, work-step
state, times, CPU used, peak memory and working directory. Open it in a spreadsheet for
anything the tables don't cover. Without `--user`, it writes every user's jobs.

For a single job, `sacct` on the cluster gives the full record. For example:

```bash
ssh fir 'sacct -j 10000001 -P --format=JobID,ReqTRES,AllocTRES,NodeList,SubmitLine'
```

## 9. Live queue

```bash
python -m drac_usage_reporter queue --cluster all
```

This shows the queue right now, for all the group's accounts, CPU and GPU:

```
| User    | Account          | Jobs running | GPUs running | Cores running | Jobs pending | GPUs pending | Cores pending | Pending because                   |
| alice   | rrg-<pi>_gpu |            3 |            3 |            18 |            5 |            5 |            30 | Dependency                        |
| bob     | rrg-<pi>_gpu |            – |            – |             – |            7 |            7 |            42 | ReqNodeNotAvail                   |
| alice   | rrg-<pi>_cpu |            – |            – |             – |          272 |            – |        25,360 | Priority (264), Dependency (8)    |
| carol   | rrg-<pi>_cpu |           31 |            – |         2,404 |            – |            – |             – |                                   |
```

Each array task counts as a job. The report shows the same table as of the last fetch.

## Adding a cluster

To add narval, for example:

1. Add an ssh alias (step 2) and log in once to accept the host key and approve MFA.
2. Add a section to your `config.ini`:

   ```ini
   [cluster narval]
   host = narval
   accounts = rrg-<pi>_gpu, def-<pi>_gpu, rrg-<pi>_cpu, def-<pi>_cpu
   ```
3. Run `check --cluster narval`. Correct the account list, and paste in the billing-rate lines it
   prints.
4. Run `fetch --cluster narval`, then `report --cluster fir,narval`. To make that the default,
   set `[report] clusters = fir, narval`.

If `sacct` on that cluster complains about the date range, lower `sacct_chunk_days`.

## Troubleshooting

| Message | Cause | Fix |
|---|---|---|
| `Host key verification failed` | First connection to this cluster, not run in a terminal | Run the command in a terminal and answer `yes`, or `ssh <host>` once |
| `Permission denied (keyboard-interactive)` or a timeout | MFA not approved, or no terminal to ask in | Run in a terminal, or keep a connection open (step 2) |
| `unknown cluster 'x'` | No `[cluster x]` section | Add one, or check the spelling with `check` |
| `NOT a member` in `check` | Wrong account name, or you're not in it | Correct `accounts =`; ask the PI to add you |
| `Too wide of a date range` | sacct window too long | Lower `sacct_chunk_days` |
| A user is missing from the job tables | `sacct` keeps fewer records than `sreport` | Use the sreport tables for totals (see method.md) |
| GPU type columns are empty | No `gpu ...` lines for the cluster | Run `check` and paste the rate lines (step 4) |
| `nothing cached ...; run fetch first` or `... days are not cached` | The cache doesn't cover that cluster or period | Run `fetch` |

## Tests

```bash
python -m unittest discover tests
```
