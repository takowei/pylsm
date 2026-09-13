# pylsm

> A from-scratch **LSM-tree key-value storage engine with MVCC transactions**
> in Python — the core ideas behind LevelDB / RocksDB, built to demonstrate a
> real grasp of storage-engine mechanics.

I built this after noticing that most of my other work — validating a trading
strategy, checking an agent's benchmark score, reviewing a GitHub repo before
depending on it — comes down to the same question: can I actually trust what
a system claims about itself. A storage engine is a good place to run that
habit end to end, because every claim (crash safety, snapshot isolation,
bloom filter false-positive rate) can be checked against a reference
implementation instead of assumed correct because the code compiles.

The intellectual core is entirely hand-written: skiplist, WAL framing, crash
recovery, SSTables, bloom filters, leveled compaction, and a multi-version
concurrency-control layer with snapshot isolation. No embedded KV library is
used. The goal is not to beat RocksDB; it is to demonstrate a real grasp of
storage-engine mechanics and back every claim with tests and honest measurement.

**No third-party runtime dependencies** — pure Python standard library.

---

## Status — storage engine complete, 132/132 tests green

The engine is a coherent, finished product: a durable, crash-safe LSM key-value
store with a multi-version transaction layer providing snapshot isolation.

| Phase | Content                                                                 | Status |
| ----- | ----------------------------------------------------------------------- | ------ |
| 1     | Skiplist memtable + WAL (CRC framing) + put/get/delete + crash recovery | Done   |
| 2     | SSTable (hand-rolled format) + memtable flush + multi-layer reads       | Done   |
| 3     | Bloom filter (hand-rolled) + read-path integration                      | Done   |
| 4     | Leveled compaction + WA/RA measurement + benchmark harness              | Done   |
| 5     | CLI (`pylsm` / `python -m pylsm`) + README + performance report         | Done   |
| DB-1  | **MVCC multi-version storage + snapshot isolation**                     | Done   |

A relational SQL layer on top of this engine (table encoding, a SQL tokenizer /
parser, and a SELECT/DELETE executor) is sketched in
[`docs/BLUEPRINT-DB.md`](docs/BLUEPRINT-DB.md) as a **future roadmap** — it is
not part of the current finished engine.

---

## Why an LSM-tree?

LSM-trees power most modern write-heavy stores (RocksDB, Cassandra, LevelDB).
They turn random writes into sequential ones by buffering in memory and flushing
sorted runs to disk, trading some read cost for high write throughput. Building
one forces you to confront the core storage trade-offs head-on: memory vs. disk,
sequential writes vs. random reads, durability vs. speed.

---

## Storage engine in one diagram

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

## MVCC and snapshot isolation

The `MVCCEngine` layer turns the single-value KV store into a **multi-version**
store: no write ever overwrites in place. Every `put` / `delete` is stamped with
a monotonically increasing sequence number and stored as a distinct _physical_
key, so old versions coexist with new ones. A `Snapshot` captures the sequence
number at a point in time; reads through it see exactly the state committed up to
that seq, no matter what is written afterwards.

**The key trick — inverted-seq physical keys.** Each logical write is encoded as:

```
physical_key = u32be(len(user_key)) | user_key | u64be(MAX_U64 − seq)
```

The `MAX_U64 − seq` term inverts the sequence number, so a _higher_ seq produces
a _smaller_ physical key. All versions of one user key are grouped by the
length-prefixed user key, and within that group the newest version sorts first.
Reading `user_key` as of `snapshot_seq` is then a single forward scan starting at
`encode(user_key, snapshot_seq)`: any version with `seq > snapshot_seq` has a
_smaller_ key and sits before the scan start, so it is naturally excluded, and
the first hit is the latest version `≤ snapshot_seq`. Snapshot isolation falls
out of the key encoding — no per-read version filtering is needed.

Values carry a one-byte tag: `\x00` + bytes for a live value, `\x01` for a
tombstone. The underlying KV's own delete mechanism is never used — deletion is
just another version.

