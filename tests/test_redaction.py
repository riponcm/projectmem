"""Unit tests for the secret redaction scrubber.

Two priorities:
  1. Real high-confidence secret patterns get redacted.
  2. Ordinary projectmem prose ("tried `contain: layout`", file paths,
     plain English about auth) is left untouched.

False positives are the failure mode that matters most here — they would
mangle real user events. The test cases below pin the conservative
behavior we promise.

A note on fixture construction
──────────────────────────────
The secret-looking strings below are assembled programmatically (prefix +
filler chars) rather than written as string literals. This is deliberate:
GitHub's push protection scans source files for real-looking secrets
and would otherwise block this file from being pushed. The assembled
fixtures still match our redaction regex (they share the right prefix
and length) but are not high-entropy enough to look like genuine
credentials to any scanner.
"""
from __future__ import annotations

import os

import pytest

from projectmem.redaction import (
    REDACTABLE_FIELDS,
    redact,
    is_redaction_enabled,
)


# ── fixture helpers (low-entropy, scanner-safe constructions) ───────────
#
# Each helper returns a string that matches the corresponding regex in
# redaction.py BUT is built from repeated low-entropy chars, so neither
# GitHub's push-protection scanner nor any real secret-leak detector
# would flag it as a credential.

_PRE_OPENAI = "s" + "k" + "-"
_PRE_GHPAT = "github" + "_" + "pat_"
_PRE_GHCLASSIC = "g" + "hp" + "_"
_PRE_AWS = "AKIA"
_PRE_GOOGLE = "AIza"
_PRE_SLACK = "x" + "oxb-"
_PRE_STRIPE_LIVE = "sk_live_"
_PRE_STRIPE_TEST = "pk_test_"
_PRE_JWT = "ey" + "J"


def _openai_key() -> str:           # sk- + 48 filler  → matches `sk-[A-Za-z0-9_-]{40,}`
    return _PRE_OPENAI + ("a" * 48)


def _github_classic_token() -> str:  # ghp_ + 36 filler → matches `gh[pousr]_[A-Za-z0-9]{36}`
    return _PRE_GHCLASSIC + ("a" * 36)


def _github_fine_grained_pat() -> str:  # github_pat_ + 22 + _ + 59 = 82 word chars
    return _PRE_GHPAT + ("a" * 22) + "_" + ("b" * 59)


def _aws_access_key() -> str:        # AKIA + 16 uppercase letters
    return _PRE_AWS + ("A" * 16)


def _google_api_key() -> str:        # AIza + 35 filler chars
    return _PRE_GOOGLE + ("a" * 35)


def _slack_token() -> str:           # xoxb-... long
    return _PRE_SLACK + ("1" * 12) + "-" + ("a" * 24)


def _stripe_live_key() -> str:       # sk_live_ + 24 chars
    return _PRE_STRIPE_LIVE + ("a" * 24)


def _stripe_test_pk() -> str:        # pk_test_ + 24 chars
    return _PRE_STRIPE_TEST + ("a" * 24)


def _jwt() -> str:
    # Three base64url-ish segments separated by dots. Segments start with
    # `eyJ` and are long enough to match our regex.
    seg = _PRE_JWT + ("a" * 30)
    return seg + "." + _PRE_JWT + ("b" * 30) + "." + ("c" * 30)


def _bearer_token() -> str:
    return "Bearer " + ("a" * 30)


def _private_key_header() -> str:
    return "-----BEGIN RSA PRIVATE KEY-----"


# ── true positives ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "input_text_fn, kind",
    [
        (lambda: "hit the API with " + _openai_key() + " key", "openai_key"),
        (lambda: "token=" + _github_classic_token(), "github_token"),
        (lambda: _github_fine_grained_pat(), "github_pat"),
        (lambda: "export AWS_ACCESS_KEY_ID=" + _aws_access_key(), "aws_access_key"),
        (lambda: "config: " + _google_api_key(), "google_api_key"),
        (lambda: "send to " + _slack_token(), "slack_token"),
        (lambda: "stripe key " + _stripe_live_key(), "stripe_key"),
        (lambda: "publishable " + _stripe_test_pk(), "stripe_key"),
        (lambda: "Authorization: " + _jwt(), "jwt"),
        (lambda: "curl -H 'Authorization: " + _bearer_token() + "'", "bearer_token"),
        (lambda: "config: " + _private_key_header(), "private_key"),
    ],
)
def test_redacts_known_secret_shapes(input_text_fn, kind: str) -> None:
    input_text = input_text_fn()
    out, fired = redact(input_text)
    assert fired, f"expected redaction for {kind!r} input but nothing fired"
    assert kind in fired
    assert f"[REDACTED:{kind}]" in out
    # The constructed secret payload itself must be gone from the output.
    if kind == "openai_key":
        assert _PRE_OPENAI + "a" * 10 not in out
    if kind == "github_token":
        assert _PRE_GHCLASSIC + "a" * 10 not in out
    if kind == "aws_access_key":
        assert _PRE_AWS + "A" * 10 not in out


# ── private keys: the whole block, not just the header ──────────────────


def _private_key_body() -> str:
    # Made-up base64 lines, with "+", "/" and "=" like a real key body.
    return "\n".join(["MII" + "a1B+" * 15, "c2/D" * 16, "e3F=="])


