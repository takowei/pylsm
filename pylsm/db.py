"""Key-value store: Phase 4 — Leveled compaction + read/write amplification stats.

Write path
──────────
  WAL (append, CRC)  →  active memtable (skiplist)
  When memtable bytes ≥ flush_threshold: flush to a new L0 SSTable, rotate WAL.
  When |L0| ≥ l0_compaction_trigger: compact L0 → L1 (cascade if L1 is full).

Read path
─────────
  active memtable  →  L0 newest-first (all files, may overlap)
                   →  L1, L2, … (at most one file per level; non-overlapping)
  First hit wins; a TOMBSTONE hit means the key was deleted.

Crash recovery
──────────────
  On open:
    1. Read MANIFEST → learn current SSTable list by level and sequence counter.
    2. Load each listed SSTable into the appropriate level.
    3. Replay ``wal.log`` → rebuild the active memtable.

  Flush crash-safety:
    _flush() writes the SSTable (fsynced), updates the MANIFEST (atomic
    rename), then truncates the WAL to zero.

    • Crash before MANIFEST update: the new SSTable file is an orphan
      (ignored on recovery); the intact WAL is replayed.  No data loss.
    • Crash between MANIFEST update and WAL truncation: the WAL data is now
      redundant but safe to replay.  No data loss.

  Compaction crash-safety:
    _compact_level() writes new SSTables (fsynced), atomically updates the
    MANIFEST, then deletes the old files.

    • Crash before MANIFEST update: new files are orphans; old files still
      listed in MANIFEST.  No data loss.
    • Crash after MANIFEST update: new files are canonical; old files are
      not listed and are ignored.  No data loss.

MANIFEST format (v2)
────────────────────
  {
    "seq": <int>,
    "levels": {
      "0": ["sst_00000003.sst", "sst_00000001.sst"],   ← L0 newest-first
      "1": ["sst_00000004.sst", "sst_00000005.sst"]    ← L1 sorted by min_key
    }
  }

  Legacy format (v1) had ``"sstables": [...]``; treated as all-L0 on load.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator

from .skiplist import MISSING as _MEM_MISSING
from .skiplist import SkipList
from .sstable import MISSING as _SST_MISSING
from .sstable import TOMBSTONE, SSTableReader, SSTableWriter
from .stats import DBStats
from .wal import OP_DELETE, OP_PUT, WAL, replay

_WAL_NAME = "wal.log"
_MANIFEST_NAME = "MANIFEST"
_MANIFEST_TMP = "MANIFEST.tmp"

# Default memtable byte threshold before triggering a flush.
_DEFAULT_FLUSH_THRESHOLD = 4 * 1024 * 1024  # 4 MiB

# Default L0 file count before triggering compaction.
_DEFAULT_L0_TRIGGER = 4


def _seq_from_name(name: str) -> int:
    """Extract sequence number from a filename like ``sst_00000042.sst``."""
    return int(os.path.basename(name)[4:12])


class DB:
    """A single-node embedded key-value store with leveled compaction.

    Public attributes
    ─────────────────
    stats : DBStats
        Cumulative read/write amplification counters.  Call ``stats.reset()``
        before a measurement window; read ``stats.write_amplification`` and
        ``stats.read_amplification`` afterwards.
    """

    def __init__(
        self,
        path: str,
        *,
        sync: bool = True,
        flush_threshold_bytes: int = _DEFAULT_FLUSH_THRESHOLD,
        l0_compaction_trigger: int = _DEFAULT_L0_TRIGGER,
    ) -> None:
        self.path = path
        self._sync = sync
        self._flush_threshold = flush_threshold_bytes
        self._l0_trigger = l0_compaction_trigger
        os.makedirs(path, exist_ok=True)
        self._wal_path = os.path.join(path, _WAL_NAME)
        self._mem = SkipList()
        # _levels[i] = list of (seq, SSTableReader) for level i.
        # Level 0: newest-first (highest seq first).
        # Level 1+: sorted by min_key (non-overlapping invariant).
        self._levels: list[list[tuple[int, SSTableReader]]] = []
        self._manifest_seq = 0
        self.stats = DBStats()
        self._recover()
        self._wal = WAL(self._wal_path, sync=sync)

    # ------------------------------------------------------------------
    # MANIFEST helpers
    # ------------------------------------------------------------------

    def _read_manifest(self) -> dict:
        path = os.path.join(self.path, _MANIFEST_NAME)
        if not os.path.exists(path):
            return {"seq": 0, "levels": {}}
        with open(path) as f:
            data = json.load(f)
        # Handle legacy v1 format: {"seq": ..., "sstables": [...]}.
        if "sstables" in data and "levels" not in data:
            data = {"seq": data["seq"], "levels": {"0": data["sstables"]}}
        return data

    def _write_manifest(self) -> None:
        """Atomically persist the current level structure and sequence counter."""
        levels_data: dict[str, list[str]] = {}
        for i, level_ssts in enumerate(self._levels):
            if level_ssts:
                levels_data[str(i)] = [os.path.basename(r.path) for _, r in level_ssts]
        data = {"seq": self._manifest_seq, "levels": levels_data}
        tmp = os.path.join(self.path, _MANIFEST_TMP)
        final = os.path.join(self.path, _MANIFEST_NAME)
        with open(tmp, "w") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, final)

    # ------------------------------------------------------------------
    # Recovery
    # ------------------------------------------------------------------

    def _recover(self) -> None:
        """Rebuild in-memory state from MANIFEST + WAL after any stop."""
        manifest = self._read_manifest()
        self._manifest_seq = manifest["seq"]
        levels_data: dict[str, list[str]] = manifest.get("levels", {})

        # Find the highest level index.
        if levels_data:
            max_level = max(int(k) for k in levels_data)
        else:
            max_level = -1

        # Initialise empty level lists.
        self._levels = [[] for _ in range(max_level + 1)]

        for level_str, names in levels_data.items():
            level = int(level_str)
            pairs: list[tuple[int, SSTableReader]] = []
            for name in names:
                sst_path = os.path.join(self.path, name)
                if os.path.exists(sst_path):
                    seq = _seq_from_name(name)
                    pairs.append((seq, SSTableReader(sst_path)))
            if level == 0:
                # L0: newest-first (highest seq first).
                pairs.sort(key=lambda x: x[0], reverse=True)
            else:
                # L1+: sorted by min_key (non-overlapping invariant).
                pairs.sort(key=lambda x: x[1].min_key or b"")
            self._levels[level] = pairs

        # Replay the WAL into the active memtable.
        for op, key, value in replay(self._wal_path):
            if op == OP_PUT:
                self._mem.insert(key, value)
            elif op == OP_DELETE:
                self._mem.insert(key, TOMBSTONE)

    # ------------------------------------------------------------------
    # Flush
    # ------------------------------------------------------------------

    def _flush(self) -> None:
        """Flush the active memtable to a new L0 SSTable, then rotate the WAL."""
        if len(self._mem) == 0:
            return

        # 1. Freeze the active memtable; new writes go to a fresh one.
        imm = self._mem
        self._mem = SkipList()

        # 2. Write the SSTable (includes fsync before returning).
        self._manifest_seq += 1
        sst_name = f"sst_{self._manifest_seq:08d}.sst"
        sst_path = os.path.join(self.path, sst_name)
        writer = SSTableWriter(sst_path)
        for key, value in imm.items():
            writer.add(key, None if value is TOMBSTONE else value)
        writer.finish()
        self.stats.disk_bytes_written += writer.file_size

        # 3. Register the new SSTable as the newest L0 file.
        if not self._levels:
            self._levels.append([])
        self._levels[0].insert(0, (self._manifest_seq, SSTableReader(sst_path)))

        # 4. Atomically persist the manifest.
        self._write_manifest()

        # 5. Rotate the WAL.
        self._wal.close()
        with open(self._wal_path, "wb") as wf:
            if self._sync:
                os.fsync(wf.fileno())
        self._wal = WAL(self._wal_path, sync=self._sync)

    def _maybe_flush(self) -> None:
        if self._mem.nbytes >= self._flush_threshold:
            self._flush()
            self._maybe_compact()

    def _maybe_compact(self) -> None:
        """Trigger compaction if L0 has reached the file-count threshold."""
        if self._levels and len(self._levels[0]) >= self._l0_trigger:
            from .compaction import do_compaction

            do_compaction(self)

    # ------------------------------------------------------------------
    # Level-aware read helpers
    # ------------------------------------------------------------------

    def _find_in_level(
        self,
        level_ssts: list[tuple[int, SSTableReader]],
        key: bytes,
    ) -> SSTableReader | None:
        """Binary search a sorted L1+ level for the SSTable whose range covers *key*.

        Returns the reader if found, ``None`` if the key is definitely absent
        from this level (no SSTable spans the key's range).
        """
        lo, hi, result = 0, len(level_ssts) - 1, -1
        while lo <= hi:
            mid = (lo + hi) // 2
            _, sst = level_ssts[mid]
            if sst.min_key is not None and sst.min_key <= key:
                result = mid
                lo = mid + 1
            else:
                hi = mid - 1
        if result == -1:
            return None
        _, sst = level_ssts[result]
        if sst.max_key is not None and sst.max_key < key:
            return None  # key lies beyond this SSTable's range
        return sst

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def put(self, key: bytes, value: bytes) -> None:
        if not isinstance(key, bytes) or not isinstance(value, bytes):
            raise TypeError("key and value must be bytes")
        self._wal.append(OP_PUT, key, value)
        self._mem.insert(key, value)
        self.stats.user_bytes_written += len(key) + len(value)
        self._maybe_flush()

    def delete(self, key: bytes) -> None:
        if not isinstance(key, bytes):
            raise TypeError("key must be bytes")
        self._wal.append(OP_DELETE, key)
        self._mem.insert(key, TOMBSTONE)
        self._maybe_flush()

    def get(self, key: bytes) -> bytes | None:
        """Return the value, or ``None`` if the key is absent or deleted."""
        # 1. Active memtable (no I/O).
        found = self._mem.get(key)
        if found is not _MEM_MISSING:
            return None if found is TOMBSTONE else found  # type: ignore[return-value]

        # 2. SSTables — track access stats.
        self.stats.get_calls += 1

        # Level 0: check all files newest-first (key ranges may overlap).
        # Every L0 file is a candidate (no key-range guarantee across files).
        if self._levels:
            for _seq, sst in self._levels[0]:
                self.stats.sst_candidates += 1
                if sst.bloom is not None and key not in sst.bloom:
                    continue
                self.stats.sst_accesses += 1
                result = sst.get(key)
                if result is not _SST_MISSING:
                    return None if result is TOMBSTONE else result  # type: ignore[return-value]

        # Level 1+: at most one SSTable per level is a candidate because the
        # non-overlapping invariant lets us binary-search by key range.
        for level_ssts in self._levels[1:]:
            sst = self._find_in_level(level_ssts, key)
            if sst is None:
                continue
            self.stats.sst_candidates += 1
            if sst.bloom is not None and key not in sst.bloom:
                continue
            self.stats.sst_accesses += 1
            result = sst.get(key)
            if result is not _SST_MISSING:
                return None if result is TOMBSTONE else result  # type: ignore[return-value]

        return None

    @property
    def _sstables(self) -> list[SSTableReader]:
        """All SSTable readers in read order, across all levels.

        Level 0 is returned newest-first; L1+ are sorted by key range.
        Provided for backward-compatibility with Phase 2/3 tests.
        """
        result: list[SSTableReader] = []
        for level_ssts in self._levels:
            result.extend(r for _, r in level_ssts)
        return result

    def compact(self) -> None:
        """Manually trigger a compaction pass (useful for benchmarking).

        Runs ``do_compaction`` regardless of whether the L0 trigger has fired.
        Safe to call at any time; a no-op if there is nothing to compact.
        """
        from .compaction import do_compaction

        do_compaction(self)

    def close(self) -> None:
        self._wal.close()

    def __enter__(self) -> DB:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Multi-level ordered scan (used by the MVCC layer)
    # ------------------------------------------------------------------

    def _scan_from(self, start: bytes = b"") -> Iterator[tuple[bytes, bytes]]:
        """Yield ``(key, value)`` for all live entries with key ≥ *start*, ascending.

        KV-level tombstones (from :meth:`delete`) are skipped.  When the same
        physical key exists in multiple levels, the newest source wins
        (memtable > L0 newest-first > L1 > …).  Uses a ``heapq`` multi-way merge.

        This is an internal primitive — callers must not modify the DB while
        iterating (single-threaded invariant).
        """
        import heapq

        # Monotone counter keeps heap tuples unambiguously ordered even when
        # key and priority match, so Python never tries to compare value/iterator.
        _ctr: list[int] = [0]

        def _next_tb() -> int:
            v = _ctr[0]
            _ctr[0] += 1
            return v

        # Each source is (priority, iterator-of-(key, raw_value)).
        # Higher priority ≡ newer data; wins for the same physical key.
        sources: list[tuple[int, Iterator[tuple[bytes, object]]]] = []
        prio = 1_000_000  # large enough; decremented per source added

        # Active memtable — always newest.
        def _mem_gen() -> Iterator[tuple[bytes, object]]:
            for k, v in self._mem.items():
                if k >= start:
                    yield k, v

        sources.append((prio, _mem_gen()))
        prio -= 1

        # L0 SSTables, already stored newest-first.
        if self._levels:
            for _seq, sst in self._levels[0]:

                def _l0_gen(r: SSTableReader = sst) -> Iterator[tuple[bytes, object]]:
                    for k, v in r.items():
                        if k >= start:
                            yield k, v

                sources.append((prio, _l0_gen()))
                prio -= 1

        # L1+ SSTables (sorted by min_key within each level).
        for level_ssts in self._levels[1:]:
            for _seq, sst in level_ssts:

                def _ln_gen(r: SSTableReader = sst) -> Iterator[tuple[bytes, object]]:
                    for k, v in r.items():
                        if k >= start:
                            yield k, v

                sources.append((prio, _ln_gen()))
                prio -= 1

        # Heap entries: (key, neg_priority, tiebreak, raw_value, iterator).
        # Comparison terminates at tiebreak (unique) so value/iterator are safe.
        heap: list[tuple[bytes, int, int, object, object]] = []
        for p, it in sources:
            try:
                k, v = next(it)  # type: ignore[call-overload]
                heapq.heappush(heap, (k, -p, _next_tb(), v, it))
            except StopIteration:
                pass

        last_key: bytes | None = None
        while heap:
            k, neg_p, _, raw, it = heapq.heappop(heap)

            if k != last_key:
                last_key = k
                # Skip KV-level tombstones: memtable stores TOMBSTONE sentinel;
                # SSTableReader.items() yields None for deleted entries.
                if raw is not TOMBSTONE and raw is not None:
                    yield k, raw  # type: ignore[misc]

            # Advance the source that produced this entry.
            try:
                nk, nv = next(it)  # type: ignore[call-overload]
                heapq.heappush(heap, (nk, neg_p, _next_tb(), nv, it))
            except StopIteration:
                pass
