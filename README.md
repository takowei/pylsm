# pylsm

> A from-scratch **LSM-tree key-value storage engine** in Python — the core
> ideas behind LevelDB / RocksDB, built to demonstrate a real grasp of storage
> engine mechanics.

The intellectual core is entirely hand-written: skiplist, WAL framing, crash
recovery, SSTables, bloom filters, and leveled compaction. No embedded KV
library is used. The goal is not to beat RocksDB; it is to demonstrate a real
grasp of storage-engine mechanics and back every claim with tests and honest
measurement.

**No third-party runtime dependencies** — pure Python standard library.

---

## Status — all five phases complete (105/105 tests green)

| Phase | Content                                                                 | Status |
| ----- | ----------------------------------------------------------------------- | ------ |
| 1     | Skiplist memtable + WAL (CRC framing) + put/get/delete + crash recovery | Done   |
| 2     | SSTable (hand-rolled format) + memtable flush + multi-layer reads       | Done   |
| 3     | Bloom filter (hand-rolled) + read-path integration                      | Done   |
| 4     | Leveled compaction + WA/RA measurement + benchmark harness              | Done   |
| 5     | CLI (`pylsm` / `python -m pylsm`) + README + performance report         | Done   |

---

## Why an LSM-tree?

LSM-trees power most modern write-heavy stores (RocksDB, Cassandra, LevelDB).
They turn random writes into sequential ones by buffering in memory and flushing
sorted runs to disk, trading some read cost for high write throughput. Building
one forces you to confront the core storage trade-offs head-on: memory vs. disk,
sequential writes vs. random reads, durability vs. speed.

---

## Design in one diagram

```
   put/delete                              get
       │                                    │
       ▼                                    ▼
  WAL (append, CRC)                read order: newest → oldest
       │  durability first          memtable → immutable memtables
       └─ replayed on open  ──────▶ recovery
       │
       ▼  (memtable full)
  ┌──────────────┐   flush    ┌──────────────────────────────────┐
  │  memtable     │ ─────────▶│  L0 SSTables (may overlap)       │
  │  (skiplist)   │           │  + bloom filter + sparse index    │
  └──────────────┘           └──────────────────────────────────┘
                                           │  compaction (L0 → L1 → …)
                                           ▼
                              ┌──────────────────────────────────┐
                              │  L1+ SSTables (non-overlapping)  │
                              │  binary search on key range       │
                              └──────────────────────────────────┘
```

- **Write path**: WAL append (durability) → memtable (skiplist, ordered). When
  the memtable reaches `flush_threshold_bytes`, it is frozen and written as a
  new L0 SSTable; the WAL is rotated atomically.
- **Read path**: memtable → L0 newest-first (all files checked; bloom filter
  skips non-matching ones) → L1+ (binary search on non-overlapping key ranges,
  then bloom filter). First hit wins; a tombstone hit means deleted.
- **Compaction**: when L0 reaches `l0_compaction_trigger` files, a multi-way
  merge (heapq) writes new non-overlapping L1 SSTables, then atomically updates
  the MANIFEST. Tombstones are physically removed at the bottom level.
- **Crash recovery**: on open, MANIFEST is read to load SSTables; then the WAL
  is replayed to rebuild the active memtable. Every acknowledged write survives.

---

## Python API

```python
from pylsm import DB

with DB("./data") as db:
    db.put(b"user:1", b"alice")
    print(db.get(b"user:1"))   # b"alice"
    db.delete(b"user:1")
    print(db.get(b"user:1"))   # None

# Crash safety: even without a clean close, reopening DB("./data")
# replays the write-ahead log and recovers every acknowledged write.
```

`DB` also exposes:

```python
db.stats.write_amplification      # disk_bytes_written / user_bytes_written
db.stats.gross_read_amplification # SSTs considered (before bloom) / get calls
db.stats.read_amplification       # SSTs block-scanned (after bloom) / get calls
db.compact()                      # manually trigger compaction
```

---

## CLI

After `pip install -e .` the `pylsm` command is available. It is also
runnable as `python -m pylsm`.

```
pylsm put    <dir> <key> <value>   store a key/value pair (UTF-8 strings)
pylsm get    <dir> <key>           print value; exits 1 if absent
pylsm delete <dir> <key>           tombstone a key
pylsm scan   <dir>                 print all live pairs sorted by key (tab-separated)
pylsm compact <dir>                manually trigger a compaction pass
```

