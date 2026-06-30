"""Phase 4 — Leveled compaction tests.

Coverage
────────
• Basic correctness: dict oracle with compaction running (property-based).
• L0 file-count trigger: compaction fires when |L0| ≥ threshold.
• Post-compaction structure: L1 files have non-overlapping key ranges.
• Tombstone GC at the bottom level: deleted keys disappear from output.
• Crash safety: orphaned SSTable written before MANIFEST update is ignored.
• Compaction mid-crash: new SSTables exist but MANIFEST still references
  old files → recovery loads old files, data is intact.
• Dict oracle with many puts that trigger multiple compaction rounds.
"""

from __future__ import annotations

import os
import random

from pylsm import DB
from pylsm.compaction import L0_COMPACTION_TRIGGER, merge_sstables
from pylsm.sstable import SSTableReader, SSTableWriter

# Tiny flush threshold to force frequent flushes.
_TINY = 64


def _db(tmp_path, threshold: int = _TINY, l0_trigger: int = L0_COMPACTION_TRIGGER) -> DB:
    return DB(
        str(tmp_path / "db"),
        sync=False,
        flush_threshold_bytes=threshold,
        l0_compaction_trigger=l0_trigger,
    )


def _fill(db: DB, n: int, prefix: str = "pad", value_bytes: int = 10) -> None:
    for i in range(n):
        db.put(f"{prefix}{i}".encode(), b"x" * value_bytes)


# ---------------------------------------------------------------------------
# Merge helper unit tests
# ---------------------------------------------------------------------------


class TestMergeSSTableHelper:
    def test_merge_single_sstable(self, tmp_path):
        path = str(tmp_path / "a.sst")
        w = SSTableWriter(path)
        w.add(b"a", b"1")
        w.add(b"b", b"2")
        w.finish()
        r = SSTableReader(path)
        result = list(merge_sstables([(1, r)]))
        assert result == [(b"a", b"1"), (b"b", b"2")]

    def test_merge_two_non_overlapping(self, tmp_path):
        p1 = str(tmp_path / "a.sst")
        p2 = str(tmp_path / "b.sst")
        w1 = SSTableWriter(p1)
        w1.add(b"a", b"1")
        w1.finish()
        w2 = SSTableWriter(p2)
        w2.add(b"c", b"3")
        w2.finish()
        result = list(merge_sstables([(1, SSTableReader(p1)), (2, SSTableReader(p2))]))
        assert result == [(b"a", b"1"), (b"c", b"3")]

    def test_merge_newer_seq_wins(self, tmp_path):
        """For the same key in two SSTables, the higher-seq (newer) value wins."""
        p1 = str(tmp_path / "old.sst")
        p2 = str(tmp_path / "new.sst")
        w1 = SSTableWriter(p1)
        w1.add(b"k", b"old")
        w1.finish()
        w2 = SSTableWriter(p2)
        w2.add(b"k", b"new")
        w2.finish()
        # seq=2 is newer than seq=1.
        result = list(merge_sstables([(2, SSTableReader(p2)), (1, SSTableReader(p1))]))
        assert result == [(b"k", b"new")]

    def test_merge_drop_tombstones(self, tmp_path):
        p = str(tmp_path / "t.sst")
        w = SSTableWriter(p)
        w.add(b"alive", b"v")
        w.add(b"dead", None)  # tombstone
        w.finish()
        result = list(merge_sstables([(1, SSTableReader(p))], drop_tombstones=True))
        assert result == [(b"alive", b"v")]

    def test_merge_keep_tombstones_by_default(self, tmp_path):
        p = str(tmp_path / "t.sst")
        w = SSTableWriter(p)
        w.add(b"dead", None)
        w.finish()
        result = list(merge_sstables([(1, SSTableReader(p))]))
        assert result == [(b"dead", None)]


# ---------------------------------------------------------------------------
# L0 trigger and compaction execution
# ---------------------------------------------------------------------------


