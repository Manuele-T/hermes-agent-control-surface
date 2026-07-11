# IMPLEMENTATION-NOTES.md — the full build log

This is the companion file to [DISCOVERY.md](DISCOVERY.md): the headline facts and
durable Hermes reference material live there, and everything else — every
per-feature implementation write-up, both live-bug investigations, and the
verification logs behind each change — lives here, in the order it was built.
Nothing below is shortened; this is the depth version.

## Contents

- [Assumptions confirmed / broken](#assumptions-confirmed--broken)
- [Early implementation notes](#early-implementation-notes--verified-while-building)
- [Local-first packaging](#local-first-packaging)
- [Public demo mode with synthetic data](#public-demo-mode-with-synthetic-data)
- [Hook telemetry sensor and the real hook-firing matrix](#hook-telemetry-sensor-and-the-real-hook-firing-matrix)
- [State-derivation engine](#state-derivation-engine-ground-truthed-against-real-hook-payloads)
- [Sub-agent visualization](#sub-agent-visualization-ground-truthed-against-real-delegatetask-runs)
- [Embodied approvals (live bug write-up)](#embodied-approvals-ground-truthed-against-real-source--two-live-round-trips)
- [Independent profiles and worker termination](#independent-profiles-and-worker-termination-ground-truthed-before-building)
- [PixiJS visual overhaul](#pixijs-visual-overhaul-ground-truthed-against-the-actual-sprite-sheet)
- [Polish pass: cost HUD and 2D wandering](#polish-pass-cost-hud-and-2d-wandering-ground-truthed-against-the-real-apiprofilessessions-payload)
- [Reassigning a running task](#reassigning-a-running-task-without-a-separate-reclaim-step)
- [Task detail click-through](#task-detail-click-through-event-timeline-modal)
- [False "no gateway is running" warning (live bug write-up)](#false-no-gateway-is-running-warning-on-spawn-two-different-gateway-checks-disagreeing)
- [UI fixes](#ui-fixes-global-font-scale-modal-scroll-note-for-next-run-agent-nameplates)
- [Live-activity source selection](#live-activity-source-selection-what-a-running-worker-is-doing-right-now)
- [Live-activity read implementation](#live-activity-read-implementing-the-source-chosen-above)
- [Live-activity panel](#live-activity-panel-wired-into-the-side-panel)
- [Board housekeeping panel: archive controls + hide-archived filter](#board-housekeeping-panel-archive-controls--hide-archived-filter)
- [Approval panel: richer Tirith explanations](#approval-panel-richer-tirith-explanations-2026-07-09--a-rebalance-investigated-then-narrowed-to-display-only)

---

## Assumptions confirmed / broken

**Confirmed**
- ✅ DB locations, WAL, read-only-safe, monotonic `task_events.id` cursor.
- ✅ Dashboard port **9119**, loopback default; live feed = `WS /events` tailing
  `task_events`; `?token=` needed for the WS.
- ✅ `heartbeat` events exist (no-hooks "working" signal).
- ✅ Raw `status=running` is blocked (API returns 400) — #19535 guarded.
- ✅ **Multiple profiles run as concurrent Kanban workers** (distinct PIDs/runs).
- ✅ `scratch` workspaces under `~/.hermes/kanban/workspaces/<task_id>`; use
  `worktree:`/`dir:` to persist.
- ✅ `~` resolves to the Linux home, so `~/.hermes/kanban.db` is correct and
  `/health` reports `kanban_db_exists:true`.

**Broken / changed — act on these**
- 🔴 **`/api/plugins/kanban/` is GATED, not unauthenticated** (v0.17). HTTP routes
  need `Authorization: Bearer <session_token>`; `?token=` query is 401 for HTTP
  (WS only). Token = `HERMES_DASHBOARD_SESSION_TOKEN` (ephemeral if unset, no file
  at rest). → **Config must carry a dashboard session token; co-launch the
  dashboard with a known token, OR use the `hermes kanban` CLI for writes (no
  token) and read the DB read-only.** The single biggest implementation change.
- 🟠 **`army-test` is empty** (the initial assumption was seeded tasks). Live data was generated
  on the **default** board (3 done test cards remain there — created via CLI; safe
  to `hermes kanban archive` if you want the default board clean).
- 🟠 **Dispatch lives in the gateway**, not the CLI/dashboard. A gateway must run
  for live workers; `hermes kanban dispatch`/`POST /dispatch` is a manual nudge.
- 🟠 **No delegation-tree REST API** (`/api/agents|/tasks|/delegation` → 404).
  Any sub-agent tree view must use `subagent_stop` events + session data.
- ⚪ Workers run with a broad gateway-injected toolset (not profile `toolsets`).

### Local process state during early implementation work
- The app's **backend runs on :8123**, **dashboard on :9119** (co-launched with
  our session token), **Vite dev on :5173** — all left running for ongoing work.
  No DB was ever written directly (all writes go via REST or the CLI). The
  OpenRouter key in `~/.hermes/auth.json` was location-noted only, never copied.
- **Default board is now clean** — all 11 accumulated test cards from early
  probing were archived (`hermes kanban archive <id>` for each). Board is empty
  and ready for the next round of verification/work.

### Reassign and cancel implementation notes
- **`reassign`** uses `POST /tasks/{id}/reassign {profile, reclaim_first:false, reason}`.
  `reclaim_first:false` means the API rejects if the task has an active running claim
  (i.e. the task is in `running` status held by a worker). Reassigning a running task
  would require killing the worker first — the v2 `POST /runs/{id}/terminate` path.
  So reassign works cleanly for `todo`/`ready`/`blocked`/`scheduled` tasks.
  CLI fallback: `hermes kanban reassign <task_id> <profile>`.
- **`cancel`** uses `PATCH /tasks/{id} {"status":"archived"}` (preferred over `DELETE`
  since archived tasks remain in history). The backend checks the task's status from the
  DB before calling REST and rejects `running` tasks with a clear message. CLI fallback:
  `hermes kanban archive <task_id>`.
- **Optimistic UI**: SidePanel maintains a `taskOverrides` map that overlays the SSE-driven
  tasks state for immediate feedback. On failure the override is removed (rollback). On
  success, the SSE `archived`/`assigned` event arrives within ~500ms and drives the real
  update. The `archived` event kind now removes the task from the fleet tasks map and
  clears the owning agent's `activeTaskId`.
- **Confirm dialogs**: `window.confirm()` for both reassign and cancel — native, no dep.
- **Cancel is guarded** from running tasks in both the backend (DB status check) and the
  frontend (button disabled when `status === "running"`, with a tooltip explaining why).

- **Two real bugs found + fixed during manual click-through verification (not caught by build/typecheck):**
  1. **"Current task" didn't show ready/unclaimed tasks.** Backend (`kanban.py`) only set
     `current_task_id` from an active run (`task_runs.ended_at IS NULL`) — a freshly created
     `ready` task has no run yet, so the panel showed "none" even though `hermes kanban list`
     confirmed the task existed and was assigned. Fix: added an "open task" query (most recent
     assigned task with `status NOT IN ('done','archived')`) so an agent reports its queued
     task even with no active run, with state `idle`/`blocked` (never a fake `working`).
     Frontend (`fleet.ts`) mirrored this: `activeTaskId` now means "owns this task" (ready or
     running); a separate `claimed` boolean gates the `working` animation, set only on
     `claimed`/`spawned`/`heartbeat`.
  2. **Reassign didn't clear the OLD agent's current task.** After reassigning, both the old
     and new agent showed it as "current" — the reducer only ever updated `prev[assignee]`
     (the new owner) on an `assigned` event, never cleared the previous owner. Fix: a
     `tookOwnership` flag set whenever an event hands a task to an agent, followed by a sweep
     clearing that `taskId` from any other agent still holding it. Confirmed via
     `hermes kanban show <id>` that the `assigned` event payload does carry the new
     assignee — the bug was purely in the frontend never reading/clearing it, not a data gap.
  **Lesson:** both bugs were invisible to `npm run build`/`tsc` and the earlier automated REST
  verify — they only surfaced from clicking through the actual UI by hand. Manual
  click-through after each change remains necessary; type-checking and curl tests alone don't
  catch state-derivation bugs in the live event reducer.

---

## Early implementation notes — verified while building

### Running the app against Hermes
- **Backend reads the DB and profiles directly**: `~` → the Linux home, so
  `kanban_db_exists:true` and `/agents` shows real profiles. If the resolved DB
  path doesn't exist, the adapter falls back to the stub (empty) — by design,
  boots without crashing.
- **venv**: `backend/.venv` (native). The **Hermes venv**
  (`~/.hermes/hermes-agent/venv`) also ships fastapi/uvicorn/httpx and works as a
  throwaway interpreter for probing.
- **`pkill -f <pat>` self-match gotcha:** `pkill -f "uvicorn app.main"` can also
  match the invoking shell (its argv contains the pattern) → it SIGTERMs its own
  shell. Use a regex char-class so the literal differs from the match, e.g.
  `pkill -f "uvicorn.app[.]main"`, or resolve a PID first.
- `hermes dashboard --stop` frequently reports exit 15 (SIGTERM) but still
  succeeds — verify by re-probing, not by exit code.

### Control layer (writes) — confirmed live on the running stack
- **Token plumbing works end-to-end.** Backend reads
  `HERMES_DASHBOARD_SESSION_TOKEN` from a gitignored repo-root `.env` (via
  `python-dotenv`); dashboard co-launched with the **same** value. `/health` shows
  `session_token_configured:true`; the token authenticates
  `GET /api/plugins/kanban/board` (200) and all writes.
- **REST write path (preferred, Bearer):** verified live —
  - spawn → `POST /api/plugins/kanban/tasks` (sets `created_by:"dashboard"`),
  - comment → `POST /api/plugins/kanban/tasks/{id}/comments`,
  - unblock → `PATCH /api/plugins/kanban/tasks/{id}` `{"status":"ready"}`
    (blocked → ready confirmed).
  Board passed as `?board=<slug>`. Bearer header only — `?token=` is WS-only.
- **CLI fallback (no token / dashboard down):** the adapter calls `hermes kanban …`
  as a **direct subprocess with an argv list** (no shell string). Verified
  `via:"cli"` when the dashboard was stopped; spawn still created the task. Note
  CLI `create` sets `created_by:"user"` (vs `"dashboard"` over REST) — cosmetic
  provenance diff.
- **Spawn → live worker** proven: app-created task `t_504cde62` was claimed by a
  worker on `hermes kanban dispatch` and ran to `completed` (gateway was down, so
  manual dispatch — same proof as a gateway pickup).
- **Gateway-presence warning** fires on spawn when no gateway is running
  (detected via `pgrep -f "gateway run"`).
- **Profile validation** before spawn: the adapter lists `~/.hermes/profiles/*`
  (+`default`); an unknown assignee is rejected `ok:false` without a write.
- `status=running` remains 400-rejected (re-confirmed by reading the source) — the
  app never sends it; spawn always uses the create→claim path.

---

## Local-first packaging
- **Single-process serving works**: `backend/app/main.py` mounts
  `StaticFiles(directory=frontend/dist, html=True)` at `/`, registered AFTER all
  API routes (`/health`, `/agents`, `/tasks`, `/act`, `/events`) so the mount
  never shadows them — Starlette matches routes in registration order. Verified
  live: `GET /` returns the built `index.html` (referencing hashed
  `/assets/index-*.js`/`.css`, not the dev `/src/main.tsx` script tag), the hashed
  asset returns 200, and `GET /agents` still returns 200 from the same process. If
  `frontend/dist` doesn't exist, the backend logs a warning naming the path and
  still boots API-only (dev mode unaffected).
- **`run.sh` is the one-command runner** (the goal was a "pipx/npx-style" one-liner —
  literal PyPI/pipx packaging was skipped as overkill for a local single-machine
  tool; this script gives the same one-command UX). It creates `backend/.venv` on
  first run (with a clear `apt install python3.X-venv` error if the venv module is
  missing), installs frontend deps on first run, builds the frontend, then execs
  uvicorn on `127.0.0.1:8123`. Verified live end to end: clean run → `/health`
  shows real `KanbanAdapter` + correct `~/.hermes/kanban.db` path +
  `session_token_configured:true` → built UI served at `/` → API still live.
- **Dockerfile provided but native `run.sh` is the recommended path** — this app's
  only job is reading/writing a local SQLite DB and a local dashboard process; a
  container needs the DB bind-mounted in and `HERMES_DASHBOARD_URL` pointed at
  `host.docker.internal` (documented in README). Not verified live (no Docker
  engine probed here) — treat the Dockerfile as a starting point, not
  load-bearing.
- **README's old safety line was stale and got corrected**: it previously said
  "the kanban REST routes are unauthenticated on localhost by design" — the
  original starting assumption; §2 above proved this false (Bearer-gated in v0.17).
  Fixed to match reality and to clarify the `0.0.0.0` warning is about the
  **Hermes dashboard** specifically (single shared-secret token, not per-user
  auth).

---

## Public demo mode with synthetic data
- **`SyntheticAdapter` (`backend/app/adapters/synthetic.py`) implements the exact
  same `DataSource` ABC as `KanbanAdapter`** (get_agents/get_tasks/subscribe/act) —
  proves the abstraction genuinely holds: **zero frontend changes** were
  needed. It also duck-types the two pull methods
  (`current_max_event_id`/`fetch_events_after`) the `/events` SSE route uses
  internally, so the route works unchanged for either source.
- **In-memory tick loop**, not a real scheduler: 4 fake profiles
  (`researcher`/`writer`/`reviewer`/`coder`), a 2s tick, per-agent
  random-probability transitions (idle→spawn, working→heartbeat or
  finish-as-done/finish-as-blocked, done/blocked→idle after a cooldown chance).
  Marked in source as a deliberate simplification (no real concurrency/contention
  modeling) — fine for a demo, not a target for fidelity work.
- **`act()` always returns `{"ok": false, "detail": "read-only public demo..."}`** —
  the actual "disable real control actions" mechanism, and it required **no
  frontend changes**: SidePanel already renders any `ok:false` response as a
  `✗ ...` result message (built for the reassign/cancel failure path earlier), so the rejection surfaces through the
  existing UI path automatically.
- **Config flag**: `HERMES_DATA_SOURCE` (`auto` default | `synthetic`), checked
  first in `main.py`'s adapter-selection chain — takes priority over Kanban-DB
  detection, so it forces the demo adapter even on a machine with a real Hermes
  install. No token, no DB path is read in this mode.
- **`main.py`'s `/events` SSE route was previously hardcoded to
  `isinstance(datasource, KanbanAdapter)`** — widened to
  `isinstance(datasource, (KanbanAdapter, SyntheticAdapter))`. The one spot where
  adding a new source touched code outside the new adapter file, and it's additive
  (a tuple check), not a UI change — consistent with the architecture rule (only
  the UI may never touch a source directly).
- **Verified live** (port 8124, `HERMES_DATA_SOURCE=synthetic`):
  - `/health` → `"adapter":"SyntheticAdapter"`.
  - `/agents` → 4 agents cycling through real `idle`/`working`/`done`/`blocked`
    states with plausible timestamps.
  - `/events` → live SSE stream, monotonic `id:`, real `created`/`claimed`/
    `spawned`/`heartbeat`/`completed`/`blocked` event kinds, keepalives between
    ticks — same wire format as the real adapter.
  - `/tasks` → real-shaped task dicts (id/title/status/assignee/priority/
    timestamps).
  - `POST /act` (spawn) → `{"ok":false,"detail":"read-only public demo — ..."}`,
    no state mutation.
- **Bounded memory**: a long-lived public demo process must not grow forever —
  capped retained events (500) and retained tasks (30, oldest-finished-first,
  never evicts an agent's currently active task).
- **Deploy target picked deliberately**: Render (Docker-based web service,
  `render.yaml` added) over Vercel. Reasoned, not assumed: this app is one
  long-lived process (in-memory simulation loop + SSE stream); Vercel's serverless
  functions are stateless and short-lived, which breaks both. Not deployed against
  an actual Render account here (no API token) — the Dockerfile build +
  `HERMES_DATA_SOURCE=synthetic` boot path is what's verified, which is what Render
  would run.
- **This is the point the core control surface was considered feature-complete** —
  everything below this section is deeper, more exploratory follow-on work.

---

## Hook telemetry sensor and the real hook-firing matrix
### Plugin registration API (v0.17, ground-truthed against `hermes_cli/plugins.py`)
- User plugins live at `~/.hermes/plugins/<name>/` (also bundled `<repo>/plugins/<name>/`
  and project `./.hermes/plugins/<name>/`), each needing a `plugin.yaml` manifest
  **and** an `__init__.py` with `register(ctx) -> None`. `ctx.register_hook(name, cb)`
  registers a callback for one of `VALID_HOOKS` (`plugins.py:128`) — our 6 target
  hooks (`pre_llm_call`, `post_llm_call`, `pre_tool_call`, `post_tool_call`,
  `pre_approval_request`, `subagent_stop`) are all valid, real hook names.
- **Standalone plugins are opt-in.** `plugins.enabled: [...]` in `config.yaml` gates
  loading; a plugin dropped into `~/.hermes/plugins/` without also running
  `hermes plugins enable <name>` never loads (silently — `hermes plugins list`
  shows it as `not enabled`). This machine's `config.yaml` had no `plugins:` key at
  all yet (pre-migration), so the v20→v21 grandfather-migration never ran; had to
  enable explicitly.
- **`invoke_hook()` is fully synchronous, in-process** (`plugins.py:1705`,
  `cb(**kwargs)` called directly, one `try/except` per callback). A hook callback
  that blocks (e.g. a synchronous HTTP POST) blocks the real agent turn. True
  fire-and-forget requires handing off to a thread from inside the callback —
  confirmed by reading the langfuse/nemo_relay bundled plugins (`plugins/observability/`),
  the reference implementation this sensor followed.
- Reference implementations read: `plugins/observability/langfuse/__init__.py` and
  `plugins/observability/nemo_relay/__init__.py` (both hook every event we needed,
  both wrap every callback body in a `_safe()`/try-except so a broken plugin can't
  crash the host — same pattern used here).

### 🔴 MAJOR finding: plugins do NOT carry across profile homes
Kanban workers run as a separate OS process per profile (`hermes -p <assignee>
chat -q "work kanban task <id>"`, per §8 above) with its **own** `HERMES_HOME` —
`~/.hermes/profiles/<profile>/` — which has its **own** `config.yaml` and its
**own** `plugins/` directory, entirely independent from the default profile's
`~/.hermes/config.yaml` / `~/.hermes/plugins/`. A plugin installed only under the
top-level `~/.hermes/plugins/` is **invisible** to every kanban worker subprocess.
Proven empirically: a first dispatched task produced zero `hermesboard-sensor` log
lines despite 5 real tool calls and 6 LLM calls in that worker's own
`~/.hermes/profiles/researcher/logs/agent.log`. Fix verified: symlink the plugin
dir into each profile's own `plugins/` folder and
`HERMES_HOME=~/.hermes/profiles/<profile> hermes plugins enable hermesboard-sensor`
per profile — hooks then fired immediately on a re-dispatched task. **Any
hook-based sensor for kanban/swarm workers must be installed+enabled per profile,
not just once in the default home.**

### `task_id` kwarg ≠ Kanban board task id
Across every hook, `kwargs.get("task_id")` is an **internal session/turn
identifier** (e.g. `20260702_230642_18913a` or a bare UUID), never the Kanban
board id (`t_xxxxxxxx`) — confirmed by comparing sensor log lines against the
dispatched task's real id in every kanban-worker test below. `subagent_stop` and
`pre_approval_request` don't even carry a `task_id` key (their real kwargs are
`parent_session_id`/`child_*` and `session_key`/`pattern_key` respectively — see
`_DEFAULT_PAYLOADS` in `hermes_cli/hooks.py`). **A hook-based sensor cannot
correlate events back to a specific Kanban card without extra plumbing** (e.g.
reading `HERMES_KANBAN_TASK` from the worker's own env, which the sensor does not
currently do — noted for v2, not implemented here per "minimal plugin" scope).

### Firing matrix (real output, this build)
| Hook | CLI chat (`hermes chat -q`) | Gateway run — **gateway's own parent process** | Kanban worker task (child subprocess, plugin installed **per-profile**) | Swarm (`hermes kanban swarm`, 3 profiles) |
|---|---|---|---|---|
| `pre_llm_call` | ✅ fired, once/turn | ❌ never fires directly — gateway itself runs no LLM turn for kanban dispatch | ✅ fired, once/turn | ✅ fired, once/turn/role (researcher+reviewer+writer) |
| `post_llm_call` | ✅ fired, once/turn | ❌ same as above | ✅ fired, once/turn | ✅ fired, once/turn/role |
| `pre_tool_call` | ✅ fired, 1:1 with post | ❌ same as above | ✅ **fired reliably, 1:1 paired every time** (8/8 across 3 test tasks, 3–8 tool calls each) | ✅ fired reliably, 1:1 paired, all 3 roles |
| `post_tool_call` | ✅ fired, 1:1 with pre | ❌ same as above | ✅ fired reliably, 1:1 paired | ✅ fired reliably, all 3 roles |
| `pre_approval_request` | ✅ fired (dangerous-command flow; no TTY → 60s fail-closed timeout → denied, command never ran) | ❌ not exercised (no messaging platform configured; gateway's own process never runs a tool call for kanban dispatch) | ✅ fired (isolated test task, same fail-closed 60s deny) | not exercised — swarm goal had no dangerous command; independently proven capable in the same subprocess type above |
| `subagent_stop` | ✅ fired (delegated a subagent via `delegate_task`) | ❌ not exercised, same reason | ✅ fired (isolated test task delegated a subagent) | not exercised — swarm goal had no delegation; independently proven capable above |

**Key takeaway on gateway run:** the gateway process itself never fires any of
these 6 hooks directly for Kanban dispatch — it only spawns the same
`hermes -p <profile> chat -q "work kanban task <id>"` child used by manual
`hermes kanban dispatch` (confirmed via `ps` — worker `PPID` = gateway PID). So
**"gateway run" and "kanban worker task" are the same runtime context** for these
hooks; the gateway just automates the spawn trigger. A live messaging-platform
conversation (Discord/Telegram/etc., not set up here — needs real bot credentials)
is the only path that would run a turn *inside* the gateway's own process; not
tested here.

**Key takeaway on swarm:** `hermes kanban swarm` is built entirely on the same
Kanban worker-task primitive across dependent cards (worker → verifier →
synthesizer, each a separate profile subprocess) — no distinct hook-firing
mechanism of its own. Once each profile has the plugin installed, all 3 roles fire
hooks identically to a plain kanban worker task.

**#25204 re-examined:** not reproduced in this build. Every `pre_tool_call` fired
exactly 1:1 with its `post_tool_call` in every context tested, including 8
back-to-back tool calls in a single kanban worker turn. The original #25204 note
almost certainly refers to the **separate shell-hook subsystem** (`hooks:` block in
`config.yaml`, `hermes_cli/hooks.py` / `agent/shell_hooks.py` — external scripts
invoked via stdin/stdout JSON), not the in-process Python-plugin `register_hook`
API exercised here — two different hook mechanisms sharing the same event names.
Not confirmed against the shell-hook path in this spike (out of scope: the task
asked for a Python plugin, not a shell hook).

### `/ingest` backend endpoint
`POST /ingest` added to `backend/app/main.py` (before the static-file mount, no
other route touched): accepts the plugin's JSON envelope, logs it via `log.info`,
returns `{"ok": true}`. No persistence, no effect on `datasource` state — verified
live with `curl` and with the plugin's real `_send()` path (both hit 200).

### Plugin source
`~/.hermes/plugins/hermesboard-sensor/` — `plugin.yaml` + `__init__.py`. Fire-and-
forget: each hook callback logs locally then hands the envelope
(`{hook, task_id, session_id, profile, ts}`) to a daemon `threading.Thread` that
POSTs to `http://127.0.0.1:8123/ingest` via stdlib `urllib.request` (2s timeout, no
new dependency). All exceptions caught in the thread; a single warning is logged
the first time `/ingest` is unreachable (module-level flag, not re-warned every
call) and the agent loop is never blocked or slowed — the callback itself returns
in microseconds regardless of backend health.

---

## State-derivation engine, ground-truthed against real hook payloads

### 🔴 MAJOR finding: the per-profile plugin install was silently dead
The `researcher`/`writer`/`reviewer` profile plugin symlinks
(`~/.hermes/profiles/<profile>/plugins/hermesboard-sensor`) were dangling —
pointing at a stale absolute path left over from an earlier machine/home
migration. `test -e` on them failed. Confirmed with `HERMES_HOME=~/.hermes/
profiles/researcher hermes plugins list` — `hermesboard-sensor` didn't even appear
(a broken symlink makes the plugin directory unreadable, so discovery skips it
rather than erroring). **Every Kanban worker's hook telemetry had been silently
dead since the migration.** Fixed by repointing each symlink at the correct
absolute path and re-running `hermes plugins enable hermesboard-sensor` per
profile home. **Lesson: a per-profile plugin symlink is a portability landmine
across any home/user migration — worth a health check, not just a one-time
verification.**

### Real hook kwargs confirmed by reading Hermes source (not guessed)
Grepped the installed Hermes package directly rather than trusting the plugin
test harness's synthetic payloads at face value (cross-checked against them too —
they matched):
- `pre_tool_call`/`post_tool_call` (`model_tools.py`): kwarg is `tool_name`
  (verbatim), plus `args`, `result`, `task_id`, `session_id`, `tool_call_id`,
  `duration_ms`. The sensor plugin initially only forwarded `task_id`/`session_id`
  — this pass added `tool_name` since fine-state tool-family sub-labelling is
  impossible without it.
- `pre_approval_request` (`tools/approval.py` `_fire_approval_hook` call site):
  real kwargs are `command`, `description`, `pattern_key`, `pattern_keys`,
  `session_key`, `surface` — confirms DISCOVERY's earlier note that this hook
  carries **no** `task_id`/`session_id`. Sensor now forwards `pattern_key` +
  `session_key` (not the raw `command` text — avoids piping potentially sensitive
  shell commands through telemetry for a feature that doesn't need it).
- `subagent_stop` (`hermes_cli/hooks.py` `_DEFAULT_PAYLOADS`, cross-checked against
  `tools/delegate_tool.py` call sites): `parent_session_id`, `child_role`,
  `child_summary`, `child_status`, `duration_ms`. Sensor forwards
  `parent_session_id`/`child_role`/`child_status`.
- Real tool names in this Hermes install (grepped `tools/*.py` `name="..."`
  registrations): `terminal`, `execute_code`, `read_terminal`, `close_terminal`,
  `process` (→ `coding`); `write_file`, `patch` (→ `writing`); `web_search`,
  `web_extract`, `x_search`, `session_search`, `computer_use`, `browser_*` (→
  `researching`); `read_file`, `search_files` (→ `reading`). Everything else
  (`kanban_*`, `delegate_task`, `memory`, `todo`, `clarify`, …) falls back to a
  generic `acting` label rather than guessing a family for tools that don't fit
  the coding/writing/researching/reading taxonomy.

### Real recorded sequence (dispatched live, not synthesized)
Dispatched `hermes kanban create ... --assignee researcher` + `hermes kanban
dispatch` with the (now-fixed) sensor enabled, captured every `/ingest` POST to a
JSONL file. One real worker turn produced 20 hook events —
`pre_llm_call → [pre/post_tool_call]×9 → post_llm_call` — covering `kanban_show`,
`terminal`, two **overlapping** `search_files` calls (second `pre_tool_call` fired
before the first `post_tool_call` resolved — a genuine concurrency edge case, not
hypothetical), `read_file`×3, `write_file`, `kanban_complete`. Saved verbatim as
`backend/tests/fixtures/researcher_task_t_dfaa4d52.jsonl` (+ a second, shorter
`writer_task_t_029dcf76.jsonl` fixture) and replayed through the reducer in
`backend/tests/test_state_engine.py` — real recorded sequences used to ground the
state-derivation logic, not synthesized fixtures. (A live dangerous-command approval prompt
and a live `delegate_task` round-trip were **not** captured the same way — forcing
those just for a fixture wasn't worth the runtime cost; those two hooks are tested
with hand-built envelopes shaped to the confirmed real kwargs above instead.)

### Design: coarse state always wins outside "working"
`state_engine.merge_fine_state(coarse_state, activity, now)` treats the Kanban
`task_events`-derived coarse state (`idle`/`blocked`/`done`/`error`) as
authoritative outside the working phase — hooks never signal a crash directly, so
a stale "thinking" from hook telemetry must not outlive the run that produced it.
Inside `working`, hook data is authoritative but only if fresh
(`STALE_AFTER_SECONDS = 120`); past that, or with no hook data at all (plugin not
installed for that profile), it falls back to a bare `working` with no `activity`
label — the UI never shows a blank or a stale lie.

### Live wiring: SSE, not polling
`/events` already tails `task_events` on a 0.5s loop; this step piggybacks
hook-derived state changes on the same connection as synthetic, id-less frames
(`{"kind":"activity","payload":{assignee,fine_state,activity}}`), deduped per
profile by `AgentActivity.updated_at` so an unchanged snapshot is never
re-sent. Verified live end-to-end with a real dispatched task: the SSE stream
emitted `thinking → working:coding → idle` within seconds of real hook firings,
and a headless-Chromium (Playwright) drive of the actual running app confirmed
the side panel rendering `WORKING` + a live `reading` label during a real
research/read/write task, with zero console errors.

---

## Sub-agent visualization, ground-truthed against real `delegate_task` runs

### Design: no `subagent_start` hook exists, so `pre_tool_call(delegate_task)` is the proxy
Of the 6 hooks the sensor plugin registers, only `subagent_stop` fires when a delegation
*finishes* — there is no `subagent_start`/`subagent_spawn` hook to say one has *begun*.
`state_engine.reduce_subagent()` (pure, mirrors `reduce_activity`'s shape) treats
`pre_tool_call` with `tool_name=="delegate_task"` as the only available "a delegation just
began" signal, producing a provisional `SubagentInfo(role=None, status="active")`;
`subagent_stop` then resolves the real `child_role`/`child_status` and closes the episode.
A `post_tool_call(delegate_task)` fallback closer guards against a missed `subagent_stop`
(confirmed necessary live — see below). Depth-1 flat only (a deliberate scope limit):
`HookEventStore` keeps at most one `SubagentInfo` per profile; a new delegation simply
replaces the old one.

### Ephemeral display: a pure `subagent_visible(info, now)` predicate, same idiom as the existing done-flash
Two failure modes bounded by wall-clock checks, not unbounded history: (a) an **active**
episode whose closing hook never arrives is only believed for `STALE_AFTER_SECONDS` (120s,
reused from the fine-state crash guard) before the sprite is dropped; (b) a **finished** episode's
sprite flashes for `SUBAGENT_VISIBLE_AFTER_STOP_SECONDS` (8s) then disappears — subagents are
ephemeral summaries, not board rows, so there is deliberately no persisted history. The
frontend (`fleet.ts`'s `subagentVisible()`) mirrors both budgets independently rather than
waiting on a backend "clear" push, exactly like `visibleState()`'s existing done-cooldown —
consistent with the pattern the codebase already uses, not a new one.

### `GET /api/sessions` enrichment — wired for real, but empirically a no-op for kanban workers
`KanbanAdapter.find_active_child_session()` calls the Bearer-gated `GET /api/sessions` (no
dedicated `parent_session_id` filter param exists — see §6 above — so it's filtered
client-side over the most recent rows) to attach a nicer `title`/`preview` on top of the bare
`child_role`, duck-typed and cached per delegation episode so it never hits the dashboard more
than once per episode. **Empirical finding:** across two live `delegate_task` round-trips
dispatched via `hermes kanban` on this machine, `GET /api/sessions?limit=50&order=recent`
never showed a single row with a non-null `parent_session_id` — Kanban-worker-spawned
sub-agent sessions (via `delegate_task` inside a `hermes -p researcher chat -q "work kanban
task <id>"` subprocess) don't appear to register a session row with that field populated,
unlike what the field's presence in the schema implies is possible for other flows (the
sessions the field is presumably meant for — e.g. an interactive delegate round-trip in a
live CLI/gateway conversation — were never exercised here). The enrichment code is real,
correct, and never breaks anything on a miss (falls back to the bare hook-derived role/title
`None`), but in this kanban-worker context it always resolved to `None` — the sprite
label came from `child_role` (or the generic "sub-agent" fallback) every time in practice, not
from a session title. Worth re-checking if a future pass exercises delegation via a live
messaging-platform conversation instead of a kanban worker.

### Real captured payloads (live, not synthesized) — and a real live gotcha
Dispatched two isolated `hermes kanban create ... --assignee researcher` tasks explicitly
instructing `delegate_task` use, `hermes kanban dispatch`'d, sensor enabled per-profile.
Real envelopes captured through `/ingest`:
```
pre_tool_call  tool_name=delegate_task           -> SubagentInfo(active, role=None)
post_tool_call tool_name=delegate_task  (+0.26s)  -> closed (subagent_stop never arrived for
                                                      this specific call — the fallback closer
                                                      this feature was built for, hit for real)
...
subagent_stop  child_role="leaf" child_status="interrupted"  -> a second, real delegation
                                                      episode, resolved with the REAL child
                                                      role name Hermes assigns ("leaf", not the
                                                      custom "fact-checker" name requested in
                                                      the test prompt — role naming is Hermes's,
                                                      not the caller's) and a real non-"completed"
                                                      status.
```
**Non-hypothetical gotcha:** both live test tasks' worker processes died mid-run without the
Kanban `task_runs` row ever closing (`ended_at` stayed `NULL`, `status` stuck at `running`,
`worker_pid` no longer a live process) — most likely the same fail-closed dangerous-command/
approval-timeout path documented earlier in the hook-firing matrix (no TTY in this environment
→ 60s deny), tripped by whatever the delegated sub-agent attempted. Both were cleaned up with
`hermes kanban reclaim <id> --reason ...` then `hermes kanban archive <id>` (same CLI fallback
path as every other verify in this project) rather than left stuck on the board. This is an
environment property of dispatching real dangerous-adjacent delegation prompts here, not a bug
in this feature's code — but it's why the `post_tool_call` fallback closer and the 120s
stale-active guard both mattered in practice, not just in the synthetic unit tests.

### Live wiring: `subagent` SSE frame, same synthetic id-less-frame technique as the `activity` frame
`/events` now also emits `{"kind":"subagent","payload":{assignee,role,status,active,
started_at,ended_at,title}}` whenever a profile's `SubagentInfo` changes (deduped by
`(started_at, ended_at, status)`), with **no** staleness re-check in the SSE loop itself —
deliberately mirroring the pre-existing `activity` frame, which also pushes raw hook state
without re-applying `merge_fine_state`'s staleness filter over SSE (that filter only applies
in `get_agents()`). Kept consistent with that existing precedent rather than inventing a
stricter scheme just for subagents.

### Live browser verification (headless Chromium via Playwright)
`playwright` wasn't installed in either venv at the start of this step (had to `pip install
playwright && python -m playwright install chromium`, which needed the non-`--with-deps` form
since system-package installation needs `sudo` and there's no non-interactive password here).
Once installed: drove the actual running app (`vite` dev server + the live backend), dispatched
a real delegation task, and captured a screenshot showing the researcher character with its
usual task-chip edge **plus** a second linked, pulsing "sub-agent" chip stacked below it in the
same flex column, and the side panel showing "↳ delegated: sub-agent (active)" — zero console
errors. Confirms the whole path end-to-end: real hook firing → `HookEventStore` →
`reduce_subagent` → SSE `subagent` frame → `fleet.ts` → `Character.tsx`/`SidePanel.tsx`.

### Files touched
`backend/app/state_engine.py` (`SubagentInfo`, `reduce_subagent`, `subagent_visible`,
`SUBAGENT_VISIBLE_AFTER_STOP_SECONDS`), `backend/app/hook_store.py` (subagent tracking +
accessors), `backend/app/adapters/hook_enriched.py` (`subagent` field on `get_agents()`,
`subagent_view()`, best-effort session-title lookup), `backend/app/adapters/kanban.py`
(`find_active_child_session()`, duck-typed, not part of the `DataSource` interface — same
treatment as `/workers/active` earlier), `backend/app/main.py` (`subagent` SSE frame),
`backend/tests/test_state_engine.py` (12 new unit tests, all passing), `frontend/src/fleet.ts`
(`SubagentView`, `subagentVisible()`, `SUBAGENT_COOLDOWN_MS`, SSE handling), `frontend/src/
components/Character.tsx` and `SidePanel.tsx` (rendering only — `fleet.ts`'s state logic and
existing `state-*` CSS classes untouched), `frontend/src/styles.css` (new `.subagent-*` classes
only, nothing renamed/removed).

---

## Embodied approvals, ground-truthed against real source + two live round-trips

### 🔴 MAJOR correction: the stock dashboard has NO shell-exec approvals queue REST API
The initial assumption was that one exists ("the stock dashboard has a shell-exec
approvals queue — confirm its exact API shape first"). Grepped every `approval` hit in `hermes_cli/
web_server.py` before writing any code: none of them are a dangerous-command queue —
they're the aux-LLM "smart approval" model-routing slot, the unrelated shell-hooks
consent subsystem, OAuth `pending_approval` polling states, and a macOS TCC-permission
dialog route. **Zero real hits.**

The real resolution point is `tools/approval.py`'s module-level
`resolve_gateway_approval(session_key, choice, resolve_all=False)` — private (no
leading underscore on the name, but not exported/documented anywhere as a plugin API;
lives alongside underscore-prefixed internals like `_gateway_queues`/`_ApprovalEntry`).
It's reachable via exactly three transports, none of which fit our Kanban-worker
architecture:
1. Chat-platform `/approve`/`/deny` slash commands (`gateway/slash_commands.py`) — needs
   a configured messaging platform (Discord/Telegram/etc.), none set up here.
2. `approval.respond` JSON-RPC over `tui_gateway` — reachable from the dashboard's
   `/api/ws` WebSocket (ticket/`?token=`-gated, same session token as our REST Bearer
   on loopback), but that bridges to the **dashboard's own embedded chat session**, a
   *different OS process* from any Kanban worker (`hermes -p researcher chat -q ...`).
   `_gateway_queues` is a plain module-level dict — process-local memory — so the
   dashboard process has zero visibility into a worker subprocess's pending approval.
3. `POST /v1/runs/{run_id}/approval` on the opt-in `api_server` gateway platform
   (separate port 8642, separate `API_SERVER_KEY` Bearer secret) — only covers runs
   started via that same server's own `POST /v1/runs`, not arbitrary kanban-dispatched
   workers.

**Kanban workers themselves have no remote-resolution path in this Hermes version at
all**, confirmed by reading `check_all_command_guards` (the function `terminal_tool.py`
actually calls — `check_dangerous_command`, the simpler non-blocking function, is
dead code only referenced from tests): with neither `HERMES_GATEWAY_SESSION`/a bound
gateway platform contextvar (`is_gateway`) nor `HERMES_EXEC_ASK` set, it falls straight
into the CLI-interactive branch — `prompt_dangerous_approval()`'s blocking `input()` on
a daemon thread, `timeout_seconds` from `approvals.timeout` (default 60s), denies with
no possible remote intervention if the thread never sees input. This is the exact
mechanism observed earlier in the hook-firing matrix ("no TTY → 60s fail-closed timeout → denied").

### Design chosen (after presenting the tradeoff to the user)
Three options were surfaced: (A) have `hermesboard-sensor` bridge Hermes's private
gateway-queue mechanism directly; (B) visualize only, label approve/deny
"unsupported for Kanban workers"; (C) wire real approve/deny only against the
unrelated opt-in `api_server` platform. **User chose (A), hardened**: verify the
private module path exists before calling it (never import speculatively), catch
`ImportError`/`AttributeError` separately, log the exact call path + Hermes version
for a "check on upgrade" note, document the extended wait window in the README, and
grey out expired approve/deny buttons in the UI.

### Real mechanism, confirmed by reading `tools/approval.py` directly (not guessed)
- `check_all_command_guards(command, env_type, ...)` computes
  `is_gateway = _is_gateway_approval_context()` and
  `is_ask = env_var_enabled("HERMES_EXEC_ASK")`; `if is_gateway or is_ask:` branches
  into the queue-based path instead of the CLI prompt. Setting `HERMES_EXEC_ASK=1`
  from inside our own plugin (same process, no Hermes core patch) is enough — verified
  the env var is read live at check-time via `env_var_enabled()`, not cached at
  import (`utils.py`'s `is_truthy_value`/`env_var_enabled`, plain `os.getenv`).
- `_await_gateway_decision(session_key, notify_cb, approval_data, surface=...)`
  enqueues a `_ApprovalEntry` (a `threading.Event` + data) onto
  `_gateway_queues[session_key]`, fires the real `pre_approval_request` hook
  (kwargs confirmed: `command, description, pattern_key, pattern_keys, session_key,
  surface`), then calls `notify_cb(approval_data)` and blocks on
  `entry.event.wait()` in ≤1s slices (heartbeating the whole time) up to
  `approvals.gateway_timeout` (config key, **default 300s** — confirmed by reading
  `_get_approval_config().get("gateway_timeout", 300)` directly), checking
  `is_interrupted()` each iteration so `/stop` still cancels cleanly. Fires
  `post_approval_response` (kwargs: same, plus `choice`) on the way out regardless
  of outcome (resolved/denied/timeout).
- `resolve_gateway_approval(session_key, choice)` is the ONLY way to `.set()` that
  Event from outside the blocked thread — pops the oldest `_ApprovalEntry` for that
  session_key (FIFO; `resolve_all=True` clears every pending one), sets
  `entry.result = choice`, fires the Event. No-ops harmlessly (`return 0`) if
  nothing is queued for that key.
- **Critical, non-obvious finding, confirmed by looking at the actual caller
  (`check_all_command_guards` line ~2444), not just `_await_gateway_decision`'s
  docstring:** the caller resolves `notify_cb = _gateway_notify_cbs.get(session_key)`
  itself and only calls `_await_gateway_decision` **if `notify_cb is not None`** —
  otherwise it takes an entirely different, non-blocking fallback (`submit_pending()`
  + immediate `status: "pending_approval"` return, **no hook fires at all**). This
  means `register_gateway_notify(session_key, cb)` must already hold an entry for the
  exact `session_key` the guard resolves at check-time, or the whole bridge — and
  even the `pre_approval_request` hook telemetry itself — silently no-ops. This is
  exactly the failure mode bug #1 below hit.

### 🔴 Real bug #1 (caught live, not a design guess): `get_current_session_key()` is not stable across a process's lifetime
`register(ctx)` calling `tools.approval.get_current_session_key()` once at plugin-load
time returned `"default"` (no session context bound yet, falls through to
`get_session_env("HERMES_SESSION_KEY", "default")`'s literal default — confirmed no
kanban-worker code path sets `HERMES_SESSION_KEY`, only `tui_gateway/slash_worker.py`,
ACP, and `gateway/run.py`'s own session loop do). Registering
`register_gateway_notify("default", cb)` at that moment looked correct and logged
"approval bridge enabled" — but the FIRST two live dispatch tests both fell into the
non-blocking `pending_approval` fallback with **zero** `pre_approval_request` hook
firings, meaning `notify_cb` was `None` at the real check.

Root-caused with a temporary diagnostic log inside the (always-fires) `pre_tool_call`
handler, printing `tools.approval`'s module `id()`, the live `get_current_session_key()`
value, and `_gateway_notify_cbs`'s contents at that exact moment — captured directly
from `~/.hermes/profiles/researcher/logs/agent.log`:
```
DIAG pre_tool_call terminal: approval module id=134658347862592
  session_key=20260705_183816_3f3aec
  notify_cbs={'default': <function _notify_approval_pending at 0x7a7896de42c0>}
  our_module_id=134658347862592 our_session_key=default
```
Same module object (`id` matches — ruled out a duplicate-import theory), our
registration really was present under `'default'` — but the guard resolved
`session_key = get_current_session_key()` fresh at check-time and got the turn's own
session id (`20260705_183816_3f3aec`) instead, a value that only exists once the
conversation turn is under way. **Fix:** re-resolve and re-register
(`register_gateway_notify(current_key, cb)`) at the top of *every* hook firing, not
once at load — a cheap, idempotent dict write that self-corrects regardless of
exactly when/why the resolved key changes. Removed the diagnostic once the live retest
confirmed a real `pre_approval_request` firing and a resolvable queue entry.

### 🟠 Real bug #2 (caught live): a "dangerous" pattern was already permanently allowlisted from earlier testing
The first two live test commands (`rm -rf /tmp/hermesboard_step12_test_dir`, absolute
path) never triggered ANY approval — not even the old 60s CLI-prompt path. Traced via
`detect_dangerous_command()` directly (confirmed it DOES flag the command, pattern_key
`"delete in root path"`) to `~/.hermes/profiles/researcher/config.yaml`'s
`command_allowlist: [script execution via -e/-c flag, delete in root path]` — a
permanent allowlist entry persisted from this same probing effort's own earlier live
dangerous-command testing (an earlier "always" choice). `is_approved()` checks this allowlist before
`is_gateway`/`is_ask` is ever consulted, so it always short-circuited straight to
`{"approved": True}`. Fixed by testing against `"recursive delete"` (triggered via a
relative path, `cd /tmp && rm -rf <name>`, which doesn't match the `"delete in root
path"` regex `\brm\s+(-[^\s]*\s+)*/`) — confirmed fresh/unapproved via
`detect_dangerous_command()` and empty session/permanent-approval state.

### Live end-to-end proof (real Hermes worker, both outcomes)
Two full round trips, each: `hermes kanban create` (explicit relative-path `rm -rf`
instruction) → `hermes kanban dispatch` → polled `GET /agents` until
`approval: {pattern_key: "recursive delete", ...}` appeared (real, ~15-20s after
dispatch) → resolved via the ACTUAL running app:
- **Approve** (via direct `POST /act {agentId:"researcher",action:"approve"}` —
  `{"ok":true,"via":"hook","choice":"once"}`): the blocked worker thread resumed:
  its own completion summary confirmed "the dangerous-command approval prompt
  triggered (recursive delete), was approved by the user, and the command completed
  ... exit code 0". `agent["approval"]` correctly returned to `None` shortly after
  (ephemeral flash window elapsed).
- **Deny** (via headless-Chromium click on the real running UI's "Deny" button, not
  curl): the panel showed a live 294s-and-counting countdown before the click, then
  `action-result` → `✓ deny`, then a live SSE-pushed `approval-outcome` → `✕ deny`
  within ~1s, matching a red ✕ badge on the character. The worker's own completion
  summary independently confirmed: "the shell returned BLOCKED... The command did not
  execute; the no-op never ran."
Screenshots captured at each stage (pending badge on the room floor, side panel with
the countdown, side panel after the click, side panel showing the resolved flash) —
zero console errors throughout. Both stray/orphaned test tasks (mirroring the
crash-during-approval-flow pattern seen in earlier delegation testing) and completed
ones were reclaimed/archived; default board left empty.

### Files touched
`backend/app/state_engine.py` (`PendingApproval`, `reduce_approval`, `approval_visible`,
`APPROVAL_TIMEOUT_SECONDS`, `APPROVAL_RESOLVED_FLASH_SECONDS`), `backend/app/
hook_store.py` (approval tracking + `set_decision`/`pop_decision` one-shot channel),
`backend/app/adapters/hook_enriched.py` (`approval` field on `get_agents()`,
`approval_view()`, `act()` handles `approve`/`deny` directly — Hermes-process-level
state, not a Kanban board verb, so NOT delegated to the wrapped KanbanAdapter),
`backend/app/main.py` (`GET /approvals/{profile}/poll`, `approval` SSE frame),
`backend/tests/test_state_engine.py` (13 new unit tests, all passing — 35 total),
`~/.hermes/plugins/hermesboard-sensor/__init__.py` + `plugin.yaml` (the approval
bridge described above, `post_approval_response` added as a 7th registered hook;
lives outside the repo, same as earlier hook-plugin work — this document records it,
nothing under version control mirrors it), `frontend/src/fleet.ts` (`ApprovalView`,
`approvalVisible()`, SSE handling), `frontend/src/components/Character.tsx` (raised-hand
`.approval-badge`, resolved ✓/✕ flash) and `SidePanel.tsx` (approval panel: description,
live countdown, Approve/Deny buttons disabled + labelled "Expired" past
`expires_at`), `frontend/src/styles.css` (new `--approval` colour + `.approval-*`
classes only — no existing `state-*` class touched), `README.md` (new "Embodied
approvals" section documenting the extended wait-window side effect).

### Known limitation, stated plainly
This bridges a **private** Hermes internal (`tools.approval.resolve_gateway_approval`,
`register_gateway_notify`, `get_current_session_key` — none are documented/exported
plugin APIs). The guard code (ImportError/AttributeError caught separately, `hasattr`
capability check before use) means a future Hermes upgrade that renames or removes
these degrades this plugin to telemetry-only — the existing `pre_approval_request`
hook still fires and the UI still shows "awaiting approval", it just can't be resolved
from the control surface anymore until the bridge code is re-grounded against the new
internals. By this point in the probing period the install had drifted to `v0.18.0`
(confirmed via `hermes --version`) — see the version note at the top of this
document; a reminder to re-confirm the version at the start of any future probing
work rather than trusting an old note.

---

## Independent profiles and worker termination, ground-truthed before building

### 1. `POST /runs/{run_id}/terminate` — exact signature (quoted from `plugin_api.py:1503-1552`)
```python
class TerminateRunBody(BaseModel):
    reason: Optional[str] = None

@router.post("/runs/{run_id}/terminate")
def terminate_run_endpoint(run_id: int, payload: TerminateRunBody, board: Optional[str] = Query(None)):
    ...
```
Full route: `POST /api/plugins/kanban/runs/{run_id}/terminate` (`run_id` is an **int**, not a
task id string), Bearer-gated like every other kanban route, optional `?board=`. Resolves
`run_id` → `task_id` via `kanban_db.get_run()`, then calls **the exact same**
`kanban_db.reclaim_task(conn, task_id, reason=...)` that `POST /tasks/{id}/reclaim` uses —
so "terminate a run" and "reclaim a task" are the same underlying mechanism, just addressed
differently. Responses: 200 `{"ok":true,"run_id":...,"task_id":...}`; 404
`run {run_id} not found`; 409 `run {run_id} already ended` or `task {task_id} is no longer
in a reclaimable state`. `reclaim_task()` → `_terminate_reclaimed_worker()`
(`kanban_db.py:5915`) is genuinely best-effort and **host-local only**: it checks the claim
lock's host prefix and no-ops (`host_local: False`) if the claim wasn't made on this same
machine, then does SIGTERM (and SIGKILL if needed) via `os.kill`.

### 2. 🔴 MAJOR correction: `GET /api/sessions` cannot answer "what's active across all profiles"
The initial assumption was that `GET /api/sessions` was the right polling target with a
`profile` field to dedup on. Read `hermes_cli/web_server.py:3580-3666` directly: `profile` is **only** added
to each row (`s["profile"] = profile_name`) when the caller explicitly passes `?profile=`
in the query — the response never carries a profile identity on its own, and without that
param the endpoint opens **only the default profile's own session store**
(`_open_session_db_for_profile(profile)`). Confirmed live: `curl .../api/sessions` returned
4 rows, none with a `profile` key at all.

The real endpoint is **`GET /api/profiles/sessions?profile=all`**
(`web_server.py:3669-3785`), explicitly documented in its own docstring as "Unified,
read-only session list aggregated across ALL profiles... opens each profile's `state.db`
directly from disk... does NOT spawn a dashboard backend per profile." Every row is tagged
`profile: name` and `is_active` (same `ended_at is None and now - last_active < 300`
formula as `/api/sessions`). Confirmed live: `?profile=all` returned 82 sessions across
`default`, `autonomous-builder`, `researcher`, `reviewer`, `writer` with real
`profile_totals`. Same Bearer gate as everything else (401 with no token, confirmed live).

### 3. 🔴 Second correction: no `source` value distinguishes a Kanban worker from a plain CLI session
The initial assumption was filtering on `source != "kanban_worker"` (or similar). Real live data: **every**
Kanban-dispatched worker session (`hermes -p researcher chat -q "work kanban task <id>"`)
reports `source: "cli"` — the exact same value a genuinely independent interactive/scripted
CLI session reports. There is no separate source tag for kanban workers at all. The real,
confirmed-by-data signal is **`cwd`**: every kanban worker's session `cwd` is
`~/.hermes/kanban/workspaces/<task_id>` (visible directly in the 82-row dump — every
`researcher`/`writer`/`reviewer` row's cwd matched this pattern; the `autonomous-builder`
rows, genuinely independent, had `cwd: null` — via Discord — or a real working directory
when driven from a plain terminal). `SessionsAdapter` excludes any session whose `cwd`
starts with the resolved `~/.hermes/kanban/workspaces` prefix, derived portably from
`config.kanban_db_path` rather than hardcoded.

### Design: `run_id` sourced from `task_runs`, kept live over SSE, never from the sessions row
`KanbanAdapter._get_agents_sync()`'s existing `task_runs` query now also selects `r.id AS
run_id`; exposed as `None` once the run has ended (`r["ended_at"] is not None`) so a stale
run_id can never be sent to `/terminate` for an attempt that's already over. Because
`task_events.run_id` (confirmed in the schema, DISCOVERY ss1) mirrors `task_runs.id` for the
same attempt, the frontend (`fleet.ts`) also updates `runId` live from every `claimed`/
`spawned`/`heartbeat` SSE event and clears it on any terminal/release/archive kind — so the
"Terminate worker" button is never stale by the time it's clicked, without a single extra
REST round-trip.

### Live end-to-end proof — terminate (real OS process, not a mock)
Dispatched `hermes kanban create "... sleep 180 ..." --assignee researcher --max-runtime
300`, dispatched it, polled `GET /agents` until `run_id` appeared (36), confirmed the real
worker PID (34411) was alive via `ps`, then `POST /act {agentId:"researcher",
action:"terminate", payload:{run_id:36}}` → `{"ok":true,"via":"rest","run_id":36,
"task_id":"t_a27d9d53"}`. Re-checked `ps -p 34411` → gone. `hermes kanban show` confirmed a
real `reclaimed` event: `{"manual":true,"reason":"Step 13 live verify","terminated":true,
"sigkill":false,"host_local":true}` — SIGTERM alone was sufficient this time — and the task
returned to `ready` (redispatchable), matching the documented "resets to ready" behavior,
not a hard failure.

### Live end-to-end proof — independent profiles (real Hermes session, not synthesized)
Launched `hermes -p autonomous-builder chat -q "... sleep 90 ..."` in the background — a
genuine one-off CLI session for a profile that is NOT a Kanban board assignee. Polled
`GET /agents` until it appeared: `{"id":"autonomous-builder","independent":true,
"session_id":"20260705_191031_073d0b","source":"cli","cwd":".../hermes-agent-control-surface
/frontend","preview":"Use the terminal tool to run: sleep 90...","estimated_cost_usd":null}`
— correctly surfaced alongside the unaffected 4-agent Kanban fleet. Drove the actual running
app with headless Chromium: the character rendered as a genuine 5th card with an
"independent · cli" tag, side panel showed "INDEPENDENT SESSION" with real
source/last-active/cwd/preview/cost, "CURRENT TASK: none", and the Kanban action buttons
degrading gracefully with no special-casing needed (Spawn stays enabled — spawning a task
onto an independent profile is still meaningful; Comment/Unblock/Cancel/Terminate all
disabled since there's no task/run to target) — zero console errors.

### 🟡 Live caveat found (not hypothetical): `is_active` is a recency heuristic, not real liveness
After killing the test `autonomous-builder` process (`kill <pid>`, confirmed gone via `ps`),
`GET /agents` **still** showed it as `independent`/`working` for a while afterward. Traced
directly: the killed session's row has `ended_at: None` (a SIGTERM'd process never got to
write a clean end-of-session record — same category of dangling state as a crashed Kanban
worker's `task_runs` row from Steps 11-12) and Hermes's own `is_active` formula is purely
`now - last_active < 300`, with no actual process-liveness check. So a killed/crashed
independent session can appear "active" for up to 5 minutes after it's genuinely gone. This
is Hermes's own heuristic, not something `SessionsAdapter` can correct without an OS-level
PID check it has no reliable way to perform for an arbitrary session row (unlike Kanban
`task_runs`, which does carry a `worker_pid`). Documented as a stated v1 limitation, matching
this project's "point-in-time, best-effort" framing for anything not driven by
`task_events`.

### Files touched
`backend/app/adapters/kanban.py` (`run_id` in the `task_runs` SQL query + `_agent_from_run`/
`_idle_agent`, `terminate` action + `_terminate()`/`_task_id_for_run()`), `backend/app/
adapters/sessions.py` (new — `SessionsAdapter`, cwd-based independent-session filtering,
duck-typed `subagent_view`/`approval_view` passthrough so Steps 11-12 keep working
unchanged), `backend/app/main.py` (wiring `SessionsAdapter` as the outermost layer;
widened 3 `isinstance(datasource, HookEnrichedAdapter)` checks to
`(HookEnrichedAdapter, SessionsAdapter)`, same additive-tuple pattern already
established for `SyntheticAdapter`), `frontend/src/fleet.ts` (`runId` live-tracked from
`task_events.run_id`; `independent`/`source`/`cwd`/`lastActiveAt`/`preview`/
`estimatedCostUsd` fields; `visibleState()` treats `independent` as always "working"),
`frontend/src/components/Character.tsx` (`.independent-tag`), `SidePanel.tsx`
("Independent session" info block; "Terminate worker" button gated on `runId`, confirm
dialog, explicit best-effort copy; fixed a stale "kill worker is v2" tooltip now that it's
shipped), `frontend/src/styles.css` (new `.independent-*` classes only).

---

## PixiJS visual overhaul, ground-truthed against the actual sprite sheet

### The sprite sheet grid is NOT "x=175, 3 idle + 6 walk columns" — measured directly
The initial description was approximate ("Robots begin at approximately x=175... IDLE
frames are the first 3 columns, WALK frames are the next 6 columns"). Rather than take
that description on faith, the asset was inspected with a pixel-content scan (PIL, scanning for opaque
columns/rows per band) before writing any slicing code:
- **9 robot rows**, each occupying a clean **16×32px cell** (row tops at y = 16, 48, 80,
  112, 144, 176, 208, 240, 272 — every measured character's bounding box fits inside its
  32px band with room to spare).
- **IDLE block: 4 columns**, not 3, starting at **x=210** (210, 226, 242, 258).
- **WALK block: 8 columns**, not 6, starting at **x=290** (290, 306, ..., 402).
- **Row 3 (a 6-legged "spider" robot) only has 4 populated WALK frames**, not 8 like every
  other row — confirmed by checking pixel content at each of the 8 WALK column x-positions
  for that row's y-band; the remaining 4 slots are fully transparent. `WALK_FRAME_COUNT`
  is looked up per-row (`4` for row index 3, `8` otherwise) so that row's walk animation
  correctly cycles through only its real frames instead of showing blank frames.

A cropped, 4x-upscaled visual of the IDLE+WALK region (`sprite_crop.png`, not committed)
was rendered and inspected to cross-check the pixel-scan numbers before finalizing — the
grid was visually unambiguous: 9 distinct robot designs, each with 4 idle + 8 walk frames
(except row 3's 4 walk frames), separated by a single fully-transparent gap row.

### The open-floor placement bounds needed measuring too, not guessing
The brief called for agents "spread horizontally... in the large empty tile area
between the desks" — a visual description, not coordinates. Rendering with a naive
15%-85%-width bound put the 4th (of 4)
agents directly on top of the background's mid-right printer/filing-cabinet cluster —
caught live via a screenshot, not assumed. Cropped and inspected `background.jpeg`
(1600×800) directly: the open floor's furniture-free tile area runs roughly x: 24%-78%,
y: 27%-70% of the image. Retuned the bounds to 22%-76% width (small safety margin) —
confirmed live afterward: all 4 agents render clearly separated from every furniture
cluster, none overlapping.

### 🔴 Real bug: React 18 StrictMode double-invoke races with PixiJS's async `init()`
`main.tsx` already wraps the app in `<React.StrictMode>` (present from early on, unrelated
to this change) — which deliberately double-invokes effects in dev (mount → cleanup → mount)
specifically to catch non-idempotent effect/cleanup pairs. The first live browser check hit
exactly that: `pageerror: this._cancelResize is not a function`, thrown from inside
`<Character>`. Root cause: the mount effect calls `new Application()` then kicks off an
**async** `app.init(...)` inside an IIFE; StrictMode's immediate first cleanup ran
`app.destroy(...)` on that same `app` instance **before `init()` had resolved** — PixiJS's
resize plugin (which `resizeTo` depends on) hadn't finished wiring up yet, so its internal
teardown method didn't exist. **Fix:** the init IIFE's promise (`ready`) is captured, and
the cleanup function chains the destroy call onto it — `ready.then(() =>
app.destroy(...)).catch(() => {})` — so destruction can never run before initialization has
genuinely settled, no matter how many times StrictMode (or a fast real remount) invokes the
effect. Confirmed fixed: re-ran the same live check afterward with zero console errors.

### Live verification (both synthetic and real backends, headless Chromium)
- **Synthetic** (`HERMES_DATA_SOURCE=synthetic`, a second backend+vite pair on ports
  8199/5174 so the already-running real stack on 8123/9119/5173 was left undisturbed): all
  4 fake agents (`researcher`/`writer`/`reviewer`/`coder`) rendered in the open floor area
  with correct tints (grey idle, blue working, red blocked); comparing two screenshots 4s
  apart showed "working" robots had moved (patrol) and changed walk-cycle frame, while the
  idle robot stayed exactly in place; one robot visibly flipped from working (blue) to done
  (green) between checks, confirming the tint reacts live to the synthetic loop's own state
  changes. Clicking a sprite (`page.mouse.click` at sampled canvas coordinates) opened the
  real side panel with the correct agent name — `onSelect(agentId)` fires unchanged.
- **Real** (port 5173, the actual `SessionsAdapter`/`KanbanAdapter` stack): 4 real agents
  rendered correctly (all idle at rest). Dispatched a genuine `hermes kanban create` +
  `dispatch` task — the assigned agent's sprite turned blue and began patrolling, driven by
  the real `task_events` SSE stream through the completely untouched `fleet.ts`
  (`visibleState()` is the only function this component calls). Task completed and was
  archived; board left clean.
- Both `vite build` and `tsc -b` succeed; `frontend/dist/sprites/{background.jpeg,
  sprites.png}` confirmed present after build (Vite copies `public/` verbatim, no import
  path changes needed).

### Design notes
- `Room.tsx` now renders one `<Character agents={agents} selectedId={selectedId}
  onSelect={onSelect} />` instead of mapping a `<Character>` per agent — required by the
  "single shared Application for the whole room" pitfall. `tasks` is still accepted in
  Room's own props (App.tsx's call site is unchanged) but no longer forwarded — the Pixi
  renderer only needs `visibleState(agent, now)`, not task details.
- A stable `row` (robot type) is assigned once per agent id, first-seen order, and never
  reassigned even if that agent temporarily drops out of the list — avoids an agent
  visually "changing robot" on an unrelated re-render.
- `homeX` positions are only recomputed when the SET of agent ids changes (a join-sorted
  membership key comparison), not on every render — an agent's patrol position isn't reset
  every time some unrelated field (e.g. `lastHeartbeatAt`) changes.
- A `Graphics` ellipse per agent (visibility toggled every tick against `selectedId`)
  substitutes for the old CSS `.character.selected` box-shadow, since there's no longer a
  DOM node per character to apply that class to.
- Scope was deliberately narrow: background + sprite + tint + patrol + click only. The
  richer per-character overlays added earlier (task-chip, subagent-chip, approval-badge
  rendered directly on the card) are not reproduced on the canvas — they were DOM-only
  affordances of the replaced `Character.tsx`, and the brief for this pass didn't call
  for preserving them. They remain fully intact in `SidePanel.tsx` (untouched by this
  change), so no information is lost, only its on-canvas presentation.

### Files touched
`frontend/package.json`/`package-lock.json` (added `pixi.js` ^8.19.0 — a real npm
dependency, a deliberate exception to the project's default no-new-npm-deps stance
since that rule is about *pure CSS/SVG* problems), `frontend/src/components/Character.tsx` (full rewrite: single
shared `Application`, measured sprite-sheet slicing, patrol/tint/click logic),
`frontend/src/components/Room.tsx` (renders the one shared Character instead of mapping
per agent), `frontend/src/styles.css` (`.room` gains `position:relative`/`overflow:hidden`
+ a `.room canvas` sizing rule; `.empty` becomes an absolutely-positioned overlay — every
existing class name and every `state-*` rule is untouched, per the pitfall). `fleet.ts` was
not opened for editing at all.

## Polish pass: cost HUD and 2D wandering, ground-truthed against the real `/api/profiles/sessions` payload

### Ground truth correction: `profile_totals` is a session COUNT, not a cost total
The initial brief ("small global token/cost HUD ... fed by `/api/sessions`") reads as if
some endpoint hands back a ready-made aggregate. Probed the real, running dashboard
(co-launched with the configured token) directly: `GET /api/profiles/sessions?profile=all`
does carry a `profile_totals` key, but live data shows it's a per-profile **session count**
(`{"default": 4, "autonomous-builder": 42, "researcher": 27, "reviewer": 6, "writer": 6}`),
not a cost/token sum. There is no pre-aggregated cost field anywhere in the response. Every
`estimated_cost_usd`/`input_tokens`/`output_tokens`/`is_active` field the HUD needs is only
present per-row, so the aggregate has to be computed by summing the `sessions` array
ourselves. Confirmed live: `limit=500&order=recent` returned all 85 real sessions in
~0.3s, summing to `estimated_cost_usd≈$3.38`, `input_tokens≈20.6M`, `output_tokens≈470k` —
fast enough to aggregate synchronously per request, no separate cache-refresh job needed
(a short in-process TTL cache is still used so several open browser tabs don't each poll
the dashboard independently — see below).

### Design: no new DataSource method — a global "costs" SSE frame instead
The architecture rule is explicit: the UI talks only to `getAgents/getTasks/subscribe/act`,
and every prior capability added to this app (fine-state/subagent/approval data) was
added the same way — a new *id-less, keyed-by-assignee* synthetic frame pushed through the
existing `/events` SSE stream, never a new interface method or raw fetch from a component.
The cost HUD is global (not per-agent), so it's pushed as an id-less frame with **no**
`assignee` key at all (`{"kind": "costs", "payload": {...}}`), parsed in `fleet.ts` exactly
like the "activity"/"subagent"/"approval" frames and exposed as a new `costs` field on
`useFleet()`'s return value — `CostHud.tsx` only ever reads that prop, never fetches
anything itself. This keeps the whole feature inside the two rules with zero interface
growth. Pushed once immediately per SSE connection (`last_costs_push` initialized to `0.0`,
so `time.monotonic() - 0.0` is trivially past the interval on the very first loop tick) and
then every 10s (`_COSTS_PUSH_INTERVAL_SECONDS`), gated by `isinstance(datasource,
(SessionsAdapter, SyntheticAdapter))` — the same duck-typed-capability idiom `main.py`
already uses for `current_max_event_id`/`fetch_events_after`/`subagent_view`/`approval_view`.
`SessionsAdapter.cost_summary()` best-effort-degrades to `{"available": false, ...}` on no
token/network error (same contract as `independent_agents()`); a bare `KanbanAdapter`/
`HookEnrichedAdapter` (no token, so wrapped without `SessionsAdapter`) or `StubAdapter`
simply never gets a "costs" frame at all, and the HUD component renders nothing (`costs ===
null` or `available === false`) rather than a misleading zero.

### Design: 2D wandering, isolated from state/tint rendering
The brief's note ("keep agent position logic isolated so it can be swapped without touching
state or rendering code") is implemented literally: `Character.tsx` now has two separate
per-tick functions, `updatePosition()` (movement only — wander-toward-a-random-target while
`working`, ease back to the assigned desk slot otherwise) and `updateVisual()` (walk/idle
frame selection, tint, the new flash-on-change and idle breathing bob). A future "seated at
desks" variant only has to replace `updatePosition()` + `computeFloorBounds()`. The open
floor rectangle is widened from the previous pass's single fixed line (`y = 62%`) to the full
`y ∈ [55%, 70%]` range originally intended but collapsed to one line in that pass — this pass
actually uses the full box for 2D wander targets, confirmed live: screenshots of two working
agents show visibly different y-positions, not just x.

### Live verification (synthetic backend, headless Chromium, separate ports so the
### user's already-running dev stack on 8124 was never touched)
Spun up an isolated `HERMES_DATA_SOURCE=synthetic` backend (port 8199) and `vite` dev server
(port 5199, `VITE_BACKEND` pointed at 8199) purely for this verification, torn down
afterward (along with a Hermes dashboard instance started only to probe the real
`/api/profiles/sessions` shape above) without touching the user's own port-8124 backend or
port-9119 dashboard. Drove the running app with Playwright/headless Chromium:
- Cost HUD appeared in the topbar (`$0.25 · 15.0k tok`) on first paint and visibly climbed
  (`$0.60 · 30.3k tok` ~14s later) — confirms both the immediate first push and the 10s
  periodic push are wired correctly end-to-end.
- Screenshots showed two "working" robots at genuinely different (x, y) — 2D wander
  confirmed, not the old single-line left-right patrol.
- Clicking a sprite opened the real side panel (`reviewer`, state `IDLE`, recent events
  list) — `onSelect` still fires unchanged through the isolated `updatePosition`/
  `updateVisual` split.
- Clicked "Spawn task onto researcher": the button read `Spawn task onto researcher`
  immediately (round-trip too fast locally to catch mid-flight, since the synthetic
  adapter's `act()` declines synchronously) and the result banner showed the clear,
  correctly-styled failure message (`✗ read-only public demo — ...`) — confirms the
  per-action `busyAction` plumbing and the existing optimistic-rollback path are intact.
- Zero console/page errors across both scripted sessions.

### Files touched
`backend/app/adapters/sessions.py` (`cost_summary()` + a 5s TTL cache field),
`backend/app/adapters/synthetic.py` (growing `_cost_usd`/`_input_tokens`/`_output_tokens`
counters + `cost_summary()`), `backend/app/main.py` (`_COSTS_PUSH_INTERVAL_SECONDS`, the
"costs" SSE frame push in the `/events` stream), `frontend/src/fleet.ts` (`CostSummary`
type, `parseCosts()`, `costs` state + "costs" frame handling, `Fleet.costs`),
`frontend/src/components/CostHud.tsx` (new, presentational only), `frontend/src/App.tsx`
(renders `CostHud` in the topbar), `frontend/src/styles.css` (`.cost-hud` rule),
`frontend/src/components/Character.tsx` (8fps `WALK_FRAME_MS`, idle breathing bob,
tint-flash-on-change via `lerpColor`, a `Graphics` dim overlay eased toward `DIM_ALPHA` when
no agent is working, `updatePosition`/`updateVisual` split, `computeFloorBounds()`),
`frontend/src/components/SidePanel.tsx` (`busy` boolean replaced by `busyAction: string |
null` so the specific clicked button shows its own in-flight label). `fleet.ts`'s existing
event-derivation logic (WORKING_KINDS/BLOCKED_KINDS/RELEASE_KINDS handling, `visibleState`)
was not touched — only additive "costs" frame handling was added.

---
## Reassigning a running task without a separate reclaim step

A direct user request against the already-shipped reassign action. The existing
`_reassign()` called `POST /tasks/{id}/reassign` with `reclaim_first:
false`, which (per the real Kanban REST route) only reassigns a task that isn't currently
claimed by a live worker — reassigning a `running` task required a separate manual reclaim
first. Fix: `backend/app/adapters/kanban.py`'s `_reassign()` now sends `reclaim_first:
true` unconditionally, so the REST call itself reclaims the running worker's claim and
hands the task to the new profile in one round trip.

### Investigated but not present: no frontend guard existed to remove
The request also asked to remove a guard disabling/erroring the Reassign button when
`task.status === "running"`. Read `SidePanel.tsx` and its git history (`git log` on the
file) before touching anything: no such guard was ever added. `isRunning` (`SidePanel.tsx`
derives it as `targetTask?.status === "running"`) is only ever read by the **Cancel task**
button (disables it, since cancelling a running task isn't safe — kill-worker is the
correct v2 action for that) — Reassign's `disabled` expression never referenced it. Flagged
this to the user via AskUserQuestion rather than guessing; confirmed to skip the no-op
frontend edit and ship the backend fix alone.

### Live verification (real stack: dashboard + backend :8123 + frontend + gateway in tmux)
Dispatched a real long-running Kanban task (`sleep 600`) to `researcher`, confirmed via
`GET /agents` it was genuinely `running` with a live `worker_pid`. Drove the actual running
UI with headless Chromium/Playwright (the room is a PixiJS `<canvas>`, so this required a
click-grid sweep over the floor area rather than a DOM text selector — see "PixiJS canvas
click-through" note below): clicked the `researcher` sprite, typed `writer` into the
reassign field, clicked **Reassign** — the button was never disabled, `window.confirm` was
accepted programmatically, the call returned `✓ reassign`, and a `reclaimed` task_event
appeared live over SSE. Confirmed with raw checks:`hermes kanban show` reported the task
back to `status: ready, assignee: writer`; the old `researcher` worker PID was gone from
`ps`; `GET /agents` showed `writer.current_task_id` set to the reassigned task with no
run yet. Nudged dispatch again and confirmed a **new** worker PID spawned under `writer`,
closing the loop (reassign → new agent actually claims and runs it). Cleaned up:
`POST /runs/{id}/terminate` on the new run, then archived the test task.

### Playwright/headless-Chromium harness notes (useful for future click-through verifies)
- `page.goto(..., { waitUntil: "networkidle" })` never resolves against this app — the open
  SSE connection (`/events`) keeps at least one network request perpetually in flight. Use
  `waitUntil: "load"` plus an explicit `waitForTimeout` instead.
- The room has no DOM text per agent (the PixiJS visual overhaul moved rendering to a single
  PixiJS `<canvas>`), so there's no `page.locator('text=researcher')` to click. Reliable
  approach: get the canvas's `boundingBox()`, then sweep click coordinates across the known
  floor area (`x` fractions spanning the 22%-76% width band from that pass, several `y`
  fractions in the 55-70% band) until the opened side panel's `innerText()` matches the
  target agent's name.
- `playwright` isn't an npm dependency of this repo (by design — the project's "no new
  npm deps for a CSS/SVG problem" default extends to not vendoring a test-only browser
  automation package). It resolves via `npx playwright --version`'s own cached install
  under `~/.npm/_npx/<hash>/node_modules/playwright`; a scratch-directory
  `node_modules/playwright` symlink to that cache path is enough for a plain `node
  script.mjs` (ESM `import { chromium } from "playwright"`) to resolve it without an
  install step.

---
## Task detail click-through (event timeline modal)

A direct user request to make each task ID inside the Recent Events
list clickable, opening a modal with the task's title, final status, result/summary, and
its full `task_events` timeline in order.

### Ground truth correction: `tasks.result` is always NULL in practice
The ask was to show the task's "result/summary field if populated." Checked
`PRAGMA table_info(tasks)` against the real DB — a `result TEXT` column genuinely exists —
but a direct query (`SELECT id, status, result FROM tasks WHERE status='done' ...`) across
every real `done` task on the board returned `result: None` for all of them,
with no exception. The real outcome summary Hermes actually writes lives in the terminal
`task_events` row's `payload.summary` (confirmed on a real completed task,
`kind="completed"`, `payload={"result_len": 0, "summary": "Wrote a 3-sentence summary of
..."}), not on the `tasks` table at all — consistent with this project's standing rule
that state/outcome must be derived from `task_events`, never a status-table snapshot.
Design change from the literal ask: the modal's "Result" section prefers `tasks.result` if
it's ever populated, else falls back to the last event in the timeline carrying a
`payload.summary` — otherwise the feature would show an empty Result section on every real
task, which defeats the point.

### Design: new `DataSource.getTaskDetail(taskId)` method, not a raw component fetch
The architecture rule ("no component reads the DB, hits Hermes endpoints, or shells out")
governed the implementation choice. Unlike the earlier fine-state/subagent/approval/costs
additions, which all fit the *global, currently-selected-agent* push model of the
existing `/events` SSE stream, this is an on-demand pull keyed by an arbitrary historical
task ID the user clicks — a task that may belong to a different, not-currently-selected
agent, or one that's since been reassigned. That doesn't fit a synthetic SSE frame without
either pushing every task's full history proactively (wasteful, unbounded) or guessing
which ID to push (impossible ahead of the click). Extended the interface itself instead:
`DataSource.getTaskDetail(taskId): Promise<TaskDetail>` (TS) / `get_task_detail(task_id)`
(Python), implemented as a real SQLite read in `KanbanAdapter` (task row + all
`task_events` for that `task_id`, ordered `ASC`), pass-through delegates in
`HookEnrichedAdapter`/`SessionsAdapter` (mirrors the existing
`current_max_event_id`/`fetch_events_after` delegation pattern), and a real implementation
in `SyntheticAdapter` too (its own in-memory `_tasks`/`_events`) so the public demo isn't
left behind. `main.py` exposes it as `GET /tasks/{task_id}`, duck-typed via
`getattr(datasource, "get_task_detail", None)` (503 if the adapter lacks it — only
`StubAdapter` does — 404 if the task itself doesn't exist). The React side stays inside
the "UI only talks to the DataSource interface" rule: `SidePanel.tsx` calls
`source.getTaskDetail(id)`, never `fetch()` directly.

### Live verification (real stack, headless Chromium)
Confirmed `GET /tasks/{id}` directly against a real completed task first (`curl`), then
drove the actual UI: opened the app, connected the SSE stream, *then* created and
dispatched a fresh task (deliberately after the browser connected — `/events` defaults to
"from now" (see §2/§3 above), so a task that finished before the browser connects never appears
in `agent.recentEvents` at all, an easy trap for verification scripts to fall into),
polled it to `done` via the CLI, and confirmed its ID appeared as a clickable
`button.ev-taskid-link` in the live Recent Events list (5 matches — one per event kind
referencing that task). Clicked it: the modal opened with the real title, a `DONE` badge,
the real completion summary (fallback-derived per the correction above), and the full
5-event chronological timeline (`created → claimed → spawned → heartbeat → completed`)
with human-relative timestamps. Clicked outside the modal — it closed
(`.modal-overlay` count 0 → confirms both the close button and outside-click dismissal
paths). Zero console/page errors. `tsc --noEmit` and `npm run build` both clean before the
live check. Archived both test tasks and tore down all four processes afterward.

### Files touched
`backend/app/adapters/kanban.py` (`get_task_detail`/`_get_task_detail_sync`),
`backend/app/adapters/hook_enriched.py` and `sessions.py` (delegate pass-through),
`backend/app/adapters/synthetic.py` (`get_task_detail` over in-memory state),
`backend/app/main.py` (`GET /tasks/{task_id}` route), `frontend/src/datasource.ts`
(`TaskDetail`/`TaskDetailEvent` types, `getTaskDetail` added to the `DataSource`
interface), `frontend/src/adapters/httpDataSource.ts` (implementation),
`frontend/src/components/SidePanel.tsx` (`TaskDetailModal` component, clickable
`ev-taskid-link` buttons in the Recent Events list, `openTaskDetail` state/handler),
`frontend/src/styles.css` (`.modal-overlay`/`.modal`/`.task-result`/`.ev-taskid-link`/
`.events.timeline` rules, dark-theme-consistent).

## False "no gateway is running" warning on spawn (two different gateway checks disagreeing)

**Symptom:** `POST /act {action:"spawn"}` returned a "No gateway is running" warning even
with a real gateway alive in tmux (`hermes -p autonomous-builder gateway run`, confirmed by
`ps`).

**Root cause, found by evidence, not guesswork.** Our own `gateway_running()`
(`backend/app/adapters/kanban.py`) was never the source — a direct-to-file debug log
(stdout logging was undiagnosable: the backend's fd 1/2 point at the user's own tty,
`/dev/pts/7`, which this session cannot safely read) proved it ran in 32ms and correctly
returned `True`, matching PIDs 4057 (tmux) and 4058 (the real gateway) via
`pgrep -f "gateway run"`. The warning the response carried was word-for-word different
text, traced by grep to Hermes's own `plugin_api.py`'s `POST /tasks` handler, which calls
`hermes_cli.kanban._check_dispatcher_presence()` → `gateway.status.get_running_pid()`. That
function reads a **PID file scoped to `get_hermes_home()` at call time** —
`{HERMES_HOME}/gateway.pid`. Since the dashboard runs under the *default* profile
(`hermes dashboard --no-open`, no `-p`), it checks `~/.hermes/gateway.pid` (absent — only a
stale `gateway_state.json` from an earlier session existed there), while our gateway,
launched with `-p autonomous-builder`, writes its PID file to
`~/.hermes/profiles/autonomous-builder/gateway.pid` (confirmed present, live, `pid: 4058`).
Reading `gateway/kanban_watchers.py` confirmed the dispatcher itself is **not**
profile-scoped: "the lock lives at the machine-global kanban root ... so it serialises ALL
gateways" — one gateway of any profile dispatches every profile's ready tasks. So the
profile-scoped gateway genuinely dispatches the task; Hermes's own dashboard warning is a
false negative in exactly this (legitimate, documented-in-this-repo) multi-profile tmux
setup.

**Fix:** stop relaying `rest["warning"]` from `POST /tasks` in `_spawn()` — it's the same
unreliable, profile-scoped signal on every call, not just this one. Made our own
`gateway_running()` (profile-agnostic process scan) the sole source of the warning, and
moved its call onto a thread (`asyncio.to_thread`) inside `act()` so the synchronous
`subprocess.run` never blocks the event loop on a live request. `pgrep`'s resolved absolute
path is now passed as argv[0] too (`shutil.which("pgrep")` result, not the bare name) —
cheap extra robustness, not itself the bug.

**Separate incident during diagnosis (unrelated to the above, but blocked verification):**
the already-running backend (`uvicorn --reload`) was found wedged — TCP handshakes
succeeded but no request (including `/health`) ever got a response, and the kernel accept
queue was backing up. `/proc` inspection showed the reload supervisor thread parked in
`do_wait` while the actual worker process (spawned via `multiprocessing`, visible as a
`spawn_main` child) sat idle in `ep_poll` — consistent with the supervisor never reaching
`accept()` on the listening socket it owns, so nothing was ever handed to the worker.
`import app.main` succeeded cleanly, ruling out the debug-log edit as the cause; this
looks like a pre-existing dev-server wedge, cause not fully isolated (no `py-spy` available,
no `ptrace` permission). Recovered by killing the exact confirmed PID (SIGTERM, then SIGKILL
after it didn't respond) and its two orphaned `multiprocessing` helper children, with the
user's explicit go-ahead before touching anything, then having the user restart it cleanly.

**Live verify:** gateway up → spawn returns with no warning in 0.12s, task claimed within
20s by lock `Hermes-Linux:4058` (the real gateway PID) and finished (`done`). Gateway killed
via `tmux kill-session` → spawn returns the warning, task confirmed sitting in `ready`.
Gateway restarted via `tmux new -d` → the same ready task claimed within 10s of restart.
`gateway_running()`'s own verdict printed directly in both states (`True` up, and matching
`False` down via the same pgrep invocation with empty stdout). `pytest`: 35 passed. Backend
stayed responsive (`/health` 200) throughout. One process cleanup note: a raw `PATCH
{"status":"archived"}` issued directly against the dashboard REST API (not through our own
guarded `_cancel()`, which refuses to archive a `running` task) during verify left an
orphaned worker process behind — archiving via the dashboard REST bypasses that app-level
guard entirely. Cleaned up by killing the confirmed PID directly (its cmdline named the
exact task id); it appeared as a harmless self-reaping zombie under the gateway's own PID
immediately after, resolved on its own.

## UI fixes: global font scale, modal scroll, "note for next run", agent nameplates

Four frontend-only fixes, verified live against an isolated `HERMES_DATA_SOURCE=synthetic`
backend (port 8199) + `vite` dev server (port 5199, `VITE_BACKEND` pointed at 8199) via
headless Chromium (Playwright, resolved through the npx cache — not added as a project
dependency), then torn down by exact PID without touching the user's real port-8123/5173
dev stack, which was confirmed still listening afterward.

1. **Global font +20%.** Every `font-size` in `styles.css` is a fixed px value, not rem, so
   the suggested `html { font-size: 120% }` would cascade to nothing — confirmed by grep,
   virtually every text-bearing class declares its own explicit px size. Used `.app { zoom:
   1.2; }` instead: the one root-level CSS lever that actually scales already-hardcoded px
   rules (fonts, padding, borders, layout) together without touching each declaration.
   Verified: `getComputedStyle(.app).zoom === "1.2"`; `scrollWidth <= clientWidth` on
   `.panel`, `.topbar`, `.actions`, `.cost-hud` (no horizontal clipping) after zoom; visually
   confirmed larger text throughout via screenshot.
2. **Scrollable result box / modal cap.** `.task-result` now has `max-height: 200px;
   overflow-y: auto`; `.modal` raised to `max-height: 85vh` (from 80vh) with a new `.modal
   .panel-head` `position: sticky; top: 0` so the title/close button stay visible while the
   body scrolls. The synthetic adapter's `get_task_detail` always returns `result: null` and
   only a handful of events, so real long content wasn't available to click through live —
   verified instead by injecting the exact same DOM shape (`.modal`/`.task-result`/
   `.events.timeline` classes) the component renders, with a 40-line result and 60 synthetic
   heartbeat rows: `.modal` computed `max-height: 765px` (85vh of the 900px test viewport)
   with real overflow (`scrollHeight 1970 > clientHeight 763`); `.task-result` computed
   `max-height: 200px` with real overflow (`656 > 198`); header `position: sticky` confirmed;
   screenshots before/after scrolling the modal show the header pinned while the body content
   moved underneath it.
3. **"Note for next run."** Reused the existing `act(agentId, "comment", {task_id, body})`
   path verbatim — no new `DataSource` method, no `fetch`, no reclaim/redispatch. Retitled the
   input placeholder to "Leave a note for the next run" and the button to "Leave note for next
   run", added one line of helper text explaining the agent reads it via `kanban_show()` on
   its *next* run, not mid-turn. Enable condition changed from "any `targetTaskId` exists" to
   "`targetTask?.status` is running/ready/blocked" (`hasOpenTask`) — deliberately NOT
   `agent.activeTaskId`, which `fleet.ts` (untouched, per guardrail) nulls out on a
   blocked-kind event; `targetTask?.status` off the `tasks` map is the same terminal-vs-not
   signal the neighbouring `isRunning`/`isBlocked`/`isArchived` checks already use, so a
   blocked task (whose `activeTaskId` is null but `lastTaskId` still points at it) still
   resolves correctly. Verified live: clicking through all 4 synthetic agents showed the note
   input enabled only for the two in `working` state, disabled for `idle`; on a `working`
   agent (`writer`, task `t_synth0025`), typing a note and clicking the button fired exactly
   one `POST /act` with body `{"agentId":"writer","action":"comment","payload":{"task_id":
   "t_synth0025","body":"..."}}` — confirmed via network interception, not just UI state.
4. **Nameplate behind each agent's name.** A per-agent `Text` label already existed in
   `Character.tsx` (added in an earlier, undocumented follow-up pass that also replaced
   wandering with fixed floor seats — see below); added a `Graphics.roundRect(...).fill({
   color: 0x000000, alpha: 0.6 })` plate sized to the label's own measured `width`/`height`
   plus 4px padding, added to the *same* per-agent `Container` as the label, immediately
   before it in `addChild` order (lower z-index) so the label renders on top. Deliberately did
   **not** add a per-tick manual x/y copy: since the plate is a child of the same `Container`
   that `updatePosition()` already repositions every tick (`container.x = seat.x` etc.), Pixi's
   scene graph propagates the parent transform to all children automatically — the plate
   structurally cannot lag behind the label, which a per-tick copy could if ever done in the
   wrong order. Verified live via screenshot: all 4 nameplates render legibly over the light
   office floor. One assumption in the original ask didn't hold against the actual code:
   "compare two frames a few seconds apart to confirm it tracks" assumes continuous
   wandering, but the live app (an undocumented follow-up past the earlier cost-HUD/wandering
   pass) now seats agents at **fixed** floor spots with no continuous movement — two
   screenshots 4s apart were pixel-identical.
   Substituted a stronger structural proof: resized the browser viewport (which forces
   `computeSeatPositions()` to recompute every agent's pixel position from scratch) and
   confirmed all 4 plates re-centred under their sprites with zero lag in the post-resize
   screenshot — the same guarantee "tracks a moving agent" was meant to verify, exercised via
   the mechanism that actually changes position in this build.

`tsc -b` and `vite build` both clean. Zero console errors across all four Playwright checks.

**Real-backend re-check of #3 (note-for-next-run), against the user's own live stack —
port 8123/5173/9119, never restarted.** Dispatched a genuine `hermes -p autonomous-builder
kanban create "Run 'sleep 180' via the terminal tool..." --assignee researcher` task
(`t_056dc895`); the gateway claimed it within 10s (real worker pid 22289, run #55, confirmed
`working`/`current_task_id` via `GET /agents`). Drove the REAL running app (port 5173,
`SessionsAdapter` against the real Kanban DB, not synthetic) with headless Chromium: selected
`researcher` (badge `working`), the note input/button were both enabled, posted a note —
network interception on `/act` showed the real request
(`{"agentId":"researcher","action":"comment","payload":{"task_id":"t_056dc895","body":"..."}}`)
and a real `200 {"ok":true,"via":"rest","task_id":"t_056dc895"}` response (i.e. it went via
REST, not the CLI fallback). `hermes -p autonomous-builder kanban show t_056dc895` confirmed
the note landed verbatim in the task's real comment thread (`[08:06] dashboard: NOTE FOR NEXT
RUN: ...`) alongside a genuine `commented` event, while run #55 was still `active` — the
worker was never interrupted, matching the "note-only, must not disturb the running worker"
requirement. Cleaned up via CLI only: `kanban reclaim` (worker process 22289 confirmed gone
afterward) then `kanban archive`; the real backend/frontend stack was left running and
`/health` confirmed healthy throughout — never restarted, per instruction.

## Live-activity source selection: what a running worker is doing right now

**No app code, DataSource, adapter, or DB write touched during this investigation — read-only probes only**
(`file:<path>?mode=ro` for every SQLite read). The user's backend (port 8123) happened to
already be down when this spike started (unrelated to this work — not restarted, per
instruction, since the spike doesn't need it: every probe targets the dashboard on 9119,
the Kanban DB, and profile-local files directly). The gateway (tmux `gateway`) and dashboard
(9119) were left exactly as found throughout.

**Setup:** dispatched a real long task (`hermes -p autonomous-builder kanban create
"Research transformer attention in depth and write ~800 words; take your time, use several
web searches and file reads." --assignee researcher`) → `t_2caa8a70`, claimed within
seconds as run #57, worker pid 24990. `tasks.session_id` (column confirmed present) was
still `NULL` at claim time and never populated during the run — the real correlation key
had to come from `GET /api/profiles/sessions?profile=all`, matching `cwd` against
`~/.hermes/kanban/workspaces/t_2caa8a70/` (the earlier cwd-matching finding holds): session
`20260708_082453_11de56`, profile `researcher`.

**1. Worker log (`~/.hermes/profiles/researcher/logs/agent.log`).** Plain-text, one line per
event, tailable live. Gives tool NAME, duration, and result size (`tool browser_navigate
completed (5.09s, 8487 chars)`), API-call latency/token counts, and `pre_tool_call`/
`post_tool_call` markers from the sensor plugin itself. Freshness: lines appear within
~1s of the event (watched `pre_tool_call` → `post_tool_call` pairs land 2-27s apart,
matching real tool durations). No auth (local file, same-user read). **Never carries
assistant message text or tool output content on success** — only counts. It DOES leak
partial content on tool *errors* (e.g. `Tool terminal returned error (38.28s):
{"output": "[Command interrupted]", ...}`) — an inconsistency worth knowing about if this
were ever exposed further than a local tail. No raw shell command strings observed logged
either way. **Verdict: good for "which tool, how long" — useless for "what is it saying."**

**2. Profile session store (`~/.hermes/profiles/researcher/state.db`, `messages` table).**
The `messages` table (plus `messages_fts`/`messages_fts_trigram` full-text indexes) has
exactly the columns needed: `role`, `content`, `tool_calls` (name + JSON arguments),
`tool_name` (on tool-result rows), `reasoning`/`reasoning_content`, `timestamp`. Read-only,
WAL-safe, same pattern as the Kanban DB. A real captured row, latest-assistant-turn:
```
role=assistant
content="Good, got substantial content from Lilian Weng's blog. Now let me also get the
         detailed content from Jay Alammar's Illustrated Transformer page"
tool_calls=[{"function":{"name":"browser_navigate","arguments":"{\"url\":
             \"https://jalammar.github.io/illustrated-transformer/\"}"}}]
reasoning="I got a lot of content from Lilian Weng's page. Let me now also get the content
           from Jay Alammar's Illustrated Transformer, which is probably the best single
           resource for understanding transformer attention..."
```
and a real tool-result row: `role=tool, tool_name=browser_console, content="<untrusted_tool_
result source=\"browser_console\">...{\"success\": true, \"result\": \"Attention? Attention!
\\nDate: June 24, 2018...<15914 chars total>"`. Freshness: polled twice, 15s apart — newest
row age was 2.9s and 13.0s respectively at poll time, i.e. rows land within a few seconds of
the turn completing (real polling overhead, not source lag). No auth — filesystem
permissions only (profile-owned, `0700` directory). **Sensitivity, confirmed important and
not hypothetical: `content` on a tool-result row is the RAW tool output** — for a browser
tool that's page text (fine), but for `terminal`/`execute_code` it would be raw stdout/
stderr, which can carry secrets or full command text verbatim; `tool_calls.arguments` is the
raw invocation JSON, same risk. **Surprise worth stating plainly per the goal's own
instruction to be honest: `reasoning`/`reasoning_content` ARE persisted here, in full** —
this is the model's actual chain-of-thought text, not a summary. It is not exposed by any
REST route probed below and reading it requires local disk access to a profile-owned SQLite
file, but it is not "almost certainly not persisted anywhere," as the task brief assumed —
it is persisted, locally, unencrypted. Displaying it verbatim in a UI would be both a
privacy/trust question (it's meant to be the model's private scratch space, and at least one
provider's terms discourage showing raw reasoning tokens to end users) and a payload-size
one (the strings above run several hundred characters routinely). **Recommendation: read
`content`/`tool_calls`/`tool_name` from this table; deliberately do NOT surface `reasoning`/
`reasoning_content` in the UI**, even though it's technically readable.

**3. Bearer-gated REST — `GET /tasks/{id}/log` and `GET /runs/{run_id}/inspect`.**
`/tasks/{id}/log` (reads `~/.hermes/kanban/logs/<task_id>.log`, confirmed by matching path
in the response) returns a genuinely nice pre-formatted, human-readable timeline — one line
per tool call with an emoji-by-family icon, an abbreviated target, and duration, e.g.:
```
  ┊ 🌐 navigate  jalammar.github.io  5.1s
  ┊ 📸 snapshot  full  0.9s
  ┊ ⚡ browser_c   0.6s
```
Confirmed the raw file updates at the same real-time cadence as `messages` (both showed a
new line/row within the same ~13s poll window). No message text or reasoning — just the
tool-call summary, already redacted to a domain/target rather than a full URL or command
(matches the sensor plugin's own "no raw command text" principle). `/runs/{run_id}/inspect`
returned real OS process data (`alive`, `pid`, `cpu_percent`, `memory_rss_bytes`, `status:
"sleeping"`, full `cmdline`) — useful for a liveness/resource check, carries zero activity
content. **Verdict: `/tasks/{id}/log` is the single best "at a glance" readable summary and
is already REST-exposed with the existing Bearer token — no new auth story needed — but it's
strictly coarser than table 2 (tool names + timing only, no assistant text).**

**4. `/ingest` envelope (hermesboard-sensor → backend, already wired since the hook-telemetry work above).**
Read `~/.hermes/plugins/hermesboard-sensor/__init__.py` directly: the `pre_tool_call`/
`post_tool_call` envelope is exactly `{hook, task_id, session_id, profile, ts, tool_name}` —
by design, no message content, no tool arguments, no tool output, ever (the plugin's own
comments confirm this is deliberate: even the approval envelope sends a human `description`
like `"recursive delete"`, never raw command text). `HookEventStore.ingest()`
(`backend/app/hook_store.py`) already folds this into per-profile `activity`/`fine_state` —
exactly what the side panel's italic activity label already shows. **Verdict: cannot be
extended into a "recent activity" text buffer without adding a new field to the envelope
(a plugin code change, out of scope for a read-only spike) — it structurally has no text to
buffer.** Confirms the goal's "tool it's calling right now" half is already solved; the
"latest assistant message / last tool output" half needs one of sources 1-3.

**Decision.** Combine **source 2 (`messages` table) as the primary content source** — it is
the only one with actual assistant text and tool output, near-live (~seconds), same
read-only/WAL-safe pattern this app already uses for the Kanban DB — **with source 4's
existing `tool_name` activity label kept as the always-on coarse fallback** for profiles
with no fresh `messages` row yet (crash-guard parity with the existing `STALE_AFTER_SECONDS`
idiom). `/tasks/{id}/log` (source 3) is a good *secondary/debug* view (pre-formatted, human
one-liners, already REST-reachable) but not the primary feed since it's strictly less
detailed. Concretely, for a later build step: poll `messages` for the target session at
~3-5s (matches observed write latency; the profile `state.db` is a local SQLite file, so
even a tight poll is cheap and carries zero lock/corruption risk read-only), select
`role IN ('assistant','tool')` ordered by `id DESC LIMIT 1`, and render: assistant `content`
verbatim (already a short, human sentence in practice — the model narrates its own steps);
for a `tool_calls` row show the tool NAME plus a short derived label (e.g. the `url`/`path`
argument, not the full JSON) mirroring source 3's own redaction choice; for a `tool` role
row show `tool_name` plus a hard-bounded preview (e.g. first ~200 chars) of `content`, never
the full raw output — this is the one deliberate content-sensitivity line, since raw tool
output is exactly where secrets/full shell text can appear. Never surface `reasoning`/
`reasoning_content`, session-store internals (`tool_call_id`, `codex_*`), or full `content`
past the bound. No source here fails outright — this is a "combine, with one field
deliberately withheld" outcome, not a drop.

**Cleanup:** `hermes -p autonomous-builder kanban reclaim t_2caa8a70` then `kanban archive`
(CLI only, no raw REST PATCH — see the earlier orphaned-worker lesson in this file). Worker
pid 24990 confirmed gone (a harmless self-reaping zombie under the gateway's own pid
immediately after, same as previous spikes); `t_2caa8a70` no longer appears in `kanban list`.
No app code, interface, or DB changes made.

## Live-activity read: implementing the source chosen above

**Endpoint:** `GET /agents/{agentId}/activity` → `{ available: bool, updatedAt: float|null,
items: [{ ts, kind: "assistant"|"tool_call"|"tool_result", label: string|null, text:
string|null }] }`, newest item last, capped at 20. `label` is the tool name for
`tool_call`/`tool_result`, `null` for `assistant`. `text` can be `null` (the coarse
hook-only fallback path carries no message text at all — see below). Duck-typed in
`main.py` exactly like `/tasks/{id}`: 503 only when the adapter has no
`get_worker_activity` at all (`StubAdapter`); every other case is a normal 200, `{available:
false}` for "nothing to show" — never a 500 for an idle agent. `DataSource.getWorkerActivity`
(TS) / `get_worker_activity` (Python, duck-typed, not on the ABC — same precedent as
`get_task_detail`) added; `HttpDataSource` implements it as a plain `fetch`, no new
component-level fetch anywhere (architecture rule held).

**Primary source, exactly as the spike concluded:** the worker's own profile session store,
`~/.hermes/profiles/<profile>/state.db`'s `messages` table, for the CURRENT `task_runs` row
where `status='running' AND ended_at IS NULL` for that profile. One improvement over the
spike's own method: session id resolution is now fully LOCAL — `sessions.cwd` in that same
`state.db` matches `~/.hermes/kanban/workspaces/<task_id>` exactly, so no dashboard/REST/token
dependency is needed at all (the spike had used `GET /api/profiles/sessions`). `role='assistant'`
rows become an `assistant` item (if `content` is non-empty) plus one `tool_call` item per entry
in `tool_calls` (name + JSON arguments); `role='tool'` rows become a `tool_result` item
(`tool_name` + `content`). **The SQL SELECT never lists `reasoning`/`reasoning_content` at
all** — not filtered after the fact, excluded at the query itself, so the model's raw
chain-of-thought never exists in this process's memory. Never throws: every `sqlite3.Error`
(missing file, locked/corrupt DB, no `cwd` match) is caught and returns
`{"available": false, "updatedAt": null, "items": []}`.

**Fallback, layered in `HookEnrichedAdapter`** (not `KanbanAdapter`, which has no
`hook_store` reference — the one deliberate exception to "pass-through delegate, mirroring
`get_task_detail`" for `SessionsAdapter`, which really is a bare pass-through): if the
primary read comes back unavailable, build items from `hook_store.recent_for(profile)` — the
same `/ingest` envelopes already feeding the coarse `fine_state`/`activity` label. Confirmed
live and unprompted during verify: once `t_6277b4ac` reached `status=done`, the primary query
correctly found no `running` `task_runs` row and returned unavailable, and the very next poll
showed a real fallback snapshot — 20 `tool_call`/`tool_result` items with tool names
(`browser_scroll`, `write_file`, `terminal`, `kanban_complete`, ...) but every `text` field
`null`, exactly the "final snapshot, not an error" behavior the verify plan called for,
observed without having to force it.

**🔴 Hard privacy rule — `app/redact.py`, one pure `sanitize_text(text, max_len=300)` helper
every emitted text field passes through** (assistant content, tool-call argument JSON,
tool-result preview — in both `KanbanAdapter` and, harmlessly, `SyntheticAdapter`'s fabricated
text). Order is truncate-then-scrub, per spec — an accepted, documented gap: a secret split
exactly across the 300-char boundary could survive in fragment form. `scrub_secrets()` covers,
in order: `sk-`/`sk-ant-` keys, AWS `AKIA...`, GitHub `ghp_`/`gho_`, Slack `xoxb-`/`xoxp-`,
`Bearer <token>` (keeps the word "Bearer", redacts the token), `KEY=`/`TOKEN=`/`SECRET=`/
`PASSWORD=` assignments (keeps the name, redacts the value), then a last-resort standalone
hex/base64 blob (≥32 chars) catch-all. **This is explicitly best-effort defense-in-depth over
known secret shapes — not a guarantee against every possible secret.** 12 unit tests in
`tests/test_redact.py` cover each shape individually, all four together in one string
(surrounding text and assignment names preserved, secrets gone), purity/idempotency, and the
truncate-then-scrub interaction.

**Verified live against the real backend (port 8123, `SessionsAdapter`, never restarted —
the user started it for this step after it was found down):** dispatched a genuine
multi-tool-call research task (`t_6277b4ac`, "Research how KV-cache works..."), claimed within
15s as run #58. Polled `GET /agents/researcher/activity` every 2s for the task's full 347s
run. **17 distinct `updatedAt` snapshots observed — items visibly advanced** (e.g.
`tool_call browser_navigate` → `tool_result browser_snapshot` → further tool calls), each
`text` field bounded (visible `…` truncation on long tool-output previews) and free of any
model narration that looked like planning-only chain-of-thought phrasing. **Real observed
latency: min 1.1s, max 18.1s, average 6.6s between snapshot changes** — matches the spike's
predicted "~3-13s, chunked on turn completion, not per-token" cadence closely (the true range
is a bit wider in practice, still the same order of magnitude and never sub-second). **Explicit
privacy check: grepped the ENTIRE captured poll log (all 45 polls, full JSON bodies) for the
literal string "reasoning" — zero matches.** Combined with the SELECT-level column exclusion
in the code itself, this is belt-and-suspenders: even if a future edit accidentally reintroduced
the columns into the query, the shipped code as verified here emitted no reasoning text over 45
real polls of a real run. Idle-agent check (`writer`, no running task) → clean
`{"available":false,"updatedAt":null,"items":[]}`, both before and after the researcher run.
`/health` returned 200 throughout the entire ~6-minute polling window. `pytest`: 47 passed
(35 prior + 12 new `test_redact.py` cases).

**Verified against the isolated synthetic backend** (port 8199, `HERMES_DATA_SOURCE=synthetic`,
torn down after — user's real stack untouched): idle agent → `{available:false}`; a "working"
agent's `/agents/writer/activity` response visibly progressed through the fabricated
"assistant narration → tool_call → tool_result" script on successive 2s polls
(`web_search` → `file_read` → `file_write`, ...), same wire shape as the real path, `text`
fields populated (nothing sensitive to scrub, but routed through `sanitize_text` anyway, per
spec, so the code path is identical either way).

**Cleanup:** task `t_6277b4ac` completed naturally during the verify window (no reclaim
needed) — archived via CLI (`kanban archive`, not a raw REST PATCH), worker pid 26330
confirmed gone. Gateway and the user's real backend/frontend/dashboard were never
restarted by this step.

### Files touched
`backend/app/redact.py` (new), `backend/tests/test_redact.py` (new),
`backend/app/adapters/kanban.py` (`get_worker_activity`/`_get_worker_activity_sync`),
`backend/app/adapters/hook_enriched.py` (`get_worker_activity` with the coarse
`_coarse_activity_fallback`), `backend/app/adapters/sessions.py` (bare pass-through),
`backend/app/adapters/synthetic.py` (`_ACTIVITY_SCRIPT`/`_advance_activity`/
`get_worker_activity`), `backend/app/main.py` (`GET /agents/{agent_id}/activity`),
`frontend/src/datasource.ts` (`WorkerActivity`/`WorkerActivityItem` types,
`getWorkerActivity` added to the `DataSource` interface),
`frontend/src/adapters/httpDataSource.ts` (implementation). No UI component wired to
display this yet — out of scope for this step (interface + adapters + endpoint only).

## Live-activity panel wired into the side panel

**Feature:** a "Live activity" section in the side panel, below "Current task" and above
"Actions". Goes through `source.getWorkerActivity(agentId)` only — no `fetch()` in the
component, `fleet.ts` and every `state-*` class untouched, no new npm deps. A local
`useWorkerActivity(source, agentId, enabled)` hook (defined in `SidePanel.tsx`, not
exported) owns the polling: `setInterval` at **1500ms**, gated on the SAME `visibleState()`
the rest of the panel already computes — `enabled = state === "working" || state === "done"`.
Widening past just `"working"` to also include the ~6s `"done"` flash (fleet.ts's
`DONE_COOLDOWN_MS`) was a deliberate choice: it's what gives the panel a real chance to
fetch the backend's post-completion coarse-fallback frame instead of freezing on the last
in-progress frame the instant a task finishes.

**Overlap/leak guards:** an `inFlightRef` skips a tick if the previous poll hasn't resolved;
the effect's cleanup (fires on `agentId`/`enabled` change AND on unmount) clears the interval
AND resets `inFlightRef.current = false` — the latter specifically so a straggling in-flight
request for the OLD agent can never block the very first poll for a newly-selected one. A
`cancelled` flag closed over per-effect-run means a late-arriving response from an abandoned
poll can never clobber state for whatever's currently selected. `setActivity(null)` at the
top of the effect means switching agents never flashes the previous agent's stale feed.

**Rendering — the `text: null` handling the prompt specifically flagged:**
`activityItemLine(item)` never inspects `text` for a `tool_call` — it's always
`"→ calling <label>"` regardless (matches the spec: a clean one-liner, not raw JSON args in
the feed). For `tool_result`, `text ? "<label>: <preview>" : label` — a null-text row
degrades to just the tool name, never `"null"` or an empty bubble. `assistant` items render
`text` as-is (the backend only ever emits these with non-empty content). Every item's line is
computed once and filtered for blankness before rendering, so no row can ever be empty
regardless of which path produced it. Auto-scroll: a `ref` on the `<ul>` plus a `useEffect`
keyed on the `workerActivity` object reference, setting `scrollTop = scrollHeight` on every
new frame.

**Honest labeling:** `<h3>Live activity</h3>` + a subtitle — "Worker output, ~1-2s delay —
the agent's latest actions as it works. Not a live in-place edit, and never the model's
hidden reasoning." — directly naming both things a user might otherwise assume (live editing,
visible reasoning) and ruling them out, per the privacy design in the live-activity
read implementation above.

**New CSS only** (`.activity-subtitle`, `.activity-feed`, `.activity-item`,
`.activity-item-tool_call`/`.activity-item-tool_result` (dimmer/italic), `.activity-text`,
`.activity-ts`) — `.activity-feed` is a bounded (`max-height: 180px`), scrollable, dark-theme
box matching `.task-result`'s existing look. Nothing renamed.

### Live verify — real backend (port 8123/5173, never restarted; gateway untouched)

Three real dispatch runs were needed to get clean evidence, documented honestly rather than
cherry-picked:

1. **First run** (`sleep 40` task): the claim-detection and completion-wait loops used fixed
   timeout budgets that turned out too tight relative to the gateway's ~60s dispatch tick —
   T0/T5 screenshots landed on the same tail-end frame (task had nearly finished by the time
   "claimed" was detected), and the "post-completion" check fired before the CLI actually
   reported `done`. Diagnosed from the captured tool sequence itself (`kanban_complete`
   already present at "T0") rather than assumed.
2. **Second run** (`sleep 30` task, status-driven waits instead of fixed budgets): confirmed
   `ITEM5_ONLY_RESEARCHER_ACTIVE: true` while rapidly clicking through writer → reviewer →
   researcher (single active poller, no leak); confirmed **20 clean post-completion items**
   captured within the done-cooldown window — all `tool_call`/`tool_result` rows, zero `null`
   strings, zero empty rows — followed by the feed correctly going empty
   (`AFTER_COOLDOWN_ITEMS: []`, muted "No live activity" line visible) once the cooldown
   expired. This run supplied the definitive post-completion proof (console-captured DOM
   text; the corresponding screenshot was later overwritten by run 3's same-named file — a
   scripting mistake, not a feature gap).
3. **Third run** (a deliberately thorough "research multi-head attention" task, ~6+ minutes
   real runtime): confirmed the feed genuinely **advances during active work** — 24
   `/activity` responses captured over a continuous 30s window while the task ran, 3 distinct
   `updatedAt` snapshots, DOM showing real assistant narration ("Starting the research task.
   I'll do 4+ web searches...") and live tool_call/tool_result rows including a genuine
   mid-run recovery (`Tool 'web_search' does not exist` → pivoted to `browser_navigate`) —
   real agent behavior, not a script artifact. Confirmed idle agent (`reviewer`, never ran)
   →  muted "No live activity — agent idle" line. Confirmed closing the panel stops polling
   (0 `/activity` requests in a 4s post-close window, 2.7× the poll interval).

**Cleanup:** all four probe tasks across the three runs (`t_38181181`, `t_8baa0517`,
`t_4ff8b6c0`, `t_458c475d`) reclaimed/completed naturally and archived via CLI; confirmed no
lingering `researcher` kanban worker processes; board left clean (`kanban list` shows none of
the probe tasks). Real backend/frontend/gateway never restarted by this step — the backend
was already up from the previous step, the gateway stayed in its tmux session throughout.

### Live verify — isolated synthetic backend (port 8199 + vite 5199, torn down after)

Selected a "working" synthetic agent (seat-resolved from `GET /agents`, since agents are
stationary — no DOM node or moving sprite to target, matching the room's actual current
design). `SYNTH_T0_ITEMS` showed the fabricated `_ACTIVITY_SCRIPT` mid-sequence
("Let me start by getting a lay of the land here." → "→ calling web_search" → "web_search:
Found 8 relevant results..."). 5 seconds later the SAME agent had finished its synthetic task
(`SYNTH_FEED_CHANGED: true`, sprite visibly turned green/DONE in the screenshot) and the panel
correctly showed "No live activity — agent idle" — an unplanned but valid capture of the exact
working→done→fallback-unavailable transition the real-backend run 2 above also exercised,
this time in the synthetic adapter. Confirms `SyntheticAdapter.get_worker_activity()`'s
literal `state == "working"` check (stricter than the frontend's own
`working`-or-`done`-cooldown gate) degrades to a clean unavailable response rather than an
error during that gap, exactly as intended.

### Playwright technique notes (for reuse)

- `waitUntil: "load"`, never `"networkidle"` — the app holds an open SSE connection, so
  network never goes idle and that wait mode times out.
- **Connect before dispatch**: navigate and let the SSE connection establish (`.conn.live`
  visible) BEFORE creating the Kanban task via CLI — `/events` defaults to "from now", so a
  task created before the browser connects never appears in `agent.recentEvents`.
  (`getWorkerActivity` itself doesn't depend on this — it's a plain poll, not SSE-fed — but
  the rest of the panel does.)
- **Canvas-coordinate clicking**: agents are stationary, single shared PixiJS canvas, no
  per-agent DOM node. Resolve an agent's seat index by sorting `GET /agents` alphabetically
  (matches `fleet.ts`'s sort and `Character.tsx`'s `SEAT_FRACTIONS` fill order) and click
  `canvasBox.x + canvasBox.width * fx, canvasBox.y + canvasBox.height * fy`.
- **Status-driven waits beat fixed timeouts**: polling `hermes kanban show <id>`'s actual
  `status:` line and looping until it changes is far more reliable than a fixed
  `waitForTimeout` budget, given the gateway's ~60s dispatch tick plus variable real task
  duration — a fixed budget either wastes time or (as run 1 above showed) cuts off before the
  real transition happens.
- Cross-origin `fetch()` from within `page.evaluate()` to a different port hits CORS; when a
  script just needs to read the isolated backend's own state (not simulate a user action),

### Panel cleanup pass: envelope strip + side-panel sizing (2026-07-08)

Stripped Hermes's `<untrusted_tool_result>` wrapper/preamble from `tool_result` text in
`get_worker_activity()` (new `app/text_clean.py::strip_tool_result_envelope`, applied before
`sanitize_text()` so secret-scrubbing still runs), verified against 79 real wrapped rows in
`~/.hermes/profiles/researcher/state.db`; and widened `.panel` 320px → 416px (+30%) with a
~30%-larger, panel-scoped font (base `font-size: 1.3em` on `.panel` plus explicit overrides on
the ~20 child rules — `.badge`/`.muted`/`.close`/`.action-result`/`.events li` — that had their
own fixed px sizes and are shared with the task-detail modal, so the modal is untouched).

### Terminate removed, Cancel absorbs the worker-kill (single stop control)

Removed the Step-13 "Terminate worker" action end to end — the button/handler in
`SidePanel.tsx`, the `"terminate"` branch of `KanbanAdapter.act()`, `_terminate()`, and its
CLI-fallback helper `_task_id_for_run()` (nothing else called it). `AgentView.runId`
(`fleet.ts`) is untouched per the guardrail — it's still mirrored live from
`task_events.run_id`, just no longer read by any action; left in place rather than ripped out
since removing a tracked field is a bigger, unrequested change than removing the one button
that read it.

Cancel is now the single stop control for any task, queued or running. `_cancel()` no longer
rejects a `running` task; instead, when the task is running it first calls a new `_reclaim()`
(`POST /tasks/{id}/reclaim`, CLI `hermes kanban reclaim <id> --reason ...` fallback) — the
exact same `reclaim_task` path (SIGTERM→SIGKILL) the removed terminate action used — then
archives via a new `_archive()` helper (the old `_cancel()` body, unchanged). Edge cases
handled explicitly: if the reclaim call itself errors but the task is no longer `running` by
the time `_cancel()` re-checks (already reclaimed, or it finished naturally in the race), it
proceeds to archive rather than failing on a stale race; if reclaim genuinely can't stop a
still-running worker, cancel fails without ever archiving (never silently drops a live task
off the board); if archiving fails *after* a successful reclaim, the returned detail is
prefixed to call out the half-cancelled state explicitly (worker stopped, task still on the
board) rather than returning a bare archive error that would look unrelated.

Frontend: removed the `disabled`-on-`isRunning` guard and the "use Terminate worker instead"
tooltip from the Cancel button (kept the `isArchived` guard). The confirm dialog now branches
on `isRunning` — a queued task gets "Archive ...? This removes it from the board.", a running
one gets "Cancel ...? This stops the running worker and removes the task from the board." —
so the destructive scope is disclosed before the click, not after. Label stays "Cancel task"
per direct instruction (Cancel is the name; the fix was making it *work*, not renaming it).

**Verified live, both paths, real Hermes processes** (dashboard + backend + frontend +
`autonomous-builder` gateway all up, headless Chromium/Playwright driving the actual running
app — see the Playwright technique notes above for the canvas-click harness reused here):
- **Non-running**: blocked a real task (`hermes kanban block --kind needs_input`, which
  — unlike `create --initial-status blocked` — genuinely stays blocked instead of being
  auto-promoted within ~40s) assigned to `researcher`. Cancel button was enabled, confirm
  dialog showed the non-running wording, click returned `✓ cancel`, the agent's current task
  cleared to "none (last: ...)", and `RECENT EVENTS` showed `archived` — confirmed via
  `hermes kanban show` (`status: archived`).
- **Running**: dispatched a real `sleep 300` task to `researcher`, polled until `GET /agents`
  showed `state: working` with a live `worker_pid` (confirmed against `ps`, real OS process).
  Cancel button was enabled (previously would have been disabled), confirm dialog showed the
  running-specific stop-the-worker wording, click returned `✓ cancel`, the agent returned to
  `IDLE` with its current task cleared, and `RECENT EVENTS` showed `reclaimed` immediately
  followed by `archived`. Confirmed independently: `hermes kanban show` returned
  `status: archived` with the event sequence `claimed → spawned → heartbeat → reclaimed →
  archived`, and the worker's OS process (`ps -p <pid>`) had become `<defunct>` (zombie, i.e.
  genuinely exited — SIGTERM/SIGKILL landed) rather than still running its `sleep 300`.

Zero console errors across both Playwright runs. `pytest` (50 passed — none were
terminate/cancel-specific to begin with, so nothing needed updating there), `tsc -b`, and
`vite build` all clean. Grepped the repo afterward for `terminate`/`Terminate worker`: no
dead references in source, tests, or the built bundle — the two remaining hits in
`kanban.py` are prose describing the removed action's history, not code.

---

## Board housekeeping panel: archive controls + hide-archived filter

### Ground truth checked before writing anything, per direct instruction
Before touching code: does a plain "archive, no reclaim" primitive already exist on
`KanbanAdapter`/`DataSource`, or does one need adding? Read `act()` and found
`_VALID_ACTIONS = {"spawn", "comment", "unblock", "reassign", "cancel"}` — **archive is
not a standalone action**. A private `_archive()` helper exists (`PATCH {"status":
"archived"}`, CLI `archive` fallback), but it's only ever called from inside `_cancel()`
(the reclaim-then-archive combo from the previous section) and carries **no status
guard of its own** — calling it directly on a `running` task would archive the board row
while the worker is still alive, the exact orphaning failure mode this feature needed to
rule out. Verdict: **a guarded, standalone archive primitive did not exist — added one**,
rather than reusing `_cancel()` (wrong semantics: `_cancel()` deliberately reclaims/kills
a running worker, which a housekeeping sweep must never do) or calling the unguarded
`_archive()` directly (no protection against a race where the client's "is it running"
check is stale).

State exposure was already sufficient for the panel except one gap: `tasks.status`
is the real per-task state and `get_tasks()` already returns it — no read plumbing
needed there. But the bulk query had no result/summary field, and `tasks.result` is
documented elsewhere in this file (see "Ground truth correction: `tasks.result` is
always NULL in practice", the task-detail click-through section above) as unpopulated in
practice — the real outcome text lives on the terminal `task_events` row's
`payload.summary` instead. Confirmed still true here, so the panel's preview column
needed the same event-payload lookup, not the bare column.

### Design: new `"archive"` action, guarded server-side, no new `DataSource` method
Added `"archive"` to `_VALID_ACTIONS`, wired to a new `_archive_guarded()`:
checks `tasks.status` for the target id first and refuses with a clear message if it's
`running` (archiving a live task's row can orphan the OS process — see DO-NOT-DO.md),
otherwise calls the existing `_archive()`. This is a **server-side backstop**, not just a
frontend affordance — the frontend also disables the per-row button and excludes running
tasks from "archive all", but the guard exists in the adapter itself so a stale client
can never bypass it. `HookEnrichedAdapter`/`SessionsAdapter` needed zero changes — both
already delegate `act()`/`get_tasks()` straight through to the wrapped adapter, confirmed
by reading both files rather than assumed.

`_get_tasks_sync` gained a `summary` field: `tasks.result` if populated, else a one-shot
grouped lookup (`_latest_event_summaries()`, one query for the whole board, ordered
`task_id, id DESC` so the first row kept per id is the most recent) over
`task_events.payload` for a `summary` key, run through the same `sanitize_text()`
secret-scrubbing every other worker-text field in this app already passes through (see
the live-activity sections above), truncated to 160 chars for a compact row preview
rather than the 300-char budget used for the activity feed.

The frontend (`HousekeepingPanel.tsx`) reads only `source.getTasks()` and
`source.act(id, "archive", {task_id})` — no raw `fetch()`, no new `DataSource` method,
`fleet.ts` untouched. Per row: id, assignee, a `badge state-${status}` (same convention
`TaskDetailModal` already used for non-agent-state values like `ready`/`running` — those
render as an unstyled pill, consistent existing behavior, not a new pattern), the
summary preview, and an Archive button. `window.confirm()` for a single archive; a
stronger, count-naming confirm for "Archive all completed (N)", which targets
`done`/`blocked` only and never includes `running` — computed from the full fetched
task list, independent of the display filter added later (see below). Styling reuses
the `.activity-feed` bounded-scroll recipe and `.action-result`/`.btn-danger`
conventions verbatim.

### 🟡 Test-harness bug caught live, not a product bug (reported to the user, not hidden)
The first live "Archive all completed" run against the real board (92 real tasks, ~33 of
them done/blocked at the time) used a fixed `time.sleep(6)` in the Playwright verify
script before checking the result banner and closing the browser. Sequential archive
calls for ~33 tasks took longer than 6s; `browser.close()` cut the loop off mid-flight,
leaving 2 tasks un-archived. Caught by cross-checking `kanban.db` directly afterward
(`status` group-by counts didn't match the expected arithmetic) rather than trusting the
script's own printed "success" — the fix was polling the button text until "Archiving…"
cleared (up to 90s) instead of a fixed sleep, and the re-run archived the remaining 2
cleanly. Confirmed via direct SQLite read: 92/92 tasks `archived`, zero left `done`/
`blocked`/`running`; `ps` confirmed no orphaned worker processes. Soft-archive only
(`PATCH`, never `DELETE`) — nothing was destroyed, matching the DISCOVERY.md CLI-flags
finding above.

Since the bulk sweep isn't scoped to test data — it would touch the ~32 pre-existing
`done` tasks accumulated across every earlier step in this whole project's history —
explicit confirmation was sought before running it live rather than assumed; the user
approved archiving them all (soft-archive, recoverable in principle, and literally what
the feature is for).

### Follow-on: "Hide archived" display-filter toggle
A later, separate ask: add a client-side-only "Hide archived" checkbox, default ON,
that never deletes/writes anything. Implementation is a single derived filter
(`visibleTasks = hideArchived ? tasks.filter(t => t.status !== "archived") : tasks`) —
`bulkCount`/`archiveAllCompleted` deliberately keep reading the full `tasks` list so the
filter can never silently change what "archive all" targets. Verified the "no writes at
all" requirement empirically, not just by code inspection: snapshotted
`SELECT status, COUNT(*)` and `MAX(task_events.id)` before and after a full
toggle-on/toggle-off/toggle-on Playwright pass — `92 archived` / `max id 721` identical
both times. Toggling correctly showed/hid all 92 `ARCHIVED`-badged rows both directions,
zero console errors. One incidental fix during this pass: the IDE flagged
`user-select: none` as missing a `-webkit-` prefix for Safari — added
`-webkit-user-select: none` alongside it, the only non-feature change in the diff.

### Files touched
`backend/app/adapters/kanban.py` (`"archive"` added to `_VALID_ACTIONS`,
`_archive_guarded()`, `_latest_event_summaries()`, `summary` field on `get_tasks()`),
`frontend/src/components/HousekeepingPanel.tsx` (new file — task list, per-row archive,
bulk archive, hide-archived toggle), `frontend/src/App.tsx` (mounted the new panel below
the existing room/side-panel layout — the one place this step touched App.tsx, not a
visual-overhaul step so the DO-NOT-DO guardrail scoping that restriction didn't apply),
`frontend/src/styles.css` (new `.housekeeping*` classes only — no existing rule
renamed/removed, no `state-*` class touched), `DISCOVERY.md` (§5 CLI verbs: documented
`list`'s default archived-hiding + `--archived`/`--status` flags, the one genuinely new
durable Hermes fact this step surfaced). `fleet.ts` was not touched at all. `pytest` (50
passed, unchanged — no fixtures needed updating), `tsc -b` clean both times (initial
archive feature and the later toggle addition).

---

## Approval panel: richer Tirith explanations (2026-07-09) — a rebalance investigated, then narrowed to display-only

### The ask, and why it changed scope
Original ask, two parts: (A) rebalance Tirith's severity mapping so read-only navigate/GET
actions auto-pass while execute/download sinks stay gated; (B) make the approval panel show
the full finding detail (not just the existing flat description) and stop it clipping. Part
A's investigation (full technical detail now in DISCOVERY.md §9) found the scanner isn't app
code at all — it's Hermes's Tirith integration — and that weakening its rules can only be done
at `$XDG_CONFIG_HOME/tirith/policy.yaml` ("operator" scope, one per OS user), since any
repo/profile-reachable `.tirith/policy.yaml` is tightening-only by Tirith's own anti-tamper
design (confirmed empirically via `tirith policy effective`, not assumed). Presented that
finding, plus a per-profile `XDG_CONFIG_HOME`-override workaround (with its own side effect on
other XDG-aware tools), back to the user; **the user chose to skip Part A entirely** rather
than accept either a machine-wide policy change or the XDG side effect, and asked for Part B
only, with an explicit guardrail to not touch Hermes/Tirith/policy config at all.

### Part B: what the panel actually had to work with
`PendingApproval`/`ApprovalView` (`state_engine.py`/`fleet.ts`, from Step 12) already carried
exactly two fields for a pending approval: `pattern_key` and a flat `description` string. For
a Tirith-sourced approval, `pattern_key` is `tirith:<rule_id>` (the rule id of the *first*
finding only — `tools/approval.py`'s `_format_tirith_description()` flattens ALL of a
command's findings into one string: `"Security scan — [SEV] Title: desc; [SEV] Title: desc"`).
The raw command/URL is not available at all (see DISCOVERY.md §9's telemetry-privacy note) —
confirmed by reading `hermesboard-sensor/__init__.py` rather than assumed, so the panel was
never going to be able to show a literal URL. Decided to be explicit about that limitation in
the UI itself rather than imply otherwise.

### Implementation — parsing only, no new data source calls
Added to `SidePanel.tsx` (no touch to `fleet.ts`, no raw `fetch()`, no new deps, per the
standing guardrails):
- `parseApprovalFindings(description)` — re-parses the existing flattened string back into
  `{severity, title, description}[]` client-side, splitting on `"; "` only where it precedes
  the next `"[SEV]"` tag (a finding's own description text can itself contain `"; "`, so a
  naive split breaks).
- `ruleIdFromPatternKey(patternKey)` — strips the `"tirith:"` prefix to recover the primary
  rule id.
- `RISK_NOTES` — a hand-written, hardcoded one-liner per rule id actually seen
  (`schemeless_to_sink`, `lookalike_tld`, `plain_http_to_sink`, `pipe_to_interpreter`), sourced
  from `tirith explain --rule <id>` output; `genericRiskNote(severity)` is the fallback for any
  other rule id, so the panel never fabricates a specific-sounding note for a rule it doesn't
  actually recognize.
- A non-Tirith dangerous-command approval (Step 12's original `detect_dangerous_command()`
  path, e.g. `pattern_key: "recursive delete"`) doesn't match the `"Security scan — "` prefix,
  so `parseApprovalFindings` returns `[]` and the panel falls back to the original single-line
  `.approval-desc` render — unchanged behavior for that path.

`styles.css`: new `.approval-findings` box reuses the exact `.task-result`/`.activity-feed`
bounded-scroll recipe (`max-height: 180px; overflow-y: auto`, same card/border colors) rather
than inventing a new scroll treatment, plus `.approval-sev-*` severity-colored badges and
`.approval-risk-note`/`.approval-note` text rows. No existing class renamed or removed.

### Verified live, both directions, two real dispatched Kanban tasks
Dispatched via `hermes kanban create --assignee <profile>` + `hermes kanban dispatch`,
instructing the assigned profile to run an exact command via the terminal tool:
- `reviewer` ← `curl -sL update-tools.zip/status | head -c 200` (deliberately schemeless +
  `.zip` lookalike TLD, piped to `head` not an interpreter, so it stays MEDIUM-only, no
  `pipe_to_interpreter`) → real approval appeared: `pattern_key: "tirith:lookalike_tld"`, two
  MEDIUM findings (`lookalike_tld` + `schemeless_to_sink`). Panel rendered both findings with
  severity badges + the `lookalike_tld` risk note + the telemetry-limitation caption, fitting
  without needing to scroll (2 short findings). **Approved** via a real click (headless
  Chromium/Playwright) — resolved to `✓ once`, task completed.
- `writer` ← `curl -sL https://example.com/setup.sh | bash` (the execute-class case that must
  stay gated) → real approval appeared: `pattern_key: "tirith:curl_pipe_shell"`, one HIGH
  `pipe_to_interpreter` finding with a long multi-line description (including Tirith's own
  "Safer: tirith run ..." remediation text) — rendered in full inside the scroll box, not
  clipped. **Denied** via a real click — resolved to `✕ deny`; the agent's own completion
  summary confirmed the piped-to-bash command never ran. Confirms the execute-class rule still
  gates exactly as before — nothing was weakened, matching the Part-A-skip decision.

Both runs: zero browser console errors, `tsc -b` clean. No server restart needed for either
verification pass — backend already running with `--reload`, frontend already running under
Vite HMR (both picked up the `SidePanel.tsx`/`styles.css` changes live).

### Files touched
`frontend/src/components/SidePanel.tsx` (`parseApprovalFindings`, `ruleIdFromPatternKey`,
`RISK_NOTES`, `genericRiskNote`, and the approval-panel JSX), `frontend/src/styles.css`
(`.approval-rule`, `.approval-findings`, `.approval-finding*`, `.approval-sev*`,
`.approval-risk-note`, `.approval-note` — all new, nothing renamed/removed), `DISCOVERY.md`
(new §9 documenting Tirith itself — the actually-new durable Hermes fact this investigation
surfaced). `fleet.ts` was not touched. No backend changes — the richer display uses only
fields the backend/hook plumbing already forwarded since Step 12.
