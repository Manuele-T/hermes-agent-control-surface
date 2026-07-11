# Claude Code Build Plan — Hermes Agent Control Surface (v2, grounded in Step 0)

One prompt per step. Give them in order. **Step 0 is done** — its findings are baked
into this document and DISCOVERY.md. Where this plan and older web research disagree,
the Step 0 findings win.

**Prompt budget:** ~15 steps. v1 (shippable) = Steps 0-8. v2 = Steps 9-14. Optional
native plugin = Step 15. Realistically ~11-13 prompts to a working v1.

**Environment note:** everything runs on native Linux (Hermes 0.18.0). `~` always
resolves to the Linux home, so DB paths and the dashboard are always local and correct.
The old WSL/Windows boundary — and every gotcha it caused — is gone.

---

## What this tool is

**One-line:** a live, agent-centric *control surface* for a multi-agent Hermes setup.
Every agent is a character in a room, animated by what it's doing right now, and you
operate the whole fleet by clicking on them.

**The problem:** running several Hermes agents at once (via the Kanban board) means
work happens across background OS processes you can't see. The stock dashboard shows a
board of cards in columns — it answers "what is the task queue doing," not "what is
each *agent* doing right now, and how do I act on the one that needs me?" Today you spot
a stuck agent on the board, then go to a terminal to fix it. Seeing and doing live apart.

**What it does:** reframes the view from task-centric to **agent-centric** and merges
seeing + doing:
1. **Live visualisation** — each agent (Kanban worker / profile) is a character whose
   animation reflects its real state: idle, working, blocked, awaiting approval.
2. **Click for status** — current state, task, recent events, tokens, elapsed time.
3. **Click to ACT (the differentiator)** — reassign, unblock, comment, spawn, cancel,
   approve/deny, all from the same panel, no terminal.

**What's different:** cute visualisers (hermes-pixel-agents, agentroom) and dashboards
(mission-control) already exist, but they only *watch*. None lets you act on an agent
from the view. The novelty is the **control layer**, not the room.

**Ships two ways (both in this plan):** a standalone local-first tool others install
(Steps 0-6, pipx/Docker), and a public synthetic-data demo for your portfolio (Step 7).
Optional native dashboard tab later (Step 15). Not monetised; value is visibility,
credibility, utility.

---

## GROUND TRUTH from Step 0 (verified on this machine)
Full detail in DISCOVERY.md. Load-bearing facts:

1. **Auth — kanban REST is GATED.** `/api/plugins/kanban/` requires
   `Authorization: Bearer <session_token>`. No token gives 401. The `?token=` query form
   works ONLY for the `/events` WebSocket (HTTP routes 401 it). Token =
   env `HERMES_DASHBOARD_SESSION_TOKEN` (ephemeral random if unset; **no file at rest**).
   `~/.hermes/auth.json` is the OpenRouter key — NOT the dashboard token; never read or
   commit it.
2. **Two write paths.** (a) Co-launch the dashboard with a known
   `HERMES_DASHBOARD_SESSION_TOKEN` and send it as a Bearer header; or (b) use the
   `hermes kanban <verb>` CLI (no token). Both hit the same `kanban_db` layer. Reads can
   also go direct to the SQLite DB read-only (`file:<path>?mode=ro`, WAL-safe).
3. **Dispatch lives in the GATEWAY, not the CLI/dashboard.** A gateway must be running
   for ready tasks to claim/spawn into live workers. `hermes kanban dispatch` /
   `POST /dispatch` is only a manual nudge (returns `Spawned: 0` if a gateway already
   grabbed them). The app must surface a "no gateway gives no live workers" warning.
4. **`task_runs` is the real per-worker record** — one row per attempt with `profile`,
   `worker_pid`, `status`, `outcome`, `last_heartbeat_at`. Drive "who's working now"
   from `task_runs` + `GET /workers/active`, not the `tasks` table alone.
5. **State comes from `task_events` transitions, never status snapshots.** There is no
   single `working` event; "actively working" = `claimed`/`spawned` then a recent
   `heartbeat`; terminal = `completed`/`blocked`/`gave_up`/`timed_out`/`archived`. A
   whole task ran in ~28s, so snapshot polling misses the working phase entirely.
6. **`status=running` is server-rejected (HTTP 400)** — #19535 is guarded API-side, but
   still always create then let the dispatcher claim.
7. **No delegation-tree REST API** (`/api/agents|/tasks|/delegation` give 404). Step 11
   must derive sub-agents from `subagent_stop` events + `/api/sessions`.
8. **Dashboard not running by default.** Nothing listens on 9119 until you run
   `hermes dashboard`. Reads can avoid this by hitting the DB directly; writes via REST
   need it up.
9. **DB path resolves to the Linux home.** `~/.hermes/kanban.db` expands to the native
   home; keep config defaulting to `~/.hermes/kanban.db` and let the runtime resolve it
   — never hardcode the home path. `/health` reports the resolved `kanban_db_path` and a
   `kanban_db_exists` boolean.
10. **Use the `default` board for testing.** Step 0's live run happened there (3 `done`
    cards remain); `army-test` is empty. Don't fight to seed `army-test`.

