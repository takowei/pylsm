"""Write-ahead log: durability for writes that have not yet been flushed.

Every mutation is appended here *before* it touches the memtable, so a crash
can never lose an acknowledged write — on restart we replay the log.

On-disk record framing (all integers little-endian, unsigned):

    ┌──────────┬───────────┬───────────────── payload ─────────────────┐
    │ u32 len  │ u32 crc32 │ u8 op │ u32 klen │ key │ u32 vlen │ value  │
    └──────────┴───────────┴────────────────────────────────────────────┘
      └ len = number of payload bytes; crc32 is computed over the payload.

``crc32`` lets us detect a *torn write* — a record that was only partially
flushed when the process died. During replay, the first record that is
truncated or whose checksum does not match ends the log: everything before it
is intact and is returned; the damaged tail is discarded.
"""

from __future__ import annotations

import os
import struct
import zlib
from collections.abc import Iterator

OP_PUT = 0
OP_DELETE = 1

_HEADER = struct.Struct("<II")  # payload length, crc32
_OP_HEADER = struct.Struct("<BI")  # op type, key length
_VLEN = struct.Struct("<I")  # value length


def _encode(op: int, key: bytes, value: bytes) -> bytes:
    payload = _OP_HEADER.pack(op, len(key)) + key + _VLEN.pack(len(value)) + value
    return _HEADER.pack(len(payload), zlib.crc32(payload)) + payload


class WAL:
    """Append-only write-ahead log."""

    def __init__(self, path: str, *, sync: bool = True) -> None:
        self.path = path
        self._sync = sync
        # Append, binary. Created if missing; never truncated.
        self._f = open(path, "ab", buffering=0)

    def append(self, op: int, key: bytes, value: bytes = b"") -> None:
        self._f.write(_encode(op, key, value))
        if self._sync:
            os.fsync(self._f.fileno())

    def close(self) -> None:
        self._f.close()


def replay(path: str) -> Iterator[tuple[int, bytes, bytes]]:
    """Yield ``(op, key, value)`` for every intact record, stopping at the
    first truncated or corrupt one (the torn tail)."""
    if not os.path.exists(path):
        return
    with open(path, "rb") as f:
        data = f.read()

    off = 0
    n = len(data)
    while off + _HEADER.size <= n:
        plen, crc = _HEADER.unpack_from(data, off)
        start = off + _HEADER.size
        end = start + plen
        if end > n:
            break  # truncated payload — torn tail
        payload = data[start:end]
        if zlib.crc32(payload) != crc:
            break  # corrupt record — torn tail
        op, klen = _OP_HEADER.unpack_from(payload, 0)
        key_start = _OP_HEADER.size
        key = payload[key_start : key_start + klen]
        (vlen,) = _VLEN.unpack_from(payload, key_start + klen)
        vstart = key_start + klen + _VLEN.size
        value = payload[vstart : vstart + vlen]
        yield op, key, value
        off = end
