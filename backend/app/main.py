"""FastAPI entrypoint. Wires the Kanban read adapter when the DB is present
(backend running inside WSL), else falls back to the stub so the app still boots.
All data flows through the DataSource interface."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.adapters.hook_enriched import HookEnrichedAdapter
from app.adapters.kanban import KanbanAdapter, _RestError
from app.adapters.sessions import SessionsAdapter
from app.adapters.stub import StubAdapter
from app.adapters.synthetic import SyntheticAdapter
from app.config import load_config
from app.datasource import DataSource
from app.hook_store import HookEventStore

log = logging.getLogger(__name__)

config = load_config()

# HERMES_DATA_SOURCE=synthetic forces the public-demo adapter (no real Hermes,
# read-only act()) regardless of whether a Kanban DB is present. Otherwise use
# the real adapter when the DB resolves to a real file, else stub so the app
# still boots (e.g. launched from Windows, where ~ has no Hermes DB).
hook_store = HookEventStore()  # fed by POST /ingest (hermesboard-sensor, Step 9)
datasource: DataSource
if config.data_source == "synthetic":
    datasource = SyntheticAdapter()
    log.warning(
        "HERMES_DATA_SOURCE=synthetic — running the public demo adapter. No "
        "real Hermes is attached; act() always declines (read-only)."
    )
elif config.kanban_db_exists:
    # HookEnrichedAdapter (Step 10) layers fine_state/activity from hook
    # telemetry on top; SessionsAdapter (Step 13) appends independent-profile
    # characters on top of that. Each delegates everything else straight
    # through, so this is additive over the Step 2 read adapter, not a
    # replacement.
    datasource = SessionsAdapter(
        HookEnrichedAdapter(
            KanbanAdapter(
                config.kanban_db_path,
                dashboard_base_url=config.dashboard_base_url,
                board_slug=config.board_slug,
                session_token=config.session_token,
            ),
            hook_store,
        ),
        dashboard_base_url=config.dashboard_base_url,
        session_token=config.session_token,
        # Kanban-worker sessions' cwd always lives under here (DISCOVERY.md
        # ss13) — the real signal to exclude them from "independent" agents.
        kanban_workspaces_prefix=str(Path(config.kanban_db_path).parent / "kanban" / "workspaces"),
    )
else:
    datasource = StubAdapter()
    log.warning(
        "Kanban DB not found at resolved path: %s — using StubAdapter (empty "
        "reads). Run the backend inside WSL (so ~ resolves to /home/<user>) or "
        "set HERMES_KANBAN_DB.",
        config.kanban_db_path,
    )

app = FastAPI(title="Hermes Agent Control Surface")

_SSE_KEEPALIVE = ": keepalive\n\n"
# Step 14: how often each SSE client re-polls the global cost/token HUD
# summary. SessionsAdapter/SyntheticAdapter cache their own result for a few
# seconds, so multiple open browser tabs don't each hammer the dashboard.
_COSTS_PUSH_INTERVAL_SECONDS = 10.0


@app.on_event("startup")
async def _check_dashboard() -> None:
    """Best-effort reachability check so a misconfigured dashboard fails loud at
    boot instead of silently breaking writes later. Never blocks startup."""
    if not isinstance(datasource, (HookEnrichedAdapter, SessionsAdapter)):
        return
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            await client.get(f"{config.dashboard_base_url}/api/status")
    except httpx.HTTPError:
        log.warning(
            "Hermes dashboard not reachable at %s — REST writes will fall back "
            "to the `hermes kanban` CLI. To enable REST: "
            "HERMES_DASHBOARD_SESSION_TOKEN=<token> hermes dashboard --no-open --skip-build "
            "(same token as HERMES_DASHBOARD_SESSION_TOKEN in .env).",
            config.dashboard_base_url,
        )


@app.on_event("startup")
async def _start_synthetic() -> None:
    if isinstance(datasource, SyntheticAdapter):
        datasource.start()


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "adapter": type(datasource).__name__,
        "board": config.board_slug,
        "dashboard_base_url": config.dashboard_base_url,
        "kanban_db_path": config.kanban_db_path,
        "kanban_db_exists": config.kanban_db_exists,
        # boolean only — the token value is never exposed
        "session_token_configured": config.has_token,
    }


@app.get("/agents")
async def agents():
    try:
        return await datasource.get_agents()
    except FileNotFoundError as exc:
        return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.get("/tasks")
async def tasks():
    try:
        return await datasource.get_tasks()
    except FileNotFoundError as exc:
        return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.get("/tasks/{task_id}")
async def task_detail(task_id: str):
    """Single-task detail (title/status/result + full task_events timeline)
    for the Recent events click-through. Only the adapters that carry a real
    or synthesized event history implement get_task_detail() — StubAdapter
    doesn't, same optional-capability duck-typing as the /events route."""
    get_detail = getattr(datasource, "get_task_detail", None)
    if get_detail is None:
        return JSONResponse(
            status_code=503, content={"detail": "task detail not available for this adapter"}
        )
    try:
        detail = await get_detail(task_id)
    except FileNotFoundError as exc:
        return JSONResponse(status_code=503, content={"detail": str(exc)})
    if detail is None:
        return JSONResponse(status_code=404, content={"detail": f"task {task_id!r} not found"})
    return detail


