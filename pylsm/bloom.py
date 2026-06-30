"""Bloom filter: probabilistic set membership with no false negatives.

Given expected element count *n* and target false-positive rate *p*, the
optimal bit-array size *m* and probe count *k* are derived automatically:

    m  = ⌈-n · ln(p) / ln(2)²⌉   bits in the bit array
    k  = round((m / n) · ln(2))   hash-function rounds (capped at 30)

Uses Kirsch–Mitzenmacher double-hashing: one SHA-256 call per key yields
a 256-bit digest; the first 64 bits become h1, the next 64 bits become h2,
and then k positions are derived as (h1 + i·h2) mod m for i in 0..k-1.
This achieves the same asymptotic false-positive rate as k independent hash
functions while requiring only a single stdlib call.

Wire format (used by :meth:`BloomFilter.to_bytes` / :meth:`BloomFilter.from_bytes`):

    m   : u32          — number of bits in the array
    k   : u8           — number of hash probe rounds
    [⌈m/8⌉ bytes]     — the bit array, little-endian bit ordering
"""

from __future__ import annotations

import hashlib
import math
import struct

_HDR = struct.Struct("<IB")  # m: u32, k: u8  (5 bytes total)


class BloomFilter:
    """Probabilistic set membership with configurable false-positive rate.

    False negatives are impossible: if ``key in bloom`` returns ``False``,
    the key was definitely never added.  A ``True`` result means the key
    *may* be present (the probability of a false positive is at most *p*).
    """

    def __init__(self, expected_items: int, false_positive_rate: float = 0.01) -> None:
        if expected_items <= 0:
            raise ValueError("expected_items must be positive")
        if not (0.0 < false_positive_rate < 1.0):
            raise ValueError("false_positive_rate must be strictly between 0 and 1")
        ln2 = math.log(2)
        m = math.ceil(-expected_items * math.log(false_positive_rate) / (ln2 * ln2))
        k = max(1, min(round((m / expected_items) * ln2), 30))
        self._m: int = m
        self._k: int = k
        self._bits: bytearray = bytearray(math.ceil(m / 8))

    # ------------------------------------------------------------------
    # Read-only properties (exposed for tests and serialization)
    # ------------------------------------------------------------------

    @property
    def m(self) -> int:
        """Total number of bits in the underlying bit array."""
        return self._m

    @property
    def k(self) -> int:
        """Number of hash probe rounds per key."""
        return self._k

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _positions(self, key: bytes):
        """Yield *k* bit-array positions for *key* via double-hashing."""
        digest = hashlib.sha256(key).digest()
        h1 = int.from_bytes(digest[:8], "little")
        h2 = int.from_bytes(digest[8:16], "little")
        for i in range(self._k):
            yield (h1 + i * h2) % self._m

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add(self, key: bytes) -> None:
        """Record *key* in the filter."""
        for pos in self._positions(key):
            self._bits[pos >> 3] |= 1 << (pos & 7)

    def __contains__(self, key: bytes) -> bool:
        """Return ``True`` if *key* may be present; ``False`` means definitely absent."""
        return all(self._bits[pos >> 3] & (1 << (pos & 7)) for pos in self._positions(key))

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_bytes(self) -> bytes:
        """Serialize the filter state to a compact byte string."""
        return _HDR.pack(self._m, self._k) + bytes(self._bits)

    @classmethod
    def from_bytes(cls, data: bytes) -> BloomFilter:
        """Reconstruct a filter from bytes produced by :meth:`to_bytes`."""
        if len(data) < _HDR.size:
            raise ValueError(f"bloom data too short: {len(data)} bytes")
        m, k = _HDR.unpack_from(data, 0)
        n_bit_bytes = math.ceil(m / 8)
        if len(data) < _HDR.size + n_bit_bytes:
            raise ValueError(
                f"bloom data truncated: need {_HDR.size + n_bit_bytes} bytes, got {len(data)}"
            )
        obj = cls.__new__(cls)
        obj._m = m
        obj._k = k
        obj._bits = bytearray(data[_HDR.size : _HDR.size + n_bit_bytes])
        return obj
