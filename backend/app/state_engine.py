"""Step 10 — pure state-derivation engine.

Maps a stream of hermesboard-sensor hook envelopes (Step 9,
``~/.hermes/plugins/hermesboard-sensor``) onto a fine-grained per-agent
activity: ``thinking`` (LLM call in flight), ``working`` sub-labelled by tool
family, ``awaiting_approval``, or ``idle``. Kept pure — no clock reads, no I/O
— so it can be unit-tested against recorded event sequences without a running
Hermes. Impure concerns (wall-clock staleness, merging with the Kanban coarse
state) live in ``merge_fine_state`` and the caller, not in ``reduce_activity``.

Hook envelope shape (from the plugin): {hook, task_id, session_id, profile,
ts, tool_name?, pattern_key?, session_key?, parent_session_id?, child_role?,
child_status?}. ``task_id`` here is an internal session/turn id, NOT a Kanban
board id (DISCOVERY.md ss8) — this module never reads it; correlation to a
Kanban agent happens by ``profile`` upstream, in HookEventStore.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

# Real tool names, grepped from tools/*.py `name="..."` registrations on the
# installed Hermes (not guessed) and grouped into families per PLAN.md Step 10.
# PLAN's parenthetical reads "coding=bash/edit/write" but then separately
# lists "writing" as its own fine state — read literally that's contradictory
# (write tools can't be in two buckets), so this module resolves it as: coding
# = running/executing tools, writing = authoring/editing content tools. Both
# still roll up under the single "working" fine_state, just different
# `activity` sub-labels — the ambiguity doesn't affect fine_state itself.
TOOL_FAMILIES: dict[str, str] = {
    # coding — executing something
    "terminal": "coding",
    "execute_code": "coding",
    "read_terminal": "coding",
    "close_terminal": "coding",
    "process": "coding",
    # writing — authoring/editing content or files
    "write_file": "writing",
    "patch": "writing",
    # researching — the web/browser/search surface
    "web_search": "researching",
    "web_extract": "researching",
    "x_search": "researching",
    "session_search": "researching",
    "computer_use": "researching",
    "browser_navigate": "researching",
    "browser_click": "researching",
    "browser_type": "researching",
    "browser_press": "researching",
    "browser_scroll": "researching",
    "browser_snapshot": "researching",
    "browser_get_images": "researching",
    "browser_vision": "researching",
    "browser_back": "researching",
    "browser_console": "researching",
    "browser_dialog": "researching",
    "browser_cdp": "researching",
    # reading — inspecting existing files
    "read_file": "reading",
    "search_files": "reading",
}
_DEFAULT_FAMILY = "acting"  # kanban_*, delegate_task, memory, todo, ... etc.

# Hook-derived activity is discarded once it's this old — past this point a
# crash mid-turn (no more hooks ever) must not freeze the display on
# "thinking"/"working" forever. Tunable (PLAN.md Step 10 pitfall).
STALE_AFTER_SECONDS = 120.0


def tool_family(tool_name: str | None) -> str:
    return TOOL_FAMILIES.get(tool_name or "", _DEFAULT_FAMILY)


@dataclass(frozen=True)
class AgentActivity:
    """Immutable snapshot of one agent's hook-derived fine-grained activity."""

    fine_state: str = "idle"  # thinking | working | awaiting_approval | idle
    activity: str | None = None  # tool family, only meaningful when fine_state=="working"
    updated_at: float = 0.0  # ts of the event that produced this state
    session_id: str | None = None


def reduce_activity(prev: AgentActivity, event: dict[str, Any]) -> AgentActivity:
    """(state, event) -> state. Pure: same inputs always give the same output.

    `event` is one normalized hook envelope. Unknown hook names pass through
    unchanged (forward-compatible with hooks this engine doesn't model yet).
    """
    hook = event.get("hook")
    ts = event.get("ts", prev.updated_at)
    session_id = event.get("session_id") or prev.session_id

    if hook == "pre_llm_call":
        return replace(prev, fine_state="thinking", activity=None, updated_at=ts, session_id=session_id)
    if hook == "pre_tool_call":
        family = tool_family(event.get("tool_name"))
        return replace(prev, fine_state="working", activity=family, updated_at=ts, session_id=session_id)
    if hook == "pre_approval_request":
        return replace(prev, fine_state="awaiting_approval", activity=None, updated_at=ts, session_id=session_id)
    if hook in ("post_llm_call", "post_tool_call", "subagent_stop"):
        # Turn boundary: the agent is between calls. The *next* pre_* event
        # (if any) will refine this further; until then the closest honest
        # label is idle, not a stale carry-over of the previous phase.
        return replace(prev, fine_state="idle", activity=None, updated_at=ts, session_id=session_id)
    return prev


@dataclass(frozen=True)
class SubagentInfo:
    """Immutable snapshot of one parent agent's most recently delegated
    sub-agent (Step 11). Depth-1 flat only, per PLAN.md — Hermes delegation
    has no nested sub-sub-agents by default, so a parent tracks at most one
    of these at a time (a new `delegate_task` call simply replaces it).

    There is no `subagent_start` hook (only `subagent_stop` — DISCOVERY.md
    ss8), so `pre_tool_call` with tool_name=="delegate_task" is the only
    available "a delegation just began" signal; `role`/`status` stay
    provisional (None / "active") until `subagent_stop` resolves them.
    """

    role: str | None = None
    status: str = "active"  # "active" while in flight, else the real child_status
    started_at: float = 0.0
    ended_at: float | None = None


