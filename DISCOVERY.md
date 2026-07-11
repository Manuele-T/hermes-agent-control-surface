# DISCOVERY.md — field notes on a real Hermes install

This document is a set of empirically-probed field notes on the internals of a real,
running Hermes install — the ground truth this control surface's code is built
against. Nothing here is guessed: every claim was checked directly against a live
dashboard, a live Kanban database, or the installed Hermes source. The install drifted
from v0.17 to v0.18 over the course of this work (via `hermes update`); treat anything
version-specific as worth re-confirming against your own install rather than as a
permanent fact.

The per-feature build log — every implementation write-up, the two live-bug
investigations, and the verification runs behind each finding — lives in a
companion file, [IMPLEMENTATION-NOTES.md](IMPLEMENTATION-NOTES.md). This file stays
lean: the headline facts and the durable reference material only.

## Key findings at a glance

- The kanban REST API (`/api/plugins/kanban/...`) is **Bearer-token gated**, not
  open on localhost, despite what an early assumption held.
- **Dispatch lives in the gateway process**, not the CLI or the dashboard — a
  gateway must be running for tasks to be picked up automatically.
- **There is no delegation-tree REST API** (`/api/agents`, `/tasks`, `/delegation`
  all 404) — a sub-agent tree has to be derived from hook events plus session data,
  not a call.
- **State must be derived from `task_events` transitions**, not status snapshots —
  short tasks can complete in under 30 seconds and a snapshot poll can miss the
  entire "working" phase.
- `heartbeat` is a real, **no-hooks "worker is alive" signal** — usable even when
  no hook-based telemetry plugin is installed.
- Setting a task's status to `running` directly is **rejected by the API (HTTP
  400)** — the only valid path is create → let the dispatcher claim it.
- A profile's local **`state.db` `messages` table is the real source for live
  worker activity** (assistant text, tool calls, tool output) — and it turns out
  the model's **reasoning / chain-of-thought IS persisted there too**, in full,
  though this app deliberately never surfaces it in the UI.
- A live worker's `run_id` comes from `task_runs`, not the sessions API — the
  sessions API carries no reliable cross-profile "what's active" signal on its own.
- `is_active` on a session row is a **5-minute recency heuristic, not real process
  liveness** — a killed session can still look "active" for minutes afterward.
- **Plugins are per-profile**: one registered under the default home is invisible
  to Kanban worker subprocesses running under other profiles, unless installed and
  enabled separately for each one.
- A false "no gateway is running" warning turned out to be **Hermes's own
  profile-scoped PID-file check** disagreeing with reality, not a bug in this
  app's own (profile-agnostic) gateway detection.
- The **"Security scan — [MEDIUM]" approval prompts are not this app's code at all** — they
  come from **Tirith**, an external cosign-signed binary Hermes shells out to. Its rule
  severities can only be weakened at `$XDG_CONFIG_HOME/tirith/policy.yaml` ("operator" scope,
  one per OS user) — any repo- or profile-reachable `.tirith/policy.yaml` is **tightening-only**
  by Tirith's own anti-tamper design (confirmed empirically, not assumed).

## Contents

