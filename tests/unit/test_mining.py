# Lenovmail — authored by satuapps (satuapps.com)
"""Tests for value mining: trigger proximity, claimed-span suppression, and masking."""

from __future__ import annotations

from lenovmail.sync.mining import mask_value, mine_values


def kinds(subject: str | None, body: str | None) -> list[tuple[str, str, int]]:
    return [(item.kind, item.value, item.confidence) for item in mine_values(subject, body)]


def test_verification_code_next_to_trigger() -> None:
    assert kinds("Your verification code is 482913", None) == [("otp", "482913", 90)]


def test_bare_number_without_trigger_is_not_a_code() -> None:
    assert kinds("Order 12345 shipped on 2026", None) == []


def test_reset_link_claims_the_token_inside_it() -> None:
    found = kinds("Reset your password", "Open https://acme.test/reset?token=abc123def456ghi789")
    assert found == [("reset_link", "https://acme.test/reset?token=abc123def456ghi789", 90)]


def test_grouped_license_key() -> None:
    assert kinds("Your license: ABCD-EFGH-IJKL-MNOP", None) == [("key", "ABCD-EFGH-IJKL-MNOP", 85)]


def test_promo_code() -> None:
    assert kinds(None, "Use promo code SPRING25 at checkout") == [("promo", "SPRING25", 80)]


def test_trigger_matching_is_word_bounded() -> None:
    # "shipping" contains "pin" but must not turn a tracking number into an OTP.
    assert kinds(None, "Your shipping reference is 4829135") == []


def test_no_input_mines_nothing() -> None:
    assert mine_values(None, None) == []


def test_duplicate_values_are_reported_once() -> None:
    body = "Your code is 482913. Repeating: code 482913."
    assert kinds(None, body) == [("otp", "482913", 90)]


def test_masking_keeps_only_the_tail() -> None:
    assert mask_value("otp", "482913") == "••••13"
    assert mask_value("key", "AB") == "••••"


def test_masking_a_link_keeps_the_host() -> None:
    assert mask_value("reset_link", "https://acme.test/reset?token=abc") == "https://acme.test/…"
    assert mask_value("reset_link", "not a url") == "•••"
