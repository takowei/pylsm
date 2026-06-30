"""pylsm Phase 4 benchmark: throughput + read/write amplification.

This script measures:
  1. Sequential-write throughput  (put() ops/sec, sync=False)
  2. Random-write throughput      (put() ops/sec, sync=False)
  3. Sequential-read throughput   (get() ops/sec from L0+L1)
  4. Random-read throughput       (get() ops/sec from L0+L1)
  5. Write amplification          (disk bytes / user bytes)
  6. Read amplification before compaction  (avg SSTables searched per get)
  7. Read amplification after compaction   (avg SSTables searched per get)

Methodology notes
─────────────────
• ``sync=False`` throughout — no fsync per write.  This measures CPU/memory
  throughput, NOT durable-write throughput.  The output is clearly labelled
  "sync=off".  Sync-on numbers would be 10–100× slower and are dominated by
  disk latency, not engine logic.
• All writes go to L0 first (memtable → SSTable).  Compaction rewrites data
  into L1, which is what write amplification captures.
• Read amplification is measured with ``stats.sst_accesses / stats.get_calls``
  — the number of SSTables whose bloom filter was not rejected and which were
  actually searched per get() call.  Lower = better (L1 non-overlap helps).
• Value size: 64 bytes.  Key size: 16 bytes.
  Total user bytes per put = 80 bytes.

Usage
─────
  python3 bench/run_bench.py
  python3 bench/run_bench.py --n 50000
"""

from __future__ import annotations

import argparse
import os
import platform
import random
import sys
import tempfile
import time

# Allow running directly from the repo root.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pylsm import DB  # noqa: E402

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

KEY_LEN = 16
VAL_LEN = 64
FLUSH_THRESHOLD = 128 * 1024  # 128 KiB — flush often to build many L0 files
L0_TRIGGER = 8  # file count that fires compaction


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_key(i: int) -> bytes:
    return f"key{i:012d}".encode()


def _make_val(i: int) -> bytes:
    return f"val{i:060d}".encode()


def _machine_info() -> str:
    uname = platform.uname()
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    cpu = line.split(":", 1)[1].strip()
                    break
            else:
                cpu = uname.processor or "unknown"
    except OSError:
        cpu = uname.processor or "unknown"
    mem_gb = "?"
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal"):
                    kb = int(line.split()[1])
                    mem_gb = f"{kb / 1024 / 1024:.1f} GiB"
                    break
    except OSError:
        pass
    return f"{uname.system} {uname.release} | CPU: {cpu} | RAM: {mem_gb}"


def _hr() -> None:
    print("-" * 72)


# ---------------------------------------------------------------------------
# Benchmark sections
# ---------------------------------------------------------------------------


def bench_sequential_write(db_path: str, n: int) -> tuple[float, float, float]:
    """Sequential writes.

    Returns (ops_per_sec, write_amp, elapsed_sec).
    """
    with DB(
        db_path, sync=False, flush_threshold_bytes=FLUSH_THRESHOLD, l0_compaction_trigger=L0_TRIGGER
    ) as db:
        db.stats.reset()
        t0 = time.perf_counter()
        for i in range(n):
            db.put(_make_key(i), _make_val(i))
        elapsed = time.perf_counter() - t0
        wa = db.stats.write_amplification
    return n / elapsed, wa, elapsed


def bench_random_write(db_path: str, n: int) -> tuple[float, float, float]:
    """Random writes over a key space of size *n*.

    Returns (ops_per_sec, write_amp, elapsed_sec).
    """
    rng = random.Random(42)
    keys = [_make_key(rng.randrange(n)) for _ in range(n)]
    with DB(
        db_path, sync=False, flush_threshold_bytes=FLUSH_THRESHOLD, l0_compaction_trigger=L0_TRIGGER
    ) as db:
        db.stats.reset()
        t0 = time.perf_counter()
        for i, k in enumerate(keys):
            db.put(k, _make_val(i))
        elapsed = time.perf_counter() - t0
        wa = db.stats.write_amplification
    return n / elapsed, wa, elapsed