- [Key findings at a glance](#key-findings-at-a-glance)
- [Hermes internals reference](#hermes-internals-reference)
  - [1. Kanban DB](#1-kanban-db)
  - [2. Dashboard + auth — major correction](#2-dashboard--auth---major-correction)
  - [3. REST API shapes](#3-rest-api-shapes-real-json-dashboard-running-bearer-auth)
  - [4. Exact write routes](#4-exact-write-routes-quoted-from-pluginskanbandashboardpluginapipy)
  - [5. CLI verbs](#5-cli-verbs-hermes-kanban---help)
  - [6. Other endpoints](#6-other-endpoints)
  - [7. Heartbeat signal — confirmed](#7-heartbeat-signal--confirmed-)
  - [8. Multi-profile concurrency — confirmed](#8-multi-profile-concurrency--confirmed-yes--the-critical-check)
  - [9. Tirith security scanner — not this app's code](#9-tirith-security-scanner--not-this-apps-code)
- [Full implementation notes](#full-implementation-notes)

## Hermes internals reference

The durable, reusable reference material: DB schema, event vocabulary, the auth
model, the REST route tables, the CLI verb list, and other-endpoints notes. This is
the part worth actually looking up later.

**Tooling caveat:** no `sqlite3` CLI was available in this distro, so every direct
read used `python3`'s stdlib `sqlite3` with `file:<path>?mode=ro` (WAL-safe,
read-only, cannot corrupt the board).

### 1. Kanban DB

Two boards on disk:

```
~/.hermes/kanban.db                              # default board
~/.hermes/kanban/boards/army-test/kanban.db      # army-test board
```

Other boards live at `~/.hermes/kanban/boards/<slug>/kanban.db`. Active board
resolves: `HERMES_KANBAN_BOARD` env → `~/.hermes/kanban/current` pointer →
`default`. Per-board metadata in `boards/<slug>/board.json`.

> **Assumption broken:** the initial assumption was that `army-test` already has tasks for
> `researcher`/`writer`/`reviewer`. It does **not** — `army-test` is **empty**
> (0 tasks, 0 events). Live data was generated on the **default** board instead
> (see §8). Schemas are identical across boards (same `init_db`).

Tables: `tasks`, `task_events`, `task_runs`, `task_comments`, `task_attachments`,
`task_links`, `kanban_notify_subs` (+ `sqlite_sequence`).

#### `tasks` schema (verbatim)
Key columns: `id TEXT PK`, `title`, `body`, `assignee`, `status`, `priority`,
`created_by`, `created_at`, `started_at`, `completed_at`,
`workspace_kind DEFAULT 'scratch'`, `workspace_path`, `branch_name`,
`claim_lock`, `claim_expires`, `tenant`, `result`, `idempotency_key`,
`consecutive_failures`, `worker_pid`, `last_failure_error`,
`max_runtime_seconds`, `last_heartbeat_at`, `current_run_id`,
`workflow_template_id`, `current_step_key`, `skills`, `model_override`,
`max_retries`, `goal_mode`, `goal_max_turns`, `session_id`.

#### `task_events` schema (verbatim) — the live feed
```sql
CREATE TABLE task_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,  -- monotonic cursor
    task_id    TEXT NOT NULL,
    run_id     INTEGER,                            -- groups events by attempt
    kind       TEXT NOT NULL,
    payload    TEXT,                               -- JSON or NULL
    created_at INTEGER NOT NULL
)
CREATE INDEX idx_events_task ON task_events(task_id, created_at);
```

#### `task_runs` schema (one row per attempt — the real per-worker claim record)
`id INTEGER PK`, `task_id`, `profile`, `step_key`, `status`
(`running|done|blocked|crashed|timed_out|failed|released`), `claim_lock`,
`claim_expires`, `worker_pid`, `max_runtime_seconds`, `last_heartbeat_at`,
`started_at`, `ended_at`, `outcome`
(`completed|blocked|crashed|timed_out|spawn_failed|gave_up|reclaimed|null`),
`summary`, `metadata`, `error`.

#### `task_comments`: `id, task_id, author, body, created_at`
#### `task_links`: `parent_id, child_id` (PK both) — dependency edges

#### Real sample rows (default board, after the §8 live run)
3 tasks, all `status=done`:
```json
{"id":"t_654142db","assignee":"researcher","status":"done","worker_pid":null,
 "started_at":1782651202,"completed_at":1782651230,"last_heartbeat_at":1782651216}
{"id":"t_2e4749e4","assignee":"writer","status":"done","completed_at":1782651227}
{"id":"t_6df6c5c6","assignee":"reviewer","status":"done","completed_at":1782651235}
```
`task_runs` (distinct row per assignee/attempt):
```json
{"id":1,"task_id":"t_654142db","profile":"researcher","status":"done","outcome":"completed","started_at":1782651202,"ended_at":1782651230}
{"id":2,"task_id":"t_2e4749e4","profile":"writer",    "status":"done","outcome":"completed","started_at":1782651203,"ended_at":1782651227}
{"id":3,"task_id":"t_6df6c5c6","profile":"reviewer",  "status":"done","outcome":"completed","started_at":1782651203,"ended_at":1782651235}
```
`task_events` for `t_654142db` (the real lifecycle, in order):
```
id=1  created               (run_id=null)
id=4  claimed               (run_id=1)
id=5  tip_scratch_workspace (run_id=null)
id=6  spawned               (run_id=1)
id=11 heartbeat             (run_id=1)
id=15 completed             (run_id=1)
```

#### Full event-kind vocabulary (from `_append_event` call sites in `kanban_db.py`)
Lifecycle: `created`, `assigned`, `claimed`, `claim_rejected`, `spawned`,
`heartbeat`, `completed`, `blocked`, `unblocked`, `scheduled`, `promoted`,
`promoted_manual`, `reclaimed`, `reclaim_deferred`, `timed_out`, `stale`,
`gave_up`, `archived`, `edited`, `specified`, `decomposed`, `linked`,
`unlinked`, `commented`, `attached`, `attachment_removed`,
`tip_scratch_workspace`. Plus dynamic outcome kinds (e.g. `crashed`) and, from
the dashboard plugin's direct writes: `status`, `reprioritized`. Hallucination
advisories: `completion_blocked_hallucination`, `suspected_hallucinated_references`.

**Implication for state derivation:** there is no single `working` event. "Actively
working" = `claimed`/`spawned` then recent `heartbeat`; terminal = `completed`/
`blocked`/`gave_up`/`timed_out`/`archived`. Drive state from these transitions,
not status snapshots (a whole task ran in ~28s here).

---

### 2. Dashboard + auth  ⚠️ MAJOR CORRECTION

- **Port:** default **9119**, host default **127.0.0.1** (`hermes dashboard
  --port --host`). `--insecure` is now a **no-op** (June 2026 hardening).
- **Not running by default.** Before the spike, nothing listened on 9119
  (`curl 127.0.0.1:9119 → HTTP 000`). You must `hermes dashboard` to get the API.
  (`--stop` / `--status` manage it.) The dispatcher does **not** need the
  dashboard — it runs in the **gateway** (see §8).

#### 🔴 `/api/plugins/` is NOT unauthenticated on localhost anymore (v0.17)
The starting assumption was that the kanban REST API is unauthenticated on
localhost. **That is false in v0.17.** Proven empirically against the running
dashboard:

| Request | Result |
|---|---|
| `GET /api/plugins/kanban/board` (no token) | **401 Unauthorized** |
| `GET /api/plugins/kanban/board` + `Authorization: Bearer <token>` | **200** |
| `GET /api/plugins/kanban/board?token=<token>` (query) | **401** |
| `GET /api/status` (no token) | 200 (public allowlist) |

Source confirms it (`plugins/kanban/dashboard/plugin_api.py` docstring): every
`/api/plugins/...` request must present the session bearer token. The allowlist
that bypasses auth (`hermes_cli/dashboard_auth/public_paths.py`,
`PUBLIC_API_PATHS`) contains only `/api/status`, `/api/config/defaults`,
`/api/config/schema`, `/api/model/info`, `/api/dashboard/themes`,
`/api/dashboard/plugins`, `/api/cron/fire` — **not** `/api/plugins/kanban/`.

**Two gates** (`web_server.py`): on a **loopback** bind, `auth_middleware`
requires the bearer `_SESSION_TOKEN` on every `/api/` path not in the allowlist.
On a **non-loopback** bind, an OAuth/password cookie gate engages instead. Either
way, the kanban routes are gated.

#### Where the token comes from
```python
# web_server.py:252
_SESSION_TOKEN = os.environ.get("HERMES_DASHBOARD_SESSION_TOKEN") or secrets.token_urlsafe(32)
```
- Ephemeral per dashboard process unless you set `HERMES_DASHBOARD_SESSION_TOKEN`
  in the environment. There is **no token file** at rest; the dashboard injects it
  into its own SPA HTML as `window.__HERMES_SESSION_TOKEN__`.
- **`~/.hermes/auth.json` is NOT the dashboard token** — it holds LLM provider
  credentials (e.g. an OpenRouter API key). Do not read/commit it.

#### 🔑 Actionable pattern for this app's writes
Launch (or co-launch) the dashboard with a known token and reuse it as a Bearer
header:
```bash
HERMES_DASHBOARD_SESSION_TOKEN=<secret> hermes dashboard --no-open --skip-build
# then: curl -H "Authorization: Bearer <secret>" http://127.0.0.1:9119/api/plugins/kanban/...
```
Read from env/config (never hardcode). The `?token=` query form works **only** for
the `/events` WebSocket, not HTTP routes. **Fallback stays robust:** the
`hermes kanban` CLI needs no token and writes via the same `kanban_db` layer —
prefer it when no token is available, or read the DB directly read-only for reads.

> Keep the README warning: never bind `--host 0.0.0.0`. Even though routes are
> gated, the token is single-secret, not multi-user.

---

### 3. REST API shapes (real JSON, dashboard running, Bearer auth)

`GET /api/plugins/kanban/board` → `{columns:[{name,tasks:[...]}], tenants,
assignees, latest_event_id, now}`. Columns order:
`triage, todo, scheduled, ready, running, blocked, review, done` (+`archived`
when requested). Each task dict = the full `tasks` row **plus** derived:
`age:{created_age_seconds, started_age_seconds, time_to_complete_seconds}`,
`latest_summary`, `link_counts:{parents,children}`, `comment_count`, `progress`,
and optional `diagnostics`/`warnings`. Example task (trimmed):
```json
{"id":"t_654142db","title":"Research: capital of France","assignee":"researcher",
 "status":"done","workspace_kind":"scratch",
 "workspace_path":"~/.hermes/kanban/workspaces/t_654142db",
 "max_runtime_seconds":300,"last_heartbeat_at":1782651216,
 "age":{"created_age_seconds":180,"started_age_seconds":140,"time_to_complete_seconds":28}}
```

`GET /api/plugins/kanban/stats`:
```json
{"by_status":{"done":3},
 "by_assignee":{"researcher":{"done":1},"reviewer":{"done":1},"writer":{"done":1}},
 "oldest_ready_age_seconds":null,"now":1782651343}
```

`GET /api/plugins/kanban/assignees` → union of `~/.hermes/profiles/*` and current
board assignees, each `{name, on_disk, counts:{status:n}}`. Saw:
`autonomous-builder*`, `default`, `researcher{done:1}`, `reviewer{done:1}`,
`writer{done:1}`.

`GET /api/plugins/kanban/workers/active` → `{workers:[...], count, checked_at}`
(empty once tasks finished). Each worker carries `run_id, task_id, task_title,
task_assignee, profile, worker_pid, started_at, claim_lock, claim_expires,
last_heartbeat_at, max_runtime_seconds` — the cross-task live-worker list.

`GET /api/plugins/kanban/profiles` → 200, roster with model/provider/description.

---

### 4. Exact write routes (quoted from `plugins/kanban/dashboard/plugin_api.py`)

All under prefix **`/api/plugins/kanban`**. (Mapped to this app's own action verbs.)

| Verb | Method + route | Notes |
|---|---|---|
| **create** | `POST /tasks` | body `CreateTaskBody{title, body?, assignee?, tenant?, priority, workspace_kind, workspace_path?, parents[], triage, idempotency_key?, max_runtime_seconds?, skills?, goal_mode, goal_max_turns?}`. `?board=` query selects board. |
| **assign / reassign** | `POST /tasks/{id}/reassign` | body `{profile, reclaim_first, reason}`. (Also `PATCH /tasks/{id}` with `assignee`.) |
| **reclaim worker claim** | `POST /tasks/{id}/reclaim` | `{reason}`. Releases a stuck running claim. |
| **comment** | `POST /tasks/{id}/comments` | `{body, author?}`. |
| **unblock** | `PATCH /tasks/{id}` `{"status":"ready"}` | routes to `unblock_task` when current status is blocked/scheduled. CLI: `hermes kanban unblock`. |
| **block** | `PATCH /tasks/{id}` `{"status":"blocked","block_reason":...}` | |
| **cancel / archive** | `DELETE /tasks/{id}` (hard delete) **or** `PATCH /tasks/{id}` `{"status":"archived"}` **or** `POST /tasks/bulk {archive:true}` | |
| **kill running worker** (v2) | `POST /runs/{run_id}/terminate` `{reason}` | SIGTERM→SIGKILL via `reclaim_task`. |
| **status/priority/title/body** | `PATCH /tasks/{id}` | `UpdateTaskBody{status?,assignee?,priority?,title?,body?,result?,block_reason?,summary?,metadata?}`. |
| dispatch nudge | `POST /dispatch?dry_run=&max=&board=` | one dispatcher pass. |
| links | `POST /links {parent_id,child_id}` / `DELETE /links?parent_id=&child_id=` | |
| boards | `GET/POST /boards`, `PATCH/DELETE /boards/{slug}`, `POST /boards/{slug}/switch` | |
| triage helpers | `POST /tasks/{id}/specify`, `POST /tasks/{id}/decompose` | aux-LLM. |
| reads | `GET /board`, `GET /tasks/{id}`, `GET /stats`, `GET /assignees`, `GET /profiles`, `GET /diagnostics`, `GET /workers/active`, `GET /runs/{id}`, `GET /runs/{id}/inspect`, `GET /tasks/{id}/log`, `GET /config` | |
| live feed | `WS /events?since=<id>&board=<slug>&token=<session_token>` | tails `task_events` id>cursor every 300ms, batches ≤200, replies `{events:[...], cursor}`. |

#### 🚫 `status=running` is rejected by the API (matches #19535)
`PATCH /tasks/{id}` with `{"status":"running"}` → **HTTP 400** *"Cannot set status
to 'running' directly; use the dispatcher/claim path"* (same in `POST
/tasks/bulk`). The footgun is server-side guarded — but still: **create → let the
dispatcher claim.**

---

### 5. CLI verbs (`hermes kanban --help`)

`init, boards, create, swarm, list(ls), show, assign, reclaim, reassign,
diagnostics(diag), link, unlink, claim, comment, complete, edit, block, schedule,
unblock, promote, archive, tail, dispatch, daemon(DEPRECATED — use gateway),
watch, stats, notify-subscribe, notify-list, notify-unsubscribe, log, runs,
heartbeat, assignees, context, specify, decompose, gc`.

Global flag `--board <slug>` (defaults to current board). `create` flags include
`--assignee --body --parent --workspace {scratch|worktree|worktree:<path>|dir:<path>}
--branch --tenant --priority --triage --idempotency-key --max-runtime
--created-by --skill --max-retries --goal --goal-max-turns --initial-status
{blocked,running} --json`. `watch` = live-stream `task_events` to terminal;
`tail` = follow one task's events; `heartbeat` = emit worker-liveness event.

`list` **hides archived tasks by default** — confirmed via `hermes kanban list --help`
(`--status {archived,blocked,done,ready,review,running,scheduled,todo,triage}`,
plus a dedicated `--archived` flag to include them, `--assignee`, `--tenant`,
`--session`, `--sort {assignee,created,created-desc,priority,priority-desc,
status,title,updated}`). Confirms archiving a task (`PATCH {"status":"archived"}`,
this app's `_archive()`) is a soft, fully-reversible-looking state change, not a
hard delete — the row stays queryable forever via `hermes kanban list --archived`
or `--status archived`, just filtered out of the default view. Relevant any time a
future feature needs to show or restore archived history (the board-housekeeping
panel, IMPLEMENTATION-NOTES.md, deliberately mirrors this default-hidden-but-
togglable-visible behavior client-side).

---

### 6. Other endpoints

- **Sessions API exists:** `GET /api/sessions` (auth required). Rich per-session
  rows: `{id, source, user_id, model, model_config, system_prompt,
  parent_session_id, started_at, ended_at, end_reason, message_count,
  tool_call_count, input/output/cache/reasoning tokens, billing_provider,
  estimated_cost_usd, actual_cost_usd, cost_status, title, cwd, archived,
  last_active, preview, is_active}` + `{total, limit, offset}`. Query params:
  `limit, offset, min_messages, archived{exclude|only|include}, order{created|
  recent}, source, exclude_sources, profile`. Useful for surfacing independent,
  non-Kanban-board profiles as characters, and for building a cost/token HUD.
- **No delegation-tree API.** `GET /api/agents`, `/api/tasks`, `/api/delegation`
  all → **404**. A delegation-tree HTTP endpoint some designs assume exists is
  **not** actually there. Any sub-agent tree view must be derived from
  `subagent_stop` hook events + session data, not a REST call.
- Plugin manifest (public): `GET /api/dashboard/plugins` lists the bundled
  `kanban` plugin (`tab.path:/kanban`, `has_api:true`) — relevant if this app were
  ever shipped as a native dashboard plugin instead of a standalone tool.

---

### 7. Heartbeat signal — CONFIRMED ✅

`task_events` carries a real `heartbeat` kind (emitted from `kanban_db.py`
`_append_event(conn, task_id, "heartbeat", ...)`; CLI `hermes kanban heartbeat`
"emit a heartbeat event for a running task — worker liveness signal"). Live proof
(`t_654142db`): `…spawned(id=6) → heartbeat(id=11) → completed(id=15)`. Both
`tasks.last_heartbeat_at` and `task_runs.last_heartbeat_at` are maintained
(`1782651216` above). This is a **no-hooks "actively working" signal** you can
read directly — de-risks the #25204 hook gap for working/idle.

---

### 8. Multi-profile concurrency — CONFIRMED YES ✅ (the critical check)

**Do multiple profiles run as concurrent Kanban workers on this machine? → YES.**

Method: created 3 `ready` tasks, one each assigned to `researcher`, `writer`,
`reviewer` (real profiles, model `deepseek/deepseek-v4-flash` via OpenRouter). A
**gateway was already running** (`hermes -p autonomous-builder gateway run`,
pid 334) and **auto-dispatched** them. `ps` captured **three distinct, concurrent
worker processes**:

```
2995  ppid=334  hermes -p researcher ... chat -q work kanban task t_654142db
2996  ppid=334  hermes -p writer     ... chat -q work kanban task t_2e4749e4
3006  ppid=334  hermes -p reviewer   ... chat -q work kanban task t_6df6c5c6
```

Distinct PIDs, one per profile, running at the same time (one finished while two
were still live), each a separate `task_runs` row (run 1/2/3) with its own
claim/outcome. All three reached `completed` in ~25–33s.

**Mechanism** (`kanban_db._default_spawn`, line ~7286): the dispatcher
fire-and-forgets `hermes -p <assignee> chat -q "work kanban task <id>"` per ready
task, pinned to the board via env (`HERMES_KANBAN_TASK/WORKSPACE`, `HERMES_HOME`
per-profile). So **agents = separate profiles = separate OS processes** — the
agent-per-character data model this app is built around holds. No fallback to
"personas within one profile" needed.

**Two non-obvious facts this surfaced:**
1. **The gateway owns dispatch, not the CLI.** `hermes kanban dispatch` returned
   `Spawned: 0` — the running gateway had already claimed/spawned the tasks.
   `daemon` is deprecated → "dispatcher now runs in the gateway." So for the app
   to see live workers, **a gateway must be running** (or call `POST /dispatch` /
   `hermes kanban dispatch` when none is). Surface a dispatcher-presence warning
   (the API already returns one on create).
2. Workers spawn with a **broad default toolset** injected by the gateway
   (browser, terminal, kanban, file, …), not the profile's configured
   `toolsets: [hermes-cli]`. Cosmetic, but explains the `ps` cmdline.

---

### 9. Tirith security scanner — not this app's code

The "Security scan — [MEDIUM]/[HIGH] ..." text in approval prompts does **not** come from
anything in `hermes-agent-control-surface`. It comes from **Tirith** (`sheeki03/tirith`), an
external, cosign-signature-verified binary Hermes shells out to via `~/.hermes/hermes-agent/
tools/tirith_security.py::check_command_security()` — one binary copy per profile, auto-
installed to `~/.hermes/profiles/<profile>/bin/tirith`. The detection rules themselves are
compiled into that binary; there is no Python/regex source for them anywhere in Hermes.

**How a Tirith finding reaches this app:** `tools/approval.py` (~line 2390) folds Tirith's
`warn`/`block` verdict into the **same** `pattern_key`-based approval queue Step 12 already
built on, keyed as `tirith:<rule_id>` (the rule id of the *first* finding only — a command can
trip multiple findings, but only one gets a structured id here). So Tirith findings surface
through the existing `pre_approval_request`/`post_approval_response` hook pair and this app's
`PendingApproval`/`SidePanel.tsx` with no new plumbing needed to *display* them (see
IMPLEMENTATION-NOTES.md's "Approval panel: richer Tirith explanations" section).

**Rule set actually pulled from the binary** (`tirith explain --rule <id>`, this machine's
tirith `0.3.3`):

| rule_id | severity | scope |
|---|---|---|
| `schemeless_to_sink` | MEDIUM | fires only when a schemeless URL feeds a **download/execute sink** (curl/wget/pip/etc.) — a plain, non-piped read isn't "sink" context at all |
| `lookalike_tld` | MEDIUM | `.zip/.mov/.app/.dev/.run` TLDs "resembling file extensions," same sink scoping |
| `plain_http_to_sink` | HIGH | unencrypted HTTP into a download/execute sink |
| `pipe_to_interpreter` | HIGH | piping curl/wget output into `bash`/`python3`/etc. |

**Rebalancing the ruleset is a Hermes-wide/OS-user-wide config decision, not an app change.**
Tirith ships its own policy system (`tirith policy init/effective/validate`), with
`severity_overrides`/`action_overrides`/`additional_known_domains`/`allowlist_rules` as the
weakening levers. Empirically confirmed, twice:
- A `.tirith/policy.yaml` discovered via cwd tree-walk (`scope: repo`) — including one
  redirected via the `TIRITH_POLICY_ROOT` env var — has all weakening fields **silently
  neutralized**: `tirith policy effective` prints "Neutralized (this repo policy is
  tightening-only; these weakening fields were ignored): additional_known_domains,
  severity_overrides." Deliberate anti-tamper design: a repo (or an agent's own ephemeral
  kanban task workspace) must never be able to lower its own security posture.
- The **only** scope where the same fields are honored is `$XDG_CONFIG_HOME/tirith/policy.yaml`
  (default `~/.config/tirith/policy.yaml`) — `scope: user`, "Operator-scoped policy — all
  fields honored (nothing neutralized)." Tirith has no concept of Hermes profiles at all (it's
  a generic shell-security tool); this path is one-per-OS-user by construction. Scoping it to a
  single Hermes profile without going fully machine-wide would require overriding
  `XDG_CONFIG_HOME` inside that profile's own `~/.hermes/profiles/<profile>/.env` (confirmed
  loaded into the profile's process env via `hermes_cli/env_loader.py::load_hermes_dotenv()`) —
  a real lever, but one that also redirects every other XDG-aware tool's config/cache for that
  profile's processes, not just Tirith's.

**Kanban worker task cwd defeats the naive "drop a policy file in the profile dir" approach**
even before the neutralization issue: task cwd is `~/.hermes/kanban/workspaces/<task_id>`
(ephemeral, wiped on completion), a completely separate directory tree from
`~/.hermes/profiles/<profile>/` — a repo-scoped policy placed in the profile dir would never
even be discovered by tree-walk for a dispatched task.

**Raw command/URL text is deliberately never sent to this app.** `~/.hermes/plugins/
hermesboard-sensor/__init__.py` (the Step 9 sensor plugin — lives outside this git repo, same
as the rest of that plugin's code) explicitly omits `kwargs.get("command")` when building the
`pre_approval_request` envelope ("same 'no sensitive shell text over telemetry'" per its own
comment) — only `pattern_key` and the pre-formatted `description` string reach
`hook_store.py`/`state_engine.py`. This is a privacy design decision, not an oversight; a
future feature wanting the literal URL/command would need to touch that plugin (outside this
app's normal edit scope) and accept the tradeoff of piping shell text through telemetry.

---

## Full implementation notes

Everything else — the per-feature build findings, both live-bug write-ups (the
embodied-approvals session-key bug and permanently-allowlisted-command bug, and the
false "no gateway is running" root cause), and the verification logs behind every
change — lives in **[IMPLEMENTATION-NOTES.md](IMPLEMENTATION-NOTES.md)**.
