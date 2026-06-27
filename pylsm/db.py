"""The key-value store (Phase 1: memtable + WAL + crash recovery).

Write path:   append WAL  ->  apply to memtable
Read path:    look up memtable  (later phases add SSTables behind it)
Delete:       write a tombstone (LSM never deletes in place)
Recovery:     on open, replay the WAL into a fresh memtable

Keys and values are ``bytes``. A deleted key is stored as a ``TOMBSTONE``
sentinel in the memtable so it shadows any older value; ``get`` reports it as
absent.
"""

from __future__ import annotations

import os
from typing import Any

from .skiplist import MISSING, SkipList
from .wal import OP_DELETE, OP_PUT, WAL, replay

# Sentinel marking a deleted key inside the memtable.
TOMBSTONE: Any = object()

_WAL_NAME = "wal.log"


class DB:
    """A single-node embedded key-value store."""

    def __init__(self, path: str, *, sync: bool = True) -> None:
        self.path = path
        os.makedirs(path, exist_ok=True)
        self._wal_path = os.path.join(path, _WAL_NAME)
        self._mem = SkipList()
        self._recover()
        self._wal = WAL(self._wal_path, sync=sync)

    def _recover(self) -> None:
        """Rebuild the memtable from the WAL after a (possibly unclean) stop."""
        for op, key, value in replay(self._wal_path):
            if op == OP_PUT:
                self._mem.insert(key, value)
            elif op == OP_DELETE:
                self._mem.insert(key, TOMBSTONE)

    def put(self, key: bytes, value: bytes) -> None:
        if not isinstance(key, bytes) or not isinstance(value, bytes):
            raise TypeError("key and value must be bytes")
        self._wal.append(OP_PUT, key, value)
        self._mem.insert(key, value)

    def delete(self, key: bytes) -> None:
        if not isinstance(key, bytes):
            raise TypeError("key must be bytes")
        self._wal.append(OP_DELETE, key)
        self._mem.insert(key, TOMBSTONE)

    def get(self, key: bytes) -> bytes | None:
        """Return the value, or ``None`` if absent or deleted."""
        found = self._mem.get(key)
        if found is MISSING or found is TOMBSTONE:
            return None
        return found

    def close(self) -> None:
        self._wal.close()

    def __enter__(self) -> DB:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
