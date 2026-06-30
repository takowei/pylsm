"""SSTable (Sorted String Table): an immutable, sorted, on-disk key-value file.

Binary layout
─────────────

  ┌──────────────────────────────────────────────────────────────────┐
  │  Data block 0  (variable length, up to ~BLOCK_SIZE bytes)        │
  │  Data block 1                                                     │
  │  …                                                               │
  │  Data block N                                                     │
  ├──────────────────────────────────────────────────────────────────┤
  │  Index block  (one sparse-index entry per data block)            │
  ├──────────────────────────────────────────────────────────────────┤
  │  Footer (16 bytes, fixed size)                                   │
  └──────────────────────────────────────────────────────────────────┘

Data entry wire format (variable length):
  klen  : u32  — key byte length
  key   : bytes[klen]
  flags : u8   — 0 = live value, 1 = tombstone (no value bytes follow)
  vlen  : u32  — value byte length  (only present when flags == 0)
  value : bytes[vlen]               (only present when flags == 0)

Data block wire format:
  num_entries : u32
  <entries — variable length>

Index entry wire format (one per data block):
  klen         : u32  — byte length of the first key in this block
  key          : bytes[klen]
  block_offset : u64  — byte offset of the block start in the file
  block_length : u32  — byte length of the block (including 4-byte header)

Index block wire format:
  num_entries : u32
  <index entries — variable length>

Footer wire format (last FOOTER_SIZE = 16 bytes of the file):
  index_offset : u64  — byte offset where the index block begins
  index_length : u32  — byte length of the index block
  magic        : u32  — always MAGIC (0x7079_6C73)
"""

from __future__ import annotations

import io
import os
import struct
from collections.abc import Iterator
from typing import Any

# Target size (bytes) for each data block before starting a new one.
BLOCK_SIZE = 4096

# Magic number identifying a valid pylsm SSTable file.
# 'pyls' as four ASCII bytes read in little-endian order → 0x736C7970;
# we store the constant as written, not its wire representation.
MAGIC: int = 0x7079_6C73

# Sentinel returned by SSTableReader.get when the key is present but deleted.
TOMBSTONE: Any = object()

# Sentinel returned by SSTableReader.get when the key is absent from this file.
MISSING: Any = object()

_FOOTER = struct.Struct("<QII")  # index_offset u64, index_length u32, magic u32
_U32 = struct.Struct("<I")
_U64 = struct.Struct("<Q")
_ENTRY_HDR = struct.Struct("<IB")  # klen u32, flags u8
_VLEN = struct.Struct("<I")

FOOTER_SIZE: int = _FOOTER.size  # 16 bytes


# ---------------------------------------------------------------------------
# Wire-encoding helpers
# ---------------------------------------------------------------------------


def _encode_entry(key: bytes, value: bytes | None) -> bytes:
    """Return the wire bytes for one data entry.  ``value=None`` is a tombstone."""
    if value is None:
        return _ENTRY_HDR.pack(len(key), 1) + key
    return _ENTRY_HDR.pack(len(key), 0) + key + _VLEN.pack(len(value)) + value


def _decode_entry(buf: bytes, off: int) -> tuple[bytes, bytes | None, int]:
    """Decode one entry starting at *off* in *buf*.

    Returns ``(key, value_or_None, next_offset)``.
    ``value=None`` means the entry is a tombstone.
    """
    klen, flags = _ENTRY_HDR.unpack_from(buf, off)
    off += _ENTRY_HDR.size
    key = buf[off : off + klen]
    off += klen
    if flags == 1:  # tombstone — no value bytes follow
        return key, None, off
    (vlen,) = _VLEN.unpack_from(buf, off)
    off += _VLEN.size
    value = buf[off : off + vlen]
    off += vlen
    return key, value, off


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


