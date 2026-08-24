"""Generate a synthetic server-monitoring CSV to develop against.

Not a stand-in for real data -- it exists so the pipeline has something with the
right *shapes* in it before anyone plugs in a real feed. The shapes that matter
to serrin are the ones the pedals react to:

  * periodicity      -- daily/hourly cycles for delta and the LFOs to chew on
  * bursts / spikes  -- for the glitch flag and the amplitude mapping
  * flatlines        -- stretches of stuck data, so stutter_repeat has something
                        to detect (a dataset with no flat patches makes that
                        pedal look broken)
  * a dead column    -- to prove automatic column selection drops it

Deterministic: same seed, same file.

    python scripts/make_sample_data.py --rows 2400 --out data/monitoring.csv
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from serrin.rng import Rng  # noqa: E402

HEADER = [
    "timestamp",
    "cpu_pct",
    "mem_pct",
    "net_in_kbps",
    "net_out_kbps",
    "disk_io_ops",
    "latency_ms",
    "errors_5xx",
    "queue_depth",
    "build_number",  # deliberately near-constant: should be dropped automatically
]


def generate(rows: int, seed: int, step_seconds: int = 30) -> list[list[str]]:
    rng = Rng(seed)
    out: list[list[str]] = []

    # Slow-moving state, so consecutive rows correlate the way real telemetry does.
    mem = 42.0
    queue = 3.0
    errors_pending = 0

    # Scheduled incidents: a burst and a flatline, placed proportionally so the
    # shapes land wherever the row count ends up.
    burst_at = int(rows * 0.42)
    burst_len = max(8, rows // 40)
    flat_at = int(rows * 0.68)
    flat_len = max(12, rows // 30)
    held: list[float] | None = None

    for i in range(rows):
        t = i * step_seconds
        day = math.sin(2.0 * math.pi * (t / 86400.0))  # daily cycle
        hour = math.sin(2.0 * math.pi * (t / 3600.0))  # hourly cycle
        in_burst = burst_at <= i < burst_at + burst_len
        in_flat = flat_at <= i < flat_at + flat_len

        cpu = 30.0 + 22.0 * day + 9.0 * hour + rng.uniform(-4.0, 4.0)
        if in_burst:
            cpu += 45.0 + rng.uniform(0.0, 12.0)
        cpu = max(0.5, min(100.0, cpu))

        # Memory drifts and only occasionally gets reclaimed -- a sawtooth, which
        # sounds completely different from the oscillating columns.
        mem += rng.uniform(-0.35, 0.55) + (0.8 if in_burst else 0.0)
        if mem > 92.0 or (rng.chance(0.004) and mem > 60.0):
            mem -= rng.uniform(18.0, 34.0)
        mem = max(20.0, min(97.0, mem))

        net_in = max(0.0, 1400.0 + 900.0 * day + rng.uniform(-260.0, 260.0))
        net_out = max(0.0, net_in * rng.uniform(0.28, 0.42) + rng.uniform(-40.0, 40.0))
        if in_burst:
            net_in *= rng.uniform(3.0, 6.5)
            net_out *= rng.uniform(2.0, 4.0)

        disk = max(0.0, 240.0 + 160.0 * hour + rng.uniform(-70.0, 70.0))
        latency = 12.0 + 0.6 * (cpu - 30.0) + rng.uniform(-3.0, 5.0)
        if in_burst:
            latency += rng.uniform(60.0, 180.0)
        latency = max(1.0, latency)

        # Errors arrive in clusters, not independently: one failure begets more.
        if errors_pending > 0:
            errors = rng.between(1, 6)
            errors_pending -= 1
        elif in_burst and rng.chance(0.55):
            errors = rng.between(3, 24)
            errors_pending = rng.between(2, 6)
        else:
            errors = rng.between(0, 1) if rng.chance(0.08) else 0

        queue += rng.uniform(-1.2, 1.2) + (errors * 0.4)
        queue = max(0.0, min(140.0, queue))

        row = [
            str(t),
            f"{cpu:.2f}",
            f"{mem:.2f}",
            f"{net_in:.1f}",
            f"{net_out:.1f}",
            f"{disk:.0f}",
            f"{latency:.2f}",
            str(errors),
            f"{queue:.1f}",
            "1042",
        ]

        # Flatline: the collector wedged and kept reporting its last sample.
        # This is the texture stutter_repeat is looking for.
        if in_flat:
            if held is None:
                held = row[1:]
            row = [str(t), *held]
        else:
            held = None

        out.append(row)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=2400, help="rows to generate")
    parser.add_argument("--seed", type=int, default=0xC0FFEE)
    parser.add_argument("--step", type=int, default=30, help="seconds between rows")
    parser.add_argument("--out", default="data/monitoring.csv")
    args = parser.parse_args()

    rows = generate(args.rows, args.seed, args.step)
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(",".join(HEADER) + "\n")
        for row in rows:
            handle.write(",".join(row) + "\n")

    print(f"wrote {target} -- {len(rows)} rows x {len(HEADER)} columns")
    print(f"  burst around row {int(args.rows * 0.42)}, flatline around row {int(args.rows * 0.68)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