def _private_key_block(kind: str = "", suffix: str = "") -> str:
    label = kind + "PRIVATE " + "KEY" + suffix
    return (
        "-----BEGIN " + label + "-----\n"
        + _private_key_body()
        + "\n-----END " + label + "-----"
    )


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\\n"])
@pytest.mark.parametrize("kind", ["", "RSA ", "EC ", "OPENSSH ", "ENCRYPTED "])
def test_redacts_whole_private_key(kind: str, newline: str) -> None:
    key = _private_key_block(kind).replace("\n", newline)
    out, fired = redact("deploy fails with " + key + " in the env")
    assert out == "deploy fails with [REDACTED:private_key] in the env"
    assert fired == ["private_key"]


def test_redacts_whole_pgp_private_key() -> None:
    out, _ = redact(_private_key_block("PGP ", " BLOCK"))
    assert out == "[REDACTED:private_key]"


def test_redacts_private_key_with_pem_headers() -> None:
    key = _private_key_block("RSA ").replace(
        "-----\n", "-----\nProc-Type: 4,ENCRYPTED\nDEK-Info: AES-128-CBC,0A1B\n\n", 1
    )
    out, _ = redact(key)
    assert out == "[REDACTED:private_key]"


def test_redacts_private_key_that_is_cut_off() -> None:
    block = _private_key_block()
    out, _ = redact(block[: block.index("\ne3F==")])
    assert out == "[REDACTED:private_key]"


@pytest.mark.parametrize("prefix", ["-", "+", "> ", "# "])
def test_redacts_private_key_in_a_diff_or_quote(prefix: str) -> None:
    lines = _private_key_block("RSA ").split("\n")
    whole, _ = redact("\n".join(prefix + line for line in lines) + "\nok")
    assert whole == prefix + "[REDACTED:private_key]\nok"
    cut, _ = redact("\n".join(prefix + line for line in lines[:-2]))
    assert cut == prefix + "[REDACTED:private_key]"


@pytest.mark.parametrize(
    "headers",
    ["Proc-Type: 4,ENCRYPTED\nDEK-Info: AES-128-CBC,0A1B\n", "Version: GnuPG v2\n"],
)
def test_redacts_cut_off_private_key_with_armor_headers(headers: str) -> None:
    block = _private_key_block().replace("-----\n", "-----\n" + headers + "\n", 1)
    out, _ = redact(block[: block.index("\ne3F==")])
    assert out == "[REDACTED:private_key]"


def test_keeps_text_between_two_private_keys() -> None:
    key = _private_key_block()
    out, fired = redact("old: " + key + " new: " + key)
    assert out == "old: [REDACTED:private_key] new: [REDACTED:private_key]"
    assert fired == ["private_key", "private_key"]


def test_keeps_text_after_a_private_key_header() -> None:
    out, _ = redact("parser rejects " + _private_key_header() + " without a newline")
    assert out == "parser rejects [REDACTED:private_key] without a newline"


# ── false-positive guards ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "safe_text",
    [
        # The motivating example from the recording-notes Bug 1 — must never
        # be touched by redaction.
        "tried `contain: layout` in styles.css and it did not fix the layout shift",
        "summary: bug in src/auth.py — login form submits twice",
        "Decision: use bcrypt rounds=12 for password hashing.",
        # Looks superficially like an env var but is plain English.
        "OPENAI_API_KEY is required in the environment for this script to run.",
        # File paths
        ".projectmem/events.jsonl",
        # Short tokens that look prefix-y but are too short to be real keys
        "sk- short prefix only",
        "ghp_short",
        # Plain language about passwords
        "Forgot password reset flow needs a token email.",
        # Bearer without the long token
        "use bearer auth",
        # An npm package version that contains 'eyJ'
        "version eyJsomeshortstring",
    ],
)
def test_does_not_touch_ordinary_prose(safe_text: str) -> None:
    out, fired = redact(safe_text)
    assert fired == [], f"unexpected redaction in {safe_text!r}: {fired}"
    assert out == safe_text


# ── opt-out ─────────────────────────────────────────────────────────────


def test_is_redaction_enabled_default_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PROJECTMEM_NO_REDACT", raising=False)
    assert is_redaction_enabled() is True


@pytest.mark.parametrize("value", ["1", "true", "yes"])
def test_env_var_disables(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("PROJECTMEM_NO_REDACT", value)
    assert is_redaction_enabled() is False


def test_redact_event_fields_honours_opt_out(monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import dataclass

    from projectmem.redaction import redact_event_fields

    @dataclass
    class Fake:
        summary: str = ""
        notes: str | None = None

    monkeypatch.setenv("PROJECTMEM_NO_REDACT", "1")
    leaked = "leaked " + _openai_key() + " key"
    fake = Fake(summary=leaked)
    fired = redact_event_fields(fake)
    assert fired == []
    assert _PRE_OPENAI + "a" * 10 in fake.summary  # untouched when disabled


def test_redact_event_fields_default_on(monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import dataclass

    from projectmem.redaction import redact_event_fields

    @dataclass
    class Fake:
        summary: str = ""
        notes: str | None = None
        command: str | None = None
        git_message: str | None = None
        location: str | None = None

    monkeypatch.delenv("PROJECTMEM_NO_REDACT", raising=False)
    fake = Fake(
        summary="see " + _openai_key(),
        notes="harmless note about auth",
    )
    fired = redact_event_fields(fake)
    assert len(fired) == 1
    assert "openai_key" in fired
    assert "[REDACTED:openai_key]" in fake.summary
    assert fake.notes == "harmless note about auth"  # untouched


def test_redactable_fields_constant_is_stable() -> None:
    # Pin the field set so a future refactor doesn't silently shrink the
    # surface that gets scrubbed.
    assert REDACTABLE_FIELDS == (
        "summary",
        "notes",
        "command",
        "git_message",
        "location",
    )
