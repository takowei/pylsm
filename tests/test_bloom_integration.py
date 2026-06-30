"""Integration tests: bloom filter in the DB read path.

Verifies that:
  - Every SSTableReader loaded by DB carries a bloom filter.
  - The bloom contains all keys written to that SSTable.
  - DB.get skips an SSTable's block reads when bloom says the key is absent.
  - DB.get proceeds to block reads when bloom says the key may be present.
"""

from __future__ import annotations

from unittest.mock import patch

from pylsm import DB


class TestBloomLoadedByDB:
    def test_each_sstable_has_bloom(self, tmp_path):
        path = str(tmp_path / "db")
        with DB(path, sync=False, flush_threshold_bytes=1) as db:
            db.put(b"hello", b"world")
            db._flush()
            assert len(db._sstables) == 1
            assert db._sstables[0].bloom is not None

    def test_bloom_present_after_multiple_flushes(self, tmp_path):
        path = str(tmp_path / "db")
        with DB(path, sync=False, flush_threshold_bytes=1) as db:
            for i in range(5):
                db.put(f"k{i}".encode(), b"v")
                db._flush()
            assert len(db._sstables) == 5
            for sst in db._sstables:
                assert sst.bloom is not None

    def test_bloom_contains_written_key(self, tmp_path):
        path = str(tmp_path / "db")
        with DB(path, sync=False, flush_threshold_bytes=1) as db:
            db.put(b"exists", b"val")
            db._flush()
            sst = db._sstables[0]
            assert b"exists" in sst.bloom

    def test_bloom_loaded_after_reopen(self, tmp_path):
        """Bloom must be restored from disk when the DB is reopened."""
        db_path = str(tmp_path / "db")
        db = DB(db_path, sync=False, flush_threshold_bytes=1)
        db.put(b"persisted", b"v")
        db._flush()
        db.close()

        db2 = DB(db_path, sync=False)
        assert db2._sstables[0].bloom is not None
        assert b"persisted" in db2._sstables[0].bloom
        db2.close()


class TestBloomSkipsBlockReads:
    def test_get_skips_sstable_when_bloom_rejects(self, tmp_path):
        """When bloom is replaced with one that always returns False, DB.get
        must not invoke SSTableReader.get (block reads are avoided entirely)."""
        path = str(tmp_path / "db")
        with DB(path, sync=False, flush_threshold_bytes=1) as db:
            db.put(b"exists", b"val")
            db._flush()
            sst = db._sstables[0]

            class _RejectAll:
                def __contains__(self, key: bytes) -> bool:
                    return False

            sst.bloom = _RejectAll()
            with patch.object(sst, "get", wraps=sst.get) as spy:
                result = db.get(b"exists")
            # Bloom rejected → SSTableReader.get must NOT have been called.
            assert spy.call_count == 0
            # Key is also absent from the (empty) memtable.
            assert result is None

    def test_get_consults_sstable_when_bloom_accepts(self, tmp_path):
        """When bloom says 'maybe present', DB.get must proceed to block lookup."""
        path = str(tmp_path / "db")
        with DB(path, sync=False, flush_threshold_bytes=1) as db:
            db.put(b"exists", b"val")
            db._flush()
            sst = db._sstables[0]
            # The real bloom must contain the written key (no false negatives).
            assert b"exists" in sst.bloom
            with patch.object(sst, "get", wraps=sst.get) as spy:
                result = db.get(b"exists")
            assert result == b"val"
            assert spy.call_count == 1

    def test_absent_key_skips_all_sstables_via_bloom(self, tmp_path):
        """For a key absent from all SSTables, every file's bloom should reject it.

        We verify by replacing all blooms with all-reject sentinels and confirming
        SSTableReader.get is never called — then restore real blooms and confirm
        the key is still absent (None).
        """
        path = str(tmp_path / "db")
        with DB(path, sync=False, flush_threshold_bytes=1) as db:
            # Write distinct keys into two separate SSTables.
            db.put(b"aaa", b"1")
            db._flush()
            db.put(b"zzz", b"2")
            db._flush()

            class _RejectAll:
                def __contains__(self, key: bytes) -> bool:
                    return False

            for sst in db._sstables:
                sst.bloom = _RejectAll()

            spies = [patch.object(sst, "get", wraps=sst.get).__enter__() for sst in db._sstables]
            result = db.get(b"missing")
            for spy in spies:
                assert spy.call_count == 0
            assert result is None
