"""Property-based correctness: the engine must behave exactly like a dict.

We drive a random sequence of put/delete/get against both the DB and a plain
``dict`` oracle and assert they always agree. Midway we reopen the DB (without
a clean close) so recovery is exercised inside the same invariant.
"""

import random

from pylsm import DB

KEYS = [bytes([c]) for c in range(b"a"[0], b"a"[0] + 8)]


def _check_all(db, oracle):
    for k in KEYS:
        assert db.get(k) == oracle.get(k), f"mismatch on {k!r}"


def test_matches_dict_oracle_with_recovery(tmp_path):
    path = str(tmp_path / "db")
    rng = random.Random(1234)
    oracle: dict[bytes, bytes] = {}

    db = DB(path, sync=True)
    for step in range(2000):
        k = rng.choice(KEYS)
        if rng.random() < 0.35:
            db.delete(k)
            oracle.pop(k, None)
        else:
            v = bytes([rng.randrange(256)]) * rng.randint(1, 6)
            db.put(k, v)
            oracle[k] = v

        # Periodically simulate a crash + reopen; semantics must be unchanged.
        if step % 500 == 499:
            db = DB(path, sync=True)  # old handle abandoned (unclean)
            _check_all(db, oracle)

    _check_all(db, oracle)
    db.close()


def test_recovery_is_stable_across_many_reopens(tmp_path):
    path = str(tmp_path / "db")
    db = DB(path, sync=True)
    db.put(b"x", b"1")
    db.close()

    for _ in range(5):
        db = DB(path, sync=True)
        assert db.get(b"x") == b"1"
        db.close()
