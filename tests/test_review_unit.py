"""Unit tests for the review workflow's pure helpers (no database needed)."""

from __future__ import annotations

from app.review import already_published, derive_last_name, is_stale, norm_name


class TestNormName:
    def test_case_punctuation_and_spacing_are_ignored(self):
        assert norm_name("  Gary  L'Heureux ") == norm_name("gary lheureux")

    def test_accents_are_ignored_for_comparison(self):
        assert norm_name("José Peña") == norm_name("Jose Pena")

    def test_real_differences_still_differ(self):
        assert norm_name("Karen Dallman") != norm_name("Rodney Head")

    def test_empty_values(self):
        assert norm_name(None) == ""
        assert norm_name("") == ""


class TestDeriveLastName:
    def test_simple(self):
        assert derive_last_name("Ralph Dance") == "Dance"

    def test_suffix_is_skipped(self):
        assert derive_last_name("Robert J. Smith Jr.") == "Smith"
        assert derive_last_name("John Doe III") == "Doe"

    def test_single_token(self):
        assert derive_last_name("Cher") == "Cher"


class TestStaleness:
    current = {"id": 10, "full_name": "Doug Diny"}

    def test_matching_leader_id_is_fresh(self):
        assert not is_stale({"db_leader_id": 10, "db_mayor": "anything"}, self.current)

    def test_different_leader_id_is_stale(self):
        assert is_stale({"db_leader_id": 9, "db_mayor": "Doug Diny"}, self.current)

    def test_legacy_rows_fall_back_to_name(self):
        assert not is_stale({"db_leader_id": None, "db_mayor": "doug  diny"}, self.current)
        assert is_stale({"db_leader_id": None, "db_mayor": "Someone Else"}, self.current)

    def test_no_current_leader(self):
        assert not is_stale({"db_leader_id": None, "db_mayor": None}, None)
        assert is_stale({"db_leader_id": None, "db_mayor": "Doug Diny"}, None)


class TestAlreadyPublished:
    def test_detects_proposed_name_already_current(self):
        assert already_published({"web_mayor": "Doug Diny"}, {"id": 1, "full_name": "Doug Diny"})

    def test_different_name(self):
        assert not already_published(
            {"web_mayor": "Sarah Brohawn"}, {"id": 1, "full_name": "Doug Diny"}
        )

    def test_no_proposed_name(self):
        assert not already_published({"web_mayor": None}, {"id": 1, "full_name": "Doug Diny"})
