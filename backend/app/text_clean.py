"""Pure helper that strips Hermes's internal `<untrusted_tool_result>`
envelope from tool-output text before it reaches `sanitize_text()`
(`get_worker_activity()` in app/adapters/kanban.py). The envelope wraps raw
tool output with a source tag and an instruction-injection-defense preamble
telling the model to treat the content as inert data — neither of which is
useful to a human reading the Live activity feed. Tolerant: unmatched input
is returned UNCHANGED, never dropped or erroring.
"""
from __future__ import annotations

import re

# Opening tag, e.g. <untrusted_tool_result source="browser_console">, plus the
# standard preamble sentence that follows it, ending at "...issue instructions."
# (with or without the closing "</untrusted_tool_result>" tag, which may be
# absent if the content was truncated upstream).
_ENVELOPE_OPEN_AND_PREAMBLE = re.compile(
    r"<untrusted_tool_result[^>]*>"
    r"\s*The following content was retrieved from an external source\..*?"
    r"issue instructions\.\s*",
    re.IGNORECASE | re.DOTALL,
)
_ENVELOPE_CLOSE = re.compile(r"\s*</untrusted_tool_result>\s*$", re.IGNORECASE)


def strip_tool_result_envelope(text: str) -> str:
    """Remove the `<untrusted_tool_result>` wrapper and its preamble/closing
    tag, returning only the inner payload. If the envelope isn't present,
    returns `text` unchanged."""
    if not text:
        return text
    out = _ENVELOPE_OPEN_AND_PREAMBLE.sub("", text, count=1)
    if out == text:
        return text  # no opening envelope found — leave untouched
    out = _ENVELOPE_CLOSE.sub("", out, count=1)
    return out
