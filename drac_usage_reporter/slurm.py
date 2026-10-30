"""Parse sacct / sreport values: durations, memory sizes and TRES strings."""

def seconds(t):
    """Slurm duration ([D-]HH:MM:SS, MM:SS.fff) -> seconds; 0 for empty or non-duration values."""
    if not t or not t[0].isdigit():
        return 0.0
    days = 0
    if "-" in t:
        d, t = t.split("-", 1)
        days = int(d)
    parts = [float(x) for x in t.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0.0)
    h, m, s = parts
    return days * 86400 + h * 3600 + m * 60 + s


def gib(s):
    """Slurm memory string (320116K, 64G, 1.5T, or plain bytes) -> GiB."""
    if not s:
        return 0.0
    units = {"K": 1 / 1024**2, "M": 1 / 1024, "G": 1.0, "T": 1024.0}
    if s[-1] in units:
        return float(s[:-1]) * units[s[-1]]
    return float(s) / 1024**3


def tres(s):
    """'billing=12200,cpu=6,gres/gpu:h100=1,mem=144G,node=1' -> {'billing': '12200', ...}."""
    return dict(kv.split("=", 1) for kv in s.split(",") if "=" in kv)


def gpu_type(t):
    """(short GPU type, count) from a parsed TRES dict; ('', 0) when the job has no GPU.

    The short type is the gres name after its last '_' (nvidia_h100_80gb_hbm3_3g.40gb -> 3g.40gb).
    """
    for k, v in t.items():
        if k.startswith("gres/gpu:"):
            return k[len("gres/gpu:"):].rsplit("_", 1)[-1], int(v)
    return "", 0


def gpu_label(t):
    """'h100', '3g.40gb', '4x h100', or 'cpu'."""
    typ, n = gpu_type(t)
    if not n:
        return "cpu"
    return typ if n == 1 else f"{n}x {typ}"


def rgu_per_hour(t):
    """Billing rate of a job in RGU/h (Slurm stores billing x 1000)."""
    return int(t.get("billing", 0)) / 1000


def billing_weights(scontrol_partitions):
    """GPU billing weights from `scontrol show partition -o`, in RGU/h.

    Returns (rgu_per_core, rgu_per_gb, {gres name: rgu}) from the weights most GPU partitions share;
    zeros and {} when no partition bills GPUs. Slurm memory weights are per MB unless suffixed
    (Mem=42.36G is 42.36 per GB).
    """
    import collections
    import re
    seen = collections.Counter(re.findall(r"TRESBillingWeights=(\S*GRES/gpu\S*)", scontrol_partitions))
    if not seen:
        return 0.0, 0.0, {}
    core, gb, gpus = 0.0, 0.0, {}
    for kv in seen.most_common(1)[0][0].split(","):
        k, _, v = kv.partition("=")
        if k.upper() == "CPU":
            core = float(v) / 1000
        elif k.lower() == "mem":
            per = {"K": 1 / 1024**2, "M": 1 / 1024, "G": 1.0, "T": 1024.0}
            num, unit = (v[:-1], v[-1].upper()) if v[-1].isalpha() else (v, "M")
            gb = float(num) / per[unit] / 1000
        elif k.lower().startswith("gres/gpu:"):
            gpus[k.lower()] = float(v) / 1000
    return core, gb, gpus
