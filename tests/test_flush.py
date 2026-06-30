"""Tests for memtable flush, multi-layer reads, tombstone shadowing, and crash recovery.

Strategy: use flush_threshold_bytes=64 (tiny) so tests exercise the flush path
without writing large volumes of data.
"""

from __future__ import annotations

import os
import random

from pylsm import DB

_TINY = 64  # byte threshold that triggers a flush after a small number of writes


def _db(tmp_path, threshold: int = _TINY) -> DB:
    return DB(str(tmp_path / "db"), sync=False, flush_threshold_bytes=threshold)


def _fill(db: DB, n: int, prefix: str = "pad", value_bytes: int = 10) -> None:
    """Write *n* distinct filler keys to push memtable bytes over a threshold."""
    for i in range(n):
        db.put(f"{prefix}{i}".encode(), b"x" * value_bytes)


# ---------------------------------------------------------------------------
# Flush basics
# ---------------------------------------------------------------------------


class TestFlushBasics:
    def test_flush_creates_sst_file(self, tmp_path):
        db_path = str(tmp_path / "db")
        # threshold=1 → any write triggers a flush.
        db = DB(db_path, sync=False, flush_threshold_bytes=1)
        db.put(b"k", b"v")
        db.close()
        sst_files = [f for f in os.listdir(db_path) if f.endswith(".sst")]
        assert sst_files, "expected at least one .sst file after flush"

    def test_read_after_flush_in_same_session(self, tmp_path):
        db = _db(tmp_path)
        db.put(b"target", b"hello")
        _fill(db, 20)  # push memtable past threshold
        assert db.get(b"target") == b"hello"
        db.close()

    def test_overwrite_survives_flush(self, tmp_path):
        db = _db(tmp_path)
        db.put(b"k", b"old")
        _fill(db, 20)  # force flush with k=old in SSTable
        db.put(b"k", b"new")  # new value lands in fresh memtable
        assert db.get(b"k") == b"new"
        db.close()

    def test_multiple_flushes_all_keys_readable(self, tmp_path):
        db = _db(tmp_path, threshold=32)
        for epoch in range(4):
            db.put(f"epoch{epoch}".encode(), f"val{epoch}".encode())
            _fill(db, 8, prefix=f"e{epoch}_fill")
        for epoch in range(4):
            assert db.get(f"epoch{epoch}".encode()) == f"val{epoch}".encode()
        db.close()


# ---------------------------------------------------------------------------
# Tombstone shadowing across layers
# ---------------------------------------------------------------------------


class TestTombstoneShadowing:
    def test_memtable_tombstone_hides_sstable_value(self, tmp_path):
        """Delete in active memtable must shadow an older value in an SSTable."""
        db = _db(tmp_path)
        db.put(b"k", b"v")
        _fill(db, 20)  # flush: k=v lands in SSTable
        db.delete(b"k")  # tombstone in memtable
        assert db.get(b"k") is None
        db.close()

    def test_sstable_tombstone_hides_older_sstable_value(self, tmp_path):
        """Tombstone in a newer SSTable must shadow a live value in an older one."""
        db = _db(tmp_path, threshold=32)
        db.put(b"k", b"v1")
        _fill(db, 8, prefix="a")  # flush 1: k=v1 in SSTable-1
        db.delete(b"k")
        _fill(db, 8, prefix="b")  # flush 2: tombstone for k in SSTable-2
        assert db.get(b"k") is None
        db.close()

    def test_newer_sstable_value_wins_over_older(self, tmp_path):
        db = _db(tmp_path, threshold=32)
        db.put(b"k", b"v1")
        _fill(db, 8, prefix="a")  # flush 1: k=v1
        db.put(b"k", b"v2")
        _fill(db, 8, prefix="b")  # flush 2: k=v2 (newer)
        assert db.get(b"k") == b"v2"
        db.close()

    def test_live_value_after_tombstone_in_older_sstable(self, tmp_path):
        """put → flush (tombstone) → flush (new value) → result must be new value."""
        db = _db(tmp_path, threshold=32)
        db.put(b"k", b"v1")
        _fill(db, 8, prefix="a")  # SSTable-1: k=v1
        db.delete(b"k")
        _fill(db, 8, prefix="b")  # SSTable-2: tombstone
        db.put(b"k", b"v3")
        _fill(db, 8, prefix="c")  # SSTable-3: k=v3 (newest)
        assert db.get(b"k") == b"v3"
        db.close()


# ---------------------------------------------------------------------------
# Crash recovery
# ---------------------------------------------------------------------------


