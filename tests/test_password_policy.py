"""Password policy: length first, compressed blocklist, obvious personal data."""

from __future__ import annotations

from core.common_passwords import common_passwords
from core.password_policy import PASSWORD_MIN_LENGTH, password_issue


def test_common_password_table_only_contains_length_passing_entries():
    table = common_passwords()
    assert table
    assert min(len(value) for value in table) >= PASSWORD_MIN_LENGTH


def test_blocklist_and_leet_variants_are_rejected():
    assert password_issue("password") is not None
    assert password_issue("password123") is not None
    assert password_issue("P@ssw0rd!2026") is not None


def test_repeated_and_sequential_values_are_rejected():
    assert password_issue("aaaaaaaa") is not None
    assert password_issue("abcdefgh") is not None
    assert password_issue("87654321") is not None


def test_email_local_part_and_site_name_are_rejected():
    assert password_issue("alice-2026", email="alice@example.com") is not None
    assert password_issue("linxzhang2026") is not None


def test_a_long_unique_password_is_accepted():
    assert password_issue("Mango-Orbit-7421") is None
    assert password_issue("correct horse battery staple") is None


def test_blank_and_short_values_are_rejected():
    assert password_issue("        ") is not None
    assert password_issue("short") is not None
