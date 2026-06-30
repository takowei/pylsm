# pylsm

> A from-scratch **LSM-tree key-value storage engine** in Python — the core ideas behind LevelDB / RocksDB, built to understand how storage engines actually work.

The intellectual core is hand-written (skiplist, WAL framing, crash recovery; SSTables, bloom filters and compaction in later phases) — no embedded KV library is used. The goal is not to beat RocksDB; it is to demonstrate a real grasp of storage-engine mechanics and to back every claim with tests and honest measurement.

**No third-party runtime dependencies** — pure Python standard library.

---

## Why an LSM-tree?

LSM-trees power most modern write-heavy stores (RocksDB, Cassandra, LevelDB). They turn random writes into sequential ones by buffering in memory and flushing sorted runs to disk, trading some read cost for high write throughput. Building one forces you to confront the core storage trade-offs head-on: memory vs. disk, sequential writes vs. random reads, durability vs. speed.

## Current status

**Phase 2 complete — 45/45 tests green:**

- **Skiplist memtable** — ordered in-memory map with expected O(log n) ops; ordered iteration drives the flush path.
- **Write-ahead log** — every mutation is appended (length-prefix + CRC32 framing) _before_ the memtable; torn tail detected via CRC and safely discarded.
- **Crash recovery** — WAL is replayed on open; every acknowledged write survives an unclean stop.
- **SSTable (hand-rolled format)** — sorted data blocks + sparse index (one entry per block) + 16-byte footer (index offset + magic `0x7079_6C73`). No pickle, shelve, or embedded KV used.
- **Memtable flush** — when the memtable exceeds `flush_threshold_bytes`, it is frozen and written to a numbered SSTable (`sst_NNNNNNNN.sst`); the WAL is then truncated atomically (manifest updated first via `os.replace`).
- **Multi-layer reads** — `get` searches active memtable → SSTables newest-first; first hit wins (tombstone = deleted, reported as `None`).
- **Tombstone shadowing** — a delete in a newer layer correctly hides an older value in any earlier SSTable.
- **Flush crash-safety** — crash before manifest update → orphaned SSTable ignored, WAL replayed; crash after manifest update → stale WAL content idempotent on replay. Both cases tested explicitly.

Planned: bloom filters (Phase 3) → leveled compaction + read/write-amplification benchmarks (Phase 4). See [`docs/BLUEPRINT.md`](docs/BLUEPRINT.md) for the full design and the per-phase acceptance gate.

## Usage

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

## Design in one diagram

```
   put/delete                         get
       │                               │
       ▼                               ▼
  WAL (append, CRC)   ──flush──▶   memtable (skiplist)
       │  durability first              │  ordered, in-memory
       └─ replayed on open ────────────▶ recovery
```

## Testing

```bash
pip install -e ".[dev]"
pytest
```

The suite (`tests/`) includes a **property-based oracle test**: 2000 random `put`/`delete`/`get` operations are run against both the engine and a plain `dict`, with the database reopened mid-sequence (simulating crashes) — the two must agree on every key throughout. Plus targeted tests for torn-tail recovery and CRC corruption detection.

## Honesty note

Only what is verified is marked complete; later phases are marked planned. Performance numbers will only appear once Phase 4 measures them with a stated method (machine, data size, on-disk vs. in-memory) — no inflated figures.
