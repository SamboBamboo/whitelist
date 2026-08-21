"""Normalization (§4): forward only, platform-scoped, never reversed.

The TypeScript Worker implements the same rules; both must pass
shared/normalization-fixtures.json. Trim is ASCII-only so JS and Python agree
byte-for-byte. Log-sourced input is never trimmed — the log token is exact,
and Bedrock names may legitimately contain spaces when replace-spaces is off.

Java underscores are real characters: Foo_Bar and Foo__Bar are distinct valid
accounts and separators are never collapsed. Floodgate's space→underscore
mapping is applied forward for form input and NEVER reversed for log input —
the mapping is one-way and lossy, and reversing it would be guesswork.
"""

from __future__ import annotations

from dataclasses import dataclass

_ASCII_WS = " \t\r\n\f\v"


@dataclass(frozen=True)
class NormResult:
    ok: bool
    normalized: str | None = None
    error: str | None = None  # 'empty' | 'prefix_missing'


def ascii_trim(s: str) -> str:
    return s.strip(_ASCII_WS)


def normalize_form(
    platform: str, input_str: str, *, prefix: str, replace_spaces: bool
) -> NormResult:
    """Normalize what the applicant typed: trim → (bedrock) replace spaces →
    lowercase."""
    s = ascii_trim(input_str)
    if platform == "bedrock" and replace_spaces:
        s = s.replace(" ", "_")
    s = s.lower()
    if s == "":
        return NormResult(False, error="empty")
    return NormResult(True, normalized=s)


def normalize_logged(
    platform: str, raw: str, *, prefix: str, replace_spaces: bool
) -> NormResult:
    """Normalize the exact token the server logged: verify and strip the
    configured Floodgate prefix (bedrock), lowercase, nothing else."""
    s = raw
    if platform == "bedrock" and prefix != "":
        if not s.startswith(prefix):
            return NormResult(False, error="prefix_missing")
        s = s[len(prefix):]
    s = s.lower()
    if s == "":
        return NormResult(False, error="empty")
    return NormResult(True, normalized=s)