### Exact write routes (prefix `/api/plugins/kanban`, all Bearer-gated)
| Verb | Method + route | Body |
|---|---|---|
| create | `POST /tasks` | `{title, body?, assignee?, priority, workspace_kind, parents[], ...}`; `?board=` selects board |
| reassign | `POST /tasks/{id}/reassign` | `{profile, reclaim_first, reason}` |
| reclaim claim | `POST /tasks/{id}/reclaim` | `{reason}` (frees a stuck running claim) |
| comment | `POST /tasks/{id}/comments` | `{body, author?}` |
| unblock | `PATCH /tasks/{id}` | `{"status":"ready"}` |
| block | `PATCH /tasks/{id}` | `{"status":"blocked","block_reason":...}` |
| cancel/archive | `DELETE /tasks/{id}` or `PATCH {"status":"archived"}` | |
| kill worker (v2) | `POST /runs/{run_id}/terminate` | `{reason}` (SIGTERM then SIGKILL) |
| edit | `PATCH /tasks/{id}` | `{status?,assignee?,priority?,title?,body?,...}` |
| dispatch nudge | `POST /dispatch?dry_run=&max=&board=` | |
| reads | `GET /board, /tasks/{id}, /stats, /assignees, /profiles, /workers/active, /runs/{id}, /runs/{id}/inspect, /tasks/{id}/log` | |
| live feed | `WS /events?since=<id>&board=<slug>&token=<tok>` | tails task_events, batches up to 200 every 300ms |

Other useful endpoints: `GET /api/sessions` (rich per-session rows incl. token/cost —
for Step 13 independent profiles + Step 14 cost HUD); `GET /api/dashboard/plugins`
(public; lists the bundled kanban plugin — for Step 15).

---

## Two rules that apply to every step
1. **The UI talks ONLY to the data-source interface** (getAgents / getTasks / subscribe
   / act). No React component reads the DB or calls Hermes directly. Keeps v1→v2 additive
   instead of a rewrite.
2. **For writes**, use the Bearer-gated REST API or the `hermes kanban` CLI. Never write
   to the DB directly; never set `status=running` by hand (400-rejected anyway).

