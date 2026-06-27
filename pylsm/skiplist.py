"""A hand-written skiplist used as the in-memory sorted table (memtable).

A skiplist keeps entries in sorted key order with expected O(log n) search,
insert and delete, using randomised "express lanes" instead of tree rotations.
We use it (rather than a dict) because the memtable must be flushed to an
SSTable in sorted order, and because range scans need ordered iteration.

Values are opaque objects: the DB layer stores raw ``bytes`` for a live value
and a ``TOMBSTONE`` sentinel for a deletion. The skiplist itself does not
interpret them.
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from typing import Any

MAX_LEVEL = 16
_P = 0.5

# Returned by ``get`` when a key is absent. Distinct from ``None`` and from any
# stored value (including a tombstone), so callers can tell "no such key" apart
# from "key explicitly mapped to something falsy".
MISSING: Any = object()


class _Node:
    __slots__ = ("key", "value", "forward")

    def __init__(self, key: bytes | None, value: Any, level: int) -> None:
        self.key = key
        self.value = value
        self.forward: list[_Node | None] = [None] * level


class SkipList:
    """An ordered map from ``bytes`` keys to arbitrary values."""

    def __init__(self, *, rng: random.Random | None = None) -> None:
        # Head is a sentinel with the maximum height; its key is None and
        # compares lower than every real key.
        self._head = _Node(None, None, MAX_LEVEL)
        self._level = 1
        self._len = 0
        self._rng = rng or random.Random()
        # Rough heap footprint of stored keys+values, for the flush threshold.
        self._nbytes = 0

    def __len__(self) -> int:
        return self._len

    @property
    def nbytes(self) -> int:
        return self._nbytes

    def _random_level(self) -> int:
        level = 1
        while level < MAX_LEVEL and self._rng.random() < _P:
            level += 1
        return level

    def insert(self, key: bytes, value: Any) -> None:
        """Insert ``key`` or overwrite its existing value."""
        update: list[_Node] = [self._head] * MAX_LEVEL
        node = self._head
        for i in reversed(range(self._level)):
            nxt = node.forward[i]
            while nxt is not None and nxt.key < key:
                node = nxt
                nxt = node.forward[i]
            update[i] = node

        nxt = node.forward[0]
        if nxt is not None and nxt.key == key:
            self._nbytes += _value_size(value) - _value_size(nxt.value)
            nxt.value = value
            return

        new_level = self._random_level()
        if new_level > self._level:
            for i in range(self._level, new_level):
                update[i] = self._head
            self._level = new_level

        new_node = _Node(key, value, new_level)
        for i in range(new_level):
            new_node.forward[i] = update[i].forward[i]
            update[i].forward[i] = new_node

        self._len += 1
        self._nbytes += len(key) + _value_size(value)

    def get(self, key: bytes) -> Any:
        """Return the stored value, or ``MISSING`` if the key is absent."""
        node = self._head
        for i in reversed(range(self._level)):
            nxt = node.forward[i]
            while nxt is not None and nxt.key < key:
                node = nxt
                nxt = node.forward[i]
        nxt = node.forward[0]
        if nxt is not None and nxt.key == key:
            return nxt.value
        return MISSING

    def items(self) -> Iterator[tuple[bytes, Any]]:
        """Yield ``(key, value)`` pairs in ascending key order."""
        node = self._head.forward[0]
        while node is not None:
            assert node.key is not None
            yield node.key, node.value
            node = node.forward[0]


def _value_size(value: Any) -> int:
    return len(value) if isinstance(value, bytes) else 1
