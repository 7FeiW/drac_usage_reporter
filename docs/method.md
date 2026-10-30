# How the usage numbers are computed

## Where the numbers come from

| Question | Source | Command |
|---|---|---|
| How much did each user use? | `sreport` (accounting rollups) | `sreport -t hours cluster AccountUtilizationByUser accounts=... -T cpu,billing,gres/gpu,...` |
| What did each job ask for, and how did it end? | `sacct` (per-job records) | `sacct -a -A ... -S ... -E ... -P --format=...` (fields in `drac_usage_reporter/sacct.py`) |
| What is queued right now? | `squeue` | `squeue -r -A ... -O ...` |
| What sbatch options were given on the command line? | `sacct` | `sacct -j <ids> -X -n -P --format=JobID,SubmitLine` |

- `sreport` is the authoritative total, but it usually lags by a few hours.
- `sacct` covers fewer jobs than `sreport` (some users' jobs were missing from it on fir). Use it for
  patterns such as request shapes, outcomes and efficiency, not for exact totals.
- Fir rejects `sacct` ranges wider than roughly 10–30 days ("Too wide of a date range"), so
  `drac_usage_reporter fetch` asks for 10-day windows. A job that spans two windows appears twice; the loader
  keeps one copy.
- Each array task counts as one job.
- The cache keeps the latest job records, not historical snapshots. For a window ending before
  the latest sacct fetch, job tables, flags and CSV exports include only jobs that finished by
  the cutoff. Jobs still pending or running at that cutoff are omitted because their earlier
  resource use and state cannot be recovered from the cache. Job costs are lifetime costs;
  they are not clipped to the window. Use sreport tables for usage within the date window.

## RGU billing on fir

Slurm stores `billing` × 1000, so **RGU-hours = billing-hours / 1000**. Slurm prices each job by the largest of three charges: its GPUs, its cores and its memory. The
weights come from the partitions' `TRESBillingWeights` (`scontrol show partition -o`), read on
2026-10-29:

| Resource | RGU/h | GPU price covers |
|---|---|---|
| H100 80GB (full) | 12.2 | 12 cores, 288G |
| H100 MIG 3g.40gb | 5.229 (3/7 H100) | 5 cores, 123G |
| H100 MIG 2g.20gb | 3.486 (2/7 H100) | 3 cores, 82G |
| H100 MIG 1g.10gb | 1.743 (1/7 H100) | 1 core, 41G |
| 1 core | 1.0167 | |
| 1 GB memory | 0.04236 | |

A core costs about 1 RGU/h on any GPU type, so asking for more cores than the GPU price covers raises
the rate to the core charge. For example, a 2g.20gb slice with 12 cores bills 12.2 RGU/h, the same
as a full H100, and a 3g.40gb slice with 6 cores bills 6.1 RGU/h instead of 5.229.

The weights are set per cluster in `config.ini` (`rgu_per_core`, `rgu_per_gb`, `gpu <type>`
lines). `python -m drac_usage_reporter check --cluster <name>` compares them with the cluster's
Slurm config and prints config lines when they differ, which is how the fir values were filled in. The DRAC
documentation site blocks automated fetches, so the rates were not taken from it. GPU-hours ×
GPU price can differ from the billed RGU-h, because jobs with extra cores pay the core charge.
The billed number is the one to trust.

## Reading what a job was given

The `ReqTRES` (requested) and `AllocTRES` (granted) fields of `sacct` show the resources, and the
per-step lines show what each step held. For example:

```
$ sacct -j 10000001 -n -P --format=JobID,ReqTRES,AllocTRES,NNodes,NodeList
10000001|billing=48800,cpu=16,gres/gpu:h100=4,gres/gpu=4,mem=64G,node=1|billing=48800,cpu=16,gres/gpu:h100=4,gres/gpu=4,mem=64G,node=1|1|fc10001
10000001.0||cpu=16,gres/gpu:h100=4,gres/gpu=4,mem=64G,node=1|1|fc10001
```

- `gres/gpu:h100=4` means 4 full H100s. MIG slices show up as
  `gres/gpu:nvidia_h100_80gb_hbm3_3g.40gb` and so on.
- `node=1` means all of them are on one node. A fir GPU node has 4 H100s.
- `billing=48800` = 4 × 12,200, so the job was charged 48.8 RGU/h.
- When `SubmitLine` has no GPU option, the request came from `#SBATCH` lines in the script.

This proves the job **held and paid for** the GPUs, not that it **used** them. Fir's `sacct`
does not record GPU utilization. To check that, look at the program's settings (for example
vLLM's tensor-parallel size), or run `nvidia-smi` on the node while the job runs
(`srun --jobid <id> --overlap nvidia-smi`).

## Job state vs. step state

The job's final state reflects the batch script's last command, so it can hide what happened in
the work itself. `drac_usage_reporter steps` compares the job state with the state of its main steps
(everything except `batch` and `extern`):

| Job state | Main step | Meaning |
|---|---|---|
| FAILED | COMPLETED | Work finished; a later command in the script failed |
| FAILED | CANCELLED | Main step was killed partway |
| FAILED | FAILED | Real failure |
| TIMEOUT | CANCELLED | Hit the time limit |
| CANCELLED | none | Cancelled while pending (no cost) |

## Flags

`flags.py` groups started jobs by request shape (user, account, GPU, cores, memory) and applies
the rules below. "At stake" is in RGU-h on GPU accounts and core-h on CPU accounts. Severity is
high at or above `[flags] high` (10,000), medium at or above `medium` (1,000), and low otherwise.
Groups below `min_at_stake` (200) are dropped. Some rules cap the severity because the cost may be
intended.

| Rule | Condition | At stake | Cap |
|---|---|---|---|
| `rate_above_gpu` | billing rate > GPU price × GPU count | (rate − GPU price) × hours | – |
| `idle_cores` | CPU account, `TotalCPU` ÷ core-h < `idle_cpu` (0.6) | idle core-h | – |
| `failures` | ≥ 3 jobs and ≥ `fail_rate` (10%) FAILED / OOM / NODE_FAIL with the work step not completed | cost of those jobs | – |
| `timeouts` | ≥ 3 jobs and ≥ `timeout_rate` (30%) TIMEOUT | cost of those jobs | medium |
| `multi_gpu` | ≥ 2 GPUs per job | cost × (GPUs − 1) ÷ GPUs | medium |
| `masked_failures` | ≥ `masked_min_jobs` (5) jobs FAILED whose work step COMPLETED, per user and account | cost of those jobs | low |

The suggestion column depends on the evidence:

- **`rate_above_gpu`:** "CPU-bound" when ≥ 80% of the cores are busy. When the core charge is no more than
  the largest GPU's price, it also suggests asking for that full GPU at about the same price. Otherwise it suggests a core count of 1.25 × the cores
  actually busy.
- **`failures`:** counts jobs whose peak memory reached `mem_ceiling` (95%) of the request, which
  points to out-of-memory kills.

The flags only point at places to look. A timeout is fine for jobs that checkpoint, and
`multi_gpu` cannot tell whether the GPUs were used.

## Column definitions

- **OK / fail / timeout / cancel:** COMPLETED / (FAILED + OUT_OF_MEMORY + NODE_FAIL) / TIMEOUT /
  CANCELLED job counts.
- **Hours:** wall-clock hours (`ElapsedRaw`).
- **Core-h:** allocated cores × hours.
- **RGU-h:** billing rate × hours, counted only on `_gpu` accounts.
- **CPU used:** `TotalCPU` ÷ core-hours. Low values mean reserved cores sat idle.
- **Memory used:** peak step `MaxRSS` ÷ allocated memory, weighted by job hours.
- **Max memory:** largest peak `MaxRSS` of any job in the group.