**Crash-safe sequence counter.** The global seq is persisted in the KV under a
reserved meta key (whose length prefix decodes to an impossible ~4 GiB user-key
length, so it can never collide with a real physical key). The counter is
written _before_ the data key on every write, so after a crash the recovered seq
is always `≥` the highest committed version — the counter can never fall behind
the data.

```python
from pylsm import DB, MVCCEngine

with DB("./data") as raw_db:
    engine = MVCCEngine(raw_db)
    engine.put(b"k", b"v1")
    snap = engine.snapshot()             # freeze the read view here
    engine.put(b"k", b"v2")              # invisible to snap
    assert snap.get(b"k") == b"v1"       # snapshot isolation
    assert engine.get(b"k") == b"v2"     # current committed state
```

---

## Python API

### Raw key-value store

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

### MVCC transaction layer

```python
from pylsm import DB, MVCCEngine

with DB("./data") as raw_db:
    engine = MVCCEngine(raw_db)
    seq = engine.put(b"k", b"v")   # returns the assigned sequence number
    engine.delete(b"k")            # tombstone at a new seq
    engine.get(b"k")               # current committed value, or None
    snap = engine.snapshot()       # Snapshot at the current seq
    snap.get(b"k")                 # read as of the snapshot
    list(engine.scan())            # all live (user_key, value) pairs
    engine.current_seq             # highest seq assigned so far
```

---

## CLI

After `pip install -e .` the `pylsm` command is available. It is also
runnable as `python -m pylsm`. The CLI drives the raw KV store.

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
full methodology, machine details, and caveats. The figures below cover the raw
KV engine; the MVCC layer adds a second underlying `put` per write (the seq meta
key), so MVCC write throughput is roughly half the raw numbers.

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

132 tests across eleven test modules:

- **Property-based oracle** (`test_property.py`): thousands of random
  put/delete/get operations run against both the engine and a plain `dict`; the
  DB is reopened mid-sequence to exercise crash recovery. The two must agree on
  every key throughout.
- **MVCC and snapshot isolation** (`test_mvcc.py`): version visibility,
  snapshot isolation across interleaved writes, tombstones vs. old snapshots,
  arbitrary-byte keys, seq persistence and crash recovery (recovered seq never
  behind committed data), and physical-key ordering.
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

1. **Single-threaded** — no concurrency support; the MVCC layer provides
   snapshot isolation for sequential access, not concurrent transactions.
2. **No MVCC version GC** — every write appends a new version; old versions
   accumulate until a future compaction-time GC is added.
3. **MVCC doubles underlying writes** — the seq meta key is persisted before
   each data key, so one logical write is two underlying `put`s.
4. **No block cache** — entire SSTable loaded into memory on open; "read
   throughput" is in-memory, not disk-I/O-bound.
5. **Benchmark covers L0 → L1 only** — insufficient data to trigger L1 → L2
   compaction; WA would be higher with deeper levels.
6. **WAL not counted in WA** — total WA ≈ WA_sstable + 1.
7. **WSL2 storage** — measurements on WSL2's virtual disk, which differs from
   bare-metal SSD.

---

## Project layout

```
pylsm/
  __init__.py    public API (DB, DBStats, MVCCEngine, Snapshot)
  __main__.py    python -m pylsm entry point
  cli.py         CLI (argparse, pure stdlib)
  db.py          KV store: put/get/delete + flush + compaction trigger
  skiplist.py    ordered memtable (hand-rolled skiplist)
  wal.py         write-ahead log (length-prefix + CRC32 framing)
  sstable.py     SSTable writer + reader (hand-rolled binary format)
  bloom.py       bloom filter (Kirsch-Mitzenmacher double-hashing)
  compaction.py  leveled compaction (heapq multi-way merge)
  mvcc.py        MVCC layer: multi-version storage + snapshot isolation
  stats.py       WA / RA counters
tests/           pytest suite (132 tests across eleven modules)
bench/           benchmark harness (run_bench.py)
docs/
  BLUEPRINT.md    design rationale + per-phase acceptance gates (KV engine)
  BLUEPRINT-DB.md future roadmap: relational SQL layer (not yet built)
  PERFORMANCE.md  measured numbers + full methodology
```

</content>
</invoke>
