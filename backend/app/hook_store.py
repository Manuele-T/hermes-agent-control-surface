"""In-memory store for hook telemetry POSTed to /ingest by the
hermesboard-sensor plugin (Step 9). Keyed by `profile`, not `task_id` — the
hook envelope's task_id is an internal session/turn id, never the Kanban
board id (DISCOVERY.md ss8/9), and `profile` matches the Kanban `assignee`
exactly (each profile runs as its own OS process per DISCOVERY ss8) so it's
the only reliable correlation key available without extra plumbing.

Thread-safe: /ingest is called from FastAPI's async request handling, but a
plain lock is simplest and this is a low-frequency, tiny-critical-section
path (one dict write per hook firing).
"""
from __future__ import annotations

import threading
from typing import Any

from app.state_engine import (
    AgentActivity,
    PendingApproval,
    SubagentInfo,
    reduce_activity,
    reduce_approval,
    reduce_subagent,
)

_MAX_RECENT_PER_PROFILE = 50  # ponytail: bounded debug/test history, not a hard requirement


class HookEventStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._activity: dict[str, AgentActivity] = {}
        # Step 11: at most one in-flight/most-recent delegated sub-agent per
        # profile (flat depth-1 — see state_engine.SubagentInfo).
        self._subagent: dict[str, SubagentInfo] = {}
        # Step 12: at most one pending dangerous-command approval per profile.
        self._approval: dict[str, PendingApproval] = {}
        # Step 12: a decision (from POST /act approve|deny) waiting to be
        # picked up by the hermesboard-sensor plugin's poller running INSIDE
        # the Hermes worker process — see main.py's GET /approvals/{profile}/poll.
        # One-shot: popped (cleared) the moment it's read.
        self._decision: dict[str, str] = {}
        self._recent: dict[str, list[dict[str, Any]]] = {}

    def ingest(self, envelope: dict[str, Any]) -> None:
        profile = envelope.get("profile")
        if not profile:
            return  # can't correlate to an agent without it
        with self._lock:
            prev = self._activity.get(profile, AgentActivity())
            self._activity[profile] = reduce_activity(prev, envelope)
            sub = reduce_subagent(self._subagent.get(profile), envelope)
            if sub is not None:
                self._subagent[profile] = sub
            appr = reduce_approval(self._approval.get(profile), envelope)
            if appr is not None:
                self._approval[profile] = appr
            if envelope.get("hook") == "post_approval_response":
                if appr is None:
                    # Resolved with nothing tracked as pending (e.g. backend
                    # restarted mid-wait) — nothing to show.
                    self._approval.pop(profile, None)
                # The wait is over one way or another; a decision we set but
                # the plugin never polled must not bleed into the next
                # approval for this profile.
                self._decision.pop(profile, None)
            bucket = self._recent.setdefault(profile, [])
            bucket.append(envelope)
            if len(bucket) > _MAX_RECENT_PER_PROFILE:
                del bucket[: len(bucket) - _MAX_RECENT_PER_PROFILE]

    def activity_for(self, profile: str) -> AgentActivity | None:
        with self._lock:
            return self._activity.get(profile)

    def snapshot(self) -> dict[str, AgentActivity]:
        """Copy of the current per-profile activity map, for the SSE loop to
        diff against on each tick."""
        with self._lock:
            return dict(self._activity)

    def subagent_for(self, profile: str) -> SubagentInfo | None:
        with self._lock:
            return self._subagent.get(profile)

    def subagent_snapshot(self) -> dict[str, SubagentInfo]:
        """Copy of the current per-profile subagent map, for the SSE loop to
        diff against on each tick (mirrors snapshot())."""
        with self._lock:
            return dict(self._subagent)

    def recent_for(self, profile: str) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._recent.get(profile, []))

    def approval_for(self, profile: str) -> PendingApproval | None:
        with self._lock:
            return self._approval.get(profile)

    def approval_snapshot(self) -> dict[str, PendingApproval]:
        """Copy of the current per-profile approval map, for the SSE loop to
        diff against on each tick (mirrors snapshot()/subagent_snapshot())."""
        with self._lock:
            return dict(self._approval)

    def set_decision(self, profile: str, choice: str) -> None:
        """Record a user's approve/deny click (POST /act), for the sensor
        plugin's poller (running inside the Hermes worker process) to pick up
        via GET /approvals/{profile}/poll."""
        with self._lock:
            self._decision[profile] = choice

    def pop_decision(self, profile: str) -> str | None:
        """One-shot read: returns and clears the pending decision, if any."""
        with self._lock:
            return self._decision.pop(profile, None)
