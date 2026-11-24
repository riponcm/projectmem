"""Secret redaction for event text fields.

projectmem stores event data — `summary`, `notes`, `command`, etc. — verbatim
on disk in `.projectmem/events.jsonl`. That's a privacy promise: 100% local,
the user can `git diff` everything. The flip side is that careless input
("the bug only repros when I set `OPENAI_API_KEY=sk-...`") would otherwise
land an API key on disk, in plain text, often in a file that's committed to
git.

This module scrubs high-confidence secret patterns out of event text *before*
it touches disk, replacing each match with `[REDACTED:<kind>]`. It is
intentionally **conservative**: patterns have low false-positive rates so we
don't mangle ordinary debugging notes (e.g. "tried `contain: layout`"
must never trigger redaction).

Escape hatch: set ``PROJECTMEM_NO_REDACT=1`` to skip scrubbing entirely (for
debugging the redactor itself, or for trusted offline contexts).
"""
from __future__ import annotations

import os
import re
from typing import Iterable

# Each pattern is intentionally narrow — anchored to a recognisable prefix
# or structural shape with a minimum length. False positives are worse than
# false negatives in projectmem's domain, so we err toward specificity.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # OpenAI / Anthropic / OpenRouter — keys begin `sk-` and are ≥40 random chars.
    ("openai_key",     re.compile(r"\bsk-[A-Za-z0-9_-]{40,}\b")),

    # GitHub fine-grained PAT (`github_pat_<22 chars>_<59 chars>`).
    ("github_pat",     re.compile(r"\bgithub_pat_[A-Za-z0-9_]{82}\b")),

    # Classic GitHub tokens: ghp_, gho_, ghu_, ghs_, ghr_ followed by 36 chars.
    ("github_token",   re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36}\b")),

    # AWS access key ID — `AKIA` + 16 uppercase alphanumerics.
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),

    # Google API key — `AIza` + 35 chars.
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),

    # Slack tokens (xoxa, xoxb, xoxp, xoxr, xoxs) — long.
    ("slack_token",    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),

    # Stripe live/test secret + publishable + restricted keys.
    ("stripe_key",     re.compile(r"\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{24,}\b")),

    # JWT — three base64url segments, header always begins `eyJ`.
    ("jwt",            re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{10,}\b")),

    # Bearer tokens after the literal "Bearer" / "bearer" keyword.
    ("bearer_token",   re.compile(r"(?<![A-Za-z])[Bb]earer\s+[A-Za-z0-9._~+/=\-]{20,}")),

    # PEM / PGP private-key blocks, from BEGIN to END. If the block is cut off,
    # the armor headers and base64 lines that follow BEGIN. Lines may carry a
    # diff or quote prefix.
    ("private_key",    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----"
        r"(?:(?:(?!-----(?:BEGIN|END) )[\s\S])*"
        r"-----END [A-Z ]*PRIVATE KEY(?: BLOCK)?-----"
        r"|(?:(?:\s|\\[rn])+(?:[-+>|#][ \t]*)?"
        r"(?:(?:Proc-Type|DEK-Info|Version|Comment): [^\r\n\\]*"
        r"|[A-Za-z0-9+/=]{20,}))*)"
    )),
]


# Fields on an Event that may legitimately contain user-supplied text and so
# warrant scrubbing. Structural fields (id, type, outcome, timestamp, etc.)
# are excluded — they're machine-generated and never carry secrets.
REDACTABLE_FIELDS: tuple[str, ...] = (
    "summary",
    "notes",
    "command",
    "git_message",
    "location",
)


def redact(text: str) -> tuple[str, list[str]]:
    """Scrub known secret patterns from ``text``.

    Returns the redacted text plus the list of pattern names that fired
    (one entry per match — duplicates indicate multiple secrets of the
    same type). Callers can use the list length as a count and/or to
    decide whether to emit a notice.

    Conservative by design: an absent pattern is preferred over a
    pattern that occasionally clobbers legitimate prose. New patterns
    should be added only with a clear, low-FP signature.
    """
    if not text:
        return text, []
    matched: list[str] = []

    def _make_repl(name: str):
        def _repl(_match: re.Match[str]) -> str:
            matched.append(name)
            return f"[REDACTED:{name}]"
        return _repl

    out = text
    for name, pat in _PATTERNS:
        out = pat.sub(_make_repl(name), out)
    return out, matched


def is_redaction_enabled() -> bool:
    """Default-on; ``PROJECTMEM_NO_REDACT=1`` disables."""
    return os.environ.get("PROJECTMEM_NO_REDACT", "").strip() not in {"1", "true", "yes"}


def redact_event_fields(obj: object, fields: Iterable[str] = REDACTABLE_FIELDS) -> list[str]:
    """Scrub each redactable string field on ``obj`` in place.

    Returns the flat list of pattern names that fired across all fields.
    Used by ``storage.append_event`` to scrub an Event before it hits
    disk.
    """
    if not is_redaction_enabled():
        return []
    fired: list[str] = []
    for field in fields:
        val = getattr(obj, field, None)
        if isinstance(val, str) and val:
            new_val, matched = redact(val)
            if matched:
                setattr(obj, field, new_val)
                fired.extend(matched)
    return fired
