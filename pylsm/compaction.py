"""Leveled compaction for pylsm.

Overview
────────
Level 0 (L0): SSTables produced directly by memtable flush.
  Key ranges across files may overlap.  Reads consult all L0 files,
  newest-first.  Trigger: |L0| ≥ L0_COMPACTION_TRIGGER files.

Level 1+ (L1, L2, …): SSTables produced by compaction.
  Within each level key ranges are NON-OVERLAPPING (strict).  Reads can
  therefore binary-search for the single SSTable whose range covers the
  query key, visiting at most one file per level.
  Size trigger: total bytes in level i ≥ L1_BASE_BYTES × SIZE_RATIO^(i-1).

Compaction steps (crash-safe)
──────────────────────────────
1. Select inputs: for L0→L1 take ALL L0 files plus every L1 file whose key
   range overlaps the combined L0 range.  For Ln→L(n+1) pick one file in Ln
   plus overlapping files in L(n+1).
2. Multi-way merge via heapq.  Equal keys: the newer SSTable (higher sequence
   number) wins.  Tombstones are dropped only when the output targets the
   bottom-most level AND the compaction covers the entire overlapping key range
   (so no older copy can lurk in an un-merged file below).
3. Write new output SSTable(s) to disk, fsynced.
4. Atomically update the MANIFEST (tmp write + fsync + os.replace).
5. Delete the old input files.

Crash safety
────────────
• Crash before step 4: new files are orphans — ignored on recovery because
  the MANIFEST still references the old files.
• Crash after step 4 but before step 5: the MANIFEST references the new
  files; the old files are no longer listed and are therefore ignored on
  recovery.  The new files are canonical.
"""

from __future__ import annotations

import dataclasses
import heapq
import itertools
import os
from collections.abc import Iterator
from typing import TYPE_CHECKING

from .sstable import SSTableReader, SSTableWriter

if TYPE_CHECKING:
    from .db import DB

# L0 file count that triggers a compaction into L1.
L0_COMPACTION_TRIGGER: int = 4

# Target size (bytes) for each L1 output SSTable during compaction.
L1_SST_SIZE_TARGET: int = 2 * 1024 * 1024  # 2 MiB

# L1 total size limit (bytes) before triggering L1 → L2 compaction.
L1_BASE_BYTES: int = 10 * 1024 * 1024  # 10 MiB

# Multiplier per level for size limits.
SIZE_RATIO: int = 10


# ---------------------------------------------------------------------------
# Multi-way merge
# ---------------------------------------------------------------------------

_COUNTER = itertools.count()


@dataclasses.dataclass
class _HeapEntry:
    """Heap entry that sorts by (key asc, seq desc) without comparing value/it."""

    key: bytes
    neg_seq: int  # -seq so min-heap pops newest first for equal keys
    tiebreak: int  # monotone counter to break ties without comparing value
    value: bytes | None
    it: Iterator[tuple[bytes, bytes | None]]

    def __lt__(self, other: _HeapEntry) -> bool:
        if self.key != other.key:
            return self.key < other.key
        if self.neg_seq != other.neg_seq:
            return self.neg_seq < other.neg_seq
        return self.tiebreak < other.tiebreak

    def __le__(self, other: _HeapEntry) -> bool:
        return self == other or self < other

    def __gt__(self, other: _HeapEntry) -> bool:
        return not self <= other

    def __ge__(self, other: _HeapEntry) -> bool:
        return not self < other

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, _HeapEntry):
            return NotImplemented
        return (self.key, self.neg_seq, self.tiebreak) == (
            other.key,
            other.neg_seq,
            other.tiebreak,
        )


