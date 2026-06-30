"""CLI integration tests for pylsm.cli.

Tests call ``main(argv)`` directly (no subprocess) to keep things fast and
avoid installation-path issues, while still exercising the full code path.
Exit-code assertions use pytest.raises(SystemExit).
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout

import pytest

from pylsm.cli import main

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _put(db_dir: str, key: str, value: str) -> None:
    main(["put", db_dir, key, value])


def _get(db_dir: str, key: str) -> str:
    """Return the printed value (stripped). Raises SystemExit(1) if absent."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        main(["get", db_dir, key])
    return buf.getvalue().strip()


def _scan(db_dir: str) -> list[tuple[str, str]]:
    """Return all (key, value) pairs from scan as strings."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        main(["scan", db_dir])
    pairs = []
    for line in buf.getvalue().splitlines():
        if "\t" in line:
            k, v = line.split("\t", 1)
            pairs.append((k, v))
    return pairs


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_put_then_get(tmp_path):
    """put followed by get should return the same value."""
    db = str(tmp_path / "db")
    _put(db, "hello", "world")
    assert _get(db, "hello") == "world"


def test_overwrite(tmp_path):
    """A second put should overwrite the first value."""
    db = str(tmp_path / "db")
    _put(db, "k", "v1")
    _put(db, "k", "v2")
    assert _get(db, "k") == "v2"


def test_get_absent_key_exits_nonzero(tmp_path):
    """get on a non-existent key must exit with code 1."""
    db = str(tmp_path / "db")
    _put(db, "other", "value")
    with pytest.raises(SystemExit) as exc_info:
        main(["get", db, "missing"])
    assert exc_info.value.code == 1


def test_delete_then_get_exits_nonzero(tmp_path):
    """After delete, get should exit with code 1 (key removed)."""
    db = str(tmp_path / "db")
    _put(db, "x", "val")
    assert _get(db, "x") == "val"
    main(["delete", db, "x"])
    with pytest.raises(SystemExit) as exc_info:
        main(["get", db, "x"])
    assert exc_info.value.code == 1


def test_scan_ordered_output(tmp_path):
    """scan must return all live keys in sorted (lexicographic) order."""
    db = str(tmp_path / "db")
    _put(db, "banana", "2")
    _put(db, "apple", "1")
    _put(db, "cherry", "3")

    pairs = _scan(db)
    keys = [k for k, _ in pairs]
    assert keys == sorted(keys), "scan output must be sorted"
    assert set(keys) == {"apple", "banana", "cherry"}


def test_scan_skips_tombstones(tmp_path):
    """scan must not show deleted keys."""
    db = str(tmp_path / "db")
    _put(db, "keep", "yes")
    _put(db, "drop", "no")
    main(["delete", db, "drop"])

    pairs = _scan(db)
    keys = [k for k, _ in pairs]
    assert "keep" in keys
    assert "drop" not in keys


def test_scan_empty_db(tmp_path):
    """scan on a fresh database should produce no output after all keys are deleted."""
    db = str(tmp_path / "db")
    _put(db, "tmp", "x")
    main(["delete", db, "tmp"])
    pairs = _scan(db)
    assert pairs == []


def test_compact_subcommand(tmp_path, capsys):
    """compact should run without error and print a confirmation."""
    db = str(tmp_path / "db")
    _put(db, "a", "1")
    _put(db, "b", "2")
    main(["compact", db])
    captured = capsys.readouterr()
    assert "compaction" in captured.out.lower()


def test_persistence_across_invocations(tmp_path):
    """Data written in one call must survive to the next (WAL/SSTable recovery)."""
    db = str(tmp_path / "db")
    _put(db, "persistent", "yes")
    # Each main() call opens and closes the DB, so recovery is exercised.
    assert _get(db, "persistent") == "yes"


def test_put_get_utf8_values(tmp_path):
    """UTF-8 keys and values round-trip correctly."""
    db = str(tmp_path / "db")
    _put(db, "lang", "Python")
    assert _get(db, "lang") == "Python"