@app.get("/agents/{agent_id}/activity")
async def worker_activity(agent_id: str):
    """Live "what is this worker doing right now" feed (DISCOVERY.md spike):
    primary source is the worker's own profile session store, coarse
    hook/ingest activity as fallback — see KanbanAdapter/HookEnrichedAdapter's
    get_worker_activity() docstrings. Duck-typed exactly like /tasks/{id}:
    503 only when the adapter doesn't implement it at all (StubAdapter); a
    live adapter with nothing to report for this agent is a normal 200 with
    {"available": false}, never an error."""
    get_activity = getattr(datasource, "get_worker_activity", None)
    if get_activity is None:
        return JSONResponse(
            status_code=503, content={"detail": "worker activity not available for this adapter"}
        )
    try:
        return await get_activity(agent_id)
    except FileNotFoundError as exc:
        return JSONResponse(status_code=503, content={"detail": str(exc)})


class ActBody(BaseModel):
    agentId: str
    action: str
    payload: dict | None = None


@app.post("/act")
async def act(body: ActBody):
    try:
        result = await datasource.act(body.agentId, body.action, body.payload)
    except _RestError as exc:
        return JSONResponse(status_code=exc.status, content={"ok": False, "detail": exc.detail})
    except FileNotFoundError as exc:
        return JSONResponse(status_code=503, content={"ok": False, "detail": str(exc)})
    return result