class SSTableWriter:
    """Builds a single SSTable file from a caller-supplied ordered sequence.

    Usage::

        writer = SSTableWriter(path)
        for key, value in sorted_pairs:   # value is bytes or None (tombstone)
            writer.add(key, value)
        writer.finish()                   # flushes, writes footer, fsyncs
    """

    def __init__(self, path: str, *, block_size: int = BLOCK_SIZE) -> None:
        self._path = path
        self._block_size = block_size
        # Accumulate all output in-memory; a single write() call at finish() time
        # keeps the file coherent (or absent) even on a mid-flush crash.
        self._buf = io.BytesIO()
        # Encoded entries waiting to be sealed into a block.
        self._pending: list[bytes] = []
        self._pending_size: int = 0
        # Sparse index: (first_key, block_offset, block_length).
        self._index: list[tuple[bytes, int, int]] = []

    def add(self, key: bytes, value: bytes | None) -> None:
        """Append one entry.  Keys must be provided in strictly ascending order.

        ``value=None`` writes a tombstone (marks the key as deleted).
        """
        encoded = _encode_entry(key, value)
        self._pending.append(encoded)
        self._pending_size += len(encoded)
        if self._pending_size >= self._block_size:
            self._seal_block()

    def _seal_block(self) -> None:
        """Flush the current pending entries as a completed data block."""
        if not self._pending:
            return
        # Extract the first key from the first entry for the sparse index.
        first_enc = self._pending[0]
        klen, _ = _ENTRY_HDR.unpack_from(first_enc, 0)
        first_key = first_enc[_ENTRY_HDR.size : _ENTRY_HDR.size + klen]

        block_start = self._buf.tell()
        self._buf.write(_U32.pack(len(self._pending)))
        for enc in self._pending:
            self._buf.write(enc)
        block_len = self._buf.tell() - block_start

        self._index.append((first_key, block_start, block_len))
        self._pending.clear()
        self._pending_size = 0

    def _write_index(self) -> tuple[int, int]:
        """Append the sparse index block; return (index_offset, index_length)."""
        idx_start = self._buf.tell()
        self._buf.write(_U32.pack(len(self._index)))
        for first_key, block_off, block_len in self._index:
            self._buf.write(
                _U32.pack(len(first_key)) + first_key + _U64.pack(block_off) + _U32.pack(block_len)
            )
        return idx_start, self._buf.tell() - idx_start

    def finish(self) -> None:
        """Seal the last block, append the index and footer, then fsync to disk."""
        self._seal_block()
        idx_off, idx_len = self._write_index()
        self._buf.write(_FOOTER.pack(idx_off, idx_len, MAGIC))
        with open(self._path, "wb", buffering=0) as f:
            f.write(self._buf.getvalue())
            os.fsync(f.fileno())


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


class SSTableReader:
    """Reads an SSTable file produced by :class:`SSTableWriter`.

    The entire file is loaded into a ``bytes`` buffer at construction so that
    repeated ``get`` calls pay no additional I/O.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        with open(path, "rb") as f:
            self._data: bytes = f.read()
        # Sparse index entries: (first_key, block_offset, block_length).
        self._index: list[tuple[bytes, int, int]] = []
        self._load_index()

    def _load_index(self) -> None:
        n = len(self._data)
        footer_off = n - FOOTER_SIZE
        if footer_off < 0:
            raise ValueError(f"File too small to be an SSTable: {self.path!r}")
        idx_off, idx_len, magic = _FOOTER.unpack_from(self._data, footer_off)
        if magic != MAGIC:
            raise ValueError(f"Bad magic 0x{magic:08X} in {self.path!r}; expected 0x{MAGIC:08X}")
        off = idx_off
        (num_entries,) = _U32.unpack_from(self._data, off)
        off += _U32.size
        for _ in range(num_entries):
            (klen,) = _U32.unpack_from(self._data, off)
            off += _U32.size
            first_key = self._data[off : off + klen]
            off += klen
            (block_off,) = _U64.unpack_from(self._data, off)
            off += _U64.size
            (block_len,) = _U32.unpack_from(self._data, off)
            off += _U32.size
            self._index.append((first_key, block_off, block_len))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _read_block(self, block_off: int, block_len: int) -> list[tuple[bytes, bytes | None]]:
        """Decode all entries in one data block; return list of (key, value|None)."""
        buf = self._data
        off = block_off
        (n,) = _U32.unpack_from(buf, off)
        off += _U32.size
        entries: list[tuple[bytes, bytes | None]] = []
        for _ in range(n):
            key, value, off = _decode_entry(buf, off)
            entries.append((key, value))
        return entries

    def _find_block(self, key: bytes) -> int:
        """Binary-search the sparse index for the last block whose first key ≤ *key*.

        Returns the block index, or -1 if *key* precedes all blocks (definitely absent).
        """
        lo, hi, result = 0, len(self._index) - 1, -1
        while lo <= hi:
            mid = (lo + hi) // 2
            if self._index[mid][0] <= key:
                result = mid
                lo = mid + 1
            else:
                hi = mid - 1
        return result

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, key: bytes) -> bytes | Any:
        """Look up *key*.

        Returns:
          - ``bytes`` — the live value.
          - ``TOMBSTONE`` — the key was explicitly deleted in this SSTable.
          - ``MISSING`` — the key is not present in this SSTable at all.
        """
        if not self._index:
            return MISSING
        idx = self._find_block(key)
        if idx == -1:
            return MISSING
        _, block_off, block_len = self._index[idx]
        for k, v in self._read_block(block_off, block_len):
            if k == key:
                return TOMBSTONE if v is None else v
            if k > key:
                break
        return MISSING

    def items(self) -> Iterator[tuple[bytes, bytes | None]]:
        """Yield all ``(key, value)`` pairs in ascending key order.

        Tombstones are yielded as ``(key, None)``.
        """
        for _, block_off, block_len in self._index:
            yield from self._read_block(block_off, block_len)