class TestCrashRecovery:
    def test_flushed_data_survives_reopen(self, tmp_path):
        """Data in an SSTable must be visible after abandoning the handle."""
        db_path = str(tmp_path / "db")
        db = DB(db_path, sync=False, flush_threshold_bytes=32)
        db.put(b"a", b"1")
        _fill(db, 8)  # trigger flush
        del db  # simulate crash (no close)

        db2 = DB(db_path, sync=False, flush_threshold_bytes=32)
        assert db2.get(b"a") == b"1"
        db2.close()

    def test_post_flush_wal_write_survives_reopen(self, tmp_path):
        """Writes that happened after the last flush (in WAL only) must survive."""
        db_path = str(tmp_path / "db")
        db = DB(db_path, sync=False, flush_threshold_bytes=32)
        _fill(db, 8)  # trigger a flush (WAL now empty)
        db.put(b"post", b"value")  # goes to new WAL only
        del db  # abandon

        db2 = DB(db_path, sync=False, flush_threshold_bytes=32)
        assert db2.get(b"post") == b"value"
        db2.close()

    def test_tombstone_survives_sstable_and_wal(self, tmp_path):
        db_path = str(tmp_path / "db")
        db = DB(db_path, sync=False, flush_threshold_bytes=32)
        db.put(b"k", b"v")
        _fill(db, 8)  # flush: k=v in SSTable
        db.delete(b"k")  # tombstone in WAL
        del db

        db2 = DB(db_path, sync=False, flush_threshold_bytes=32)
        assert db2.get(b"k") is None
        db2.close()

    def test_multiple_flushes_survive_reopen(self, tmp_path):
        db_path = str(tmp_path / "db")
        db = DB(db_path, sync=False, flush_threshold_bytes=32)
        for i in range(4):
            db.put(f"key{i}".encode(), f"val{i}".encode())
            _fill(db, 8, prefix=f"fill{i}")
        del db

        db2 = DB(db_path, sync=False, flush_threshold_bytes=32)
        for i in range(4):
            assert db2.get(f"key{i}".encode()) == f"val{i}".encode()
        db2.close()

    def test_orphaned_sst_before_manifest_update_ignored(self, tmp_path):
        """Simulate the case where an SSTable file was written but the MANIFEST
        was not yet updated (crash between steps 2 and 4 of _flush).

        On recovery, the orphaned file must be ignored and the WAL replayed.
        """
        db_path = str(tmp_path / "db")
        db = DB(db_path, sync=False, flush_threshold_bytes=32)
        db.put(b"safe", b"value")
        del db  # no flush happened yet

        # Plant an orphaned .sst file that is NOT in the MANIFEST.
        orphan = os.path.join(db_path, "sst_99999999.sst")
        from pylsm.sstable import SSTableWriter

        w = SSTableWriter(orphan)
        w.add(b"injected", b"evil")
        w.finish()

        # On recovery: manifest lists no SSTables; WAL has b"safe".
        db2 = DB(db_path, sync=False, flush_threshold_bytes=32)
        assert db2.get(b"safe") == b"value"
        assert db2.get(b"injected") is None  # orphan not loaded
        db2.close()


# ---------------------------------------------------------------------------
# Property-based oracle: engine must behave exactly like a dict
# ---------------------------------------------------------------------------


class TestPropertyOracle:
    KEYS = [bytes([c]) for c in range(ord("a"), ord("a") + 10)]

    def _check_all(self, db: DB, oracle: dict) -> None:
        for k in self.KEYS:
            assert db.get(k) == oracle.get(k), f"mismatch on {k!r}"

    def test_dict_oracle_with_flush_and_reopens(self, tmp_path):
        """2 000 random put/delete operations verified against a dict oracle.

        The threshold is tiny (64 bytes) so dozens of flushes happen during the
        run.  The DB is also abandoned and reopened periodically to exercise
        both SSTable-based recovery and WAL replay.
        """
        db_path = str(tmp_path / "db")
        rng = random.Random(42)
        oracle: dict[bytes, bytes] = {}

        db = DB(db_path, sync=False, flush_threshold_bytes=_TINY)
        for step in range(2000):
            k = rng.choice(self.KEYS)
            if rng.random() < 0.35:
                db.delete(k)
                oracle.pop(k, None)
            else:
                v = bytes([rng.randrange(256)]) * rng.randint(1, 8)
                db.put(k, v)
                oracle[k] = v

            if step % 400 == 399:
                del db  # simulate crash (no close)
                db = DB(db_path, sync=False, flush_threshold_bytes=_TINY)
                self._check_all(db, oracle)

        self._check_all(db, oracle)
        db.close()
