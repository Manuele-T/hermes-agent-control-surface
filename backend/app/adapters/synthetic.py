"""SyntheticAdapter — plausible agents/tasks/events on a loop, no real Hermes
attached. Implements the SAME DataSource interface as KanbanAdapter (Step 7),
which is what proves the abstraction: the UI and the SSE route don't know or
care which adapter is behind them.

act() always declines — this is what makes the public demo read-only. It still
goes through the real act() call so the SidePanel shows the same rejection UI
a real auth failure would (no separate "demo mode" branch needed in the UI).
"""
from __future__ import annotations

import asyncio
import itertools
import random
import time
from typing import Any

from app.datasource import Agent, DataSource, Event, EventHandler, Task, Unsubscribe
from app.redact import sanitize_text

_PROFILES = ["researcher", "writer", "reviewer", "coder"]
_TITLES = [
    "Research the competitive landscape for the Q3 launch",
    "Draft the v2 announcement post",
    "Review the auth refactor PR",
    "Summarize last week's support tickets",
    "Write integration tests for the new endpoint",
    "Investigate the flaky CI job",
    "Outline the onboarding email sequence",
]
_TICK_SECONDS = 2.0
# ponytail: unbounded in-memory growth on a long-lived public demo process —
# cap retained history. No real data integrity to preserve here.
_MAX_RETAINED_EVENTS = 500
_MAX_RETAINED_TASKS = 30

# Live-activity feed (DISCOVERY.md spike): a plausible, evolving
# "thinking -> tool call -> observation" rolling script for the public demo,
# same wire shape as the real KanbanAdapter's state.db-backed feed so the UI
# needs no adapter-specific branch. Fabricated text — nothing sensitive here —
# but still routed through sanitize_text() so the shape/behavior matches the
# real path exactly (truncation + a no-op scrub pass).
_ACTIVITY_MAX_ITEMS = 20
_ACTIVITY_SCRIPT: list[tuple[str, str | None, str | None]] = [
    ("assistant", None, "Let me start by getting a lay of the land here."),
    ("tool_call", "web_search", '{"query": "competitive landscape Q3 launch"}'),
    ("tool_result", "web_search", "Found 8 relevant results — skimming the top 3 for relevance."),
    ("assistant", None, "Good context. Let me also check what we already wrote on this."),
    ("tool_call", "file_read", '{"path": "docs/notes.md"}'),
    ("tool_result", "file_read", "## Notes\n- prior research from last quarter\n(212 lines total)"),
    ("assistant", None, "That confirms the direction. Drafting the summary now."),
    ("tool_call", "file_write", '{"path": "draft.md"}'),
    ("tool_result", "file_write", "wrote 640 bytes to draft.md"),
    ("assistant", None, "Reviewing the draft once more before wrapping up."),
]