Example session:

```
$ pylsm put ./mydb name Alice
$ pylsm put ./mydb city Taipei
$ pylsm put ./mydb lang Python
$ pylsm scan ./mydb
city    Taipei
lang    Python
name    Alice
$ pylsm delete ./mydb city
$ pylsm scan ./mydb
lang    Python
name    Alice
$ pylsm compact ./mydb
compaction complete
$ pylsm get ./mydb missing
key not found: 'missing'
$ echo $?
1
```

---

## Performance summary

All numbers measured with `sync=False` on WSL2 (Intel Core i7-14700HX,
15.5 GiB RAM). See [`docs/PERFORMANCE.md`](docs/PERFORMANCE.md) for
full methodology, machine details, and caveats.

| Workload          | Throughput      | Write amplification |
| ----------------- | --------------- | ------------------- |
| Sequential writes | ~61 000 ops/sec | 1.82×               |
| Random writes     | ~54 000 ops/sec | 1.63×               |

| Metric                            | Before compaction | After compaction |
| --------------------------------- | ----------------- | ---------------- |
| Gross RA (SSTs considered / get)  | 1.46              | 1.00             |
| Net RA (SSTs block-scanned / get) | 1.00              | 1.00             |
| Read throughput                   | —                 | ~38 000 ops/sec  |

**Key caveats** (see PERFORMANCE.md for full list):

- `sync=False` skips `fsync()` per write. Durable (`sync=True`) write
  throughput is roughly 10–100× lower, bounded by disk latency rather than
  CPU cost.
- `SSTableReader` loads the entire file into memory at open time — there is no
  block cache. "Read throughput" is effectively an in-memory scan.
- WA counter covers only SSTable bytes (flush + compaction); WAL bytes
  (~1× user bytes) are excluded. Total WA ≈ WA_sstable + 1.

---

## Testing

```bash
pip install -e ".[dev]"
pytest
```

105 tests across nine files:

- **Property-based oracle** (`test_property.py`): 3 000 random put/delete/get
  operations run against both the engine and a plain `dict`; the DB is reopened
  mid-sequence to exercise crash recovery. The two must agree on every key
  throughout.
- **Torn-tail and CRC corruption** (`test_wal.py`): WAL entries truncated at
  arbitrary byte boundaries are detected via CRC32 and safely discarded.
- **Compaction crash scenarios** (`test_compaction.py`): orphaned SSTable
  before MANIFEST update, and mid-compaction simulated crash — both leave data
  intact.
- **Bloom filter accuracy** (`test_bloom.py`, `test_bloom_integration.py`):
  measured FPR = 1.00 % at n = 1 000, p = 0.01 (matches theoretical). DB.get
  verified to skip blocks when the bloom rejects the key.
- **CLI round-trips** (`test_cli.py`): put→get, delete→get-fails, scan ordered,
  tombstones hidden, compact confirmation, cross-invocation persistence.

---

## Known limitations

1. **Single-threaded** — no concurrency support.
2. **No block cache** — entire SSTable loaded into memory on open; "read
   throughput" is in-memory, not disk-I/O-bound.
3. **Benchmark covers L0 → L1 only** — insufficient data to trigger L1 → L2
   compaction; WA would be higher with deeper levels.
4. **WAL not counted in WA** — total WA ≈ WA_sstable + 1.
5. **WSL2 storage** — measurements on WSL2's virtual disk, which differs from
   bare-metal SSD.

---

## Project layout

```
pylsm/
  __init__.py    public API (DB, DBStats)
  __main__.py    python -m pylsm entry point
  cli.py         CLI (argparse, pure stdlib)
  db.py          KV store: put/get/delete + flush + compaction trigger
  skiplist.py    ordered memtable (hand-rolled skiplist)
  wal.py         write-ahead log (length-prefix + CRC32 framing)
  sstable.py     SSTable writer + reader (hand-rolled binary format)
  bloom.py       bloom filter (Kirsch-Mitzenmacher double-hashing)
  compaction.py  leveled compaction (heapq multi-way merge)
  stats.py       WA / RA counters
tests/           pytest suite (105 tests)
bench/           benchmark harness (run_bench.py)
docs/
  BLUEPRINT.md   design rationale + per-phase acceptance gates
  PERFORMANCE.md measured numbers + full methodology
```
