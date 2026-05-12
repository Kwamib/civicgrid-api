"""Unit tests for auth and rate limiting (no DB or Redis required).

Run with: pytest tests/
"""
from __future__ import annotations

import time

import pytest

from app.auth import (
    KEY_PREFIX,
    PREFIX_LENGTH,
    extract_prefix,
    generate_key,
    verify_key,
)
from app.rate_limit import TIERS, InMemoryBackend


# ---------------------------------------------------------------------------
# Key generation
# ---------------------------------------------------------------------------

class TestKeyGeneration:
    def test_generated_key_has_correct_prefix(self):
        full, prefix, _ = generate_key()
        assert full.startswith(KEY_PREFIX)
        assert prefix == full[:PREFIX_LENGTH]

    def test_generated_keys_are_unique(self):
        seen = set()
        for _ in range(50):
            full, _, _ = generate_key()
            assert full not in seen
            seen.add(full)

    def test_hash_verifies_against_original_key(self):
        full, _, hashed = generate_key()
        assert verify_key(full, hashed) is True

    def test_hash_rejects_wrong_key(self):
        full, _, hashed = generate_key()
        wrong = full[:-1] + ("X" if full[-1] != "X" else "Y")
        assert verify_key(wrong, hashed) is False

    def test_verify_handles_malformed_input(self):
        assert verify_key("", "garbage") is False
        assert verify_key("something", "") is False


class TestPrefixExtraction:
    def test_valid_key_returns_prefix(self):
        full, prefix, _ = generate_key()
        assert extract_prefix(full) == prefix

    def test_missing_prefix_returns_none(self):
        assert extract_prefix("notakey") is None

    def test_empty_string_returns_none(self):
        assert extract_prefix("") is None

    def test_short_key_returns_none(self):
        assert extract_prefix("cg_live_") is None


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

class TestInMemoryBackend:
    def test_first_request_allowed(self):
        b = InMemoryBackend()
        check = b.check_and_increment("test_key", "free")
        assert check.allowed is True
        assert check.day_used == 1

    def test_free_tier_blocks_at_minute_limit(self):
        b = InMemoryBackend()
        limit = TIERS["free"]["per_minute"]
        # Burn the per-minute allowance.
        for _ in range(limit):
            check = b.check_and_increment("burst_key", "free")
            assert check.allowed is True
        # Next request should be blocked.
        blocked = b.check_and_increment("burst_key", "free")
        assert blocked.allowed is False
        assert blocked.retry_after_seconds > 0
        assert blocked.retry_after_seconds <= 60

    def test_starter_tier_higher_minute_ceiling(self):
        b = InMemoryBackend()
        free_min = TIERS["free"]["per_minute"]
        # Should easily allow more than free's minute limit.
        for i in range(free_min + 5):
            check = b.check_and_increment("starter_key", "starter")
            assert check.allowed is True, f"Failed on request {i + 1}"

    def test_unknown_tier_defaults_to_free(self):
        b = InMemoryBackend()
        check = b.check_and_increment("unknown_tier_key", "platinum_emerald")
        assert check.allowed is True
        assert check.day_limit == TIERS["free"]["per_day"]

    def test_different_keys_have_separate_quotas(self):
        b = InMemoryBackend()
        limit = TIERS["free"]["per_minute"]
        for _ in range(limit):
            b.check_and_increment("key_a", "free")
        # key_a is exhausted, key_b should still be fine.
        check_a = b.check_and_increment("key_a", "free")
        check_b = b.check_and_increment("key_b", "free")
        assert check_a.allowed is False
        assert check_b.allowed is True

    def test_rate_limit_headers_populated(self):
        b = InMemoryBackend()
        check = b.check_and_increment("hdr_key", "free")
        assert check.day_limit == TIERS["free"]["per_day"]
        assert check.minute_limit == TIERS["free"]["per_minute"]
        assert check.day_used == 1
        assert check.minute_used == 1
