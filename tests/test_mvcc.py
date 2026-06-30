"""Tests for the MVCC layer (Phase DB-1).

Coverage
────────
• Basic put/get
• Multiple versions of the same key
• Snapshot isolation — writes after snapshot() are invisible to old snapshot
• Tombstone (delete): get returns None; old snapshots still see the value
• Cross-flush: versions survive memtable flush to SSTable
• Cross-compaction: versions survive compaction
• Crash recovery: seq counter persists; no seq reuse after reopen
• Multiple independent user keys
• Property-based oracle: MVCCEngine matches a versioned dict oracle
"""

import random

import pytest

from pylsm import DB, MVCCEngine, Snapshot

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _open(path: str) -> tuple[DB, MVCCEngine]:
    db = DB(path, sync=False, flush_threshold_bytes=512)
    return db, MVCCEngine(db)


# ---------------------------------------------------------------------------
# Basic put / get
# ---------------------------------------------------------------------------


def test_put_then_get(tmp_path):
    db, eng = _open(str(tmp_path / "db"))
    eng.put(b"hello", b"world")
    assert eng.get(b"hello") == b"world"
    db.close()


def test_get_absent_key(tmp_path):
    db, eng = _open(str(tmp_path / "db"))
    assert eng.get(b"never_written") is None
    db.close()


def test_put_rejects_non_bytes(tmp_path):
    db, eng = _open(str(tmp_path / "db"))
    with pytest.raises(TypeError):
        eng.put("str_key", b"v")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        eng.put(b"k", 42)  # type: ignore[arg-type]
    db.close()


def test_delete_rejects_non_bytes(tmp_path):
    db, eng = _open(str(tmp_path / "db"))
    with pytest.raises(TypeError):
        eng.delete("str_key")  # type: ignore[arg-type]
    db.close()


# ---------------------------------------------------------------------------
# Multiple versions
# ---------------------------------------------------------------------------


def test_second_put_overwrites_in_current_view(tmp_path):
    db, eng = _open(str(tmp_path / "db"))
    eng.put(b"k", b"v1")
    eng.put(b"k", b"v2")
    assert eng.get(b"k") == b"v2"
    db.close()


def test_three_versions_current_is_latest(tmp_path):
    db, eng = _open(str(tmp_path / "db"))
    eng.put(b"k", b"a")
    eng.put(b"k", b"b")
    eng.put(b"k", b"c")
    assert eng.get(b"k") == b"c"
    db.close()


def test_get_with_explicit_snapshot_seq(tmp_path):
    db, eng = _open(str(tmp_path / "db"))
    seq1 = eng.put(b"k", b"v1")
    seq2 = eng.put(b"k", b"v2")
    assert eng.get(b"k", seq1) == b"v1"
    assert eng.get(b"k", seq2) == b"v2"
    db.close()


def test_get_at_seq_before_any_write(tmp_path):
    db, eng = _open(str(tmp_path / "db"))
    eng.put(b"k", b"v1")
    # seq=0 means "before the first write"
    assert eng.get(b"k", 0) is None
    db.close()


# ---------------------------------------------------------------------------
# Snapshot isolation
# ---------------------------------------------------------------------------


def test_snapshot_is_frozen_at_capture_time(tmp_path):
    db, eng = _open(str(tmp_path / "db"))
    eng.put(b"x", b"before")
    snap = eng.snapshot()
    eng.put(b"x", b"after")

    assert snap.get(b"x") == b"before"  # snapshot unaffected
    assert eng.get(b"x") == b"after"  # current state updated
    db.close()


def test_snapshot_sees_key_absent_before_write(tmp_path):
    db, eng = _open(str(tmp_path / "db"))
    snap = eng.snapshot()  # captured before any write
    eng.put(b"k", b"value")
    assert snap.get(b"k") is None  # write postdates snapshot
    assert eng.get(b"k") == b"value"
    db.close()


def test_multiple_snapshots_independent(tmp_path):
    db, eng = _open(str(tmp_path / "db"))
    eng.put(b"a", b"1")
    snap1 = eng.snapshot()
    eng.put(b"a", b"2")
    snap2 = eng.snapshot()
    eng.put(b"a", b"3")

    assert snap1.get(b"a") == b"1"
    assert snap2.get(b"a") == b"2"
    assert eng.get(b"a") == b"3"
    db.close()


def test_snapshot_repr(tmp_path):
    db, eng = _open(str(tmp_path / "db"))
    eng.put(b"k", b"v")
    snap = eng.snapshot()
    assert "Snapshot" in repr(snap)
    assert str(snap.seq) in repr(snap)
    db.close()


