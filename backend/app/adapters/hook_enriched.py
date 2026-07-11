"""HookEnrichedAdapter — Step 10. Wraps another DataSource (KanbanAdapter in
practice) and layers `fine_state`/`activity` onto each Agent using hook
telemetry ingested via POST /ingest (hermesboard-sensor, Step 9).

This is the "new adapter behind the SAME DataSource interface" PLAN.md asks
for: composition over inheritance so it works with any wrapped source, adds
exactly two keys per agent, and changes nothing else — get_tasks/subscribe/act
and the SSE pull methods all delegate straight through. If a profile never
sent hook telemetry (plugin not installed for that profile's Hermes home —
see DISCOVERY.md's per-profile install gotcha), `fine_state` falls back to the
wrapped source's own coarse `state` with no `activity` label. The UI never
breaks either way.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from app.datasource import Agent, DataSource, Event, EventHandler, Task, Unsubscribe
from app.hook_store import HookEventStore
from app.state_engine import (
    APPROVAL_TIMEOUT_SECONDS,
    PendingApproval,
    SubagentInfo,
    approval_visible,
    merge_fine_state,
    subagent_visible,
)

log = logging.getLogger(__name__)

# Step 12: the only two choices the control surface offers — deliberately no
# "session"/"always" from the UI (those mutate Hermes's persistent security
# config; a click here should never silently widen an allowlist). "Never
# auto-approve" — nothing in this adapter ever calls set_decision() itself.
_APPROVAL_CHOICES = {"approve": "once", "deny": "deny"}


class HookEnrichedAdapter(DataSource):
    def __init__(self, inner: DataSource, hook_store: HookEventStore) -> None:
        self.inner = inner
        self.hook_store = hook_store
        # Step 11: best-effort GET /api/sessions title lookup, cached per
        # delegation episode (profile -> (started_at, resolved title)) so
        # repeat calls during the same episode (this get_agents() poll, the
        # SSE loop's per-tick re-check) never refetch the dashboard twice.
        self._session_title_cache: dict[str, tuple[float, str | None]] = {}

    async def get_agents(self) -> list[Agent]:
        agents = await self.inner.get_agents()
        now = time.time()
        for agent in agents:
            profile = str(agent.get("id"))
            activity = self.hook_store.activity_for(profile)
            fine_state, label = merge_fine_state(str(agent.get("state", "idle")), activity, now)
            agent["fine_state"] = fine_state
            agent["activity"] = label
            sub = self.hook_store.subagent_for(profile)
            agent["subagent"] = (
                await self.subagent_view(profile, sub) if sub is not None and subagent_visible(sub, now) else None
            )
            appr = self.hook_store.approval_for(profile)
            agent["approval"] = (
                self.approval_view(appr) if appr is not None and approval_visible(appr, now) else None
            )
        return agents

    def approval_view(self, appr: PendingApproval) -> dict[str, Any]:
        """Wire shape for one profile's pending dangerous-command approval
        (Step 12): hook-derived pattern/description plus an approximate
        expiry the UI uses to grey out stale approve/deny buttons."""
        return {
            "pattern_key": appr.pattern_key,
            "description": appr.description,
            "requested_at": appr.requested_at,
            "expires_at": appr.requested_at + APPROVAL_TIMEOUT_SECONDS,
            "resolved": appr.resolved,
            "choice": appr.choice,
            "resolved_at": appr.resolved_at,
        }

    async def subagent_view(self, profile: str, sub: SubagentInfo) -> dict[str, Any]:
        """Wire shape for one profile's delegated sub-agent (Step 11):
        hook-derived role/status/timing, plus a best-effort GET /api/sessions
        title/preview layered on top — PLAN.md asks for both signals. Cached
        per delegation episode (keyed by started_at) so this never hits the
        dashboard more than once per episode even though both get_agents() and
        the /events SSE loop call it."""
        cached = self._session_title_cache.get(profile)
        if cached is not None and cached[0] == sub.started_at:
            title = cached[1]
        else:
            title = await self._lookup_child_title(profile)
            self._session_title_cache[profile] = (sub.started_at, title)
        return {
            "role": sub.role,
            "status": sub.status,
            "active": sub.ended_at is None,
            "started_at": sub.started_at,
            "ended_at": sub.ended_at,
            "title": title,
        }

    async def _lookup_child_title(self, profile: str) -> str | None:
        """Best-effort only: duck-types `find_active_child_session` on the
        wrapped source (KanbanAdapter has it; SyntheticAdapter doesn't and is
        silently skipped, same pattern as the optional /workers/active
        enrichment in Step 2). Never raises — a lookup failure just means the
        sprite falls back to the bare hook-derived role."""
        lookup = getattr(self.inner, "find_active_child_session", None)
        activity = self.hook_store.activity_for(profile)
        if lookup is None or activity is None or not activity.session_id:
            return None
        try:
            row = await lookup(activity.session_id)
        except Exception:
            log.debug("subagent session lookup failed for %s", profile, exc_info=True)
            return None
        if not row:
            return None
        return row.get("title") or row.get("preview") or None

    async def get_tasks(self) -> list[Task]:
        return await self.inner.get_tasks()

    async def subscribe(self, on_event: EventHandler) -> Unsubscribe:
        return await self.inner.subscribe(on_event)

    async def act(
        self, agent_id: str, action: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        # Step 12: approve/deny is Hermes-process-level state (a pending
        # dangerous-command wait inside a worker's own memory), not a Kanban
        # board verb — handled here, not delegated to the wrapped source.
        if action in _APPROVAL_CHOICES:
            appr = self.hook_store.approval_for(agent_id)
            if appr is None or appr.resolved:
                return {"ok": False, "detail": f"no pending approval for {agent_id!r}"}
            self.hook_store.set_decision(agent_id, _APPROVAL_CHOICES[action])
            return {"ok": True, "via": "hook", "choice": _APPROVAL_CHOICES[action]}
        return await self.inner.act(agent_id, action, payload)

    # -- SSE pull API: delegate so /events works unchanged (mirrors how
    # SyntheticAdapter duck-types these for the same route, Step 7). --
    def current_max_event_id(self) -> int:
        return self.inner.current_max_event_id()  # type: ignore[attr-defined]

    def fetch_events_after(self, cursor: int, limit: int = 500) -> list[Event]:
        return self.inner.fetch_events_after(cursor, limit)  # type: ignore[attr-defined]

    async def get_task_detail(self, task_id: str) -> dict[str, Any] | None:
        return await self.inner.get_task_detail(task_id)  # type: ignore[attr-defined]

    # -- live worker activity (DISCOVERY.md spike) --------------------------
    async def get_worker_activity(self, agent_id: str) -> dict[str, Any]:
        """Try the wrapped source's primary (state.db messages) read first;
        only if THAT can't find anything, layer the coarse hook/ingest
        activity this adapter already tracks — the fallback the spike called
        for, and the one piece of logic that has to live here rather than in
        KanbanAdapter, since only this layer holds `hook_store`."""
        primary = await self.inner.get_worker_activity(agent_id)  # type: ignore[attr-defined]
        if primary.get("available"):
            return primary
        fallback = self._coarse_activity_fallback(agent_id)
        return fallback if fallback["available"] else primary

    def _coarse_activity_fallback(self, profile: str) -> dict[str, Any]:
        """Built from the same `/ingest` envelopes already folded into
        `hook_store` for the fine_state/activity label (Step 10) — tool NAMES
        and hook markers only, never message text (the envelope carries none,
        confirmed by reading the sensor plugin source — DISCOVERY.md)."""
        items: list[dict[str, Any]] = []
        for env in self.hook_store.recent_for(profile):
            tool_name = env.get("tool_name")
            hook = env.get("hook")
            if hook == "pre_tool_call" and tool_name:
                items.append({"ts": env.get("ts"), "kind": "tool_call", "label": tool_name, "text": None})
            elif hook == "post_tool_call" and tool_name:
                items.append({"ts": env.get("ts"), "kind": "tool_result", "label": tool_name, "text": None})
        items = items[-20:]
        return {
            "available": bool(items),
            "updatedAt": items[-1]["ts"] if items else None,
            "items": items,
        }
