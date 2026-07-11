"""Unit tests for the pure secret-scrubbing helper (app/redact.py), added
alongside the live-worker-activity feature (DISCOVERY.md spike). Best-effort
over KNOWN secret shapes — these cases are exactly the shapes the feature's
hard privacy rule calls out, not an exhaustive security audit."""
from __future__ import annotations

from app.redact import sanitize_text, scrub_secrets


def test_scrubs_anthropic_style_key() -> None:
    raw = "using key sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz1234567890ABCDEFGH for the call"
    out = scrub_secrets(raw)
    assert "AbCdEfGhIjKlMnOpQrStUvWxYz1234567890ABCDEFGH" not in out
    assert "sk-ant" not in out
    assert "[REDACTED]" in out
    assert "using key" in out and "for the call" in out


def test_scrubs_aws_access_key() -> None:
    raw = "export AWS key: AKIAIOSFODNN7EXAMPLE please rotate it"
    out = scrub_secrets(raw)
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert "[REDACTED]" in out
    assert "please rotate it" in out


def test_scrubs_bearer_token() -> None:
    raw = "curl -H 'Authorization: Bearer abc123def456ghi789jkl' https://api.example.com"
    out = scrub_secrets(raw)
    assert "abc123def456ghi789jkl" not in out
    assert "Bearer [REDACTED]" in out
    assert "https://api.example.com" in out


def test_scrubs_assignment_keeps_name() -> None:
    raw = "ran with TOKEN=supersecret and DEBUG=true"
    out = scrub_secrets(raw)
    assert "supersecret" not in out
    assert "TOKEN=[REDACTED]" in out
    # DEBUG= isn't a KEY/TOKEN/SECRET/PASSWORD name — must survive untouched.
    assert "DEBUG=true" in out


def test_scrubs_all_four_together_preserves_surrounding_text() -> None:
    raw = (
        "Config dump: sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz1234567890ABCDEFGH, "
        "AKIAIOSFODNN7EXAMPLE, header 'Bearer abc123def456ghi789jkl', "
        "and TOKEN=supersecret were all found in the file."
    )
    out = scrub_secrets(raw)
    for secret in (
        "AbCdEfGhIjKlMnOpQrStUvWxYz1234567890ABCDEFGH",
        "AKIAIOSFODNN7EXAMPLE",
        "abc123def456ghi789jkl",
        "supersecret",
    ):
        assert secret not in out
    assert out.count("[REDACTED]") == 4
    assert "Config dump:" in out
    assert "were all found in the file." in out
    assert "TOKEN=[REDACTED]" in out


def test_scrubs_github_and_slack_tokens() -> None:
    raw = "gh token ghp_abcdefghijklmnopqrstuvwxyz0123456789AB and slack xoxb-1234567890-abcdefgh"
    out = scrub_secrets(raw)
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789AB" not in out
    assert "xoxb-1234567890-abcdefgh" not in out
    assert out.count("[REDACTED]") == 2


def test_scrubs_standalone_hex_blob() -> None:
    raw = "session hash was deadbeefdeadbeefdeadbeefdeadbeefdeadbeef end"
    out = scrub_secrets(raw)
    assert "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef" not in out
    assert "session hash was" in out and "end" in out


def test_scrub_is_pure_and_idempotent() -> None:
    raw = "TOKEN=supersecret"
    assert scrub_secrets(raw) == scrub_secrets(raw)
    once = scrub_secrets(raw)
    assert scrub_secrets(once) == once  # already-redacted text is stable


def test_scrub_empty_and_none_safe() -> None:
    assert scrub_secrets("") == ""


def test_sanitize_text_truncates_then_scrubs() -> None:
    long_text = "a" * 400
    out = sanitize_text(long_text, max_len=300)
    assert out is not None
    assert len(out) <= 301  # 300 + the ellipsis marker
    assert out.endswith("…")


def test_sanitize_text_none_passthrough() -> None:
    assert sanitize_text(None) is None


def test_sanitize_text_scrubs_after_truncation() -> None:
    # The secret sits well within the truncation bound, so it must still be caught.
    text = "prefix " + "x" * 50 + " TOKEN=supersecret suffix"
    out = sanitize_text(text, max_len=300)
    assert out is not None
    assert "supersecret" not in out
    assert "TOKEN=[REDACTED]" in out