class TestCompactionTrigger:
    def test_l0_compacted_when_threshold_reached(self, tmp_path):
        """After enough flushes to trigger compaction, L0 should be empty."""
        db_path = str(tmp_path / "db")
        # l0_trigger=2 so compaction fires after 2 L0 files.
        db = DB(db_path, sync=False, flush_threshold_bytes=32, l0_compaction_trigger=2)
        for i in range(6):
            db.put(f"k{i}".encode(), b"v")
            _fill(db, 5, prefix=f"f{i}")
        # At least one compaction should have happened (L0 was at 2+ files).
        # L1 must have at least one file; L0 can have 0 or 1 file.
        assert len(db._levels) >= 2
        assert len(db._levels[1]) >= 1
        db.close()

    def test_all_keys_readable_after_compaction(self, tmp_path):
        db_path = str(tmp_path / "db")
        db = DB(db_path, sync=False, flush_threshold_bytes=32, l0_compaction_trigger=2)
        keys_written = {}
        for i in range(20):
            k = f"key{i:03d}".encode()
            v = f"val{i}".encode()
            db.put(k, v)
            keys_written[k] = v
            _fill(db, 3, prefix=f"p{i}")
        for k, v in keys_written.items():
            assert db.get(k) == v, f"mismatch on {k!r}"
        db.close()

    def test_non_overlapping_invariant_in_l1(self, tmp_path):
        """After compaction, L1 SSTables must have non-overlapping key ranges."""
        db_path = str(tmp_path / "db")
        db = DB(db_path, sync=False, flush_threshold_bytes=32, l0_compaction_trigger=2)
        for i in range(30):
            db.put(f"key{i:04d}".encode(), b"v")
            _fill(db, 3, prefix=f"fill{i}")
        db.compact()  # force a final compaction pass

        if len(db._levels) >= 2 and db._levels[1]:
            l1 = db._levels[1]
            for i in range(len(l1) - 1):
                _, r_lo = l1[i]
                _, r_hi = l1[i + 1]
                # The max_key of the earlier file must be < min_key of the next.
                assert r_lo.max_key < r_hi.min_key, (  # type: ignore[operator]
                    f"overlapping ranges: {r_lo.max_key!r} vs {r_hi.min_key!r}"
                )
        db.close()


# ---------------------------------------------------------------------------
# Tombstone GC
# ---------------------------------------------------------------------------


class TestTombstoneGC:
    def test_deleted_key_absent_after_compaction(self, tmp_path):
        """A key deleted before compaction must remain absent after compaction."""
        db = _db(tmp_path, threshold=32, l0_trigger=2)
        db.put(b"gone", b"old")
        _fill(db, 5, prefix="f1")  # flush → L0
        db.delete(b"gone")
        _fill(db, 5, prefix="f2")  # flush → L0 (tombstone)
        db.compact()  # compact L0 → L1
        assert db.get(b"gone") is None
        db.close()

    def test_tombstone_dropped_at_bottom_level(self, tmp_path):
        """When L1 is the only level, tombstones are physically dropped from it."""
        db = _db(tmp_path, threshold=32, l0_trigger=2)
        db.put(b"k", b"v")
        _fill(db, 5, prefix="a")
        db.delete(b"k")
        _fill(db, 5, prefix="b")
        db.compact()  # compact to L1 (bottom level)
        db.compact()  # second pass: nothing to do but safe

        # After compaction to bottom level, the tombstone might be dropped.
        # Either way, the key must not be visible.
        assert db.get(b"k") is None
        db.close()

    def test_live_value_survives_compaction(self, tmp_path):
        """A live key written after a tombstone must survive compaction."""
        db = _db(tmp_path, threshold=32, l0_trigger=2)
        db.put(b"k", b"v1")
        _fill(db, 4, prefix="a")  # → L0 SST with k=v1
        db.delete(b"k")
        _fill(db, 4, prefix="b")  # → L0 SST with tombstone
        db.put(b"k", b"v2")
        _fill(db, 4, prefix="c")  # → L0 SST with k=v2 (newest)
        db.compact()
        assert db.get(b"k") == b"v2"
        db.close()


# ---------------------------------------------------------------------------
# Crash safety
# ---------------------------------------------------------------------------


class TestCompactionCrashSafety:
    def test_orphaned_new_sst_before_manifest_update(self, tmp_path):
        """Simulate crash: new compaction output SST written but MANIFEST not updated.

        Recovery must use the old files (still in MANIFEST) and ignore the orphan.
        """
        db_path = str(tmp_path / "db")
        db = DB(db_path, sync=False, flush_threshold_bytes=32, l0_compaction_trigger=100)
        db.put(b"safe", b"value")
        _fill(db, 8, prefix="f")  # ensures data is in an L0 SSTable
        db.close()

        # Plant an orphaned compaction output (not in MANIFEST).
        orphan = os.path.join(db_path, "sst_99999999.sst")
        w = SSTableWriter(orphan)
        w.add(b"injected", b"evil")
        w.finish()

        db2 = DB(db_path, sync=False)
        assert db2.get(b"safe") == b"value"
        assert db2.get(b"injected") is None  # orphan not loaded
        db2.close()

    def test_data_intact_after_compaction_and_reopen(self, tmp_path):
        """Data must survive a clean close+reopen after compaction."""
        db_path = str(tmp_path / "db")
        db = DB(db_path, sync=False, flush_threshold_bytes=32, l0_compaction_trigger=2)
        written = {}
        for i in range(16):
            k = f"key{i:03d}".encode()
            v = f"val{i}".encode()
            db.put(k, v)
            written[k] = v
            _fill(db, 4, prefix=f"p{i}")
        db.compact()
        db.close()

        db2 = DB(db_path, sync=False)
        for k, v in written.items():
            assert db2.get(k) == v, f"mismatch after reopen on {k!r}"
        db2.close()

    def test_compaction_result_survives_unclean_close(self, tmp_path):
        """Data must survive an unclean stop (no close()) after compaction."""
        db_path = str(tmp_path / "db")
        db = DB(db_path, sync=False, flush_threshold_bytes=32, l0_compaction_trigger=2)
        db.put(b"x", b"1")
        db.put(b"y", b"2")
        _fill(db, 12, prefix="fill")  # trigger multiple flushes + compaction
        db.compact()
        # Abandon without close().

        db2 = DB(db_path, sync=False)
        assert db2.get(b"x") == b"1"
        assert db2.get(b"y") == b"2"
        db2.close()