# How long a finished delegation's sprite stays visible before it's swept —
# subagents are ephemeral summaries, not board rows (PLAN.md Step 11), so this
# is deliberately a brief flash, not a history log.
SUBAGENT_VISIBLE_AFTER_STOP_SECONDS = 8.0


def reduce_subagent(prev: SubagentInfo | None, event: dict[str, Any]) -> SubagentInfo | None:
    """(state, event) -> state for the delegated-subagent sprite. Pure, same
    shape as reduce_activity. Events that don't touch delegation return `prev`
    unchanged (including None, when this profile has never delegated)."""
    hook = event.get("hook")
    ts = event.get("ts", 0.0)
    if hook == "pre_tool_call" and event.get("tool_name") == "delegate_task":
        return SubagentInfo(role=None, status="active", started_at=ts, ended_at=None)
    if hook == "subagent_stop":
        # Keep the pre_tool_call's start time for an accurate duration when we
        # saw it; otherwise (hook ordering/miss) this stop begins a
        # brand-new, already-finished episode.
        started_at = prev.started_at if prev is not None and prev.ended_at is None else ts
        return SubagentInfo(
            role=event.get("child_role"),
            status=event.get("child_status") or "completed",
            started_at=started_at,
            ended_at=ts,
        )
    if hook == "post_tool_call" and event.get("tool_name") == "delegate_task":
        # Fallback closer in case subagent_stop never arrives — still ends the
        # episode so the sprite doesn't hang "active" forever.
        if prev is not None and prev.ended_at is None:
            return replace(prev, ended_at=ts)
        return prev
    return prev


def subagent_visible(info: SubagentInfo | None, now: float) -> bool:
    """Whether the sprite should still be shown. Mirrors the two failure modes
    a purely hook-driven signal has to guard against: a missed closing hook
    (active forever) and an old finished episode overstaying its ephemeral
    flash — both bounded by a wall-clock check, never by unbounded history."""
    if info is None:
        return False
    if info.ended_at is None:
        # Reuse the same staleness budget as the fine-state engine's crash
        # guard: if no closing hook ever arrives, don't believe "active" forever.
        return (now - info.started_at) <= STALE_AFTER_SECONDS
    return (now - info.ended_at) <= SUBAGENT_VISIBLE_AFTER_STOP_SECONDS


@dataclass(frozen=True)
class PendingApproval:
    """Immutable snapshot of one agent's currently pending dangerous-command
    approval request (Step 12), sourced from the `pre_approval_request` /
    `post_approval_response` hook pair (DISCOVERY.md: the only two approval
    lifecycle hooks Hermes fires; both observer-only, kwargs confirmed against
    `tools/approval.py`)."""

    pattern_key: str | None = None
    description: str | None = None
    requested_at: float = 0.0
    resolved: bool = False  # True once post_approval_response has fired
    choice: str | None = None  # "once"|"session"|"always"|"deny"|"timeout", set on resolution
    resolved_at: float | None = None


# Mirrors Hermes's own default gateway approval wait
# (`tools/approval.py`'s `_get_approval_config().get("gateway_timeout", 300)`)
# so the UI can show an expiry without reading the operator's config.yaml —
# approximate only if `approvals.gateway_timeout` has been customized there.
APPROVAL_TIMEOUT_SECONDS = 300.0
# Ephemeral flash after resolution, same idiom as SUBAGENT_VISIBLE_AFTER_STOP_SECONDS.
APPROVAL_RESOLVED_FLASH_SECONDS = 8.0


def reduce_approval(prev: PendingApproval | None, event: dict[str, Any]) -> PendingApproval | None:
    """(state, event) -> state for the pending-approval indicator. Pure, same
    shape as reduce_subagent/reduce_activity."""
    hook = event.get("hook")
    ts = event.get("ts", 0.0)
    if hook == "pre_approval_request":
        return PendingApproval(
            pattern_key=event.get("pattern_key"),
            description=event.get("description"),
            requested_at=ts,
        )
    if hook == "post_approval_response":
        if prev is None:
            return None  # nothing was tracked as pending; nothing to resolve
        return replace(prev, resolved=True, choice=event.get("choice"), resolved_at=ts)
    return prev


def approval_visible(info: PendingApproval | None, now: float) -> bool:
    """Whether the approval indicator should still be shown. An unresolved
    request is only believed up to Hermes's own timeout budget (past that the
    wait has certainly ended even if we never received post_approval_response
    — e.g. a missed hook); a resolved one flashes briefly then clears."""
    if info is None:
        return False
    if not info.resolved:
        return (now - info.requested_at) <= APPROVAL_TIMEOUT_SECONDS
    if info.resolved_at is None:
        return False
    return (now - info.resolved_at) <= APPROVAL_RESOLVED_FLASH_SECONDS


def merge_fine_state(
    coarse_state: str, activity: AgentActivity | None, now: float
) -> tuple[str, str | None]:
    """Combine the Kanban coarse `state` (authoritative for terminal states —
    done/blocked/error — since hooks never signal a crash directly) with the
    hook-derived `activity` (authoritative for the *working* phase only).

    Returns (fine_state, activity_label). `activity_label` is only non-None
    when fine_state is "working".
    """
    if coarse_state != "working":
        # Kanban's own transitions (task_events) always win outside the
        # working phase — a hook-derived "thinking" from a crashed run must
        # not outlive the run itself.
        return coarse_state, None
    if activity is None or (now - activity.updated_at) > STALE_AFTER_SECONDS:
        return "working", None
    if activity.fine_state == "idle":
        # Hooks say "between calls" but Kanban still shows a claimed/running
        # worker — still "working" overall, just no fine label yet.
        return "working", None
    return activity.fine_state, activity.activity
