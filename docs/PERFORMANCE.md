# pylsm Phase 4 — Performance Report

> This document reports **measured numbers**, not estimates. Every figure was
> produced by running `bench/run_bench.py` on the machine described below.
> Read the methodology section before interpreting any number.

---

## Machine

| Field   | Value                                    |
| ------- | ---------------------------------------- |
| OS      | Linux 6.6.87.2-microsoft-standard-WSL2   |
| CPU     | Intel Core i7-14700HX                    |
| RAM     | 15.5 GiB                                 |
| Storage | WSL2 virtual disk (no direct-attach SSD) |

---

## Benchmark parameters

| Parameter       | Value    | Why                                          |
| --------------- | -------- | -------------------------------------------- |
| Operations      | 20 000   | Enough to trigger multiple compaction rounds |
| Key size        | 16 bytes | Typical short key                            |
| Value size      | 64 bytes | Typical small value                          |
| Flush threshold | 128 KiB  | Produces several flushes per benchmark       |
| L0 trigger      | 8 files  | Compaction fires after 8 L0 SSTables         |
| sync            | **off**  | See caveat below                             |

---

## Results

### Write throughput

| Workload          | Throughput      | Write amplification |
| ----------------- | --------------- | ------------------- |
| Sequential writes | ~61 000 ops/sec | 1.82x               |
| Random writes     | ~54 000 ops/sec | 1.63x               |

### Read throughput and amplification

Workload: 20 000 writes over 2 500 unique keys (each key overwritten ~8×)
→ same key appears in multiple L0 SSTables, creating real L0 overlap.

| Metric                               | Before compact | After compact   |
| ------------------------------------ | -------------- | --------------- |
| L0 file count / L1 file count        | 7              | 1               |
| **Gross RA** (SSTs considered / get) | **1.46**       | **1.00**        |
| Net RA (SSTs block-scanned / get)    | 1.00           | 1.00            |
| Read throughput                      | —              | ~38 000 ops/sec |

---

## Methodology

### Write amplification

```
WA = disk_bytes_written / user_bytes_written
```

`user_bytes_written` accumulates `len(key) + len(value)` for each `put()`.
`disk_bytes_written` accumulates the size of every SSTable file written to
disk — both from memtable-flush output and from compaction output. Every time
the same logical byte is rewritten during compaction it is counted again; this
is the definition of write amplification.

A WA of 1.82 means: for every byte the user writes, 1.82 bytes end up on disk
in total (the extra 0.82x is the cost of compaction rewriting data from L0 into
L1 to establish the non-overlapping invariant).

### Gross read amplification

```
Gross RA = sst_candidates / get_calls
```

`sst_candidates` increments for every SSTable the engine **considers** before
the bloom filter check — i.e. for every L0 file (all are candidates since their
key ranges may overlap) and for at most one L1 file per level (non-overlapping
ranges allow binary search).

**Before compaction (7 L0 files)**: every L0 file is a candidate. However the
engine searches L0 files newest-first and short-circuits on the first hit, so
the average is not 7 but approximately 7/2 ≈ 3.5 for uniformly distributed
write ages. The measured 1.46 is lower still because the overwrite workload
concentrates recent writes in the few newest SSTables.

**After compaction (1 L1 file)**: binary search on the L1 key range finds
exactly one candidate. Gross RA = 1.00.

**Worst-case gross RA** (not shown in the table above but verifiable with the
test `test_gross_read_amplification_lower_after_compaction`): when all keys are
written once, spread evenly across N L0 SSTables, the average gross RA is
approximately N/2. After compaction it becomes 1. At N = 100 the test
confirms the structural drop from ~51 to 1.

### Net read amplification

```
Net RA = sst_accesses / get_calls
```

`sst_accesses` increments only when the bloom filter says "maybe present" and
the engine proceeds to a block-level scan. With a 1% false-positive rate and
present keys, the bloom filter accepts exactly the one SSTable that holds the
key → Net RA ≈ 1.0 regardless of how many L0 files exist.

This is why the bloom filter (Phase 3) and leveled compaction (Phase 4) address
different problems:

- Bloom filter reduces **block-scan cost** for absent or rare-overwrite keys.
- Leveled compaction reduces **structural cost** (gross RA) and write
  amplification by enforcing non-overlapping ranges in L1+.

### Sync mode caveat

All benchmarks run with `sync=False`. This skips `fsync()` after each write
and measures CPU / in-memory throughput, not durable-write throughput. On a
real spinning disk or even a fast NVMe with `sync=True`, write throughput would
be ~10–100× lower because it would be bound by disk latency (~0.1–10 ms per
fsync). The engine is correct regardless of sync mode; sync controls durability
guarantees, not correctness.

---

## Known limitations and caveats

1. **Single-threaded**: pylsm has no concurrency. A multi-threaded benchmark
   would show different throughput characteristics.

2. **No block cache**: `SSTableReader` loads the entire file into memory at
   open time. This avoids repeated I/O but means all "reads" are in-memory.
   A production engine would use a block cache and show different read latency
   profiles.

3. **L0 → L1 only in benchmark**: the benchmark does not generate enough data
   to trigger L1 → L2 compaction (L1 limit is 10 MiB). WA would increase
   further with deeper levels.

4. **Write amplification is a lower bound**: the counter only tracks SSTable
   file bytes. WAL bytes (~1× user bytes) are not included. If WAL were
   counted, WA_total ≈ WA_sstable + 1.

5. **WSL2 storage**: measurements are on WSL2's virtual disk, which has
   different I/O characteristics than bare-metal SSD.