# ---------------------------------------------------------------------------
# Delete (MVCC tombstone)
# ---------------------------------------------------------------------------


def test_delete_makes_key_absent(tmp_path):
    db, eng = _open(str(tmp_path / "db"))
    eng.put(b"k", b"v")
    eng.delete(b"k")
    assert eng.get(b"k") is None
    db.close()


def test_snapshot_before_delete_sees_value(tmp_path):
    db, eng = _open(str(tmp_path / "db"))
    eng.put(b"k", b"alive")
    snap = eng.snapshot()
    eng.delete(b"k")

    assert snap.get(b"k") == b"alive"  # snapshot predates delete
    assert eng.get(b"k") is None  # current state: deleted
    db.close()


def test_snapshot_after_delete_sees_absent(tmp_path):
    db, eng = _open(str(tmp_path / "db"))
    eng.put(b"k", b"v")
    eng.delete(b"k")
    snap = eng.snapshot()  # captured after delete
    assert snap.get(b"k") is None
    db.close()


def test_put_after_delete_restores_key(tmp_path):
    db, eng = _open(str(tmp_path / "db"))
    eng.put(b"k", b"v1")
    eng.delete(b"k")
    eng.put(b"k", b"v2")
    assert eng.get(b"k") == b"v2"
    db.close()


def test_snapshot_between_delete_and_rewrite(tmp_path):
    db, eng = _open(str(tmp_path / "db"))
    eng.put(b"k", b"v1")
    eng.delete(b"k")
    snap_deleted = eng.snapshot()
    eng.put(b"k", b"v2")

    assert snap_deleted.get(b"k") is None  # sees the deletion
    assert eng.get(b"k") == b"v2"
    db.close()


# ---------------------------------------------------------------------------
# Multiple keys
# ---------------------------------------------------------------------------


def test_independent_keys_no_cross_contamination(tmp_path):
    db, eng = _open(str(tmp_path / "db"))
    eng.put(b"a", b"apple")
    eng.put(b"b", b"banana")
    eng.delete(b"a")

    assert eng.get(b"a") is None
    assert eng.get(b"b") == b"banana"
    db.close()


def test_snapshot_correct_across_multiple_keys(tmp_path):
    db, eng = _open(str(tmp_path / "db"))
    eng.put(b"x", b"x1")
    eng.put(b"y", b"y1")
    snap = eng.snapshot()
    eng.put(b"x", b"x2")
    eng.put(b"z", b"z1")

    assert snap.get(b"x") == b"x1"
    assert snap.get(b"y") == b"y1"
    assert snap.get(b"z") is None  # written after snapshot
    db.close()


# ---------------------------------------------------------------------------
# Cross-flush: versions must survive memtable flush to SSTable
# ---------------------------------------------------------------------------


def test_versions_survive_flush(tmp_path):
    """Write enough data to trigger a flush; verify MVCC reads still correct."""
    path = str(tmp_path / "db")
    db = DB(path, sync=False, flush_threshold_bytes=256)
    eng = MVCCEngine(db)

    eng.put(b"key", b"v1")
    snap = eng.snapshot()
    eng.put(b"key", b"v2")

    # Flood the memtable to force a flush.
    for i in range(50):
        eng.put(f"filler_{i}".encode(), b"x" * 20)

    assert snap.get(b"key") == b"v1"
    assert eng.get(b"key") == b"v2"
    db.close()


def test_tombstone_survives_flush(tmp_path):
    path = str(tmp_path / "db")
    db = DB(path, sync=False, flush_threshold_bytes=256)
    eng = MVCCEngine(db)

    eng.put(b"k", b"alive")
    snap_before = eng.snapshot()
    eng.delete(b"k")
    snap_after = eng.snapshot()

    for i in range(50):
        eng.put(f"pad_{i}".encode(), b"y" * 20)

    assert snap_before.get(b"k") == b"alive"
    assert snap_after.get(b"k") is None
    assert eng.get(b"k") is None
    db.close()


# ---------------------------------------------------------------------------
# Cross-compaction: versions must survive compaction
# ---------------------------------------------------------------------------


def test_versions_survive_compaction(tmp_path):
    path = str(tmp_path / "db")
    db = DB(path, sync=False, flush_threshold_bytes=256, l0_compaction_trigger=2)
    eng = MVCCEngine(db)

    eng.put(b"k", b"v1")
    snap = eng.snapshot()
    eng.put(b"k", b"v2")

    # Flood to trigger flush + compaction.
    for i in range(200):
        eng.put(f"pad_{i:04d}".encode(), b"z" * 20)

    db.compact()

    assert snap.get(b"k") == b"v1"
    assert eng.get(b"k") == b"v2"
    db.close()