class SyntheticAdapter(DataSource):
    def __init__(self) -> None:
        self._agents: dict[str, dict[str, Any]] = {p: self._fresh_agent(p) for p in _PROFILES}
        self._tasks: dict[str, dict[str, Any]] = {}
        self._events: list[Event] = []
        self._event_ids = itertools.count(1)
        self._task_ids = itertools.count(1)
        self._subscribers: list[EventHandler] = []
        self._started = False
        # Step 14: a plausible, ever-growing cost/token total so the demo's
        # global HUD visibly updates live, same spirit as the rest of this
        # adapter (real interface, synthesized numbers).
        self._cost_usd = 0.0
        self._input_tokens = 0
        self._output_tokens = 0
        # Live-activity feed: per-profile script cursor + accumulated items,
        # reset whenever a fresh task starts (mirrors the real feed only ever
        # reflecting the CURRENT running task, never a past one).
        self._activity_idx: dict[str, int] = {}
        self._activity_items: dict[str, list[dict[str, Any]]] = {}

    @staticmethod
    def _fresh_agent(profile: str) -> dict[str, Any]:
        return {
            "id": profile,
            "name": profile,
            "state": "idle",
            "current_task_id": None,
            "worker_pid": None,
            "last_heartbeat_at": None,
            "last_started_at": None,
            "last_ended_at": None,
            "last_outcome": None,
        }

    # -- simulation loop ----------------------------------------------
    def start(self) -> None:
        """Start the background tick loop. Idempotent; must be called from a
        running event loop (wired into the FastAPI startup event)."""
        if self._started:
            return
        self._started = True
        asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(_TICK_SECONDS)
            self._tick()

    def _tick(self) -> None:
        self._cost_usd += random.uniform(0.001, 0.012)
        self._input_tokens += random.randint(80, 450)
        self._output_tokens += random.randint(15, 140)
        for profile, agent in self._agents.items():
            state = agent["state"]
            if state == "idle" and random.random() < 0.15:
                self._spawn_task(profile)
            elif state == "working":
                if random.random() < 0.12:
                    self._finish_task(profile, blocked=random.random() < 0.25)
                else:
                    self._heartbeat(profile)
                    self._advance_activity(profile)
            elif state in ("done", "blocked") and random.random() < 0.3:
                agent["state"] = "idle"
                agent["current_task_id"] = None

    def _spawn_task(self, profile: str) -> None:
        task_id = f"t_synth{next(self._task_ids):04d}"
        now = int(time.time())
        self._tasks[task_id] = {
            "id": task_id,
            "title": random.choice(_TITLES),
            "status": "running",
            "assignee": profile,
            "priority": "normal",
            "created_at": now,
            "started_at": now,
            "completed_at": None,
        }
        self._agents[profile]["current_task_id"] = task_id
        self._agents[profile]["state"] = "working"
        self._agents[profile]["last_started_at"] = now
        self._emit(task_id, "created", profile)
        self._emit(task_id, "claimed", profile)
        self._emit(task_id, "spawned", profile)
        self._prune_tasks()
        # Fresh task -> fresh activity stream (never show a past task's steps).
        self._activity_idx[profile] = 0
        self._activity_items[profile] = []

    def _advance_activity(self, profile: str) -> None:
        idx = self._activity_idx.get(profile, 0)
        if idx >= len(_ACTIVITY_SCRIPT):
            return  # script exhausted for this run — hold at the last state
        kind, label, text = _ACTIVITY_SCRIPT[idx]
        items = self._activity_items.setdefault(profile, [])
        items.append(
            {"ts": time.time(), "kind": kind, "label": label, "text": sanitize_text(text)}
        )
        if len(items) > _ACTIVITY_MAX_ITEMS:
            del items[: len(items) - _ACTIVITY_MAX_ITEMS]
        self._activity_idx[profile] = idx + 1

    def _heartbeat(self, profile: str) -> None:
        task_id = self._agents[profile]["current_task_id"]
        if not task_id:
            return
        self._agents[profile]["last_heartbeat_at"] = int(time.time())
        self._emit(task_id, "heartbeat", profile)

    def _finish_task(self, profile: str, *, blocked: bool) -> None:
        task_id = self._agents[profile]["current_task_id"]
        if not task_id:
            return
        now = int(time.time())
        self._tasks[task_id]["status"] = "blocked" if blocked else "done"
        self._tasks[task_id]["completed_at"] = now
        self._agents[profile]["last_ended_at"] = now
        self._agents[profile]["last_outcome"] = "blocked" if blocked else "completed"
        self._agents[profile]["state"] = "blocked" if blocked else "done"
        self._emit(task_id, "blocked" if blocked else "completed", profile)
        # Task is no longer running — no live worker activity to show.
        self._activity_items.pop(profile, None)
        self._activity_idx.pop(profile, None)

    def _prune_tasks(self) -> None:
        terminal = [tid for tid, t in self._tasks.items() if t["status"] in ("done", "blocked")]
        while len(self._tasks) > _MAX_RETAINED_TASKS and terminal:
            del self._tasks[terminal.pop(0)]

    def _emit(self, task_id: str, kind: str, assignee: str) -> None:
        ev: Event = {
            "id": next(self._event_ids),
            "task_id": task_id,
            "run_id": None,
            "kind": kind,
            "payload": {"assignee": assignee},
            "created_at": int(time.time()),
        }
        self._events.append(ev)
        if len(self._events) > _MAX_RETAINED_EVENTS:
            del self._events[: len(self._events) - _MAX_RETAINED_EVENTS]
        for cb in list(self._subscribers):
            cb(ev)

    # -- DataSource interface -------------------------------------------
    async def get_agents(self) -> list[Agent]:
        return [dict(a) for a in self._agents.values()]

    async def get_tasks(self) -> list[Task]:
        return [dict(t) for t in self._tasks.values()]

    async def subscribe(self, on_event: EventHandler) -> Unsubscribe:
        self._subscribers.append(on_event)

        def _unsubscribe() -> None:
            if on_event in self._subscribers:
                self._subscribers.remove(on_event)

        return _unsubscribe

    async def act(
        self, agent_id: str, action: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return {
            "ok": False,
            "detail": "read-only public demo — connect your own Hermes instance for real control",
        }

    async def cost_summary(self) -> dict[str, Any]:
        return {
            "available": True,
            "total_cost_usd": round(self._cost_usd, 4),
            "input_tokens": self._input_tokens,
            "output_tokens": self._output_tokens,
            "session_count": len(self._agents),
            "active_session_count": sum(1 for a in self._agents.values() if a["state"] == "working"),
        }

    # -- SSE pull API (mirrors KanbanAdapter so /events works unchanged) --
    def current_max_event_id(self) -> int:
        return self._events[-1]["id"] if self._events else 0

    def fetch_events_after(self, cursor: int, limit: int = 500) -> list[Event]:
        return [e for e in self._events if e["id"] > cursor][:limit]

    async def get_task_detail(self, task_id: str) -> dict[str, Any] | None:
        task = self._tasks.get(task_id)
        if task is None:
            return None
        events = [e for e in self._events if e["task_id"] == task_id]
        return {**task, "body": None, "result": None, "last_failure_error": None, "events": events}

    async def get_worker_activity(self, agent_id: str) -> dict[str, Any]:
        """Public-demo mirror of the real feed: a fabricated but plausible,
        time-evolving stream for any currently-"working" synthetic agent
        (see `_ACTIVITY_SCRIPT`/`_advance_activity`); `{available: False}` for
        idle/blocked/done agents, same as the real adapter reports for an
        agent with no running worker."""
        agent = self._agents.get(agent_id)
        if agent is None or agent.get("state") != "working":
            return {"available": False, "updatedAt": None, "items": []}
        items = self._activity_items.get(agent_id, [])
        return {
            "available": bool(items),
            "updatedAt": items[-1]["ts"] if items else None,
            "items": list(items),
        }
