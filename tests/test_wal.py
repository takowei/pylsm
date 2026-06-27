import os

from pylsm.wal import OP_DELETE, OP_PUT, WAL, replay


def test_append_replay_roundtrip(tmp_path):
    path = str(tmp_path / "wal.log")
    w = WAL(path, sync=False)
    w.append(OP_PUT, b"a", b"1")
    w.append(OP_PUT, b"b", b"22")
    w.append(OP_DELETE, b"a")
    w.close()

    assert list(replay(path)) == [
        (OP_PUT, b"a", b"1"),
        (OP_PUT, b"b", b"22"),
        (OP_DELETE, b"a", b""),
    ]


def test_replay_missing_file_is_empty(tmp_path):
    assert list(replay(str(tmp_path / "nope.log"))) == []


def test_torn_tail_is_discarded(tmp_path):
    """A half-written final record must be dropped, keeping the intact prefix."""
    path = str(tmp_path / "wal.log")
    w = WAL(path, sync=False)
    w.append(OP_PUT, b"a", b"1")
    w.append(OP_PUT, b"b", b"2")
    w.close()

    # Simulate a crash mid-write: chop the last 3 bytes off the file.
    size = os.path.getsize(path)
    with open(path, "r+b") as f:
        f.truncate(size - 3)

    assert list(replay(path)) == [(OP_PUT, b"a", b"1")]


def test_crc_detects_corruption(tmp_path):
    """A flipped byte in a record's payload must end replay at that record."""
    path = str(tmp_path / "wal.log")
    w = WAL(path, sync=False)
    w.append(OP_PUT, b"a", b"1")
    w.append(OP_PUT, b"b", b"2")
    w.close()

    data = bytearray(open(path, "rb").read())
    data[-1] ^= 0xFF  # corrupt the value byte of the 2nd record
    open(path, "wb").write(data)

    assert list(replay(path)) == [(OP_PUT, b"a", b"1")]