# ---------------------------------------------------------------------------
# Crash recovery: seq counter must survive reopen
# ---------------------------------------------------------------------------


def test_seq_counter_persists_after_reopen(tmp_path):
    """After reopen, MVCCEngine must not reuse a seq that was already committed."""
    path = str(tmp_path / "db")

    db = DB(path, sync=True)
    eng = MVCCEngine(db)
    seq_before = eng.put(b"k", b"first")
    db.close()  # clean close

    db2 = DB(path, sync=True)
    eng2 = MVCCEngine(db2)
    assert eng2.current_seq >= seq_before, "seq must not regress after reopen"
    seq_after = eng2.put(b"k", b"second")
    assert seq_after > seq_before, "new seq must be strictly greater than pre-crash seq"
    assert eng2.get(b"k") == b"second"
    db2.close()


def test_data_and_seq_survive_crash(tmp_path):
    """Simulate a crash (no close()) and verify data + seq are recovered."""
    path = str(tmp_path / "db")

    db = DB(path, sync=True)
    eng = MVCCEngine(db)
    eng.put(b"a", b"alpha")
    eng.put(b"b", b"beta")
    seq_at_crash = eng.current_seq
    # No close() — simulate unclean stop.

    db2 = DB(path, sync=True)
    eng2 = MVCCEngine(db2)
    assert eng2.get(b"a") == b"alpha"
    assert eng2.get(b"b") == b"beta"
    assert eng2.current_seq >= seq_at_crash
    db2.close()


def test_snapshot_seq_correct_after_reopen(tmp_path):
    """Snapshots taken in a new session must work correctly."""
    path = str(tmp_path / "db")

    db = DB(path, sync=True)
    eng = MVCCEngine(db)
    eng.put(b"x", b"old")
    db.close()

    db2 = DB(path, sync=True)
    eng2 = MVCCEngine(db2)
    snap = eng2.snapshot()
    eng2.put(b"x", b"new")

    assert snap.get(b"x") == b"old"
    assert eng2.get(b"x") == b"new"
    db2.close()


# ---------------------------------------------------------------------------
# scan() helper
# ---------------------------------------------------------------------------


def test_scan_yields_live_pairs(tmp_path):
    db, eng = _open(str(tmp_path / "db"))
    eng.put(b"a", b"1")
    eng.put(b"b", b"2")
    eng.put(b"c", b"3")
    eng.delete(b"b")

    pairs = dict(eng.scan())
    assert pairs[b"a"] == b"1"
    assert b"b" not in pairs  # deleted
    assert pairs[b"c"] == b"3"
    db.close()


# ---------------------------------------------------------------------------
# Property-based oracle test
# ---------------------------------------------------------------------------


KEYS = [bytes([c]) for c in range(ord("a"), ord("a") + 6)]


def test_mvcc_matches_versioned_dict_oracle(tmp_path):
    """Random put/delete/get sequence; engine must match a dict oracle."""
    path = str(tmp_path / "db")
    db = DB(path, sync=False, flush_threshold_bytes=512)
    eng = MVCCEngine(db)
    rng = random.Random(42)

    # Oracle: maps key → (value, seq_written)
    oracle: dict[bytes, tuple[bytes, int] | None] = {}
    # Snapshots: list of (Snapshot, oracle copy at that moment)
    snapshots: list[tuple[Snapshot, dict[bytes, bytes | None]]] = []

    for step in range(500):
        op = rng.random()
        key = rng.choice(KEYS)

        if op < 0.45:
            value = rng.randbytes(rng.randint(1, 8))
            eng.put(key, value)
            oracle[key] = (value, eng.current_seq)

        elif op < 0.65:
            eng.delete(key)
            oracle[key] = None

        elif op < 0.75 and len(snapshots) < 5:
            # Capture a snapshot; record oracle state as of now.
            oracle_now: dict[bytes, bytes | None] = {
                k: (v[0] if v is not None else None) for k, v in oracle.items()
            }
            snapshots.append((eng.snapshot(), oracle_now))

        else:
            # Read current state; compare with oracle.
            expected = None
            if key in oracle and oracle[key] is not None:
                expected = oracle[key][0]  # type: ignore[index]
            assert eng.get(key) == expected, f"mismatch at step {step} key={key!r}"

        # Periodically verify all stored snapshots are still correct.
        if step % 100 == 99:
            for snap, snap_oracle in snapshots:
                for k in KEYS:
                    expected = snap_oracle.get(k)
                    got = snap.get(k)
                    assert got == expected, (
                        f"snapshot seq={snap.seq} key={k!r}: "
                        f"expected {expected!r} got {got!r} at step {step}"
                    )

    db.close()
