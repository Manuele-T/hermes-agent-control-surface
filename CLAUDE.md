# CLAUDE.md

A live, agent-centric **control surface** for a multi-agent Hermes setup. Agents
appear as characters in a room; clicking one shows live status and lets you act on
it (reassign, unblock, comment, spawn, approve/deny). The differentiator is the
control layer, not the visuals.

## Stack
- Backend: **Python / FastAPI** (matches Hermes; can later become a native dashboard plugin)
- Frontend: **React (Vite)**
- Reads/writes Hermes's existing **Kanban** primitives. v1 needs no Hermes plugin.

## The two rules that must never be broken
1. **The UI talks ONLY to the data-source interface** (getAgents / getTasks /
   subscribe / act). A React component must NEVER read the DB or call Hermes
   directly. This is what keeps v1 -> v2 additive instead of a rewrite.
2. **For writes, prefer the Kanban REST API** at `/api/plugins/kanban/`
   (unauthenticated on localhost), with the `hermes kanban` CLI as fallback. NEVER
   write to kanban.db directly. NEVER set a task's status to "running" by hand.

## Hermes facts to respect (verified — but re-confirm in Step 0)
- Kanban DB: `~/.hermes/kanban.db` (default board); other boards under
  `~/.hermes/kanban/boards/<slug>/`. SQLite, WAL mode — read-only access is safe.
- Dashboard API: FastAPI at `/api/plugins/kanban/`, default port **9119**. Live
  updates via a WebSocket at `/events` that tails the append-only `task_events`
  table. Only the WebSocket needs `?token=`; REST is unauthenticated on localhost.
- **State must be driven by `task_events` transitions, NOT status snapshots** —
  short tasks finish in seconds and snapshot polling misses the `working` phase.
- **#19535:** a raw `status=running` write creates a task with no worker. Always
  create -> let the dispatcher claim.
- **#25204:** `pre_tool_call` hooks are unreliable in kanban-worker contexts; if you
  add hooks later (v2), use Kanban polling / the heartbeat activity bridge as backstop.
- `scratch` workspaces are wiped on task completion; use `worktree:`/`dir:` to persist.
- **Never bind the dashboard to `0.0.0.0`** — it exposes create/reassign/archive to
  the network. Note this in the README.

## How to work
- Follow **PLAN.md** one step at a time. Do not jump ahead.
- **Ground everything in real output.** Step 0 produces DISCOVERY.md; trust it over
  any assumption. Do NOT guess REST routes, ports, or schemas — read/probe them.
- Think before coding. State assumptions. Minimal code that solves the step. No
  speculative features. Match existing style.
- Never hardcode or commit tokens. Read from env/config.
- Define a success check per step (PLAN.md lists one for each) and verify before moving on.

## Extra rules

- Please check @DO-NOT-DO.md file for further instructions.