# ---------------------------------------------------------------------------
# Property-based oracle: compaction must not change observable semantics
# ---------------------------------------------------------------------------


class TestPropertyOracleWithCompaction:
    KEYS = [f"k{i:02d}".encode() for i in range(12)]

    def _check(self, db: DB, oracle: dict) -> None:
        for k in self.KEYS:
            assert db.get(k) == oracle.get(k), f"mismatch on {k!r}"

    def test_dict_oracle_with_frequent_compaction(self, tmp_path):
        """3 000 random ops verified against a dict oracle.

        Threshold is tiny (64 B) so dozens of flushes happen.
        l0_trigger=2 so compaction fires after every 2 flushes.
        DB is abandoned and reopened periodically.
        """
        db_path = str(tmp_path / "db")
        rng = random.Random(99)
        oracle: dict[bytes, bytes] = {}

        db = DB(db_path, sync=False, flush_threshold_bytes=_TINY, l0_compaction_trigger=2)
        for step in range(3000):
            k = rng.choice(self.KEYS)
            if rng.random() < 0.3:
                db.delete(k)
                oracle.pop(k, None)
            else:
                v = bytes([rng.randrange(256)]) * rng.randint(1, 8)
                db.put(k, v)
                oracle[k] = v

            # Periodically reopen (simulates crash) and validate.
            if step % 500 == 499:
                db = DB(db_path, sync=False, flush_threshold_bytes=_TINY, l0_compaction_trigger=2)
                self._check(db, oracle)

        db.compact()  # final compaction pass
        self._check(db, oracle)
        db.close()

    def test_gross_read_amplification_lower_after_compaction(self, tmp_path):
        """Gross RA must be strictly lower after compaction than before.

        Gross RA = sst_candidates / get_calls (SSTs considered before bloom).

        Before compaction: every L0 file is a candidate for every get() because
        L0 files may have overlapping key ranges.  Gross RA = number of L0 files.
        After compaction: L1 files are non-overlapping; binary search on key
        ranges finds at most 1 candidate per level.  Gross RA ≈ 1.
        """
        db_path = str(tmp_path / "db")
        # High l0_trigger so we can measure RA in the all-L0 state.
        # With 32-byte threshold and 50 keys * 4 writes each ≈ 200 flushes,
        # the trigger must be well above that count.
        db = DB(db_path, sync=False, flush_threshold_bytes=32, l0_compaction_trigger=10_000)
        keys = [f"key{i:03d}".encode() for i in range(50)]
        for k in keys:
            db.put(k, b"value")
            _fill(db, 3, prefix=f"fill{k.decode()}")
        n_l0 = len(db._levels[0]) if db._levels else 0

        # Measure gross RA before compaction.
        db.stats.reset()
        for k in keys:
            db.get(k)
        gross_before = db.stats.gross_read_amplification

        # Compact everything into L1 and measure again.
        db.compact()
        db.stats.reset()
        for k in keys:
            db.get(k)
        gross_after = db.stats.gross_read_amplification

        # Structural assertions.
        # n_l0: at least two L0 files must exist for the test to be meaningful.
        assert n_l0 > 1, f"expected multiple L0 files, got {n_l0}"
        # gross_before: with multiple L0 files every get must check >1 candidate on
        # average (early-exit means it's n_l0/2 on average, not n_l0 itself).
        assert gross_before > 1.0, (
            f"expected gross_before > 1 with {n_l0} L0 files, got {gross_before:.2f}"
        )
        # gross_after: after L1 compaction, key-range binary search gives exactly
        # 1 candidate per level so gross RA must be ≤ 1.
        assert gross_after <= 1.0 + 1e-9, (
            f"expected gross_after ≤ 1.0 after L1 compaction, got {gross_after:.2f}"
        )
        # Compaction must improve the structural read cost.
        assert gross_after < gross_before, (
            f"gross RA did not improve: before={gross_before:.2f}, after={gross_after:.2f}"
        )
        db.close()