def merge_sstables(
    seq_reader_pairs: list[tuple[int, SSTableReader]],
    *,
    drop_tombstones: bool = False,
) -> Iterator[tuple[bytes, bytes | None]]:
    """Multi-way merge of SSTables; yields one entry per key (newest wins).

    Parameters
    ──────────
    seq_reader_pairs
        ``[(seq, reader), ...]`` where a higher *seq* means newer data.
        The caller decides ordering; this function only uses *seq* to break
        ties when the same key appears in multiple SSTables.
    drop_tombstones
        When ``True``, tombstone entries (``value is None``) are not yielded.
        Safe only when there is no older data on disk that the tombstone would
        need to shadow (i.e. the output is going to the bottom-most level).

    Yields
    ──────
    ``(key, value)`` pairs in ascending key order, deduplicated so that each
    key appears at most once.  ``value=None`` means tombstone (unless
    *drop_tombstones* is True, in which case those pairs are omitted).
    """
    heap: list[_HeapEntry] = []
    for seq, reader in seq_reader_pairs:
        it = reader.items()
        try:
            key, value = next(it)
            heapq.heappush(
                heap,
                _HeapEntry(
                    key=key,
                    neg_seq=-seq,
                    tiebreak=next(_COUNTER),
                    value=value,
                    it=it,
                ),
            )
        except StopIteration:
            pass

    last_key: bytes | None = None
    while heap:
        entry = heapq.heappop(heap)
        if entry.key != last_key:
            last_key = entry.key
            if not (drop_tombstones and entry.value is None):
                yield entry.key, entry.value
        # Advance this SSTable's iterator and push the next entry.
        try:
            nk, nv = next(entry.it)
            heapq.heappush(
                heap,
                _HeapEntry(
                    key=nk,
                    neg_seq=entry.neg_seq,
                    tiebreak=next(_COUNTER),
                    value=nv,
                    it=entry.it,
                ),
            )
        except StopIteration:
            pass


# ---------------------------------------------------------------------------
# Sequence number helpers
# ---------------------------------------------------------------------------


def _seq_from_name(name: str) -> int:
    """Extract the sequence number from a filename like ``sst_00000042.sst``."""
    return int(os.path.basename(name)[4:12])


# ---------------------------------------------------------------------------
# Compaction decision + execution
# ---------------------------------------------------------------------------


def _level_total_bytes(level_ssts: list[tuple[int, SSTableReader]]) -> int:
    """Return the sum of on-disk file sizes for a level's SSTables."""
    return sum(os.path.getsize(r.path) for _, r in level_ssts)


def _level_size_limit(level: int) -> int:
    """Size cap (bytes) for level *level* (1-indexed)."""
    # L1 = L1_BASE_BYTES, L2 = L1_BASE_BYTES * SIZE_RATIO, etc.
    return L1_BASE_BYTES * (SIZE_RATIO ** (level - 1))


def _overlapping(
    ssts: list[tuple[int, SSTableReader]],
    lo: bytes,
    hi: bytes,
) -> tuple[list[tuple[int, SSTableReader]], list[tuple[int, SSTableReader]]]:
    """Split *ssts* into (overlapping, non-overlapping) with the range [lo, hi]."""
    overlap, non_overlap = [], []
    for seq, r in ssts:
        if r.min_key is None or r.max_key is None:
            non_overlap.append((seq, r))
            continue
        if r.min_key <= hi and r.max_key >= lo:
            overlap.append((seq, r))
        else:
            non_overlap.append((seq, r))
    return overlap, non_overlap


def _write_compaction_output(
    db: DB,
    merged: Iterator[tuple[bytes, bytes | None]],
) -> list[str]:
    """Stream *merged* into one or more new L1-style SSTables; return their paths.

    Each output file is fsynced individually before the next one is started.
    ``db.stats.disk_bytes_written`` is incremented for each file written.
    """
    paths: list[str] = []
    writer: SSTableWriter | None = None
    current_size = 0

    for key, value in merged:
        if writer is None or current_size >= L1_SST_SIZE_TARGET:
            if writer is not None:
                writer.finish()
                paths.append(writer._path)
                db.stats.disk_bytes_written += writer.file_size
                current_size = 0
            db._manifest_seq += 1
            sst_name = f"sst_{db._manifest_seq:08d}.sst"
            writer = SSTableWriter(os.path.join(db.path, sst_name))
        writer.add(key, value)
        current_size += len(key) + (len(value) if value is not None else 0)

    if writer is not None:
        writer.finish()
        paths.append(writer._path)
        db.stats.disk_bytes_written += writer.file_size

    return paths