@app.get("/events")
async def events(request: Request, since: int | None = None):
    """SSE stream tailing task_events by monotonic id. Each client tracks its own
    last-seen id (via ?since=, or the Last-Event-ID header on reconnect). A fresh
    read-only query runs each tick so new WAL commits are visible.

    HookEnrichedAdapter, SessionsAdapter (which delegates to it), and
    SyntheticAdapter all expose the same pull shape
    (current_max_event_id/fetch_events_after) so this route works unchanged
    for any of them; the stub has nothing to stream."""
    if not isinstance(datasource, (HookEnrichedAdapter, SessionsAdapter, SyntheticAdapter)):
        return JSONResponse(
            status_code=503,
            content={"detail": "live events require the Kanban DB (run inside WSL)"},
        )
    adapter = datasource

    start = since
    if start is None:
        last_event_id = request.headers.get("last-event-id")
        if last_event_id and last_event_id.isdigit():
            start = int(last_event_id)

    async def stream():
        # Default new clients to "from now" so they see the live lifecycle, not
        # the whole history. Pass ?since=0 to replay everything.
        cursor = start if start is not None else await asyncio.to_thread(
            adapter.current_max_event_id
        )
        # Step 10: alongside task_events, also push hook-derived fine-state
        # changes as synthetic (id-less) SSE frames so the UI can show
        # thinking/working:<family>/awaiting_approval live without polling.
        # Keyed by `now` (== AgentActivity.updated_at) per profile so we only
        # emit on an actual change, never re-send an unchanged snapshot.
        last_sent: dict[str, float] = {}
        # Step 11: dedupe key per profile for the subagent frame, so an
        # unchanged delegation episode is never re-sent (mirrors last_sent).
        last_sent_sub: dict[str, tuple[float, float | None, str]] = {}
        # Step 12: dedupe key per profile for the approval frame.
        last_sent_appr: dict[str, tuple[float, bool, str | None]] = {}
        # Step 14: global cost/token HUD, pushed on connect then every
        # _COSTS_PUSH_INTERVAL_SECONDS — 0.0 so the very first loop iteration
        # (monotonic() is always positive) fires immediately, not after a
        # 10s wait for the first paint.
        last_costs_push = 0.0
        while True:
            if await request.is_disconnected():
                break
            try:
                batch = await asyncio.to_thread(adapter.fetch_events_after, cursor)
            except FileNotFoundError as exc:
                yield f"event: error\ndata: {json.dumps({'detail': str(exc)})}\n\n"
                break
            emitted = False
            if batch:
                for ev in batch:
                    cursor = ev["id"]
                    yield f"id: {ev['id']}\ndata: {json.dumps(ev)}\n\n"
                emitted = True
            if isinstance(datasource, (HookEnrichedAdapter, SessionsAdapter)):
                for profile, activity in hook_store.snapshot().items():
                    if last_sent.get(profile) == activity.updated_at:
                        continue
                    last_sent[profile] = activity.updated_at
                    frame = {
                        "kind": "activity",
                        "payload": {
                            "assignee": profile,
                            "fine_state": activity.fine_state,
                            "activity": activity.activity,
                        },
                    }
                    yield f"data: {json.dumps(frame)}\n\n"
                    emitted = True
                # Step 11: same synthetic, id-less frame technique for the
                # delegated-subagent sprite. No staleness re-check here on
                # purpose — mirrors the "activity" frame above, which also
                # pushes hook state raw; get_agents() is where the pure
                # subagent_visible() staleness filter applies (same split as
                # merge_fine_state vs the raw activity push).
                for profile, sub in hook_store.subagent_snapshot().items():
                    key = (sub.started_at, sub.ended_at, sub.status)
                    if last_sent_sub.get(profile) == key:
                        continue
                    last_sent_sub[profile] = key
                    payload = await datasource.subagent_view(profile, sub)  # type: ignore[attr-defined]
                    payload["assignee"] = profile
                    yield f"data: {json.dumps({'kind': 'subagent', 'payload': payload})}\n\n"
                    emitted = True
                # Step 12: same technique for the pending-approval indicator.
                for profile, appr in hook_store.approval_snapshot().items():
                    key = (appr.requested_at, appr.resolved, appr.choice)
                    if last_sent_appr.get(profile) == key:
                        continue
                    last_sent_appr[profile] = key
                    payload = datasource.approval_view(appr)  # type: ignore[attr-defined]
                    payload["assignee"] = profile
                    yield f"data: {json.dumps({'kind': 'approval', 'payload': payload})}\n\n"
                    emitted = True
            # Step 14: global (no assignee) cost/token HUD frame — only the
            # two adapters that carry real or synthesized session cost data
            # implement cost_summary(); a bare KanbanAdapter/HookEnrichedAdapter
            # (no token, so no SessionsAdapter wrapper) or StubAdapter just
            # never sends one, and the HUD stays hidden client-side.
            if isinstance(datasource, (SessionsAdapter, SyntheticAdapter)) and (
                time.monotonic() - last_costs_push >= _COSTS_PUSH_INTERVAL_SECONDS
            ):
                last_costs_push = time.monotonic()
                try:
                    costs = await datasource.cost_summary()
                except Exception:
                    log.exception("cost_summary failed")
                    costs = None
                if costs is not None:
                    yield f"data: {json.dumps({'kind': 'costs', 'payload': costs})}\n\n"
                    emitted = True
            if not emitted:
                yield _SSE_KEEPALIVE
            await asyncio.sleep(0.5)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/ingest")
async def ingest(body: dict):
    """Sink for the hermesboard-sensor Hermes plugin's fire-and-forget hook
    telemetry (~/.hermes/plugins/hermesboard-sensor). Step 10: feeds the pure
    state_engine reducer via HookEventStore, keyed by `profile` (the only
    reliable correlation key back to a Kanban agent — DISCOVERY.md ss8/9).
    Never raises: a malformed envelope is logged and dropped, never 500s the
    fire-and-forget sensor thread that's calling this."""
    log.info("ingest: %s", body)
    try:
        hook_store.ingest(body)
    except Exception:
        log.exception("ingest: failed to fold envelope into HookEventStore")
    return {"ok": True}


@app.get("/approvals/{profile}/poll")
async def approvals_poll(profile: str):
    """Step 12: polled by the hermesboard-sensor plugin's background thread
    (running INSIDE a Hermes worker process, blocked on a pending
    dangerous-command approval) to learn whether the user has approved/denied
    it from the control surface. One-shot — a decision is returned at most
    once, then cleared, so a slow/duplicate poll can never double-resolve.
    Same trust boundary as /ingest (local-only, no auth)."""
    return {"choice": hook_store.pop_decision(profile)}


# Serve the built frontend (if present) from this same process, so one command
# serves both the API and the UI. Mounted LAST so it never shadows the API
# routes above. Dev mode (vite dev server + proxy) doesn't need this.
_FRONTEND_DIST = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
if _FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=_FRONTEND_DIST, html=True), name="frontend")
else:
    log.warning(
        "Built frontend not found at %s — run `npm run build` in frontend/ (or "
        "./run.sh) to serve the UI from this process. API routes still work.",
        _FRONTEND_DIST,
    )
