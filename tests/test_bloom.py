"""Unit tests for BloomFilter.

Covers: parameter derivation, no false negatives, false-positive rate within
bounds, serialization roundtrip, and error handling.
"""

from __future__ import annotations

import math
import random

import pytest

from pylsm.bloom import _HDR, BloomFilter

# ---------------------------------------------------------------------------
# Parameter derivation
# ---------------------------------------------------------------------------


class TestParameterDerivation:
    def test_m_and_k_match_formula(self):
        """m and k must match the optimal closed-form expressions."""
        n, p = 1000, 0.01
        bf = BloomFilter(n, p)
        ln2 = math.log(2)
        expected_m = math.ceil(-n * math.log(p) / (ln2**2))
        expected_k = max(1, min(round((expected_m / n) * ln2), 30))
        assert bf.m == expected_m
        assert bf.k == expected_k

    def test_higher_fpr_yields_smaller_m(self):
        n = 500
        bf_tight = BloomFilter(n, 0.001)
        bf_loose = BloomFilter(n, 0.10)
        assert bf_tight.m > bf_loose.m

    def test_more_items_yields_larger_m(self):
        bf_small = BloomFilter(100, 0.01)
        bf_large = BloomFilter(10_000, 0.01)
        assert bf_large.m > bf_small.m

    def test_k_at_least_one(self):
        # Even with extreme parameters k must be ≥ 1.
        bf = BloomFilter(1, 0.999)
        assert bf.k >= 1

    def test_k_capped_at_30(self):
        # Very low FPR → large m/n → k could exceed 30 without the cap.
        bf = BloomFilter(1, 1e-10)
        assert bf.k <= 30

    def test_invalid_expected_items_raises(self):
        with pytest.raises(ValueError, match="expected_items"):
            BloomFilter(0, 0.01)

    def test_invalid_fpr_zero_raises(self):
        with pytest.raises(ValueError, match="false_positive_rate"):
            BloomFilter(10, 0.0)

    def test_invalid_fpr_one_raises(self):
        with pytest.raises(ValueError, match="false_positive_rate"):
            BloomFilter(10, 1.0)


# ---------------------------------------------------------------------------
# No false negatives (soundness)
# ---------------------------------------------------------------------------


class TestNoFalseNegatives:
    def test_single_key_always_found(self):
        bf = BloomFilter(1, 0.01)
        bf.add(b"hello")
        assert b"hello" in bf

    def test_all_added_keys_found(self):
        """Every key inserted must return True — zero false negatives allowed."""
        bf = BloomFilter(100, 0.01)
        keys = [f"key:{i}".encode() for i in range(100)]
        for k in keys:
            bf.add(k)
        for k in keys:
            assert k in bf, f"False negative for {k!r}"

    def test_keys_with_null_bytes(self):
        bf = BloomFilter(10, 0.01)
        keys = [bytes([0, i, 255]) for i in range(10)]
        for k in keys:
            bf.add(k)
        for k in keys:
            assert k in bf

    def test_empty_key(self):
        bf = BloomFilter(5, 0.01)
        bf.add(b"")
        assert b"" in bf

    def test_large_key(self):
        bf = BloomFilter(5, 0.01)
        k = b"x" * 10_000
        bf.add(k)
        assert k in bf

    def test_empty_filter_never_returns_true(self):
        """An empty bloom filter must return False for every probe."""
        bf = BloomFilter(10, 0.01)
        for i in range(100):
            assert f"key{i}".encode() not in bf


# ---------------------------------------------------------------------------
# False-positive rate
# ---------------------------------------------------------------------------


class TestFalsePositiveRate:
    def test_fpr_within_3x_theoretical(self):
        """Observed FPR over 10 000 non-members should be ≤ 3·p."""
        rng = random.Random(42)
        n, p = 1_000, 0.01
        bf = BloomFilter(n, p)
        members: set[bytes] = set()
        while len(members) < n:
            members.add(rng.randbytes(8))
        for k in members:
            bf.add(k)

        non_members: list[bytes] = []
        while len(non_members) < 10_000:
            k = rng.randbytes(8)
            if k not in members:
                non_members.append(k)

        false_positives = sum(1 for k in non_members if k in bf)
        observed = false_positives / len(non_members)
        assert observed <= 3 * p, f"FPR {observed:.4f} exceeds 3·p = {3 * p:.4f}"


# ---------------------------------------------------------------------------
# Serialization roundtrip
# ---------------------------------------------------------------------------


class TestSerialization:
    def test_empty_filter_roundtrip(self):
        bf = BloomFilter(10, 0.01)
        restored = BloomFilter.from_bytes(bf.to_bytes())
        assert restored.m == bf.m
        assert restored.k == bf.k
        assert restored._bits == bf._bits

    def test_populated_filter_roundtrip(self):
        bf = BloomFilter(50, 0.01)
        keys = [f"k{i}".encode() for i in range(50)]
        for k in keys:
            bf.add(k)
        restored = BloomFilter.from_bytes(bf.to_bytes())
        for k in keys:
            assert k in restored, f"key {k!r} missing after roundtrip"

    def test_to_bytes_exact_length(self):
        bf = BloomFilter(100, 0.01)
        expected = _HDR.size + math.ceil(bf.m / 8)
        assert len(bf.to_bytes()) == expected

    def test_roundtrip_preserves_m_and_k(self):
        for n, p in [(10, 0.05), (500, 0.01), (10_000, 0.001)]:
            bf = BloomFilter(n, p)
            restored = BloomFilter.from_bytes(bf.to_bytes())
            assert restored.m == bf.m, f"m mismatch for n={n}, p={p}"
            assert restored.k == bf.k, f"k mismatch for n={n}, p={p}"

    def test_from_bytes_too_short_raises(self):
        with pytest.raises(ValueError, match="too short"):
            BloomFilter.from_bytes(b"\x00\x00")

    def test_from_bytes_truncated_raises(self):
        """Partial bit array (header only) must raise ValueError."""
        bf = BloomFilter(100, 0.01)
        header_only = bf.to_bytes()[: _HDR.size]  # exactly 5 bytes — no bit data
        with pytest.raises(ValueError, match="truncated"):
            BloomFilter.from_bytes(header_only)
