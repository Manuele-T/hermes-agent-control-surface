"""KanbanAdapter — read-only reads off the Hermes Kanban SQLite DB.

Runs INSIDE WSL so `~` resolves to /home/<user> (see DISCOVERY.md host reality).
All access is read-only (`file:<path>?mode=ro`) with a FRESH connection per call
— never a long-lived read txn — so WAL commits from the dispatcher are always
visible and we never lock the board.

Agents = profiles, with a coarse live state derived from `task_runs` (the real
per-attempt claim record). The /events stream (in main.py) tails `task_events`
for transitions; this snapshot is the point-in-time view.

`GET /workers/active` (REST) is intentionally NOT used here — it needs the
dashboard running + a Bearer token (Step 4). DB-only is the required path.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import httpx

from app.datasource import Agent, DataSource, Event, EventHandler, Task, Unsubscribe
from app.redact import sanitize_text
from app.text_clean import strip_tool_result_envelope

log = logging.getLogger(__name__)

_POLL_SECONDS = 0.5
_EVENT_COLUMNS = "id, task_id, run_id, kind, payload, created_at"
# Live-activity feed (DISCOVERY.md spike): cap on items returned/considered.
_ACTIVITY_MAX_ITEMS = 20

# Run outcomes that map to a non-idle terminal state.
_ERROR_OUTCOMES = {"crashed", "timed_out", "gave_up", "spawn_failed"}

_KANBAN_API = "/api/plugins/kanban"
_VALID_ACTIONS = {"spawn", "comment", "unblock", "reassign", "cancel", "archive"}


class KanbanAdapter(DataSource):
    def __init__(
        self,
        db_path: str,
        *,
        dashboard_base_url: str = "http://127.0.0.1:9119",
        board_slug: str = "default",
        session_token: str | None = None,
        hermes_home: str | None = None,
    ) -> None:
        self.db_path = db_path
        self.dashboard_base_url = dashboard_base_url.rstrip("/")
        self.board_slug = board_slug
        self.session_token = session_token
        # Hermes home holds profiles/; defaults to ~/.hermes (backend runs in WSL).
        self.hermes_home = Path(hermes_home) if hermes_home else Path.home() / ".hermes"

    # -- connection -------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        # Explicit existence check so we name the path instead of failing with
        # an opaque "unable to open database file". Never read a phantom file.
        if not Path(self.db_path).exists():
            raise FileNotFoundError(
                f"Kanban DB not found at {self.db_path!r}. Run the backend inside "
                "WSL (so ~ resolves to /home/<user>) or set HERMES_KANBAN_DB."
            )
        con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        return con

    # -- agents -----------------------------------------------------------
    async def get_agents(self) -> list[Agent]:
        return await asyncio.to_thread(self._get_agents_sync)

    def _get_agents_sync(self) -> list[Agent]:
        con = self._connect()
        try:
            # Latest run per profile (max id == most recent attempt).
            latest = {
                r["profile"]: r
                for r in con.execute(
                    "SELECT r.id AS run_id, r.profile, r.task_id, r.status, r.outcome, "
                    "       r.worker_pid, r.last_heartbeat_at, r.started_at, r.ended_at "
                    "FROM task_runs r "
                    "JOIN (SELECT profile, MAX(id) AS mid FROM task_runs "
                    "      WHERE profile IS NOT NULL GROUP BY profile) m "
                    "  ON r.profile = m.profile AND r.id = m.mid"
                ).fetchall()
            }
            # Current OPEN task per profile = most recent non-terminal assigned
            # task (ready/blocked/scheduled/running...). This is the agent's
            # "current task" even before a worker claims it — a ready task sits
            # here until the dispatcher picks it up. ASC + overwrite keeps newest.
            open_tasks: dict[str, sqlite3.Row] = {}
            for t in con.execute(
                "SELECT assignee, id, status FROM tasks "
                "WHERE assignee IS NOT NULL AND status NOT IN ('done', 'archived') "
                "ORDER BY created_at ASC"
            ).fetchall():
                open_tasks[t["assignee"]] = t
            agents: list[Agent] = []
            for profile in sorted(set(latest) | set(open_tasks)):
                r = latest.get(profile)
                owned = open_tasks.get(profile)
                # An active run (not ended) = actively working on its task.
                if r is not None and r["ended_at"] is None:
                    agents.append(self._agent_from_run(profile, r, r["task_id"]))
                # No active run but owns a queued/blocked task: that's its current
                # task. State is blocked or idle (queued), NOT working — no worker
                # has claimed it yet.
                elif owned is not None:
                    agents.append(
                        {
                            **self._idle_agent(profile),
                            "state": "blocked" if owned["status"] == "blocked" else "idle",
                            "current_task_id": owned["id"],
                            "last_heartbeat_at": r["last_heartbeat_at"] if r else None,
                            "last_started_at": r["started_at"] if r else None,
                            "last_ended_at": r["ended_at"] if r else None,
                            "last_outcome": r["outcome"] if r else None,
                            # No active run to kill — the task is queued, not running.
                            "run_id": None,
                        }
                    )
                # No current task: terminal run state (done/error) or fully idle.
                elif r is not None:
                    agents.append(self._agent_from_run(profile, r, None))
                else:
                    agents.append(self._idle_agent(profile))
            return agents
        finally:
            con.close()

    def _agent_from_run(
        self, profile: str, r: sqlite3.Row, current_task_id: str | None
    ) -> Agent:
        return {
            "id": profile,
            "name": profile,
            "state": self._state_from_run(r),
            "current_task_id": current_task_id,
            "worker_pid": r["worker_pid"],
            "last_heartbeat_at": r["last_heartbeat_at"],
            "last_started_at": r["started_at"],
            "last_ended_at": r["ended_at"],
            "last_outcome": r["outcome"],
            # Step 13: the real per-worker run id (task_runs.id), sourced here —
            # NOT from the sessions API, which has no such field — so the kill
            # action always addresses the actual claim record. Only meaningful
            # while the run hasn't ended (a terminated/finished run 404s/409s).
            "run_id": r["run_id"] if r["ended_at"] is None else None,
        }

    @staticmethod
    def _idle_agent(profile: str) -> Agent:
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
            "run_id": None,
        }

    @staticmethod
    def _state_from_run(r: sqlite3.Row) -> str:
        if r["ended_at"] is None and r["status"] == "running":
            return "working"
        outcome = r["outcome"]
        if outcome == "completed":
            return "done"
        if outcome == "blocked":
            return "blocked"
        if outcome in _ERROR_OUTCOMES:
            return "error"
        return "idle"

    # -- tasks ------------------------------------------------------------
    async def get_tasks(self) -> list[Task]:
        return await asyncio.to_thread(self._get_tasks_sync)

    def _get_tasks_sync(self) -> list[Task]:
        con = self._connect()
        try:
            rows = con.execute(
                "SELECT id, title, status, assignee, priority, created_by, "
                "       created_at, started_at, completed_at, workspace_kind, "
                "       worker_pid, current_run_id, last_heartbeat_at, result "
                "FROM tasks ORDER BY created_at ASC"
            ).fetchall()
            summaries = self._latest_event_summaries(con)
            tasks: list[Task] = []
            for r in rows:
                t = dict(r)
                raw_summary = t.get("result") or summaries.get(t["id"])
                t["summary"] = sanitize_text(str(raw_summary), max_len=160) if raw_summary else None
                tasks.append(t)
            return tasks
        finally:
            con.close()

    @staticmethod
    def _latest_event_summaries(con: sqlite3.Connection) -> dict[str, str]:
        """One-shot lookup of each task's most recent `payload.summary` from
        `task_events` — `tasks.result` is typically unpopulated in practice
        (the real outcome text lives on the terminal event instead, per
        DISCOVERY.md), so this is the fallback the housekeeping panel's
        preview column actually relies on. Ordered DESC per task_id so the
        first row kept per id is the most recent; a single query for the
        whole board, not one query per task."""
        out: dict[str, str] = {}
        for row in con.execute(
            "SELECT task_id, payload FROM task_events "
            "WHERE payload IS NOT NULL ORDER BY task_id, id DESC"
        ):
            task_id = row["task_id"]
            if task_id in out:
                continue
            try:
                payload = json.loads(row["payload"])
            except (ValueError, TypeError):
                continue
            summary = (payload or {}).get("summary")
            if summary:
                out[task_id] = str(summary)
        return out

    # -- live events (used by the SSE route + subscribe) ------------------
    def current_max_event_id(self) -> int:
        con = self._connect()
        try:
            row = con.execute("SELECT COALESCE(MAX(id), 0) AS m FROM task_events").fetchone()
            return int(row["m"])
        finally:
            con.close()

    def fetch_events_after(self, cursor: int, limit: int = 500) -> list[Event]:
        """Fresh read-only tail of task_events with id > cursor."""
        con = self._connect()
        try:
            rows = con.execute(
                f"SELECT {_EVENT_COLUMNS} FROM task_events WHERE id > ? "
                "ORDER BY id ASC LIMIT ?",
                (cursor, limit),
            ).fetchall()
            return [self._event_dict(r) for r in rows]
        finally:
            con.close()

    @staticmethod
    def _event_dict(r: sqlite3.Row) -> Event:
        try:
            payload = json.loads(r["payload"]) if r["payload"] else None
        except (ValueError, TypeError):
            payload = None
        return {
            "id": r["id"],
            "task_id": r["task_id"],
            "run_id": r["run_id"],
            "kind": r["kind"],
            "payload": payload,
            "created_at": r["created_at"],
        }

    # -- single-task detail (event timeline click-through) -----------------
    async def get_task_detail(self, task_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_task_detail_sync, task_id)

    def _get_task_detail_sync(self, task_id: str) -> dict[str, Any] | None:
        con = self._connect()
        try:
            row = con.execute(
                "SELECT id, title, body, status, assignee, result, created_at, "
                "       started_at, completed_at, last_failure_error "
                "FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                return None
            events = con.execute(
                f"SELECT {_EVENT_COLUMNS} FROM task_events WHERE task_id = ? ORDER BY id ASC",
                (task_id,),
            ).fetchall()
            return {**dict(row), "events": [self._event_dict(r) for r in events]}
        finally:
            con.close()

    # -- live worker activity (DISCOVERY.md spike) --------------------------
    async def get_worker_activity(self, agent_id: str) -> dict[str, Any]:
        """Primary source, per the DISCOVERY.md spike: the agent's CURRENT
        running task's own profile session store
        (`~/.hermes/profiles/<profile>/state.db`, `messages` table) — the
        only source with actual readable assistant text and tool
        calls/results, not just tool names. Session id is resolved fully
        locally (no dashboard/REST dependency): the `sessions` table's own
        `cwd` column matches `~/.hermes/kanban/workspaces/<task_id>` exactly
        (confirmed live in the spike; an improvement over the spike's own
        REST-based lookup, since this needs no token and can't be blocked by
        the dashboard being down). Never throws — any missing file, locked
        DB, or absent match cleanly returns `{"available": False, ...}` so
        the caller (HookEnrichedAdapter) can fall back to coarse hook data.

        🔴 Privacy: `reasoning`/`reasoning_content` columns are excluded from
        the SELECT entirely — they hold the model's raw chain-of-thought and
        must never enter this process's memory, let alone the response. Every
        text field that IS emitted (assistant content, tool args, tool
        output) is bounded and passed through `sanitize_text()` before
        returning — raw tool output can carry secrets or full shell command
        text (see app/redact.py's docstring for the "best-effort, not a
        guarantee" caveat)."""
        return await asyncio.to_thread(self._get_worker_activity_sync, agent_id)

    _UNAVAILABLE_ACTIVITY: dict[str, Any] = {"available": False, "updatedAt": None, "items": []}

    def _get_worker_activity_sync(self, agent_id: str) -> dict[str, Any]:
        try:
            con = self._connect()
        except FileNotFoundError:
            return dict(self._UNAVAILABLE_ACTIVITY)
        try:
            run = con.execute(
                "SELECT task_id FROM task_runs "
                "WHERE profile = ? AND status = 'running' AND ended_at IS NULL "
                "ORDER BY id DESC LIMIT 1",
                (agent_id,),
            ).fetchone()
        except sqlite3.Error:
            return dict(self._UNAVAILABLE_ACTIVITY)
        finally:
            con.close()
        if run is None:
            return dict(self._UNAVAILABLE_ACTIVITY)  # no running worker for this agent

        state_db = self.hermes_home / "profiles" / agent_id / "state.db"
        if not state_db.exists():
            return dict(self._UNAVAILABLE_ACTIVITY)
        workspace_cwd = str(self.hermes_home / "kanban" / "workspaces" / run["task_id"])

        try:
            scon = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
            scon.row_factory = sqlite3.Row
        except sqlite3.Error:
            return dict(self._UNAVAILABLE_ACTIVITY)
        try:
            srow = scon.execute(
                "SELECT id FROM sessions WHERE cwd = ? ORDER BY started_at DESC LIMIT 1",
                (workspace_cwd,),
            ).fetchone()
            if srow is None:
                return dict(self._UNAVAILABLE_ACTIVITY)
            # EXCLUDING reasoning/reasoning_content per the hard privacy rule —
            # they are not selected, so they never exist in this process at all.
            rows = scon.execute(
                "SELECT role, content, tool_calls, tool_name, timestamp FROM messages "
                "WHERE session_id = ? AND role IN ('assistant', 'tool') "
                "ORDER BY id DESC LIMIT ?",
                (srow["id"], _ACTIVITY_MAX_ITEMS),
            ).fetchall()
        except sqlite3.Error:
            return dict(self._UNAVAILABLE_ACTIVITY)
        finally:
            scon.close()

        items: list[dict[str, Any]] = []
        for r in reversed(rows):  # oldest -> newest
            ts = r["timestamp"]
            if r["role"] == "assistant":
                if r["content"]:
                    items.append(
                        {"ts": ts, "kind": "assistant", "label": None, "text": sanitize_text(r["content"])}
                    )
                if r["tool_calls"]:
                    try:
                        calls = json.loads(r["tool_calls"])
                    except (ValueError, TypeError):
                        calls = []
                    for call in calls:
                        fn = (call or {}).get("function") or {}
                        name = fn.get("name") or "tool"
                        args = fn.get("arguments")
                        items.append(
                            {
                                "ts": ts,
                                "kind": "tool_call",
                                "label": name,
                                "text": sanitize_text(args if isinstance(args, str) else json.dumps(args)),
                            }
                        )
            else:  # role == "tool"
                content = r["content"]
                if content:
                    content = strip_tool_result_envelope(content)
                items.append(
                    {
                        "ts": ts,
                        "kind": "tool_result",
                        "label": r["tool_name"] or "tool",
                        "text": sanitize_text(content),
                    }
                )
        items = items[-_ACTIVITY_MAX_ITEMS:]
        return {
            "available": bool(items),
            "updatedAt": items[-1]["ts"] if items else None,
            "items": items,
        }

    # -- sub-agent enrichment (Step 11, best-effort) -----------------------
    async def find_active_child_session(self, parent_session_id: str | None) -> dict[str, Any] | None:
        """Look up `GET /api/sessions` for a child session whose
        `parent_session_id` matches, to surface a nicer title/preview than the
        bare `child_role` hook telemetry gives while a `delegate_task` call is
        still in flight (DISCOVERY.md ss6: sessions carry parent_session_id,
        no dedicated filter param, so this is filtered client-side over the
        most recent rows). Duck-typed by HookEnrichedAdapter, not part of the
        DataSource interface — same optional-enrichment treatment as
        `/workers/active` (Step 2). Never raises: no token, no match, or a
        dashboard error all just mean the sprite falls back to hook data alone.
        """
        if not self.session_token or not parent_session_id:
            return None
        url = f"{self.dashboard_base_url}/api/sessions"
        headers = {"Authorization": f"Bearer {self.session_token}"}
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(url, headers=headers, params={"limit": 25, "order": "recent"})
            if resp.status_code >= 400:
                return None
            rows = resp.json().get("sessions", [])
        except httpx.HTTPError:
            return None
        for row in rows:
            if row.get("parent_session_id") == parent_session_id:
                return row
        return None

    async def subscribe(self, on_event: EventHandler) -> Unsubscribe:
        stop = asyncio.Event()

        async def _run() -> None:
            cursor = await asyncio.to_thread(self.current_max_event_id)
            while not stop.is_set():
                for ev in await asyncio.to_thread(self.fetch_events_after, cursor):
                    cursor = ev["id"]
                    on_event(ev)
                await asyncio.sleep(_POLL_SECONDS)

        task = asyncio.create_task(_run())

        def _unsubscribe() -> None:
            stop.set()
            task.cancel()

        return _unsubscribe

    # -- control / writes -------------------------------------------------
    def valid_profiles(self) -> set[str]:
        """Known profile names = dirs under ~/.hermes/profiles plus 'default'."""
        names = {"default"}
        pdir = self.hermes_home / "profiles"
        if pdir.is_dir():
            names.update(p.name for p in pdir.iterdir() if p.is_dir())
        return names

    def gateway_running(self) -> bool | None:
        """True/False if a gateway (the dispatcher host) is running; None if we
        can't tell. Spawned tasks only become live workers when one is up.

        Deliberately a process-table scan (any profile), not Hermes's own
        PID-file check (`hermes_cli.kanban._check_dispatcher_presence`,
        used by the dashboard's own `POST /tasks` "warning" field) — that
        check reads `{HERMES_HOME}/gateway.pid`, which is scoped to whatever
        profile the caller (the dashboard process) is running under. A
        gateway launched for a DIFFERENT profile (e.g. `hermes -p
        autonomous-builder gateway run`) writes its PID file under
        `~/.hermes/profiles/autonomous-builder/`, invisible to the
        dashboard's default-profile check — even though Hermes's kanban
        dispatcher lock is machine-global and that gateway genuinely
        dispatches every profile's ready tasks (confirmed by reading
        `gateway/kanban_watchers.py`: "the lock lives at the machine-global
        kanban root ... so it serialises ALL gateways"). Live-tested: with
        only a profile-scoped gateway running, the dashboard's own `POST
        /tasks` response carried a false "No gateway is running" warning
        while this pgrep-based check correctly returned True — see
        `_spawn()` for why we no longer relay that upstream field.
        """
        pgrep_path = shutil.which("pgrep")
        if pgrep_path is None:
            return None
        try:
            r = subprocess.run(
                [pgrep_path, "-f", "gateway run"], capture_output=True, text=True, timeout=5
            )
            return r.returncode == 0 and bool(r.stdout.strip())
        except (OSError, subprocess.SubprocessError):
            return None

    async def act(
        self, agent_id: str, action: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        payload = payload or {}
        if action not in _VALID_ACTIONS:
            return {"ok": False, "detail": f"unknown action {action!r}"}

        if action == "spawn":
            title = (payload.get("title") or "").strip()
            if not title:
                return {"ok": False, "detail": "spawn requires a title"}
            if agent_id not in self.valid_profiles():
                return {
                    "ok": False,
                    "detail": f"unknown profile {agent_id!r}; known: "
                    + ", ".join(sorted(self.valid_profiles())),
                }
            result = await self._spawn(agent_id, payload)
        elif action == "comment":
            task_id = (payload.get("task_id") or "").strip()
            body = (payload.get("body") or "").strip()
            if not task_id or not body:
                return {"ok": False, "detail": "comment requires task_id and body"}
            result = await self._comment(task_id, body, payload.get("author") or "dashboard")
        elif action == "unblock":
            task_id = (payload.get("task_id") or "").strip()
            if not task_id:
                return {"ok": False, "detail": "unblock requires task_id"}
            result = await self._unblock(task_id)
        elif action == "reassign":
            task_id = (payload.get("task_id") or "").strip()
            profile = (payload.get("profile") or "").strip()
            if not task_id or not profile:
                return {"ok": False, "detail": "reassign requires task_id and profile"}
            if profile not in self.valid_profiles():
                return {
                    "ok": False,
                    "detail": f"unknown profile {profile!r}; known: "
                    + ", ".join(sorted(self.valid_profiles())),
                }
            reason = payload.get("reason") or "reassigned via control surface"
            result = await self._reassign(task_id, profile, str(reason))
        elif action == "cancel":
            task_id = (payload.get("task_id") or "").strip()
            if not task_id:
                return {"ok": False, "detail": "cancel requires task_id"}
            result = await self._cancel(task_id)
        else:  # archive
            task_id = (payload.get("task_id") or "").strip()
            if not task_id:
                return {"ok": False, "detail": "archive requires task_id"}
            result = await self._archive_guarded(task_id)

        # Spawned tasks need a dispatcher to become live workers — warn if none.
        # Runs the (synchronous) process scan off the event loop so a live
        # request behaves like the standalone call it's equivalent to.
        if result.get("ok") and action == "spawn":
            running = await asyncio.to_thread(self.gateway_running)
            if running is False:
                result["warning"] = (
                    "no gateway running — the task is queued but won't be picked up "
                    "until a gateway starts (hermes gateway start) or you dispatch "
                    "(hermes kanban dispatch)."
                )
        return result

    # -- per-verb: REST (Bearer) preferred, CLI fallback ------------------
    async def _spawn(self, assignee: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = payload.get("body")
        max_runtime = payload.get("max_runtime_seconds")
        if self.session_token:
            json_body: dict[str, Any] = {"title": payload["title"].strip(), "assignee": assignee}
            if body:
                json_body["body"] = body
            if max_runtime:
                json_body["max_runtime_seconds"] = int(max_runtime)
            rest = await self._rest("POST", "/tasks", json_body)
            if rest is not None:
                task = rest.get("task") or {}
                # NOT relaying rest["warning"]: on `POST /tasks` it is always
                # Hermes's own dispatcher-presence check (`plugin_api.py`
                # calls `_check_dispatcher_presence()`), which reads a
                # profile-scoped `gateway.pid` under the DASHBOARD's own
                # HERMES_HOME — it goes false-negative for a gateway running
                # under a different profile even though that gateway holds
                # the machine-global dispatch lock and genuinely picks up
                # the task. `act()`'s own `gateway_running()` (a
                # profile-agnostic process scan) is the authoritative check.
                return {"ok": True, "via": "rest", "task_id": task.get("id"), "task": task}
        # CLI fallback (direct in-WSL subprocess, argv list — no shell, no wsl hop)
        argv = ["create", payload["title"].strip(), "--assignee", assignee, "--json"]
        if body:
            argv += ["--body", body]
        if max_runtime:
            argv += ["--max-runtime", str(int(max_runtime))]
        cli = self._cli(argv, parse_json=True)
        if cli["ok"]:
            cli["task_id"] = (cli.get("json") or {}).get("id")
        return cli

    async def _comment(self, task_id: str, body: str, author: str) -> dict[str, Any]:
        if self.session_token:
            rest = await self._rest(
                "POST", f"/tasks/{task_id}/comments", {"body": body, "author": author}
            )
            if rest is not None:
                return {"ok": True, "via": "rest", "task_id": task_id}
        return self._cli(["comment", task_id, body, "--author", author])

    async def _unblock(self, task_id: str) -> dict[str, Any]:
        if self.session_token:
            rest = await self._rest("PATCH", f"/tasks/{task_id}", {"status": "ready"})
            if rest is not None:
                return {"ok": True, "via": "rest", "task_id": task_id}
        return self._cli(["unblock", task_id])

    async def _reassign(self, task_id: str, profile: str, reason: str) -> dict[str, Any]:
        if self.session_token:
            rest = await self._rest(
                "POST",
                f"/tasks/{task_id}/reassign",
                {"profile": profile, "reclaim_first": True, "reason": reason},
            )
            if rest is not None:
                return {"ok": True, "via": "rest", "task_id": task_id}
        return self._cli(["reassign", task_id, profile])

    async def _cancel(self, task_id: str) -> dict[str, Any]:
        """Archive a task — the single stop control for any task, queued or
        running. A running task is stopped first: reclaim releases its claim
        via the same `reclaim_task` path (SIGTERM then SIGKILL) the removed
        terminate action used, THEN the task is archived. If the reclaim call
        itself fails but the task is no longer running by the time we check
        again (e.g. it finished naturally, or was already reclaimed), archive
        proceeds anyway rather than erroring on a stale race. A genuine
        failure to stop a still-running worker is reported and archiving is
        skipped — never silently archive a task whose worker is still alive.
        If archiving fails AFTER a successful reclaim, the worker-killed/
        still-on-board mismatch is called out explicitly in the detail."""
        status = await asyncio.to_thread(self._task_status, task_id)
        if status is None:
            return {"ok": False, "detail": f"task {task_id!r} not found"}

        was_running = status == "running"
        if was_running:
            reclaim = await self._reclaim(task_id, "cancelled via control surface")
            if not reclaim.get("ok"):
                still_running = await asyncio.to_thread(self._task_status, task_id) == "running"
                if still_running:
                    return {
                        "ok": False,
                        "detail": f"failed to stop the running worker: {reclaim.get('detail', 'unknown error')}",
                    }

        try:
            archive = await self._archive(task_id)
        except _RestError as exc:
            detail = f"{exc.status}: {exc.detail}"
            if was_running:
                detail = f"worker was stopped, but archiving the task failed — it may still be on the board: {detail}"
            return {"ok": False, "detail": detail}
        if not archive.get("ok") and was_running:
            archive = {
                **archive,
                "detail": (
                    "worker was stopped, but archiving the task failed — it may still be "
                    f"on the board: {archive.get('detail', 'unknown error')}"
                ),
            }
        return archive

    async def _archive_guarded(self, task_id: str) -> dict[str, Any]:
        """Archive-only control for the housekeeping panel — unlike `cancel`,
        this NEVER reclaims/kills a worker. It refuses outright if the task is
        currently `running`: archiving a live task's row out from under its
        worker can orphan the OS process (see DO-NOT-DO.md's Kanban-data
        section). The frontend already disables this per-row and excludes
        running tasks from "archive all", but this is the server-side
        backstop — never trust the client alone for a destructive write."""
        status = await asyncio.to_thread(self._task_status, task_id)
        if status is None:
            return {"ok": False, "detail": f"task {task_id!r} not found"}
        if status == "running":
            return {
                "ok": False,
                "detail": "task is running — archiving it here could orphan the worker; use Cancel instead",
            }
        try:
            return await self._archive(task_id)
        except _RestError as exc:
            return {"ok": False, "detail": f"{exc.status}: {exc.detail}"}

    async def _reclaim(self, task_id: str, reason: str) -> dict[str, Any]:
        """Release a running task's worker claim (stops the OS worker process,
        same `reclaim_task` path the removed terminate action used). REST
        first, CLI fallback. A REST error response (404/409) is caught and
        returned as a normal failure dict rather than raised, so `_cancel`
        can decide whether to still proceed to archive."""
        if self.session_token:
            try:
                rest = await self._rest("POST", f"/tasks/{task_id}/reclaim", {"reason": reason})
            except _RestError as exc:
                return {"ok": False, "detail": f"{exc.status}: {exc.detail}"}
            if rest is not None:
                return {"ok": True, "via": "rest", "task_id": task_id}
        return self._cli(["reclaim", task_id, "--reason", reason])

    async def _archive(self, task_id: str) -> dict[str, Any]:
        if self.session_token:
            rest = await self._rest("PATCH", f"/tasks/{task_id}", {"status": "archived"})
            if rest is not None:
                return {"ok": True, "via": "rest", "task_id": task_id}
        return self._cli(["archive", task_id])

    def _task_status(self, task_id: str) -> str | None:
        con = self._connect()
        try:
            row = con.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
            return row["status"] if row else None
        finally:
            con.close()

    async def _rest(
        self, method: str, path: str, json_body: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Call the Bearer-gated kanban REST API. Returns the JSON on success,
        None on a connection error (so the caller falls back to the CLI). Raises
        nothing; HTTP error bodies are returned as {'ok': False, ...} via caller.
        """
        url = f"{self.dashboard_base_url}{_KANBAN_API}{path}"
        headers = {"Authorization": f"Bearer {self.session_token}"}
        params = {"board": self.board_slug}
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.request(
                    method, url, json=json_body, headers=headers, params=params
                )
        except httpx.ConnectError:
            log.info("dashboard not reachable at %s — falling back to CLI", url)
            return None
        if resp.status_code >= 400:
            # A real validation/auth error — surface it rather than masking with CLI.
            detail = resp.text
            try:
                detail = resp.json().get("detail", detail)
            except (ValueError, AttributeError):
                pass
            raise _RestError(resp.status_code, str(detail))
        return resp.json()

    def _cli(self, argv: list[str], *, parse_json: bool = False) -> dict[str, Any]:
        """Run `hermes kanban --board <slug> <argv...>` as a direct subprocess.
        Backend runs inside WSL, so this is a native call — NOT a wsl-hop shell
        string — and args are a proper argv list (safe quoting)."""
        if shutil.which("hermes") is None:
            return {"ok": False, "detail": "hermes CLI not found on PATH (run backend in WSL)"}
        cmd = ["hermes", "kanban", "--board", self.board_slug, *argv]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            return {"ok": False, "detail": f"CLI failed: {exc}"}
        if proc.returncode != 0:
            return {"ok": False, "via": "cli", "detail": (proc.stderr or proc.stdout).strip()}
        out: dict[str, Any] = {"ok": True, "via": "cli"}
        if parse_json and proc.stdout.strip():
            try:
                out["json"] = json.loads(proc.stdout)
            except ValueError:
                out["raw"] = proc.stdout.strip()
        return out


class _RestError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        self.status = status
        self.detail = detail
        super().__init__(f"{status}: {detail}")
