"""pylsm command-line interface.

Provides shell access to a pylsm database directory:

  pylsm put   <dir> <key> <value>   — store a key/value pair
  pylsm get   <dir> <key>           — retrieve a value (exit 1 if absent)
  pylsm delete <dir> <key>          — tombstone a key
  pylsm scan  <dir>                 — print all live key/value pairs, sorted
  pylsm compact <dir>               — manually trigger a compaction pass

Keys and values are UTF-8 strings at the CLI layer; internally stored as bytes.

Example
-------
  pylsm put ./mydb name Alice
  pylsm get ./mydb name
  pylsm scan ./mydb
  pylsm delete ./mydb name
  pylsm compact ./mydb

The module is also runnable as ``python -m pylsm``.
"""

from __future__ import annotations

import argparse
import sys

from .db import DB
from .sstable import TOMBSTONE


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pylsm",
        description="pylsm — LSM-tree KV store CLI",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    # put
    p_put = sub.add_parser("put", help="store a key/value pair")
    p_put.add_argument("dir", help="database directory")
    p_put.add_argument("key", help="UTF-8 key string")
    p_put.add_argument("value", help="UTF-8 value string")

    # get
    p_get = sub.add_parser("get", help="retrieve a value (exit 1 if absent)")
    p_get.add_argument("dir", help="database directory")
    p_get.add_argument("key", help="UTF-8 key string")

    # delete
    p_del = sub.add_parser("delete", help="tombstone a key")
    p_del.add_argument("dir", help="database directory")
    p_del.add_argument("key", help="UTF-8 key string")

    # scan
    p_scan = sub.add_parser("scan", help="print all live key/value pairs, sorted")
    p_scan.add_argument("dir", help="database directory")

    # compact
    p_compact = sub.add_parser("compact", help="manually trigger a compaction pass")
    p_compact.add_argument("dir", help="database directory")

    return parser


def _all_live_pairs(db: DB) -> list[tuple[bytes, bytes]]:
    """Collect every live (non-tombstone) key/value from the DB in sorted order.

    Walks memtable + all SSTable levels, applying the same newest-first shadowing
    that ``DB.get`` uses, then returns a deterministically sorted list.

    Tombstone conventions:
      - Memtable (skiplist): deletes are stored as the ``TOMBSTONE`` sentinel.
      - SSTableReader.items(): deletes are yielded as ``value = None``.
    Both are mapped to ``None`` in ``seen`` so the final filter is uniform.
    """
    # seen[key] = bytes  → live value
    # seen[key] = None   → tombstone / deleted; skip in output
    seen: dict[bytes, bytes | None] = {}

    # 1. Active memtable (highest priority).
    for key, value in db._mem.items():
        if key not in seen:
            seen[key] = None if value is TOMBSTONE else value  # type: ignore[arg-type]

    # 2. Level 0 SSTables, newest-first (key ranges may overlap; all are candidates).
    if db._levels:
        for _seq, sst in db._levels[0]:
            for key, value in sst.items():
                # sst.items() returns None for tombstones, bytes for live values.
                if key not in seen:
                    seen[key] = value  # already None for tombstones

    # 3. Level 1+ SSTables (non-overlapping, sorted by key range).
    for level_ssts in db._levels[1:]:
        for _seq, sst in level_ssts:
            for key, value in sst.items():
                if key not in seen:
                    seen[key] = value

    # Filter out tombstones and sort by key.
    live = [(k, v) for k, v in seen.items() if v is not None]
    live.sort(key=lambda kv: kv[0])
    return live


def main(argv: list[str] | None = None) -> None:
    """Entry point for both ``pylsm`` CLI and ``python -m pylsm``."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "put":
        key = args.key.encode()
        value = args.value.encode()
        with DB(args.dir, sync=True) as db:
            db.put(key, value)

    elif args.command == "get":
        key = args.key.encode()
        with DB(args.dir, sync=True) as db:
            result = db.get(key)
        if result is None:
            print(f"key not found: {args.key!r}", file=sys.stderr)
            sys.exit(1)
        print(result.decode(errors="replace"))

    elif args.command == "delete":
        key = args.key.encode()
        with DB(args.dir, sync=True) as db:
            db.delete(key)

    elif args.command == "scan":
        with DB(args.dir, sync=True) as db:
            pairs = _all_live_pairs(db)
        for key, value in pairs:
            print(f"{key.decode(errors='replace')}\t{value.decode(errors='replace')}")

    elif args.command == "compact":
        with DB(args.dir, sync=True) as db:
            db.compact()
        print("compaction complete")
