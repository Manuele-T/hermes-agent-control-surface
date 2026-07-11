"""Step 10 — unit tests for the pure state-derivation engine.

`researcher_task_t_dfaa4d52.jsonl` and `writer_task_t_029dcf76.jsonl` are REAL
hook envelopes captured from POST /ingest during two live Kanban worker runs
against this machine's own Hermes install (dispatched via `hermes kanban
dispatch`, hermesboard-sensor enabled per-profile — see DISCOVERY.md Step 8/9
and the Step 10 notes). They are not hand-written.

The approval and subagent_stop cases are NOT captured live (forcing a real
dangerous-command approval prompt and a real delegate_task round-trip just for
a fixture wasn't worth the runtime cost) — those are synthesized from the
exact kwarg shapes confirmed by reading tools/approval.py (`pattern_key`,
`session_key`) and hermes_cli/hooks.py's `_DEFAULT_PAYLOADS` (`child_role`,
`child_status`), so the shape is still grounded in real source, just not a
live capture.
"""
from __future__ import annotations

import json
from pathlib import Path

from app.state_engine import (
    AgentActivity,
    PendingApproval,
    SubagentInfo,
    approval_visible,
    merge_fine_state,
    reduce_activity,
    reduce_approval,
    reduce_subagent,
    subagent_visible,
    tool_family,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> list[dict]:
    lines = (FIXTURES / name).read_text().splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _replay(events: list[dict]) -> list[AgentActivity]:
    """Run every event through the reducer, returning the state after each."""
    state = AgentActivity()
    trace = []
    for ev in events:
        state = reduce_activity(state, ev)
        trace.append(state)
    return trace


# -- tool_family --------------------------------------------------------

def test_tool_family_known_names():
    assert tool_family("terminal") == "coding"
    assert tool_family("execute_code") == "coding"
    assert tool_family("write_file") == "writing"
    assert tool_family("patch") == "writing"
    assert tool_family("web_search") == "researching"
    assert tool_family("browser_navigate") == "researching"
    assert tool_family("read_file") == "reading"
    assert tool_family("search_files") == "reading"


def test_tool_family_unknown_falls_back_to_acting():
    assert tool_family("kanban_complete") == "acting"
    assert tool_family("delegate_task") == "acting"
    assert tool_family(None) == "acting"
    assert tool_family("some_tool_that_does_not_exist_yet") == "acting"


# -- reduce_activity: recorded sequences ---------------------------------

def test_recorded_researcher_task_thinking_then_tool_families():
    events = _load("researcher_task_t_dfaa4d52.jsonl")
    trace = _replay(events)

    # event 0: pre_llm_call -> thinking
    assert trace[0].fine_state == "thinking"

    # event 1: pre_tool_call kanban_show -> working/acting (no family match)
    assert (trace[1].fine_state, trace[1].activity) == ("working", "acting")
    # event 2: post_tool_call kanban_show -> idle (between calls)
    assert trace[2].fine_state == "idle"

    # event 3: pre_tool_call terminal -> working/coding
    assert (trace[3].fine_state, trace[3].activity) == ("working", "coding")
    assert trace[4].fine_state == "idle"  # post_tool_call terminal

    # events 5-8: two overlapping search_files pre/post calls (real overlap —
    # the second pre_tool_call fired before the first post_tool_call). The
    # reducer has no concept of concurrent calls (single last-writer-wins
    # state per agent, by design — see module docstring) so it should just
    # track "working/reading" through the whole overlap without erroring.
    assert (trace[5].fine_state, trace[5].activity) == ("working", "reading")
    assert (trace[6].fine_state, trace[6].activity) == ("working", "reading")
    assert trace[7].fine_state == "idle"
    assert trace[8].fine_state == "idle"

    # events 9-12: two sequential read_file calls -> working/reading each time
    assert (trace[9].fine_state, trace[9].activity) == ("working", "reading")
    assert trace[10].fine_state == "idle"
    assert (trace[11].fine_state, trace[11].activity) == ("working", "reading")
    assert trace[12].fine_state == "idle"

    # event 13: write_file -> working/writing
    assert (trace[13].fine_state, trace[13].activity) == ("working", "writing")
    assert trace[14].fine_state == "idle"

    # event 15: another read_file -> working/reading
    assert (trace[15].fine_state, trace[15].activity) == ("working", "reading")
    assert trace[16].fine_state == "idle"

    # event 17: kanban_complete -> working/acting (unmapped tool)
    assert (trace[17].fine_state, trace[17].activity) == ("working", "acting")
    assert trace[18].fine_state == "idle"

    # event 19: post_llm_call -> idle (turn over)
    assert trace[19].fine_state == "idle"


def test_recorded_writer_task_no_approval_needed():
    events = _load("writer_task_t_029dcf76.jsonl")
    trace = _replay(events)
    assert trace[0].fine_state == "thinking"
    # kanban_show, terminal, kanban_complete — all real pre/post pairs land on
    # working with a family, then idle, exactly like the researcher fixture.
    assert (trace[1].fine_state, trace[1].activity) == ("working", "acting")
    assert (trace[3].fine_state, trace[3].activity) == ("working", "coding")
    assert (trace[5].fine_state, trace[5].activity) == ("working", "acting")
    assert trace[-1].fine_state == "idle"  # final post_llm_call


# -- reduce_activity: synthetic edge cases (shape-matched to real kwargs) --

def test_pre_approval_request_sets_awaiting_approval():
    # Kwargs confirmed against tools/approval.py's _fire_approval_hook call
    # site: pattern_key/session_key, NOT session_id/task_id (DISCOVERY.md ss8).
    ev = {
        "hook": "pre_approval_request",
        "task_id": None,
        "session_id": None,
        "profile": "writer",
        "ts": 100.0,
        "pattern_key": "rm -rf *",
        "session_key": "sess-123",
    }
    state = reduce_activity(AgentActivity(fine_state="thinking", updated_at=99.0), ev)
    assert state.fine_state == "awaiting_approval"
    assert state.activity is None
    assert state.updated_at == 100.0


def test_subagent_stop_returns_to_idle():
    # Kwargs confirmed against hermes_cli/hooks.py _DEFAULT_PAYLOADS.
    ev = {
        "hook": "subagent_stop",
        "task_id": None,
        "session_id": None,
        "profile": "researcher",
        "ts": 200.0,
        "parent_session_id": "parent-sess",
        "child_role": "researcher-sub",
        "child_status": "completed",
    }
    state = reduce_activity(AgentActivity(fine_state="working", activity="acting", updated_at=199.0), ev)
    assert state.fine_state == "idle"
    assert state.activity is None


def test_unknown_hook_name_passes_through_unchanged():
    prev = AgentActivity(fine_state="working", activity="coding", updated_at=50.0)
    ev = {"hook": "on_session_start", "profile": "writer", "ts": 51.0}
    assert reduce_activity(prev, ev) == prev


def test_reduce_activity_is_pure_same_input_same_output():
    ev = {"hook": "pre_tool_call", "tool_name": "web_search", "profile": "researcher", "ts": 10.0}
    prev = AgentActivity()
    assert reduce_activity(prev, ev) == reduce_activity(prev, ev)


# -- merge_fine_state -----------------------------------------------------

def test_merge_non_working_coarse_state_always_wins():
    activity = AgentActivity(fine_state="thinking", updated_at=1000.0)
    for coarse in ("idle", "blocked", "done", "error"):
        assert merge_fine_state(coarse, activity, now=1000.0) == (coarse, None)


def test_merge_working_uses_fresh_hook_activity():
    activity = AgentActivity(fine_state="working", activity="coding", updated_at=1000.0)
    assert merge_fine_state("working", activity, now=1005.0) == ("working", "coding")


def test_merge_working_no_hook_data_falls_back_generic():
    assert merge_fine_state("working", None, now=1000.0) == ("working", None)


def test_merge_working_stale_hook_data_falls_back_generic():
    from app.state_engine import STALE_AFTER_SECONDS

    activity = AgentActivity(fine_state="thinking", updated_at=1000.0)
    now = 1000.0 + STALE_AFTER_SECONDS + 1
    # A crash mid-turn: no more hooks ever arrive. Past the staleness window
    # the "thinking" label must not freeze forever.
    assert merge_fine_state("working", activity, now=now) == ("working", None)


def test_merge_working_hook_says_idle_between_calls():
    # Kanban still shows a claimed/running worker, but the hook stream is
    # between calls (post_tool_call/post_llm_call already reduced to idle).
    activity = AgentActivity(fine_state="idle", updated_at=1000.0)
    assert merge_fine_state("working", activity, now=1001.0) == ("working", None)


def test_merge_working_awaiting_approval_surfaces():
    activity = AgentActivity(fine_state="awaiting_approval", updated_at=1000.0)
    assert merge_fine_state("working", activity, now=1001.0) == ("awaiting_approval", None)


# -- reduce_subagent (Step 11) --------------------------------------------
# Kwargs shape-matched to the same real hook sources as the subagent_stop
# tests above (hermes_cli/hooks.py _DEFAULT_PAYLOADS): parent_session_id,
# child_role, child_status. pre/post_tool_call kwargs confirmed against
# model_tools.py (tool_name) — same as the tool_family tests above.


def test_delegate_task_pre_tool_call_starts_active_episode():
    ev = {"hook": "pre_tool_call", "tool_name": "delegate_task", "profile": "researcher", "ts": 100.0}
    info = reduce_subagent(None, ev)
    assert info == SubagentInfo(role=None, status="active", started_at=100.0, ended_at=None)


def test_unrelated_pre_tool_call_does_not_start_an_episode():
    ev = {"hook": "pre_tool_call", "tool_name": "read_file", "profile": "researcher", "ts": 100.0}
    assert reduce_subagent(None, ev) is None


def test_subagent_stop_resolves_role_and_keeps_start_time():
    prev = SubagentInfo(role=None, status="active", started_at=100.0, ended_at=None)
    ev = {
        "hook": "subagent_stop",
        "profile": "researcher",
        "ts": 130.0,
        "parent_session_id": "parent-sess",
        "child_role": "researcher-sub",
        "child_status": "completed",
    }
    info = reduce_subagent(prev, ev)
    assert info == SubagentInfo(role="researcher-sub", status="completed", started_at=100.0, ended_at=130.0)


def test_subagent_stop_with_no_prior_pre_tool_call_starts_fresh():
    # Hook ordering/miss: subagent_stop arrives with no matching pre_tool_call
    # seen — the episode is treated as already-finished, started_at == ts.
    ev = {
        "hook": "subagent_stop",
        "profile": "writer",
        "ts": 200.0,
        "child_role": "writer-sub",
        "child_status": "completed",
    }
    info = reduce_subagent(None, ev)
    assert info == SubagentInfo(role="writer-sub", status="completed", started_at=200.0, ended_at=200.0)


def test_post_tool_call_closes_episode_if_subagent_stop_never_arrived():
    prev = SubagentInfo(role=None, status="active", started_at=100.0, ended_at=None)
    ev = {"hook": "post_tool_call", "tool_name": "delegate_task", "profile": "researcher", "ts": 140.0}
    info = reduce_subagent(prev, ev)
    assert info == SubagentInfo(role=None, status="active", started_at=100.0, ended_at=140.0)


def test_post_tool_call_does_not_reopen_an_already_closed_episode():
    prev = SubagentInfo(role="researcher-sub", status="completed", started_at=100.0, ended_at=130.0)
    ev = {"hook": "post_tool_call", "tool_name": "delegate_task", "profile": "researcher", "ts": 140.0}
    assert reduce_subagent(prev, ev) == prev


def test_unrelated_hook_passes_subagent_through_unchanged():
    prev = SubagentInfo(role=None, status="active", started_at=100.0, ended_at=None)
    ev = {"hook": "pre_llm_call", "profile": "researcher", "ts": 110.0}
    assert reduce_subagent(prev, ev) == prev
    assert reduce_subagent(None, ev) is None


# -- subagent_visible -------------------------------------------------------


def test_subagent_visible_none_is_never_visible():
    assert subagent_visible(None, now=1000.0) is False


def test_subagent_visible_active_within_stale_budget():
    info = SubagentInfo(role=None, status="active", started_at=1000.0, ended_at=None)
    assert subagent_visible(info, now=1050.0) is True


def test_subagent_visible_active_past_stale_budget_hides():
    from app.state_engine import STALE_AFTER_SECONDS

    info = SubagentInfo(role=None, status="active", started_at=1000.0, ended_at=None)
    assert subagent_visible(info, now=1000.0 + STALE_AFTER_SECONDS + 1) is False


def test_subagent_visible_finished_within_flash_window():
    info = SubagentInfo(role="researcher-sub", status="completed", started_at=1000.0, ended_at=1030.0)
    assert subagent_visible(info, now=1035.0) is True


def test_subagent_visible_finished_past_flash_window_hides():
    from app.state_engine import SUBAGENT_VISIBLE_AFTER_STOP_SECONDS

    info = SubagentInfo(role="researcher-sub", status="completed", started_at=1000.0, ended_at=1030.0)
    now = 1030.0 + SUBAGENT_VISIBLE_AFTER_STOP_SECONDS + 1
    assert subagent_visible(info, now=now) is False


# -- reduce_approval (Step 12) ---------------------------------------------
# Kwargs shape-matched to the real pre_approval_request/post_approval_response
# hooks confirmed by reading tools/approval.py directly (see DISCOVERY.md
# Step 12 notes): pattern_key, description, session_key for the request;
# same plus choice ("once"|"session"|"always"|"deny"|"timeout") for the response.


def test_pre_approval_request_opens_pending_approval():
    ev = {
        "hook": "pre_approval_request",
        "profile": "researcher",
        "ts": 100.0,
        "pattern_key": "recursive delete",
        "description": "rm -rf on a broad path",
        "session_key": "default",
    }
    info = reduce_approval(None, ev)
    assert info == PendingApproval(
        pattern_key="recursive delete", description="rm -rf on a broad path", requested_at=100.0
    )


def test_post_approval_response_resolves_pending_approval():
    prev = PendingApproval(pattern_key="recursive delete", description="rm -rf", requested_at=100.0)
    ev = {
        "hook": "post_approval_response",
        "profile": "researcher",
        "ts": 105.0,
        "pattern_key": "recursive delete",
        "choice": "once",
    }
    info = reduce_approval(prev, ev)
    assert info == PendingApproval(
        pattern_key="recursive delete",
        description="rm -rf",
        requested_at=100.0,
        resolved=True,
        choice="once",
        resolved_at=105.0,
    )


def test_post_approval_response_with_nothing_pending_returns_none():
    ev = {"hook": "post_approval_response", "profile": "researcher", "ts": 105.0, "choice": "deny"}
    assert reduce_approval(None, ev) is None


def test_unrelated_hook_passes_approval_through_unchanged():
    prev = PendingApproval(pattern_key="recursive delete", requested_at=100.0)
    ev = {"hook": "pre_tool_call", "tool_name": "terminal", "profile": "researcher", "ts": 101.0}
    assert reduce_approval(prev, ev) == prev
    assert reduce_approval(None, ev) is None


# -- approval_visible --------------------------------------------------------


def test_approval_visible_none_is_never_visible():
    assert approval_visible(None, now=1000.0) is False


def test_approval_visible_unresolved_within_timeout_budget():
    info = PendingApproval(pattern_key="k", requested_at=1000.0)
    assert approval_visible(info, now=1050.0) is True


def test_approval_visible_unresolved_past_timeout_budget_hides():
    from app.state_engine import APPROVAL_TIMEOUT_SECONDS

    info = PendingApproval(pattern_key="k", requested_at=1000.0)
    assert approval_visible(info, now=1000.0 + APPROVAL_TIMEOUT_SECONDS + 1) is False


def test_approval_visible_resolved_within_flash_window():
    info = PendingApproval(pattern_key="k", requested_at=1000.0, resolved=True, choice="once", resolved_at=1005.0)
    assert approval_visible(info, now=1010.0) is True


def test_approval_visible_resolved_past_flash_window_hides():
    from app.state_engine import APPROVAL_RESOLVED_FLASH_SECONDS

    info = PendingApproval(pattern_key="k", requested_at=1000.0, resolved=True, choice="deny", resolved_at=1005.0)
    now = 1005.0 + APPROVAL_RESOLVED_FLASH_SECONDS + 1
    assert approval_visible(info, now=now) is False
