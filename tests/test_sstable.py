"""Unit tests for SSTableWriter and SSTableReader.

Covers: roundtrip (single/multi block), tombstones, sparse-index binary search,
ordered iteration, missing-key probes, and corrupt-magic detection.
"""

from __future__ import annotations

import pytest

from pylsm.sstable import MISSING, TOMBSTONE, SSTableReader, SSTableWriter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(tmp_path, pairs: list[tuple[bytes, bytes | None]], block_size: int = 4096) -> str:
    """Write *pairs* to a fresh SSTable and return the file path."""
    path = str(tmp_path / "t.sst")
    w = SSTableWriter(path, block_size=block_size)
    for key, value in pairs:
        w.add(key, value)
    w.finish()
    return path


# ---------------------------------------------------------------------------
# Roundtrip — single block (all entries fit in one 4 KB block)
# ---------------------------------------------------------------------------


class TestRoundtripSingleBlock:
    def test_single_entry(self, tmp_path):
        path = _write(tmp_path, [(b"k", b"v")])
        assert SSTableReader(path).get(b"k") == b"v"

    def test_multiple_entries(self, tmp_path):
        pairs = [(f"key{i:03}".encode(), f"val{i}".encode()) for i in range(20)]
        r = SSTableReader(_write(tmp_path, pairs))
        for k, v in pairs:
            assert r.get(k) == v

    def test_missing_before_first_key(self, tmp_path):
        r = SSTableReader(_write(tmp_path, [(b"b", b"2"), (b"d", b"4")]))
        assert r.get(b"a") is MISSING

    def test_missing_between_keys(self, tmp_path):
        r = SSTableReader(_write(tmp_path, [(b"a", b"1"), (b"c", b"3")]))
        assert r.get(b"b") is MISSING

    def test_missing_after_last_key(self, tmp_path):
        r = SSTableReader(_write(tmp_path, [(b"a", b"1")]))
        assert r.get(b"z") is MISSING

    def test_tombstone_roundtrip(self, tmp_path):
        path = _write(tmp_path, [(b"k", None)])
        assert SSTableReader(path).get(b"k") is TOMBSTONE

    def test_mixed_live_and_tombstone(self, tmp_path):
        pairs = [(b"a", b"1"), (b"b", None), (b"c", b"3")]
        r = SSTableReader(_write(tmp_path, pairs))
        assert r.get(b"a") == b"1"
        assert r.get(b"b") is TOMBSTONE
        assert r.get(b"c") == b"3"
        assert r.get(b"x") is MISSING


# ---------------------------------------------------------------------------
# Multi-block sparse index
# ---------------------------------------------------------------------------


class TestMultiBlockSparseIndex:
    def test_multi_block_all_keys_found(self, tmp_path):
        # Small block_size forces one block per entry.
        pairs = [(f"k{i:02}".encode(), b"x" * 20) for i in range(10)]
        r = SSTableReader(_write(tmp_path, pairs, block_size=10))
        assert len(r._index) > 1, "expected more than one block"
        for k, v in pairs:
            assert r.get(k) == v

    def test_first_key_of_each_block_found(self, tmp_path):
        # Each entry ~120 bytes; block_size=100 → roughly one block per entry.
        pairs = [(f"key{i:03}".encode(), b"y" * 110) for i in range(8)]
        r = SSTableReader(_write(tmp_path, pairs, block_size=100))
        for k, v in pairs:
            assert r.get(k) == v

    def test_key_before_first_block_is_missing(self, tmp_path):
        pairs = [(f"key{i:03}".encode(), b"z" * 50) for i in range(5)]
        r = SSTableReader(_write(tmp_path, pairs, block_size=50))
        assert r.get(b"aaa") is MISSING

    def test_key_after_last_block_is_missing(self, tmp_path):
        pairs = [(f"key{i:03}".encode(), b"z" * 50) for i in range(5)]
        r = SSTableReader(_write(tmp_path, pairs, block_size=50))
        assert r.get(b"zzz") is MISSING

    def test_tombstone_in_middle_block(self, tmp_path):
        pairs: list[tuple[bytes, bytes | None]] = [
            (b"a", b"1"),
            (b"b", None),  # tombstone in the middle
            (b"c", b"3"),
            (b"d", b"4"),
            (b"e", b"5"),
        ]
        # block_size=10 puts entries in separate blocks.
        r = SSTableReader(_write(tmp_path, pairs, block_size=10))
        assert r.get(b"b") is TOMBSTONE
        assert r.get(b"a") == b"1"
        assert r.get(b"c") == b"3"


# ---------------------------------------------------------------------------
# Ordered iteration
# ---------------------------------------------------------------------------


class TestIteration:
    def test_items_in_sorted_order(self, tmp_path):
        pairs = [(f"k{i:03}".encode(), f"v{i}".encode()) for i in range(50)]
        r = SSTableReader(_write(tmp_path, pairs, block_size=64))
        assert list(r.items()) == pairs

    def test_items_includes_tombstones(self, tmp_path):
        pairs = [(b"a", b"1"), (b"b", None), (b"c", b"3")]
        r = SSTableReader(_write(tmp_path, pairs))
        assert list(r.items()) == pairs

    def test_items_empty_sstable(self, tmp_path):
        # An SSTable with no entries should iterate to nothing.
        path = str(tmp_path / "empty.sst")
        w = SSTableWriter(path)
        w.finish()
        r = SSTableReader(path)
        assert list(r.items()) == []
        assert r.get(b"any") is MISSING


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_bad_magic_raises(self, tmp_path):
        p = tmp_path / "bad.sst"
        p.write_bytes(b"\x00" * 64)
        with pytest.raises(ValueError, match="magic"):
            SSTableReader(str(p))

    def test_file_too_small_raises(self, tmp_path):
        p = tmp_path / "tiny.sst"
        p.write_bytes(b"\x00" * 4)
        with pytest.raises(ValueError, match="too small"):
            SSTableReader(str(p))
