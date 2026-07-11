"""SessionsAdapter — Step 13. Wraps another DataSource and appends
INDEPENDENT profile characters: Hermes profiles with an active session that
ISN'T a Kanban-dispatched worker (already represented via task_runs/the
wrapped source). Sourced from `GET /api/profiles/sessions?profile=all`.

Ground truth (DISCOVERY.md ss13, corrects PLAN's assumption): `GET /api/sessions`
alone only ever queries the DEFAULT profile's own session store and never
carries a `profile` field unless `?profile=` is passed. `GET /api/profiles/sessions`
is the real cross-profile endpoint — it opens every profile's `state.db`
directly (read-only) and tags each row with its owning `profile`. There is also
no dedicated "kanban worker" `source` value to filter on — kanban workers and
plain interactive CLI sessions both report `source="cli"` — so the real signal
is `cwd`: every kanban-dispatched worker's session cwd is inside
`~/.hermes/kanban/workspaces/<task_id>`.

Point-in-time only: there is no task_events-equivalent stream for these
sessions, so (unlike the Kanban fleet) an independent character's state is a
snapshot as of the last `get_agents()` call, not continuously live — stated
plainly as a v1 limitation (see DISCOVERY.md), not hidden.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from app.datasource import Agent, DataSource, Event, EventHandler, Task, Unsubscribe

log = logging.getLogger(__name__)

# Step 14: cost/token HUD. Best-effort, same "no token/network error -> empty"
# contract as independent_agents(). A short TTL cache means every SSE client's
# periodic poll (main.py) doesn't each trigger their own round trip to the
# dashboard when several browser tabs are open.
_COST_CACHE_TTL_SECONDS = 5.0
_UNAVAILABLE_COSTS: dict[str, Any] = {
    "available": False,
    "total_cost_usd": 0.0,
    "input_tokens": 0,
    "output_tokens": 0,
    "session_count": 0,
    "active_session_count": 0,
}


class SessionsAdapter(DataSource):
    def __init__(
        self,
        inner: DataSource,
        *,
        dashboard_base_url: str,
        session_token: str | None,
        kanban_workspaces_prefix: str,
    ) -> None:
        self.inner = inner
        self.dashboard_base_url = dashboard_base_url.rstrip("/")
        self.session_token = session_token
        self.kanban_workspaces_prefix = kanban_workspaces_prefix
        self._cost_cache: tuple[float, dict[str, Any]] | None = None

    async def get_agents(self) -> list[Agent]:
        agents = await self.inner.get_agents()
        known_profiles = {str(a.get("id")) for a in agents}
        agents.extend(await self.independent_agents(known_profiles))
        return agents

    async def independent_agents(self, known_profiles: set[str]) -> list[Agent]:
        """Best-effort: no token/dashboard/network error all just mean no
        independent agents are shown (the Kanban fleet is unaffected) — same
        optional-enrichment treatment as `/workers/active` (Step 2) and
        `find_active_child_session` (Step 11)."""
        if not self.session_token:
            return []
        url = f"{self.dashboard_base_url}/api/profiles/sessions"
        headers = {"Authorization": f"Bearer {self.session_token}"}
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(
                    url, headers=headers, params={"profile": "all", "limit": 200, "order": "recent"}
                )
            if resp.status_code >= 400:
                return []
            rows = resp.json().get("sessions", [])
        except httpx.HTTPError:
            log.info("independent-session lookup failed — dashboard unreachable at %s", url)
            return []

        seen: set[str] = set()
        result: list[Agent] = []
        for row in rows:
            if not row.get("is_active"):
                continue
            profile = row.get("profile")
            if not profile or profile in known_profiles or profile in seen:
                continue
            cwd = row.get("cwd") or ""
            if cwd.startswith(self.kanban_workspaces_prefix):
                # A Kanban-dispatched worker's own session — already
                # represented via task_runs; `source` can't distinguish it
                # (both report "cli"), so `cwd` is the real signal.
                continue
            seen.add(profile)
            result.append(
                {
                    "id": profile,
                    "name": profile,
                    "state": "working",  # active by construction (is_active filter above)
                    "current_task_id": None,
                    "run_id": None,  # no task_runs row backs an independent session
                    "independent": True,
                    "session_id": row.get("id"),
                    "source": row.get("source"),
                    "cwd": cwd or None,
                    "last_active": row.get("last_active"),
                    "preview": row.get("preview"),
                    "estimated_cost_usd": row.get("estimated_cost_usd"),
                }
            )
        return result

    async def cost_summary(self) -> dict[str, Any]:
        """Step 14: GLOBAL cost/token HUD data, aggregated across every
        profile's sessions (not just the independent ones surfaced as
        characters). Real fields confirmed live against this endpoint
        (DISCOVERY.md): estimated_cost_usd, input_tokens, output_tokens,
        is_active per row — `profile_totals` on the response is a per-profile
        session COUNT, not a cost total, so the aggregate is computed here.

        `limit=500, order=recent` mirrors independent_agents()'s own
        best-effort windowing: a recent-session aggregate, not a guaranteed
        lifetime total if a profile has accumulated more than that."""
        now = time.monotonic()
        if self._cost_cache and now - self._cost_cache[0] < _COST_CACHE_TTL_SECONDS:
            return self._cost_cache[1]
        if not self.session_token:
            return _UNAVAILABLE_COSTS
        url = f"{self.dashboard_base_url}/api/profiles/sessions"
        headers = {"Authorization": f"Bearer {self.session_token}"}
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(
                    url, headers=headers, params={"profile": "all", "limit": 500, "order": "recent"}
                )
            if resp.status_code >= 400:
                return _UNAVAILABLE_COSTS
            rows = resp.json().get("sessions", [])
        except httpx.HTTPError:
            log.info("cost summary lookup failed — dashboard unreachable at %s", url)
            return _UNAVAILABLE_COSTS

        total_cost = 0.0
        input_tokens = 0
        output_tokens = 0
        active = 0
        for row in rows:
            cost = row.get("estimated_cost_usd")
            if cost is not None:
                total_cost += cost
            input_tokens += row.get("input_tokens") or 0
            output_tokens += row.get("output_tokens") or 0
            if row.get("is_active"):
                active += 1
        summary = {
            "available": True,
            "total_cost_usd": round(total_cost, 4),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "session_count": len(rows),
            "active_session_count": active,
        }
        self._cost_cache = (now, summary)
        return summary

    async def get_tasks(self) -> list[Task]:
        return await self.inner.get_tasks()

    async def subscribe(self, on_event: EventHandler) -> Unsubscribe:
        return await self.inner.subscribe(on_event)

    async def act(
        self, agent_id: str, action: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return await self.inner.act(agent_id, action, payload)

    # -- delegate everything the SSE route / Step 10-12 enrichment need ----
    def current_max_event_id(self) -> int:
        return self.inner.current_max_event_id()  # type: ignore[attr-defined]

    def fetch_events_after(self, cursor: int, limit: int = 500) -> list[Event]:
        return self.inner.fetch_events_after(cursor, limit)  # type: ignore[attr-defined]

    async def get_task_detail(self, task_id: str) -> dict[str, Any] | None:
        return await self.inner.get_task_detail(task_id)  # type: ignore[attr-defined]

    async def get_worker_activity(self, agent_id: str) -> dict[str, Any]:
        return await self.inner.get_worker_activity(agent_id)  # type: ignore[attr-defined]

    async def subagent_view(self, profile: str, sub: Any) -> dict[str, Any]:
        return await self.inner.subagent_view(profile, sub)  # type: ignore[attr-defined]

    def approval_view(self, appr: Any) -> dict[str, Any]:
        return self.inner.approval_view(appr)  # type: ignore[attr-defined]
