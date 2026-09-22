# Lenovmail — authored by satuapps (satuapps.com)
"""Extract actionable values out of a message: one-time codes, password reset links,
license keys, and promo codes.

Everything here is pure text work — no session, no I/O — so the ingest path can call it
inline and the unit tests can exercise it without a database.

Two rules keep the output honest:

* A bare number or an uppercase word means nothing on its own, so every kind except a
  grouped license key requires a trigger word nearby. Triggers are matched on word
  boundaries: a substring search would find "pin" inside "shipping".
* Spans are claimed. Links are detected first, so the digits inside a reset URL are not
  also reported as a one-time code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

VALUE_KINDS: tuple[str, ...] = ("otp", "reset_link", "key", "promo")

# Bodies are scanned from the top: the actionable value is in the first screen of a
# transactional mail, and the tail is footers and legal boilerplate.
MAX_SCAN_CHARS = 20_000
MAX_PER_KIND = 20

# A trigger this close to the candidate makes the match confident; up to `NEAR_CHARS` it is
# still accepted, but reported with lower confidence.
NEAR_CHARS = 80
CLOSE_CHARS = 40

_TRIGGERS: dict[str, tuple[str, ...]] = {
    "otp": (
        "code",
        "kode",
        "otp",
        "verification",
        "verifikasi",
        "passcode",
        "pin",
        "one-time",
        "sekali pakai",
        "2fa",
        "security code",
    ),
    "reset_link": (
        "reset",
        "password",
        "sandi",
        "forgot",
        "recover",
        "verify",
        "confirm",
        "activate",
        "magic",
        "sign in",
        "login",
    ),
    "key": (
        "license",
        "licence",
        "serial",
        "product key",
        "api key",
        "lisensi",
        "kunci",
        "token",
    ),
    "promo": (
        "promo",
        "promo code",
        "kode promo",
        "coupon",
        "kupon",
        "voucher",
        "discount",
        "diskon",
    ),
}


def _trigger_pattern(kind: str) -> re.Pattern[str]:
    words = sorted(_TRIGGERS[kind], key=len, reverse=True)
    return re.compile(r"\b(?:" + "|".join(re.escape(word) for word in words) + r")\b")


_TRIGGER_RE = {kind: _trigger_pattern(kind) for kind in VALUE_KINDS}

_OTP_RE = re.compile(r"(?<![\w-])\d{4,8}(?![\w-])")
_URL_RE = re.compile(r"https?://[^\s<>\"'\]\)]+")
_KEY_GROUPED_RE = re.compile(r"(?<![\w-])[A-Z0-9]{4}(?:-[A-Z0-9]{4}){2,5}(?![\w-])")
_KEY_BARE_RE = re.compile(r"(?<![\w-])[A-Za-z0-9_-]{16,128}(?![\w-])")
_PROMO_RE = re.compile(r"(?<![\w-])[A-Z0-9][A-Z0-9-]{3,23}(?![\w-])")

# A URL whose own path or query names the action is worth more than one that merely sits
# under a "reset your password" sentence.
_STRONG_LINK_RE = re.compile(r"(?i)(reset|verify|confirm|activate|token=|key=)")

# Trailing sentence punctuation is part of the prose, not of the link.
_URL_TRAILING = ".,;:!?'\""

# Links are claimed before anything else so the digits and tokens inside them are not
# re-reported as codes or keys. Output is re-sorted into `VALUE_KINDS` order.
_DETECTION_ORDER: tuple[str, ...] = ("reset_link", "otp", "key", "promo")


@dataclass(frozen=True, slots=True)
class MinedValue:
    kind: str
    value: str
    confidence: int


def _trigger_distance(
    lowered: str, start: int, end: int, kind: str, *, before_only: bool = False
) -> int | None:
    """Characters between the candidate and the nearest trigger word, or None if there is none."""
    window_start = max(0, start - NEAR_CHARS)
    window_end = start if before_only else min(len(lowered), end + NEAR_CHARS)
    if window_end <= window_start:
        return None

    best: int | None = None
    for match in _TRIGGER_RE[kind].finditer(lowered, window_start, window_end):
        if match.end() <= start:
            gap = start - match.end()
        elif match.start() >= end:
            gap = match.start() - end
        else:
            gap = 0
        if best is None or gap < best:
            best = gap
    return best


def _claimed(spans: list[tuple[int, int]], start: int, end: int) -> bool:
    return any(start < span_end and end > span_start for span_start, span_end in spans)


def _trim_url(raw: str) -> str:
    return raw.rstrip(_URL_TRAILING)


def _find_links(text: str, lowered: str) -> list[tuple[int, int, str, int]]:
    found: list[tuple[int, int, str, int]] = []
    for match in _URL_RE.finditer(text):
        value = _trim_url(match.group())
        if not value:
            continue
        end = match.start() + len(value)
        if _STRONG_LINK_RE.search(value):
            found.append((match.start(), end, value, 90))
            continue
        distance = _trigger_distance(lowered, match.start(), end, "reset_link", before_only=True)
        if distance is not None or _TRIGGER_RE["reset_link"].search(value.lower()):
            found.append((match.start(), end, value, 70))
    return found


def _find_otps(text: str, lowered: str) -> list[tuple[int, int, str, int]]:
    found: list[tuple[int, int, str, int]] = []
    for match in _OTP_RE.finditer(text):
        distance = _trigger_distance(lowered, match.start(), match.end(), "otp")
        if distance is None:
            continue
        found.append(
            (match.start(), match.end(), match.group(), 90 if distance <= CLOSE_CHARS else 70)
        )
    return found


def _find_keys(text: str, lowered: str) -> list[tuple[int, int, str, int]]:
    found: list[tuple[int, int, str, int]] = [
        (match.start(), match.end(), match.group(), 85) for match in _KEY_GROUPED_RE.finditer(text)
    ]
    for match in _KEY_BARE_RE.finditer(text):
        distance = _trigger_distance(lowered, match.start(), match.end(), "key", before_only=True)
        if distance is None:
            continue
        found.append((match.start(), match.end(), match.group(), 70))
    return found


def _find_promos(text: str, lowered: str) -> list[tuple[int, int, str, int]]:
    found: list[tuple[int, int, str, int]] = []
    for match in _PROMO_RE.finditer(text):
        distance = _trigger_distance(lowered, match.start(), match.end(), "promo")
        if distance is None:
            continue
        found.append(
            (match.start(), match.end(), match.group(), 80 if distance <= CLOSE_CHARS else 60)
        )
    return found


_FINDERS = {
    "reset_link": _find_links,
    "otp": _find_otps,
    "key": _find_keys,
    "promo": _find_promos,
}


def mine_values(subject: str | None, body_text: str | None) -> list[MinedValue]:
    """Values worth acting on in one message, deduplicated and capped per kind."""
    if subject is None and body_text is None:
        return []

    text = f"{subject or ''}\n{(body_text or '')[:MAX_SCAN_CHARS]}"
    if not text.strip():
        return []
    lowered = text.lower()

    spans: list[tuple[int, int]] = []
    seen: set[tuple[str, str]] = set()
    per_kind: dict[str, list[MinedValue]] = {kind: [] for kind in VALUE_KINDS}

    for kind in _DETECTION_ORDER:
        for start, end, value, confidence in _FINDERS[kind](text, lowered):
            if _claimed(spans, start, end):
                continue
            spans.append((start, end))
            if (kind, value) in seen or len(per_kind[kind]) >= MAX_PER_KIND:
                continue
            seen.add((kind, value))
            per_kind[kind].append(MinedValue(kind=kind, value=value, confidence=confidence))

    return [item for kind in VALUE_KINDS for item in per_kind[kind]]


def mask_value(kind: str, value: str) -> str:
    """Display form that proves a value is there without handing it over."""
    if kind == "reset_link":
        parts = urlsplit(value)
        if not parts.netloc:
            return "•••"
        return f"{parts.scheme}://{parts.netloc}/…"
    if len(value) <= 2:
        return "••••"
    return "••••" + value[-2:]