## Mistakes to avoid
- Treating the kanban REST API as unauthenticated (it's Bearer-gated).
- Using `?token=` on HTTP routes (WS-only; HTTP needs the Bearer header).
- Reading `auth.json` for the dashboard token (wrong file — it's the LLM key).
- Expecting live workers with no gateway running.
- Driving state from status snapshots instead of `task_events` transitions.
- Hardcoding/committing the token. Read from env/config.
- Letting components touch data sources directly (breaks the v2 path).
- **Deriving "current task" only from active runs** — a freshly created, unclaimed
  (`ready`) task has no run yet and will incorrectly show as "no current task" unless you
  also check for an open/assigned task with no terminal status (see Step 5 notes).

## Test data (you already have some)
The `default` board has 3 `done` cards from Step 0 — enough for read testing. To make
*live* activity (so you can watch states change), with a gateway running:
```bash
hermes kanban create "Research X and write 800 words" --assignee researcher
```
A gateway auto-dispatches within ~60s; or `hermes kanban dispatch` to nudge.
**Short-task problem:** trivial tasks finish in seconds. Mitigations: long prompts;
queue many tasks; a `sleep 120` task to pin an agent in `working` for 2 min; or develop
against the SyntheticAdapter. Use `--workspace dir:<p>` to persist a finished task's
output (scratch is wiped on completion). See `test-army-setup.md` for a ready-made
3-agent research → write → review pipeline for a busier floor (e.g. a demo GIF).

---

## STEP 0 — Discovery spike  (DONE)
**Goal:** ground everything in real machine output before app code. Results in
DISCOVERY.md and the GROUND TRUTH block above.

Produced: real `tasks`/`task_events`/`task_runs` schemas + sample rows; the full
event-kind vocabulary; the exact Bearer-gated REST routes (quoted from `plugin_api.py`);
confirmation that `status=running` is 400-rejected; the heartbeat "working" signal;
proof that **multiple profiles run as concurrent Kanban workers** (3 distinct PIDs, one
per profile); the auth correction; the gateway-owns-dispatch correction; and the
no-delegation-API finding.

*If you re-run it (e.g. after a Hermes upgrade):* read-only Python/bash scripts that dump
the DB schema + sample rows for the `default` board, probe the Bearer-gated REST API with
a co-launched `HERMES_DASHBOARD_SESSION_TOKEN`, quote write routes from
`plugins/kanban/dashboard/plugin_api.py`, confirm the heartbeat signal, and re-verify
concurrent multi-profile workers via `ps` + `task_runs`. Write it verbatim to
DISCOVERY.md with an "assumptions confirmed/broken" summary. Never write to a DB.

## STEP 1 — Scaffold + data-source interface  (DONE)
**Goal:** skeleton + the abstraction + config.

> Create a repo with a Python FastAPI backend and a React (Vite) frontend. Define a
> `DataSource` interface with exactly these methods: getAgents(), getTasks(),
> subscribe(onEvent), act(agentId, action, payload). Add a config module that loads from
> env/config: the kanban DB path (`~/.hermes/kanban.db`, default board), the dashboard
> base URL (`http://127.0.0.1:9119`), the board slug (`default`), and the dashboard
> session token (`HERMES_DASHBOARD_SESSION_TOKEN`). Create a stub adapter implementing
> the interface with empty returns. Add a /health endpoint. The React app imports ONLY
> the DataSource interface, never anything Hermes-specific.
>
> The config module must `expanduser().resolve()` the DB path to an absolute path at load
> time. On startup, if the resolved DB file does not exist, log one loud WARNING naming
> the exact resolved path (do NOT crash — the stub doesn't read the DB yet). `/health`
> must report the resolved `kanban_db_path` and a `kanban_db_exists` boolean.

**Verify (passed):** `/health` shows `kanban_db_path` at the Linux home and
`kanban_db_exists:true`; token surfaced as a boolean, never the value.

## STEP 2 — Kanban read adapter + live events  (DONE)
**Goal:** real reads + a live event stream.

> Implement KanbanAdapter.getAgents() and getTasks() reading the kanban DB read-only
> (`file:<path>?mode=ro`, WAL-safe, never lock it). If the resolved DB path does not
> exist, raise a clear error naming the path — never read a phantom file. For "who is
> working right now" derive from the `task_runs` table (profile, worker_pid, status,
> outcome, last_heartbeat_at) reading the DB directly — this is the ONLY required path for
> Step 2. `GET /workers/active` is an OPTIONAL enrichment that needs the dashboard running
> AND a Bearer token (neither is set up until Step 4), so treat it as best-effort: if no
> token/dashboard, skip it silently and rely on the DB. Do NOT make REST a hard dependency
> here.
> Implement a /events stream (SSE or WebSocket to the browser) that tails the `task_events`
> table by its monotonic `id` and pushes new rows; track each client's last-seen id. Poll
> with a FRESH read each tick (no long-lived read transaction) so new WAL commits are
> visible. Expose /agents and /tasks. State MUST be derived from `task_events` transitions
> (created, claimed, spawned, heartbeat, completed/blocked), never from status snapshots —
> tasks finish in ~28s so snapshots miss the working phase.

**Verify (passed):** `curl /agents` showed real profiles/workers (DB-only, no dashboard
needed); a task created via `hermes kanban create` + manual dispatch streamed its full
lifecycle (`created → claimed → spawned → heartbeat → completed`) through `/events` as
proper SSE frames with the monotonic cursor working.

## STEP 3 — Frontend room (read-only)  (DONE)
**Goal:** the visual.

> Build the React room: one character per agent in a room/floor layout. Derive a coarse
> state (idle / working / blocked / done) from `task_events` transitions + heartbeat
> recency (no hooks yet). Draw assignment edges between agents and tasks. Subscribe to
> /events for live updates; reconnect on drop. Clicking a character opens a side panel with
> its status and recent events. Keep rendering simple (state-driven CSS/sprites). Do NOT
> snap to idle when a task ends: show a brief `done` flash, then fall to idle only after a
> short cooldown (a few seconds with no new events) — this stops fast tasks from looking
> like nothing happened.

**Verify:** agents appear; states change live when you drive the board via CLI.
**Pitfall:** idle gated behind a cooldown, not the instant a task completes.

## STEP 4 — Control layer (safe writes)  (DONE)
**Goal:** act() for the safe verbs.

> Implement act() for spawn-task-onto-agent, comment, unblock. The kanban REST API is
> Bearer-gated, so writes send `Authorization: Bearer <HERMES_DASHBOARD_SESSION_TOKEN>`
> (from env/config, never hardcoded). If no token is configured, fall back to the
> `hermes kanban <verb>` CLI, invoked as a DIRECT subprocess with a proper argv list (not
> a shell string) so quoting is safe. Routes: create `POST /tasks` (with assignee; let the
> dispatcher claim — never status=running, it's 400-rejected), comment
> `POST /tasks/{id}/comments`, unblock `PATCH /tasks/{id} {"status":"ready"}`. Wire as
> click-actions in the character panel. Validate the assignee is a real profile first.
> Surface a clear warning if no gateway is running (spawned tasks won't become live
> workers). This is the step where you create a real `.env` (gitignored) with
> `HERMES_DASHBOARD_SESSION_TOKEN` and co-launch the dashboard with that same value.

**Verify:** with a gateway running, clicking spawn creates a task a worker picks up;
comment and unblock work. **Pitfall:** Bearer header for HTTP (not `?token=`); use the
claim path; CLI fallback is a direct subprocess with an argv list.

## STEP 5 — More control + safety polish  (DONE)
**Goal:** reassign + cancel, safely.

> Add act() for reassign (`POST /tasks/{id}/reassign {profile, reclaim_first, reason}`)
> and cancel-queued-task (`DELETE /tasks/{id}` or `PATCH {"status":"archived"}`). Use
> optimistic UI updates with rollback if the call fails. Add a confirm dialog for
> destructive actions. Refresh from /events rather than trusting local state. Do NOT
> implement killing a running worker yet (that's `POST /runs/{run_id}/terminate`, deferred
> to v2).

**Verify (passed):** reassign moves a task; cancel removes a queued one; failures roll
back.

**Two real bugs found + fixed during manual verify** (full detail in DISCOVERY.md):
1. **"Current task" didn't show ready/unclaimed tasks.** The backend derived
   `current_task_id` only from an active run (`task_runs.ended_at IS NULL`) — a freshly
   created `ready` task has no run yet, so it showed as "none" even though the task existed
   and was assigned. Fixed by adding an "open task" query (most recent assigned task with
   `status NOT IN ('done','archived')`) so an agent reports its queued task with no active
   run, with state `idle`/`blocked` — never a fake `working`. The frontend reducer matched:
   `activeTaskId` now means "owns this task" (ready or running); a separate `claimed`
   boolean gates the `working` animation, set only on `claimed`/`spawned`/`heartbeat`.
2. **Reassign didn't clear the OLD agent's current task.** After reassigning, both agents
   showed the task as current — the reducer only updated the new owner on an `assigned`
   event, never cleared the previous one. Fixed with a `tookOwnership` flag set whenever an
   event hands a task to an agent, followed by a sweep clearing that task id from any other
   agent still holding it.

Neither bug was caught by `tsc`/`npm run build` or the Step 4 REST verify — both only
surfaced from clicking through the running UI by hand. **Manual UI verification after every
step is required, not optional**; type-checks and curl tests don't catch state-derivation
bugs in a live event reducer.

## STEP 6 — Local-first packaging  (DONE)
**Goal:** one-command run.

> Package as a single local-first app: one process serving the API and the built frontend.
> Provide a pipx/`npx`-style runner and a Dockerfile. Add a README and a .env.example
> documenting `HERMES_DASHBOARD_SESSION_TOKEN`, the DB path, board slug, and the "co-launch
> the dashboard with a known token, and keep a gateway running" setup. On startup, if the
> DB or dashboard isn't found, print a clear error. Never commit the token. README must
> warn: never bind the Hermes dashboard to `0.0.0.0`.

**Verify (passed):** one command starts it and it connects on a clean run.

## STEP 7 — Public demo + synthetic data  (DONE)
**Goal:** a shareable portfolio link, no real Hermes attached.

> Create a SyntheticAdapter implementing the SAME DataSource interface, emitting plausible
> agents/tasks/events on a loop (states moving, spawning, finishing, occasional
> blocked/approval). Add a config flag (`HERMES_DATA_SOURCE=synthetic`) to run against it.
> Make it deployable to Render (not Vercel — SSE + in-memory loop require a persistent
> process, which serverless can't provide). On the public demo, disable real control
> actions (read-only: act() always returns an error message).

**Verify (passed):** `HERMES_DATA_SOURCE=synthetic ./run.sh` → 4 agents cycling live
states → /events streams real event kinds → POST /act correctly declined. Zero frontend
changes required — proving the abstraction holds.
**Pitfall:** the synthetic adapter MUST go through the same interface; no token exposed;
no real writes on the public demo. Use Render not Vercel (serverless kills the SSE loop).

## STEP 8 — Visual overhaul: pixel-art robot characters + office room
**Goal:** make the UI good enough to post. Replace emoji-in-a-box with proper SVG
pixel-art robot characters in a styled office setting before v1 ships publicly.

> Replace `Character.tsx` and `styles.css` with a full visual overhaul. Each agent becomes
> a small inline SVG pixel-art robot (no external assets, no new npm deps). The room becomes
> a dark office floor with desk elements suggesting a real workspace. State-driven visuals
> per agent: idle = robot sitting still (dim); working = animated blinking light or subtle
> bob (blue glow); blocked = red warning indicator; done = brief green flash then back to
> idle. Colours obvious at a glance. The side panel keeps all existing functionality but
> gets matching dark styling. Keep all state logic in `fleet.ts` and the existing CSS class
> names (`state-idle`, `state-working`, `state-done`, `state-blocked`) completely untouched
> — this is a pure visual layer swap on top of working logic.

**Verify:** open the app with `HERMES_DATA_SOURCE=synthetic` — robots visible, animated per
state, room has office feel, GIF-worthy. Click-through still works.
**Pitfall:** pure CSS/SVG only, no new npm deps; do NOT rename or remove existing
state-class names or the logic layer breaks silently.

**>>> v1 is shippable after this step. Record GIF, push to GitHub, deploy to Render. <<<**

---
## v2 steps below. Ship v1 first, then continue.
---

## STEP 9 — Hook sensor plugin + firing spike
> Write a minimal Hermes plugin registering pre/post_llm_call, pre/post_tool_call,
> pre_approval_request, subagent_stop, logging each event. Run it across CLI, gateway, a
> kanban-worker run, and `hermes kanban swarm`; produce a firing matrix (event by context)
> — `pre_tool_call` is reported unreliable in kanban-worker (#25204). Then have the plugin
> POST a normalized event envelope to the backend /ingest endpoint, fire-and-forget, never
> blocking the agent, failing open if the backend is down.

**Verify:** firing matrix documented; events arrive at /ingest. **Pitfall:** never block
the agent loop; the heartbeat signal (from Step 0) is your no-hooks backstop where hooks
miss.

## STEP 10 — State-derivation engine  (DONE)
> Build a PURE reducer `(state, event) -> state` mapping hook + task_events sequences and
> timing to fine states: thinking (LLM call in flight), working sub-labelled by tool family
> (coding=bash/edit/write, researching=web/fetch, reading=file reads), writing,
> awaiting_approval, idle (gap past a threshold), error. Unit-test with recorded sequences
> from Step 9. Expose as a new adapter behind the SAME DataSource interface so the UI shows
> fine states with no rewrite. Where hooks are missing, fall back to the Kanban
> heartbeat/transition coarse state.

**Verify (passed):** 14 pytest cases pass — 2 against REAL hook sequences captured live from
this machine's own Hermes (dispatched `hermes kanban` tasks with hermesboard-sensor enabled),
the rest synthetic edge cases (approval, subagent_stop, staleness, purity) shape-matched to
real Hermes source kwargs. Live browser check (headless Chromium via Playwright) confirmed
the side panel showing `WORKING` + an italic `reading`/`thinking` activity label that changed
in real time during a real dispatched task, zero console errors.

**Two real bugs found while building, neither was hypothetical — full detail in
DISCOVERY.md:**
1. The Step 9 per-profile plugin symlinks (`~/.hermes/profiles/<profile>/plugins/
   hermesboard-sensor`) were dangling — leftover from the WSL→Linux migration, still pointing
   at `/home/manue/...`. Hook telemetry for every Kanban worker was silently dead on this
   machine until the symlinks were repointed and the plugin re-enabled per profile.
2. `PLAN.md`'s own fine-state list is internally ambiguous: "coding=bash/edit/write" already
   claims the write tools, then separately lists "writing" as its own state. Resolved as two
   `activity` sub-labels under the same `working` fine_state: `coding` = executing (terminal,
   execute_code), `writing` = authoring (write_file, patch) — see `state_engine.py`'s
   docstring.

**Pitfall:** keep it pure and tested; tune the idle threshold (`STALE_AFTER_SECONDS`, 120s —
also the mechanism that stops a mid-turn crash from freezing the display on "thinking"
forever).

## STEP 11 — Sub-agent visualization  (DONE)
> There is NO delegation-tree REST API (confirmed 404 in Step 0). Derive the sub-agent tree
> from `subagent_stop` events (Step 9) plus `GET /api/sessions` (which carries
> `parent_session_id`). Render subagents as sprites linked to their parent character.
> Remember delegation is flat depth-1 by default; subagents are ephemeral and only a summary
> returns.

**Verify (passed):** dispatched two REAL kanban tasks instructing `researcher` to call
`delegate_task`; captured real `pre_tool_call(delegate_task) → subagent_stop(role="leaf",
status="interrupted")` sequences through `/ingest`, watched the `subagent` SSE frame fire and
the ephemeral sprite appear/disappear correctly against `/agents`' 8s cooldown window, then
drove the actual running app with headless Chromium (Playwright): the researcher character
showed a linked, pulsing "sub-agent" chip below its task chip, and the side panel showed
"↳ delegated: sub-agent (active)" — zero console errors.

**Pitfall confirmed:** no board rows for subagents — done entirely from `subagent_stop`
hook events (Step 9/10 plumbing) plus a best-effort `GET /api/sessions` title lookup. Full
detail, including the real captured payloads and the `/api/sessions` finding, in
DISCOVERY.md.

## STEP 12 — Embodied approvals  (DONE)
> On `pre_approval_request`, make the character raise a hand / show a bubble. Wire
> approve/deny to the approvals queue (confirm its exact API shape first — the stock
> dashboard has a shell-exec approvals queue). Handle timeouts. Never auto-approve.

**Ground truth correction (confirmed by reading `web_server.py`/`tools/approval.py`
directly before building — full detail in DISCOVERY.md):** the stock dashboard has
**no** shell-exec approvals queue REST API at all. The real resolution point,
`tools.approval.resolve_gateway_approval`, is a private, per-session in-memory
function reachable only via a chat-platform slash command, a WebSocket JSON-RPC
channel the desktop app uses, or a separate opt-in "API server" gateway platform
that doesn't cover our Kanban workers. Kanban workers themselves hit a local,
no-TTY, 60s-timeout prompt with **no remote-resolution path** at all by default.

**Design (user-approved after a 3-way tradeoff discussion):** the `hermesboard-sensor`
plugin opts a worker process into Hermes's gateway/queue-based approval path
(`HERMES_EXEC_ASK=1`, never for an interactive TTY or cron session) and calls
`resolve_gateway_approval` directly when the backend records a decision — guarded
end-to-end (ImportError/AttributeError caught separately) so a Hermes upgrade that
moves the private function degrades to telemetry-only, never crashes the worker.

**Verify (passed, live, twice — once approved, once denied):** dispatched a real
Kanban task instructing `rm -rf` on a relative path (avoiding an already-allowlisted
pattern from earlier testing — see DISCOVERY.md); the approval genuinely appeared via
`GET /agents` (`pattern_key: "recursive delete"`); clicked **Approve** via the real
running UI (headless Chromium) — the blocked Hermes worker thread resumed and the
command actually executed, confirmed by the worker's own completion summary. Repeated
with **Deny** — the worker's own summary confirmed the shell command was blocked and
never ran. Side panel showed a live countdown, a raised-hand badge, and the
resolved ✓/✕ flash, all pushed live over SSE with zero console errors.

**Two real bugs found + fixed during this build (not hypothetical, full detail in
DISCOVERY.md):**
1. `tools.approval.get_current_session_key()` is **not stable across a process's
   lifetime** — it resolves to a generic default at plugin-load time but to the
   actual per-turn session key once a conversation turn begins. Registering the
   notify callback once at `register(ctx)` time silently failed at the real
   approval-check moment (looked up under the wrong key) — caught with a temporary
   diagnostic log comparing the two, live. Fixed by re-registering under the
   current key at the top of every hook firing (cheap, self-correcting).
2. The first two live test prompts (`rm -rf /tmp/...`, absolute path) never
   triggered an approval at all — turned out `"delete in root path"` was already
   in the `researcher` profile's persisted `command_allowlist` from earlier
   (Step 8-11) live testing, so `is_approved()` short-circuited before reaching the
   gateway/ask branch. Fixed by testing against a genuinely fresh, unapproved
   pattern (`"recursive delete"`, via a relative path).

**Pitfall confirmed:** the approvals API shape had to be discovered from source, not
assumed — the stock dashboard's `PUBLIC_API_PATHS` allowlist and REST surface have
nothing for this at all.

## STEP 13 — Independent profiles + optional kill  (DONE)

> Before building, grep `web_server.py` / `plugin_api.py` in `~/.hermes/hermes-agent/` for
> the exact `/runs/{id}/terminate` route signature and record it in DISCOVERY.md.
>
> Add an adapter polling `GET /api/sessions` (Bearer-gated; filter `is_active=true`,
> client-side filter for rows where `source` is NOT a kanban-worker task — kanban workers
> already appear via `task_runs`; dedup on `profile` keeping the most-recent row). Each
> distinct active profile becomes a character alongside the existing fleet. Source the
> `run_id` for the kill action from `task_runs` (the real per-worker record), not from the
> sessions row. Add `POST /runs/{run_id}/terminate` behind a confirm dialog, clearly
> labelled best-effort (SIGTERM then SIGKILL). If the endpoint returns an error or the
> `run_id` isn't found, surface the failure message directly — don't promise a clean stop.
> If no independent sessions are found, the fleet renders kanban-only — never error on an
> empty sessions response.

**Ground truth corrections (confirmed by reading the source before building — full detail
in DISCOVERY.md):**
1. `GET /api/sessions` alone only ever queries the DEFAULT profile's own session store and
   never carries a `profile` field unless `?profile=` is passed — it cannot answer "what's
   active across all profiles" at all. The real endpoint is `GET /api/profiles/sessions
   ?profile=all`, which opens every profile's `state.db` directly (read-only) and tags each
   row with its owning `profile`.
2. There is no dedicated "kanban worker" `source` value — a Kanban-dispatched worker and a
   plain interactive CLI session both report `source="cli"`. The real signal confirmed
   against live data is `cwd`: every kanban worker's session cwd sits inside
   `~/.hermes/kanban/workspaces/<task_id>`.
3. `POST /api/plugins/kanban/runs/{run_id}/terminate` (`run_id: int`, body
   `{"reason"?: str}`, optional `?board=`) resolves `run_id` → `task_id` and calls the exact
   same `kanban_db.reclaim_task()` that `POST /tasks/{id}/reclaim` uses — 200
   `{"ok":true,"run_id":...,"task_id":...}`, 404 unknown run, 409 already-ended/not-reclaimable.

**Verify (passed, live):** dispatched a real long-running (`sleep 180`) Kanban task,
confirmed `run_id` appeared live via `GET /agents` and stayed in sync with the SSE
`task_events` stream; called `POST /act {action:"terminate"}` — the real OS worker process
was confirmed gone (`ps` before/after) and the task returned to `ready` with a real
`reclaimed` event (`terminated:true, sigkill:false`). Separately launched a genuine
independent, non-Kanban `hermes -p autonomous-builder chat -q ...` session and confirmed it
appeared live as a 5th character (`GET /agents` → `independent:true`) alongside the
existing 4-agent Kanban fleet, then drove the actual running app with headless Chromium:
the character rendered with an "independent · cli" tag, and the side panel showed a real
"Independent session" info block (source, last-active, cwd, preview, cost) with the
Kanban action buttons gracefully disabled (no task to act on) — zero console errors.

**Pitfall confirmed, plus one caveat found live:** `run_id` is sourced from `task_runs.id`
(SQL) at read time and kept live from `task_events.run_id` over SSE — never from the
sessions API, which has no such field. Live caveat: `is_active` is a
`last_active`-recency heuristic (< 300s), not real process liveness — a killed/crashed
independent session can still show as "active" for up to 5 minutes afterward, same
category of staleness as a crashed Kanban worker's dangling `task_runs` row from Steps
11-12. Documented, not hidden.

## STEP 13v2 — PixiJS visual overhaul: sprite robots + office room  (DONE)

> Install `pixi.js` as an npm dependency. Assets are at `frontend/public/sprites/background.jpeg`

> and `frontend/public/sprites/sprites.png` — load from those paths, never inline them.

>

> The sprite sheet (`sprites.png`) is the 0x72 robot tileset. Robots begin at approximately

> x=175 on the sheet; each row is a different robot type; IDLE frames are the first 3 columns,

> WALK frames are the next 6 columns; each frame is 16x16px. Pick one robot row per agent

> (cycle through rows so agents look distinct). Use the WALK frames for working state (loop),

> IDLE frame 0 for idle/done, IDLE frame 1 for blocked. Scale sprites 3x for visibility.

>

> Replace `Character.tsx` with a single shared PixiJS Application on one `<canvas>` covering

> the full room. Render `background.jpeg` scaled to fill the canvas. Place agents in the open

> floor area (centre of the image, roughly y=55-70% of canvas height, spread horizontally with

> even spacing) — this is the large empty tile area between the desks. Agents patrol: each

> ticks left/right within ±100px of their home x position, reversing at the boundary, sprite

> flipped horizontally when moving left. Apply PixiJS `tint` per state: idle=0xaaaaaa,

> working=0x4488ff, blocked=0xff4444, done=0x44ff88. Each agent Sprite must be

> `interactive=true`, `cursor="pointer"` — clicking fires the existing `onSelect(agentId)`

> unchanged. Make sure to set a sprite.scale.set(3) per agent.

>

> Keep ALL existing CSS class names and all logic in `fleet.ts` completely untouched.

> Destroy the PixiJS Application on unmount.

**Verify:** `HERMES_DATA_SOURCE=synthetic` — robots walk across the open office floor,

colours change per state, clicking opens the side panel.

**Pitfall:** single shared PixiJS Application for the whole room, not one per agent.

Create inside `useEffect`, destroy on cleanup. Sprite sheet x-offset for robots is ~175px —

do not start slicing from x=0 or you'll hit the tileset props on the left half.

**Ground truth correction (measured directly against the asset before slicing — full
detail in DISCOVERY.md):** the sheet is NOT "3 idle + 6 walk columns starting ~x=175".
A pixel-content scan found 9 robot rows, each a 16x32 cell; IDLE is 4 columns starting
x=210, WALK is 8 columns starting x=290. Row 3 (a spider-legged robot) only has 4
populated walk frames, not 8. Used the measured grid, not the approximate description.

**Verify (passed, live, both synthetic and real backends):** installed `pixi.js` (v8.19.0).
Drove the actual running app with headless Chromium against `HERMES_DATA_SOURCE=synthetic`
— all 4 robots rendered in the open floor area, correctly tinted per state (idle=grey,
working=blue, blocked=red — one flipped live from working→done mid-session, confirmed
reactive), "working" robots visibly patrolled and animated through their walk cycle between
two screenshots 4s apart, idle/done robots stayed put, clicking a sprite opened the real side
panel (`onSelect` fired unchanged) — zero console errors. Re-verified against the REAL
Kanban-backed app (port 5173, real `SessionsAdapter`) too: 4 real agents rendered correctly,
and dispatching a genuine Kanban task turned the assigned agent's sprite blue and patrolling
live via the real SSE `task_events` stream — confirms `fleet.ts`'s state model needed zero
changes, exactly as required.

**One real bug found and fixed live:** React 18 `StrictMode` (already enabled in
`main.tsx`) double-invokes effects in dev, and the initial cleanup called `app.destroy()`
while `app.init()` was still in flight — threw `this._cancelResize is not a function`
because PixiJS's resize plugin hadn't finished wiring itself up yet. Fixed by having the
effect's cleanup chain the destroy call onto the same init promise (`ready.then(() =>
app.destroy(...))`) so it can never run before init has genuinely settled, regardless of
how many times StrictMode invokes the effect.

**One placement bug found and fixed live:** the initial 15%-85%-width floor bounds put the
4th (rightmost) agent directly on top of the background's mid-right printer/desk cluster,
nearly camouflaging it. Tightened to 22%-76% after measuring the actual open-floor tile
area in `background.jpeg` directly (furniture clusters start around 24%/78% width) —
confirmed live: all 4 agents now render clearly inside the open floor, no overlap.

**Pitfall confirmed:** one shared `Application` for the whole room (Room.tsx now renders a
single `<Character agents={...} .../>` instead of mapping one per agent); created once in a
mount-only `useEffect`, destroyed only via the cleanup's deferred `ready.then(...)`. `fleet.ts`
was not touched at all (only reads `visibleState()`); every CSS `state-*` class name still
exists in styles.css untouched, though the new canvas renderer no longer applies them
directly (state is now expressed via PixiJS `tint`, not the DOM). The old per-character DOM
overlays from Steps 8/11/12 (task-chip, subagent-chip, approval-badge on the card itself) are
superseded by this canvas renderer per PLAN's explicit narrow scope — they're unaffected in
the SidePanel, which this step didn't touch.

## STEP 14 — Polish pass  (DONE)
> Polish the PixiJS room: tune walk cycle frame rate (target ~8fps for the walk loop),
> add a subtle idle breathing bob (slow y sine wave, ±2px), smooth state transitions with
> a brief tint flash on change. Add a PixiJS overlay filter that dims the whole canvas
> slightly when all agents are idle/blocked — cleared when any agent is working. Add a
> small global token/cost HUD in the React layer (outside the canvas) fed by
> `/api/sessions` (estimated_cost_usd / token fields). Optimistic control feedback:
> button states must reflect in-flight actions instantly, with clear success/fail messages.
> Agents patrol in 2D across the open floor area — not just horizontal, but a natural
> wandering path (random target point within the floor bounds, walk toward it, pick a new
> target on arrival). Keep everything driven by real events.

**Verify:** animations feel alive, not janky; agents wander naturally across the floor;
cost HUD updates live; control feels instant.
**Pitfall:** a laggy or wrong-firing control surface is worse than a read-only mirror.
Note: a future variant may replace wandering with agents seated at desks — keep agent
position logic isolated so it can be swapped without touching state or rendering code.

**Ground truth correction (confirmed live against the real dashboard before building —
full detail in DISCOVERY.md):** `GET /api/profiles/sessions?profile=all`'s own
`profile_totals` field is a per-profile session COUNT, not a cost/token total — there is no
pre-aggregated cost field anywhere in the response. The HUD aggregates `estimated_cost_usd`/
`input_tokens`/`output_tokens` across the real `sessions` array itself (confirmed live:
85 real sessions, ~0.3s, ≈$3.38 / 20.6M in / 470k out tokens).

**Design (kept inside the two architecture rules — no new DataSource method):** the cost
HUD is pushed as a global, id-less "costs" SSE frame through the existing `/events` stream
— the same synthetic-frame idiom Steps 10-12 used for fine-state/subagent/approval data —
so `CostHud.tsx` only ever reads `useFleet().costs`, never fetches anything itself.
`SessionsAdapter.cost_summary()` (real, 5s-TTL-cached aggregate) and
`SyntheticAdapter.cost_summary()` (a plausible ever-growing counter) both implement it;
a bare `KanbanAdapter`/`HookEnrichedAdapter` (no token) or `StubAdapter` just never sends a
frame, and the HUD renders nothing rather than a misleading zero.

**Verify (passed, live, headless Chromium against an isolated synthetic backend+dev-server
pair — the user's own running 8124/9119 stack was never touched):** cost HUD appeared on
first paint (`$0.25 · 15.0k tok`) and visibly climbed over ~14s (`$0.60 · 30.3k tok`),
confirming both the immediate first push and the 10s periodic push; two working agents'
screenshots showed genuinely different (x, y) positions — real 2D wander, not the old
single-line patrol; clicking a sprite opened the real side panel unchanged; clicking
"Spawn task" showed the correct instant failure banner (read-only synthetic demo), proving
the new per-action `busyAction` state and the existing optimistic-rollback path both still
work. Zero console errors.

**Pitfall confirmed / design note:** `updatePosition()` (movement) and `updateVisual()`
(tint/texture/breathing/flash) are two separate per-tick functions in `Character.tsx`,
exactly per PLAN's isolation note — a future "seated at desks" variant only needs to
replace `updatePosition()` + `computeFloorBounds()`.

## STEP 15 — (Optional) Native Hermes dashboard plugin
> Package the UI as a Hermes dashboard plugin: manifest.json + JS bundle calling
> `window.__HERMES_PLUGINS__.register(name, Component)`, using the SDK on
> `window.__HERMES_PLUGIN_SDK__` (do NOT bundle React — the SDK provides it). Expose the
> backend as a FastAPI router under `/api/plugins/<name>/`. Drop into
> `~/.hermes/plugins/<name>/`. Use theme vars `var(--color-*)`. Note `GET
> /api/dashboard/plugins` is public and lists installed plugins; there's a rescan path.

**Verify:** the tab appears in `hermes dashboard`. **Pitfall:** plugin has ~2s to register
after its script loads; keep the bundle tiny.

---

## Sequencing summary
- **Ship v1 and post about it after Step 8** (visual overhaul done, GIF recorded).
- Steps 1-2 are the foundation. Reads can hit the DB directly (no token, no dashboard
  needed); only REST writes need the dashboard up with a Bearer token.
- Step 10 (state engine) is the deepest, best "engineering depth" content. Step 14 is where
  it stops looking like a tech demo.
- **Standing rule from Step 5 onward: manual click-through in the browser after every step,
  not just `tsc`/`npm run build`/curl checks.** Two real bugs (Step 5) were invisible to
  automated checks and only surfaced from using the actual UI.

## Distribution / launch checklist (after v1)
Full detail in distribution-plan.md. Lead with the control-surface differentiator — the
category is crowded (mission-control, hermes-workspace), so "another dashboard" gets ignored;
"click an agent to act on it" gets a look.
1. README: demo GIF at top, one-line install, differentiator in the first sentence.
2. Nous Discord first (quiet feedback), then fix rough edges.
3. r/hermesagent + r/LocalLLaMA (technical framing, lead with the GIF).
4. Directory PRs: Hermes Atlas + awesome-hermes-agent lists.
5. X (GIF + one sharp sentence, tag @NousResearch) + LinkedIn (career-transition framing).