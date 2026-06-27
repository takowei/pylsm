import random

from pylsm.skiplist import MISSING, SkipList


def test_insert_get_and_overwrite():
    sl = SkipList()
    sl.insert(b"b", b"2")
    sl.insert(b"a", b"1")
    assert sl.get(b"a") == b"1"
    assert sl.get(b"b") == b"2"
    sl.insert(b"a", b"99")  # overwrite
    assert sl.get(b"a") == b"99"
    assert len(sl) == 2


def test_missing_is_distinct_from_none():
    sl = SkipList()
    sl.insert(b"k", None)  # a stored None (e.g. a tombstone sentinel)
    assert sl.get(b"k") is None
    assert sl.get(b"absent") is MISSING


def test_items_are_sorted():
    sl = SkipList(rng=random.Random(1))
    keys = [f"{i:03d}".encode() for i in range(200)]
    shuffled = keys[:]
    random.Random(7).shuffle(shuffled)
    for k in shuffled:
        sl.insert(k, b"v")
    assert [k for k, _ in sl.items()] == sorted(keys)
    assert len(sl) == 200


def test_nbytes_tracks_overwrite():
    sl = SkipList()
    sl.insert(b"k", b"xx")
    base = sl.nbytes
    sl.insert(b"k", b"yyyy")  # +2 bytes value, key unchanged
    assert sl.nbytes == base + 2
