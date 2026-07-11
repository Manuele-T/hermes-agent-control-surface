"""Pure, best-effort secret-scrubbing for any text field that might reach the
UI from a worker's live session data (`get_worker_activity()` in
app/adapters/kanban.py and app/adapters/synthetic.py). The profile `state.db`
`messages` table's `content`/`tool_calls` columns are raw model/tool
input-output — for a `terminal`/`execute_code` tool call that can be a full
shell command or its stdout/stderr, secrets included. This module is
defense-in-depth over KNOWN secret shapes (API key prefixes, AWS/GitHub/Slack
token formats, `Bearer` headers, `KEY=`/`TOKEN=`-style assignments, and long
opaque hex/base64 blobs) — it is NOT a guarantee against every possible
secret shape. See DISCOVERY.md for the sourcing rationale and this caveat
stated plainly.
"""
from __future__ import annotations

import re

_REDACTED = "[REDACTED]"

# Applied first, in order — each pattern consumes a full known-prefix token
# (including its prefix) so the later generic hex/base64 catch-alls never
# see a partial, already-redacted fragment.
_PREFIXED_TOKEN_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{16,}\b"),  # OpenAI/Anthropic-style API keys
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"\bgh[po]_[A-Za-z0-9]{20,}\b"),  # GitHub personal-access / OAuth token
    re.compile(r"\bxox[bp]-[A-Za-z0-9-]{10,}\b"),  # Slack bot/user token
]
_BEARER = re.compile(r"(?i)\b(Bearer)\s+\S+")
# KEY=/TOKEN=/SECRET=/PASSWORD= assignments (any case, any prefix/suffix on
# the name, e.g. API_KEY=, DB_PASSWORD=) — mask the value, keep the name.
_ASSIGNMENT = re.compile(r"(?i)\b(\w*(?:KEY|TOKEN|SECRET|PASSWORD)\w*)\s*=\s*\S+")
# Standalone opaque blobs with no recognizable prefix — last resort catch-all.
_HEX_BLOB = re.compile(r"\b[A-Fa-f0-9]{32,}\b")
_B64_BLOB = re.compile(r"\b[A-Za-z0-9+/]{32,}={0,2}\b")


def scrub_secrets(text: str) -> str:
    """Best-effort redaction of common secret shapes in `text`. Pure: no I/O,
    no shared state, same input always yields the same output."""
    if not text:
        return text
    out = text
    for pattern in _PREFIXED_TOKEN_PATTERNS:
        out = pattern.sub(_REDACTED, out)
    out = _BEARER.sub(lambda m: f"{m.group(1)} {_REDACTED}", out)
    out = _ASSIGNMENT.sub(lambda m: f"{m.group(1)}={_REDACTED}", out)
    out = _HEX_BLOB.sub(_REDACTED, out)
    out = _B64_BLOB.sub(_REDACTED, out)
    return out


def sanitize_text(text: str | None, max_len: int = 300) -> str | None:
    """The ONE shared helper every emitted worker-activity text field passes
    through: truncate to `max_len` chars, THEN scrub. Truncating first (per
    spec) means a secret split exactly across the boundary could survive in
    fragment form — an accepted, documented gap in this best-effort pass, not
    a silent one (see DISCOVERY.md)."""
    if text is None:
        return None
    truncated = text if len(text) <= max_len else text[:max_len] + "…"
    return scrub_secrets(truncated)
