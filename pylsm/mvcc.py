"""MVCC (Multi-Version Concurrency Control) layer on top of the raw KV engine.

Architecture
────────────
Every user write (put / delete) is stamped with a monotonically increasing
sequence number (seq) and stored as a distinct *physical* key in the underlying
KV store.  No in-place overwrites; old versions persist until compaction GC
(future work).

Physical key encoding
─────────────────────
  physical_key = u32be(len(user_key)) | user_key | u64be(MAX_U64 - seq)

• Big-endian length prefix: unambiguously groups all versions of the same
  user_key together regardless of key content (supports arbitrary bytes incl. \\x00).
• Inverted seq (MAX_U64 − seq): within a user_key block, higher seq produces a
  *smaller* physical key, so a forward scan from encode(user_key, snapshot_seq)
  yields the latest version ≤ snapshot_seq as its first hit.

Physical value encoding
───────────────────────
  live value  →  b'\\x00' + value_bytes
  tombstone   →  b'\\x01'

The underlying KV's own tombstone / delete() mechanism is never used.

Sequence counter persistence
────────────────────────────
The global seq is stored in the underlying KV under _META_SEQ_KEY (a key whose
length-prefix decodes to an impossibly large user-key length, so it can never
collide with a real MVCC physical key).  The meta key is written *before* each
data key so that on crash recovery seq is always ≥ the highest committed seq.

Snapshot Isolation
──────────────────
  Snapshot.get(user_key)
    → DB._scan_from(encode(user_key, snapshot_seq))
    → first entry whose key starts with the user_key prefix
    → live tag → return value; dead tag → return None; no entry → return None

Writes stamped with seq > snapshot_seq have a *smaller* physical key than the
scan start, so they are naturally excluded from the forward scan.

Known limitations (Phase DB-1)
───────────────────────────────
• No version GC: every write appends a new version; old versions accumulate.
• seq meta is written on every MVCC write (2× underlying puts per user write).
• Single-threaded only (same constraint as the underlying DB).
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .db import DB

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_SEQ: int = (1 << 64) - 1  # 0xFFFF_FFFF_FFFF_FFFF

# Tag bytes prepended to every stored value.
_LIVE_TAG: bytes = b"\x00"
_DEAD_TAG: bytes = b"\x01"

# Special KV key used to persist the global sequence counter.
# First 4 bytes decode as klen = 0xFFFF_FFFF (≈ 4 GiB), which is impossible
# for any real MVCC physical key, so this key can never collide.
_META_SEQ_KEY: bytes = b"\xff\xff\xff\xff__mvcc_seq__"

# Struct helpers — big-endian so the byte order matches lexicographic order.
_U32BE = struct.Struct(">I")
_U64BE = struct.Struct(">Q")

# Physical key size overhead: 4 bytes klen prefix + 8 bytes inverted seq.
_KEY_OVERHEAD: int = _U32BE.size + _U64BE.size  # 12


# ---------------------------------------------------------------------------
# Encoding helpers
# ---------------------------------------------------------------------------


def _encode_mvcc_key(user_key: bytes, seq: int) -> bytes:
    """Encode (user_key, seq) as a physical KV key.

    Higher seq ⟹ smaller physical key ⟹ sorts first in a forward scan.
    """
    return _U32BE.pack(len(user_key)) + user_key + _U64BE.pack(_MAX_SEQ - seq)


def _decode_mvcc_key(phys: bytes) -> tuple[bytes, int]:
    """Decode a physical key back into (user_key, seq).  Inverse of _encode_mvcc_key."""
    (klen,) = _U32BE.unpack_from(phys, 0)
    user_key = phys[_U32BE.size : _U32BE.size + klen]
    (inv_seq,) = _U64BE.unpack_from(phys, _U32BE.size + klen)
    return user_key, _MAX_SEQ - inv_seq


def _user_key_prefix(user_key: bytes) -> bytes:
    """Return the fixed prefix shared by all physical keys for *user_key*."""
    return _U32BE.pack(len(user_key)) + user_key


def _encode_live(value: bytes) -> bytes:
    return _LIVE_TAG + value


def _decode_value(raw: bytes) -> bytes | None:
    """Decode a physical value; return ``None`` for tombstones."""
    return raw[1:] if raw[:1] == _LIVE_TAG else None


# ---------------------------------------------------------------------------
# Seq persistence
# ---------------------------------------------------------------------------


def _load_seq(db: DB) -> int:
    raw = db.get(_META_SEQ_KEY)
    if raw is None:
        return 0
    (seq,) = _U64BE.unpack(raw)
    return seq


def _save_seq(db: DB, seq: int) -> None:
    db.put(_META_SEQ_KEY, _U64BE.pack(seq))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class Snapshot:
    """A frozen read view of the MVCC engine at a specific sequence number.

    All reads through a snapshot see exactly the state committed up to and
    including :attr:`seq`.  Writes made after :meth:`MVCCEngine.snapshot` was
    called are invisible, providing snapshot isolation.

    Obtain a snapshot via :meth:`MVCCEngine.snapshot`; do not construct directly.
    """

    def __init__(self, engine: MVCCEngine, seq: int) -> None:
        self._engine = engine
        self.seq = seq

    def get(self, user_key: bytes) -> bytes | None:
        """Return the value of *user_key* as of this snapshot, or ``None`` if absent/deleted."""
        return self._engine.get(user_key, self.seq)

    def __repr__(self) -> str:
        return f"Snapshot(seq={self.seq})"


class MVCCEngine:
    """MVCC layer wrapping a :class:`~pylsm.DB` instance.

    Each :meth:`put` / :meth:`delete` call is stamped with a monotonically
    increasing *sequence number*.  A :class:`Snapshot` captures the seq at a
    point in time; reads through the snapshot see only versions committed up to
    that seq, regardless of later writes.

    Usage::

        from pylsm import DB, MVCCEngine

        with DB("./data") as raw_db:
            engine = MVCCEngine(raw_db)
            engine.put(b"k", b"v1")
            snap = engine.snapshot()
            engine.put(b"k", b"v2")          # invisible to snap
            assert snap.get(b"k") == b"v1"   # snapshot isolation
            assert engine.get(b"k") == b"v2" # current state
    """

    def __init__(self, db: DB) -> None:
        self._db = db
        # Restore persisted seq so we never reuse a committed seq number.
        self._seq: int = _load_seq(db)

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def put(self, user_key: bytes, value: bytes) -> int:
        """Write *value* for *user_key*; return the assigned sequence number.

        The write is durable as soon as the underlying DB acknowledges it
        (WAL append).  The seq counter is persisted *before* the data key so
        that crash recovery never leaves the counter behind the data.
        """
        if not isinstance(user_key, bytes):
            raise TypeError("user_key must be bytes")
        if not isinstance(value, bytes):
            raise TypeError("value must be bytes")
        self._seq += 1
        _save_seq(self._db, self._seq)  # durable before data write
        phys = _encode_mvcc_key(user_key, self._seq)
        self._db.put(phys, _encode_live(value))
        return self._seq

    def delete(self, user_key: bytes) -> int:
        """Mark *user_key* as deleted at a new sequence number; return that seq.

        The deletion is a tombstone version in the MVCC layer — old snapshots
        taken before this seq still see the key as live.
        """
        if not isinstance(user_key, bytes):
            raise TypeError("user_key must be bytes")
        self._seq += 1
        _save_seq(self._db, self._seq)
        phys = _encode_mvcc_key(user_key, self._seq)
        self._db.put(phys, _DEAD_TAG)
        return self._seq

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get(self, user_key: bytes, snapshot_seq: int | None = None) -> bytes | None:
        """Return the latest value of *user_key* visible at *snapshot_seq*.

        If *snapshot_seq* is ``None``, reads the current committed state
        (equivalent to ``snapshot_seq = self.current_seq``).

        Returns ``None`` when the key has never been written or was deleted
        at or before *snapshot_seq*.
        """
        if not isinstance(user_key, bytes):
            raise TypeError("user_key must be bytes")
        if snapshot_seq is None:
            snapshot_seq = self._seq

        prefix = _user_key_prefix(user_key)
        start = _encode_mvcc_key(user_key, snapshot_seq)

        # Forward scan from the physical key for (user_key, snapshot_seq).
        # Versions with seq > snapshot_seq have smaller physical keys (inverted
        # encoding) and are therefore *before* `start` in the scan — naturally
        # excluded.  The first hit with a matching prefix is the highest seq
        # ≤ snapshot_seq.
        for phys_key, raw_value in self._db._scan_from(start):
            if not phys_key.startswith(prefix):
                break  # moved past all versions of this user_key
            return _decode_value(raw_value)

        return None

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    def snapshot(self) -> Snapshot:
        """Return a :class:`Snapshot` capturing the current committed state."""
        return Snapshot(self, self._seq)

    # ------------------------------------------------------------------
    # Scan (yields all current live user keys in MVCC physical key order)
    # ------------------------------------------------------------------

    def scan(self) -> Iterator[tuple[bytes, bytes]]:
        """Yield all live ``(user_key, value)`` pairs as of the current seq.

        Pairs are yielded in the physical-key order, which groups by
        ``len(user_key)`` then ``user_key`` content (not plain lexicographic
        order of user_key).  Suitable for debugging; a future Phase may add
        a user-key-ordered scan.
        """
        seen_prefix: bytes | None = None
        for phys_key, raw_value in self._db._scan_from():
            if phys_key == _META_SEQ_KEY:
                continue  # skip internal metadata
            if len(phys_key) < _KEY_OVERHEAD:
                continue  # not an MVCC key
            try:
                klen = _U32BE.unpack_from(phys_key)[0]
            except struct.error:
                continue
            if klen > len(phys_key) - _KEY_OVERHEAD:
                continue  # malformed or non-MVCC key

            prefix = phys_key[: _U32BE.size + klen]
            if prefix == seen_prefix:
                # Already emitted a version for this user_key (older version).
                continue
            seen_prefix = prefix

            decoded = _decode_value(raw_value)
            if decoded is not None:  # live version
                user_key = phys_key[_U32BE.size : _U32BE.size + klen]
                yield user_key, decoded

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def current_seq(self) -> int:
        """The highest sequence number assigned so far."""
        return self._seq
