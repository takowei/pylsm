# pylsm

> A from-scratch **LSM-tree key-value storage engine** in Python — the core ideas behind LevelDB / RocksDB, built to understand how storage engines actually work.

The intellectual core is hand-written (skiplist, WAL framing, crash recovery; SSTables, bloom filters and compaction in later phases) — no embedded KV library is used. The goal is not to beat RocksDB; it is to demonstrate a real grasp of storage-engine mechanics and to back every claim with tests and honest measurement.

**No third-party runtime dependencies** — pure Python standard library.

---

## Why an LSM-tree?

LSM-trees power most modern write-heavy stores (RocksDB, Cassandra, LevelDB). They turn random writes into sequential ones by buffering in memory and flushing sorted runs to disk, trading some read cost for high write throughput. Building one forces you to confront the core storage trade-offs head-on: memory vs. disk, sequential writes vs. random reads, durability vs. speed.

## Current status

**Phase 4 complete — 95/95 tests green:**

- **Skiplist memtable** — ordered in-memory map with expected O(log n) ops; ordered iteration drives the flush path.
- **Write-ahead log** — every mutation is appended (length-prefix + CRC32 framing) _before_ the memtable; torn tail detected via CRC and safely discarded.
- **Crash recovery** — WAL is replayed on open; every acknowledged write survives an unclean stop.
- **SSTable (hand-rolled format)** — sorted data blocks + sparse index (one entry per block) + 28-byte footer (index offset/length, bloom offset/length, magic `0x7079_6C73`). No pickle, shelve, or embedded KV used.
- **Memtable flush** — when the memtable exceeds `flush_threshold_bytes`, it is frozen and written to a numbered L0 SSTable (`sst_NNNNNNNN.sst`); the WAL is truncated atomically (manifest updated first via `os.replace`).
- **Multi-layer reads** — `get` searches active memtable → L0 SSTables newest-first → L1+ with binary search on non-overlapping key ranges; first hit wins.
- **Tombstone shadowing** — a delete in a newer layer correctly hides an older value in any earlier SSTable.
- **Bloom filter (hand-rolled)** — each SSTable embeds a per-file bloom filter (Kirsch–Mitzenmacher double-hashing, pure stdlib `hashlib.sha256`). Parameters derived from entry count and target FPR (default 1 %): for 1 000 entries, m = 9 586 bits (1 199 bytes), k = 7. Measured FPR = 1.00 % (matches theoretical). `DB.get` checks the bloom before any block reads; an SSTable whose bloom rejects the key is skipped entirely.
- **Leveled compaction (hand-rolled)** — L0 files (key ranges may overlap) are merged into L1 (non-overlapping key ranges) via heapq multi-way merge when the L0 file count reaches `l0_compaction_trigger` (default 4). Compaction cascades to L2+ when size limits are exceeded. Tombstones are physically dropped at the bottom level. Crash-safe: new SSTables are fsynced, the MANIFEST is atomically renamed, then old files are deleted.
- **Definitive read/write amplification measurement** — `DB.stats` exposes `write_amplification`, `gross_read_amplification`, and `read_amplification` counters. Measured at 20 000 ops (16 B key / 64 B value, sync=off): write amplification 1.82×; gross RA drops from 1.46 → 1.00 after compaction. See [`docs/PERFORMANCE.md`](docs/PERFORMANCE.md) for full methodology and caveats.

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

The suite (`tests/`) includes a **property-based oracle test**: 3000 random `put`/`delete`/`get` operations are run against both the engine and a plain `dict`, with the database reopened mid-sequence (simulating crashes) — the two must agree on every key throughout. Plus targeted tests for torn-tail recovery, CRC corruption, and compaction crash scenarios (orphaned SSTable + mid-compaction simulated crash).

## Honesty note

Only what is verified is marked complete; later phases are marked planned. Performance numbers will only appear once Phase 4 measures them with a stated method (machine, data size, on-disk vs. in-memory) — no inflated figures.