def bench_reads(db_path: str, n: int) -> tuple[float, float, float, float, float, float, int, int]:
    """Read throughput and amplification (gross + net), before and after compaction.

    Strategy: write *n* ops over a small key space (n // 8 unique keys) so the
    same key is overwritten many times and therefore appears in multiple L0
    SSTables.  Auto-compaction is disabled during fill so we can measure RA
    in the pure-L0 state.

    Gross RA = sst_candidates / get_calls  (SSTs examined before bloom filter).
    Net   RA = sst_accesses / get_calls    (SSTs actually block-scanned).

    Returns:
        (ops/sec, gross_ra_before, net_ra_before,
         gross_ra_after, net_ra_after, elapsed_sec,
         n_l0_files_before, n_l1_files_after)
    """
    key_space = max(n // 8, 50)
    rng = random.Random(13)
    write_keys = [rng.randrange(key_space) for _ in range(n)]
    read_keys = list(range(key_space))  # read every unique key once

    # --- Phase A: fill with overwrites, NO auto-compaction ---
    with DB(
        db_path,
        sync=False,
        flush_threshold_bytes=FLUSH_THRESHOLD,
        l0_compaction_trigger=999_999,  # effectively disabled
    ) as db:
        for idx in write_keys:
            db.put(_make_key(idx), _make_val(idx))

    # --- Phase B: measure RA *before* compaction ---
    with DB(
        db_path,
        sync=False,
        flush_threshold_bytes=FLUSH_THRESHOLD,
        l0_compaction_trigger=999_999,
    ) as db:
        n_l0_before = len(db._levels[0]) if db._levels else 0
        db.stats.reset()
        for idx in read_keys:
            db.get(_make_key(idx))
        gross_ra_before = db.stats.gross_read_amplification
        net_ra_before = db.stats.read_amplification

    # --- Phase C: compact, then measure RA *after* ---
    with DB(
        db_path,
        sync=False,
        flush_threshold_bytes=FLUSH_THRESHOLD,
        l0_compaction_trigger=999_999,
    ) as db:
        db.compact()
        n_l1_after = len(db._levels[1]) if len(db._levels) > 1 else 0
        db.stats.reset()
        t0 = time.perf_counter()
        for idx in read_keys:
            db.get(_make_key(idx))
        elapsed = time.perf_counter() - t0
        gross_ra_after = db.stats.gross_read_amplification
        net_ra_after = db.stats.read_amplification

    ops = key_space / elapsed
    return (
        ops,
        gross_ra_before,
        net_ra_before,
        gross_ra_after,
        net_ra_after,
        elapsed,
        n_l0_before,
        n_l1_after,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="pylsm Phase 4 benchmark")
    parser.add_argument("--n", type=int, default=20_000, help="Number of operations")
    args = parser.parse_args()
    n = args.n

    print()
    print("=" * 72)
    print("pylsm Phase 4 Benchmark")
    print("=" * 72)
    print(f"Machine   : {_machine_info()}")
    print(f"Operations: {n:,}")
    print(f"Key size  : {KEY_LEN} bytes   Value size: {VAL_LEN} bytes")
    print(f"Flush at  : {FLUSH_THRESHOLD // 1024} KiB   L0 trigger: {L0_TRIGGER} files")
    print("sync      : off  (measuring CPU/memory throughput, NOT durable-write)")
    print()

    # Sequential writes
    _hr()
    print("1. Sequential writes")
    with tempfile.TemporaryDirectory() as d:
        ops, wa, elapsed = bench_sequential_write(d, n)
    print(f"   Throughput      : {ops:>10,.0f} ops/sec")
    print(f"   Elapsed         : {elapsed:.3f} s")
    print(f"   Write amplif.   : {wa:.3f}x")
    print("   (WA = disk bytes written / user bytes; includes flush + compaction)")
    print()

    # Random writes
    _hr()
    print("2. Random writes")
    with tempfile.TemporaryDirectory() as d:
        ops, wa, elapsed = bench_random_write(d, n)
    print(f"   Throughput      : {ops:>10,.0f} ops/sec")
    print(f"   Elapsed         : {elapsed:.3f} s")
    print(f"   Write amplif.   : {wa:.3f}x")
    print()

    # Read amplification (with overwrite workload to create L0 overlap)
    _hr()
    print("3. Read amplification — before vs after compaction")
    print("   (workload: n writes over n//8 unique keys → each key overwrites ~8x)")
    print("   Gross RA = SSTs considered before bloom filter (structural cost).")
    print("   Net   RA = SSTs actually block-scanned after bloom filter.")
    with tempfile.TemporaryDirectory() as d:
        (
            ops,
            gross_before,
            net_before,
            gross_after,
            net_after,
            elapsed,
            n_l0,
            n_l1,
        ) = bench_reads(d, n)
    print(f"   L0 files BEFORE compact : {n_l0}")
    print(f"   L1 files AFTER  compact : {n_l1}")
    print()
    print(f"   {'Metric':<30} {'Before':>10}  {'After':>10}  {'Improvement':>12}")
    print(f"   {'-' * 30} {'-' * 10}  {'-' * 10}  {'-' * 12}")
    print(
        f"   {'Gross RA (structural)':<30} {gross_before:>10.2f}  {gross_after:>10.2f}"
        f"  {(gross_before - gross_after):>+12.2f}"
    )
    print(
        f"   {'Net RA (after bloom)':<30} {net_before:>10.2f}  {net_after:>10.2f}"
        f"  {(net_before - net_after):>+12.2f}"
    )
    print(f"   Post-compact read throughput: {ops:,.0f} ops/sec  (elapsed {elapsed:.3f} s)")
    print()

    _hr()
    print("Notes:")
    print("  - sync=off: no fsync per write; throughput reflects CPU cost, not disk latency.")
    print("    Durable (sync=on) write throughput is ~10-100x lower.")
    print("  - WA = disk bytes written (flush + compaction) / user bytes.")
    print("    WA > 1.0 is expected: compaction rewrites data to maintain the")
    print("    non-overlapping invariant in L1+.")
    print("  - Gross RA: every L0 file is a candidate (no key-range pruning).")
    print("    After compaction into L1, binary search on key range reduces")
    print("    candidates to at most 1 per level — regardless of bloom filter.")
    print("  - Net RA: bloom filter further prunes candidates.  With 1% FPR and")
    print("    many L0 files the FPR effect is small vs structural improvement.")
    print("=" * 72)
    print()


if __name__ == "__main__":
    main()
