from pylsm import DB


def test_put_get_delete(tmp_path):
    with DB(str(tmp_path / "db"), sync=False) as db:
        db.put(b"k", b"v")
        assert db.get(b"k") == b"v"
        db.put(b"k", b"v2")  # overwrite
        assert db.get(b"k") == b"v2"
        db.delete(b"k")
        assert db.get(b"k") is None
        assert db.get(b"absent") is None


def test_rejects_non_bytes(tmp_path):
    with DB(str(tmp_path / "db"), sync=False) as db:
        for bad in ("str", 1, None):
            try:
                db.put(bad, b"v")
            except TypeError:
                pass
            else:
                raise AssertionError("expected TypeError")


def test_recovery_after_unclean_stop(tmp_path):
    """Writes that were acknowledged must survive reopening without close()."""
    path = str(tmp_path / "db")
    db = DB(path, sync=True)
    db.put(b"a", b"1")
    db.put(b"b", b"2")
    db.delete(b"a")
    # Deliberately do NOT call db.close() — simulate a crash.

    reopened = DB(path, sync=True)
    assert reopened.get(b"a") is None  # tombstone survived
    assert reopened.get(b"b") == b"2"
    reopened.close()


def test_recovery_preserves_latest_overwrite(tmp_path):
    path = str(tmp_path / "db")
    db = DB(path, sync=True)
    db.put(b"k", b"old")
    db.put(b"k", b"new")
    db.close()

    reopened = DB(path, sync=True)
    assert reopened.get(b"k") == b"new"
    reopened.close()