def _compact_level(db: DB, level_in: int) -> None:
    """Compact one or more SSTables from *level_in* into *level_in + 1*.

    For L0 → L1: all L0 files are merged with any overlapping L1 files.
    For L1 → L2 (and higher): the first file in *level_in* (by key order)
    is merged with all overlapping files in *level_in + 1*.
    """
    level_out = level_in + 1

    # Ensure both levels exist in _levels.
    while len(db._levels) <= level_out:
        db._levels.append([])

    inputs_in = db._levels[level_in]
    inputs_out = db._levels[level_out]

    if not inputs_in:
        return  # nothing to compact

    # --- Select compaction inputs -----------------------------------------
    if level_in == 0:
        # L0: take ALL files (they may overlap each other).
        selected_in = inputs_in[:]
    else:
        # L1+: take just the first file (round-robin would be better in
        # production; for a showcase one file at a time is correct).
        selected_in = [inputs_in[0]]

    # Compute combined key range of selected input files.
    lo = min(r.min_key for _, r in selected_in if r.min_key is not None)
    hi = max(r.max_key for _, r in selected_in if r.max_key is not None)

    # Split the output level into overlapping and non-overlapping partitions.
    selected_out, kept_out = _overlapping(inputs_out, lo, hi)

    # Older files (lower-numbered levels excluded from this run).
    remaining_in = [] if level_in == 0 else inputs_in[1:]

    # Tombstones can be safely dropped when:
    #   • no data exists in any level deeper than level_out (bottom-most level), AND
    #   • no remaining files in level_in hold an older version of the same key.
    no_deeper_data = not any(bool(db._levels[lvl]) for lvl in range(level_out + 1, len(db._levels)))
    drop_ts = no_deeper_data and not remaining_in

    # --- All inputs for the merge ----------------------------------------
    all_inputs: list[tuple[int, SSTableReader]] = selected_in + selected_out
    merged_iter = merge_sstables(all_inputs, drop_tombstones=drop_ts)

    # --- Write new output SSTables ---------------------------------------
    new_paths = _write_compaction_output(db, merged_iter)
    new_readers = [
        (db._manifest_seq - len(new_paths) + i + 1, SSTableReader(p))
        for i, p in enumerate(new_paths)
    ]

    # --- Update in-memory level state ------------------------------------
    if level_in == 0:
        db._levels[0] = []
    else:
        db._levels[level_in] = remaining_in

    # Merge kept_out with new readers and re-sort by min_key.
    combined_out = kept_out + new_readers
    combined_out.sort(key=lambda x: x[1].min_key or b"")
    db._levels[level_out] = combined_out

    # --- Atomically persist MANIFEST, then delete old files --------------
    old_paths = [r.path for _, r in all_inputs]
    db._write_manifest()
    for p in old_paths:
        try:
            os.unlink(p)
        except FileNotFoundError:
            pass  # already deleted (safe)


def do_compaction(db: DB) -> None:
    """Run compaction passes starting from L0 until no trigger fires.

    L0 trigger: |L0| ≥ L0_COMPACTION_TRIGGER.
    L1+ trigger: total bytes in level ≥ _level_size_limit(level).

    Compaction is cascaded: if compacting L0 → L1 causes L1 to exceed its
    limit, L1 → L2 is compacted immediately in the same call.
    """
    # L0 trigger.
    if db._levels and len(db._levels[0]) >= L0_COMPACTION_TRIGGER:
        _compact_level(db, 0)

    # Cascade: check L1, L2, … for size overflow.
    level = 1
    while level < len(db._levels):
        if _level_total_bytes(db._levels[level]) >= _level_size_limit(level):
            _compact_level(db, level)
        else:
            break
        level += 1
