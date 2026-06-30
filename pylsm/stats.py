"""DB statistics for measuring read and write amplification.

Definitions
───────────

Write amplification (WA)
  WA = disk_bytes_written / user_bytes_written

  ``user_bytes_written``: cumulative bytes the caller passed to put()
    (len(key) + len(value) per call).
  ``disk_bytes_written``: cumulative bytes written to SSTable files on disk
    (both memtable-flush output and compaction output).  Each time the same
    logical byte is rewritten during compaction it is counted again — this
    is exactly what WA measures.

  WA = 1.0 means every user byte was written to disk exactly once (ideal).
  Compaction rewrites data, so WA > 1.0 in practice.

Read amplification (RA)
  RA = sst_accesses / get_calls

  ``get_calls``: get() calls that reached the SSTable layer (key not found
    in the active memtable).
  ``sst_accesses``: SSTables actually searched per get() — i.e. the bloom
    filter did not reject the file and we performed a block-level lookup.

  With no compaction (many L0 files, overlapping ranges) every SSTable must
  be checked in the worst case.  After compaction into L1, the non-overlapping
  invariant means at most one SSTable per level needs to be searched.

Usage
─────
  stats = db.stats
  stats.reset()          # start a fresh measurement window
  ... do operations ...
  print(stats.write_amplification)
  print(stats.read_amplification)
"""

from __future__ import annotations

import dataclasses


@dataclasses.dataclass
class DBStats:
    """Cumulative counters for WA and RA.  Thread-unsafe (single-threaded DB)."""

    user_bytes_written: int = 0
    disk_bytes_written: int = 0
    get_calls: int = 0
    # sst_candidates: SSTables considered before bloom filter check.
    # This is the "gross" RA: with no bloom filter, every candidate
    # would require a full block scan.
    sst_candidates: int = 0
    # sst_accesses: SSTables actually block-scanned (bloom filter accepted).
    # This is the "net" RA the engine pays in practice.
    sst_accesses: int = 0

    @property
    def write_amplification(self) -> float:
        """disk_bytes_written / user_bytes_written; 0.0 when no user writes."""
        if self.user_bytes_written == 0:
            return 0.0
        return self.disk_bytes_written / self.user_bytes_written

    @property
    def read_amplification(self) -> float:
        """Average SSTables block-scanned per get() (net, after bloom filter).

        Returns 0.0 when no get() calls have reached the SSTable layer.
        """
        if self.get_calls == 0:
            return 0.0
        return self.sst_accesses / self.get_calls

    @property
    def gross_read_amplification(self) -> float:
        """Average SSTables considered per get() *before* bloom-filter pruning.

        This reflects the structural cost: with leveled compaction, L1+ files
        have non-overlapping ranges so at most 1 file per level is a candidate.
        Without compaction, every L0 file is a candidate for every get().
        """
        if self.get_calls == 0:
            return 0.0
        return self.sst_candidates / self.get_calls

    def reset(self) -> None:
        """Zero all counters to start a new measurement window."""
        self.user_bytes_written = 0
        self.disk_bytes_written = 0
        self.get_calls = 0
        self.sst_candidates = 0
        self.sst_accesses = 0
