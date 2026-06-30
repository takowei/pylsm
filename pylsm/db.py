"""Key-value store: Phase 2 — SSTable flush + multi-layer reads.

Write path
──────────
  WAL (append, CRC)  →  active memtable (skiplist)
  When memtable bytes ≥ flush_threshold: flush to a new SSTable (L0), rotate WAL.

Read path
─────────
  active memtable  →  SSTables newest-first
  First hit wins; a TOMBSTONE hit means the key was deleted.

Crash recovery
──────────────
  On open:
    1. Read MANIFEST → learn current SSTable list and sequence counter.
    2. Load each listed SSTable (newest first) into memory.
    3. Replay ``wal.log`` → rebuild the active memtable.

  Flush crash-safety (WAL is always ``wal.log``):
    _flush() writes the SSTable (fsynced), updates the MANIFEST (atomic
    rename), then truncates the WAL to zero.

    • Crash before MANIFEST update: the new SSTable file is an orphan
      (ignored on recovery); the intact WAL is replayed.  No data loss.
    • Crash between MANIFEST update and WAL truncation: the WAL data is now
      redundant but safe to replay — it produces the same memtable state as
      the SSTable already holds.  No data loss.
"""

from __future__ import annotations

import json
import os

from .skiplist import MISSING as _MEM_MISSING
from .skiplist import SkipList
from .sstable import MISSING as _SST_MISSING
from .sstable import TOMBSTONE, SSTableReader, SSTableWriter
from .wal import OP_DELETE, OP_PUT, WAL, replay

_WAL_NAME = "wal.log"
_MANIFEST_NAME = "MANIFEST"
_MANIFEST_TMP = "MANIFEST.tmp"

# Default memtable byte threshold before triggering a flush.
_DEFAULT_FLUSH_THRESHOLD = 4 * 1024 * 1024  # 4 MiB


class DB:
    """A single-node embedded key-value store."""

    def __init__(
        self,
        path: str,
        *,
        sync: bool = True,
        flush_threshold_bytes: int = _DEFAULT_FLUSH_THRESHOLD,
    ) -> None:
        self.path = path
        self._sync = sync
        self._flush_threshold = flush_threshold_bytes
        os.makedirs(path, exist_ok=True)
        self._wal_path = os.path.join(path, _WAL_NAME)
        self._mem = SkipList()
        # Loaded newest-first; searches proceed left-to-right so the newest
        # SSTable is consulted before older ones.
        self._sstables: list[SSTableReader] = []
        self._manifest_seq = 0
        self._recover()
        self._wal = WAL(self._wal_path, sync=sync)

    # ------------------------------------------------------------------
    # MANIFEST helpers
    # ------------------------------------------------------------------

    def _read_manifest(self) -> dict:
        path = os.path.join(self.path, _MANIFEST_NAME)
        if not os.path.exists(path):
            return {"seq": 0, "sstables": []}
        with open(path) as f:
            return json.load(f)

    def _write_manifest(self) -> None:
        """Atomically persist the current SSTable list and sequence counter."""
        data = {
            "seq": self._manifest_seq,
            "sstables": [os.path.basename(r.path) for r in self._sstables],
        }
        tmp = os.path.join(self.path, _MANIFEST_TMP)
        final = os.path.join(self.path, _MANIFEST_NAME)
        with open(tmp, "w") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, final)  # atomic on POSIX

    # ------------------------------------------------------------------
    # Recovery
    # ------------------------------------------------------------------

    def _recover(self) -> None:
        """Rebuild in-memory state from the MANIFEST + WAL after any stop."""
        manifest = self._read_manifest()
        self._manifest_seq = manifest["seq"]
        # Load SSTables (manifest stores them newest-first).
        for sst_name in manifest["sstables"]:
            sst_path = os.path.join(self.path, sst_name)
            if os.path.exists(sst_path):
                self._sstables.append(SSTableReader(sst_path))
        # Replay the WAL into the fresh memtable.
        for op, key, value in replay(self._wal_path):
            if op == OP_PUT:
                self._mem.insert(key, value)
            elif op == OP_DELETE:
                self._mem.insert(key, TOMBSTONE)

    # ------------------------------------------------------------------
    # Flush
    # ------------------------------------------------------------------

    def _flush(self) -> None:
        """Flush the active memtable to a new SSTable file, then rotate the WAL."""
        if len(self._mem) == 0:
            return

        # 1. Freeze the active memtable; new writes will accumulate in a fresh one.
        imm = self._mem
        self._mem = SkipList()

        # 2. Write the SSTable (includes fsync before returning).
        self._manifest_seq += 1
        sst_name = f"sst_{self._manifest_seq:08d}.sst"
        sst_path = os.path.join(self.path, sst_name)
        writer = SSTableWriter(sst_path)
        for key, value in imm.items():
            # The memtable stores TOMBSTONE for deleted keys; the SSTable
            # encodes tombstones as entries with flags=1 and no value bytes.
            writer.add(key, None if value is TOMBSTONE else value)
        writer.finish()

        # 3. Register the new SSTable (prepend = newest first).
        self._sstables.insert(0, SSTableReader(sst_path))

        # 4. Persist the manifest atomically (atomic rename).
        #    A crash before this point leaves the SSTable as an orphan; on
        #    recovery the intact WAL is replayed instead.  Safe.
        self._write_manifest()

        # 5. Rotate the WAL: close, truncate to zero, reopen.
        #    A crash here is safe because the new SSTable is already in the
        #    manifest; any stale WAL content is idempotent when replayed.
        self._wal.close()
        with open(self._wal_path, "wb") as wf:
            if self._sync:
                os.fsync(wf.fileno())
        self._wal = WAL(self._wal_path, sync=self._sync)

    def _maybe_flush(self) -> None:
        if self._mem.nbytes >= self._flush_threshold:
            self._flush()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def put(self, key: bytes, value: bytes) -> None:
        if not isinstance(key, bytes) or not isinstance(value, bytes):
            raise TypeError("key and value must be bytes")
        self._wal.append(OP_PUT, key, value)
        self._mem.insert(key, value)
        self._maybe_flush()

    def delete(self, key: bytes) -> None:
        if not isinstance(key, bytes):
            raise TypeError("key must be bytes")
        self._wal.append(OP_DELETE, key)
        self._mem.insert(key, TOMBSTONE)
        self._maybe_flush()

    def get(self, key: bytes) -> bytes | None:
        """Return the value, or ``None`` if the key is absent or deleted."""
        # 1. Active memtable.
        found = self._mem.get(key)
        if found is not _MEM_MISSING:
            return None if found is TOMBSTONE else found  # type: ignore[return-value]
        # 2. SSTables newest-first; bloom filter skips files that cannot contain key.
        for sst in self._sstables:
            if sst.bloom is not None and key not in sst.bloom:
                continue
            result = sst.get(key)
            if result is not _SST_MISSING:
                return None if result is TOMBSTONE else result  # type: ignore[return-value]
        return None

    def close(self) -> None:
        self._wal.close()

    def __enter__(self) -> DB:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
