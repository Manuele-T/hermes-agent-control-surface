// Fleet state model. Coarse agent state is derived from task_events TRANSITIONS
// (claimed/spawned/heartbeat/completed/blocked...) + heartbeat recency — never
// from status snapshots. A finished agent shows a brief `done` flash and only
// falls to `idle` after a cooldown, so fast (~28s) tasks don't look like nothing
// happened. The fine-grained reducer (thinking/working:<family>/awaiting_approval)
// lives backend-side in app/state_engine.py (Step 10); `fineState`/`activity`
// below just mirror its output, pushed live via synthetic "activity" SSE frames.
import { useCallback, useEffect, useRef, useState } from "react";
import type { Agent, AgentEvent, DataSource } from "./datasource";

export type VisibleState = "idle" | "working" | "done" | "blocked";

export interface AgentView {
  id: string;
  name: string;
  activeTaskId: string | null; // current OWNED task: ready (queued) or running
  claimed: boolean; // true once a worker claimed it — drives the "working" visual
  lastTaskId: string | null; // most recent task touched (for comment/unblock targets)
  lastTerminalKind: string | null;
  lastTerminalAt: number | null; // ms
  lastActivityAt: number | null; // ms
  lastHeartbeatAt: number | null; // ms
  recentEvents: { id?: number; kind: string; taskId?: string; at: number }[];
  // Step 10: hook-derived fine state. null when no hook telemetry has arrived
  // for this profile yet (plugin not installed there) — the UI must fall back
  // to the coarse `state`/`visibleState` in that case, never show a blank.
  fineState: string | null; // thinking | working | awaiting_approval | idle | null
  activity: string | null; // tool family (coding/writing/researching/reading/acting), only set when fineState==="working"
  // Step 11: the parent's most recent delegated sub-agent (flat depth-1 —
  // Hermes delegation isn't nested by default). null when this profile has
  // never delegated. Ephemeral: use subagentVisible() to decide whether to
  // render it, never render straight off presence alone.
  subagent: SubagentView | null;
  // Step 12: a pending dangerous-command approval request. null when none is
  // outstanding. Ephemeral: use approvalVisible() to decide whether to render
  // it — never render straight off presence alone.
  approval: ApprovalView | null;
  // Step 13: the real per-worker run id (task_runs.id), mirrored live from
  // task_events' own `run_id` column. Only set while a worker is actively
  // claiming/running the task; null otherwise. Not currently read by any
  // action — never sourced from the sessions API.
  runId: number | null;
  // Step 13: true for a profile surfaced via GET /api/profiles/sessions (an
  // active, non-Kanban-worker Hermes session) rather than the Kanban board.
  // Point-in-time only — see fleet.ts's seedAgent()/SessionsAdapter docstring.
  independent: boolean;
  source: string | null; // e.g. "cli" | "discord" | "telegram" — only set when independent
  cwd: string | null;
  lastActiveAt: number | null; // ms, only set when independent
  preview: string | null;
  estimatedCostUsd: number | null;
}

export interface SubagentView {
  role: string | null; // resolved once subagent_stop fires; null while still in flight
  status: string; // "active" | the real child_status (e.g. "completed", "failed")
  active: boolean; // true until the closing hook (subagent_stop/post_tool_call) lands
  startedAt: number; // ms
  endedAt: number | null; // ms
  title: string | null; // best-effort GET /api/sessions title/preview, else null
}

export interface ApprovalView {
  patternKey: string | null;
  description: string | null; // human-readable, never the raw command text
  requestedAt: number; // ms
  expiresAt: number; // ms — approximate, mirrors Hermes's own default gateway_timeout
  resolved: boolean;
  choice: string | null; // "once"|"session"|"always"|"deny"|"timeout", only set once resolved
  resolvedAt: number | null; // ms, only set once resolved
}

export interface TaskLite {
  id: string;
  title: string;
  status: string;
  assignee?: string | null;
}

// Step 14: global (not per-agent) cost/token HUD, pushed as an id-less
// "costs" SSE frame — same synthetic-frame idiom as "activity"/"subagent"/
// "approval", so no new DataSource method is needed (see architecture.md:
// all data flows through subscribe()). null until the first frame arrives,
// or forever if the backing adapter has no cost data (StubAdapter, or a
// SessionsAdapter with no dashboard token — see `available`).
export interface CostSummary {
  available: boolean;
  totalCostUsd: number;
  inputTokens: number;
  outputTokens: number;
  sessionCount: number;
  activeSessionCount: number;
}

function parseCosts(raw: unknown): CostSummary | null {
  const c = raw as
    | {
        available?: boolean;
        total_cost_usd?: number;
        input_tokens?: number;
        output_tokens?: number;
        session_count?: number;
        active_session_count?: number;
      }
    | null
    | undefined;
  if (!c) return null;
  return {
    available: Boolean(c.available),
    totalCostUsd: Number(c.total_cost_usd ?? 0),
    inputTokens: Number(c.input_tokens ?? 0),
    outputTokens: Number(c.output_tokens ?? 0),
    sessionCount: Number(c.session_count ?? 0),
    activeSessionCount: Number(c.active_session_count ?? 0),
  };
}

export const DONE_COOLDOWN_MS = 6000;
// Step 11: how long a finished delegation's sprite stays visible (mirrors
// backend SUBAGENT_VISIBLE_AFTER_STOP_SECONDS — subagents are ephemeral
// summaries, not board rows, so this is a brief flash, not a history log).
export const SUBAGENT_COOLDOWN_MS = 8000;
// If a delegation looks "active" for longer than this with no further update,
// stop believing it — same crash-guard budget as the backend's
// STALE_AFTER_SECONDS, applied client-side too since SSE pushes raw hook
// state without a re-check (see main.py's subagent frame comment).
const SUBAGENT_STALE_MS = 120_000;
// Step 12: ephemeral flash after resolution, mirrors backend
// APPROVAL_RESOLVED_FLASH_SECONDS. Unresolved visibility is governed by the
// per-approval `expiresAt` sent from the backend, not a fixed constant here.
export const APPROVAL_RESOLVED_FLASH_MS = 8000;
const WORKING_KINDS = new Set(["claimed", "spawned", "heartbeat"]);
const BLOCKED_KINDS = new Set(["blocked", "gave_up", "timed_out", "crashed", "spawn_failed"]);
const RELEASE_KINDS = new Set(["reclaimed", "reclaim_deferred", "stale"]);

export function visibleState(a: AgentView, now: number): VisibleState {
  // Step 13: an independent session is surfaced only while active (the
  // backend's is_active filter) — always "working" while shown at all.
  if (a.independent) return "working";
  // Owning a task only looks like "working" once a worker has claimed it; a
  // ready (queued) task is owned but not yet being worked.
  if (a.activeTaskId && a.claimed) return "working";
  if (a.lastTerminalKind && BLOCKED_KINDS.has(a.lastTerminalKind)) return "blocked";
  if (
    a.lastTerminalKind === "completed" &&
    a.lastTerminalAt &&
    now - a.lastTerminalAt < DONE_COOLDOWN_MS
  ) {
    return "done";
  }
  return "idle";
}

// Step 10: the human-readable fine-grained label for the side panel. Only
// meaningful while the coarse state is "working" — outside that window the
// coarse badge (idle/blocked/done) already says everything there is to say,
// and stale hook telemetry must not contradict it.
export function activityLabel(agent: AgentView, coarse: VisibleState): string | null {
  if (coarse !== "working") return null;
  if (!agent.fineState || agent.fineState === "idle") return null;
  if (agent.fineState === "working") return agent.activity; // null until a tool call lands
  return agent.fineState; // "thinking" | "awaiting_approval"
}

// Step 11: pure predicate for whether to render the sub-agent sprite right
// now — same "derive from timestamps + now, not from a push-to-hide event"
// idiom as visibleState()'s done-flash cooldown.
export function subagentVisible(agent: AgentView, now: number): boolean {
  const s = agent.subagent;
  if (!s) return false;
  if (s.active) return now - s.startedAt < SUBAGENT_STALE_MS;
  return s.endedAt == null || now - s.endedAt < SUBAGENT_COOLDOWN_MS;
}

// Step 12: pure predicate for whether to render the approval bubble/buttons
// right now — same timestamps-vs-now idiom as subagentVisible().
export function approvalVisible(agent: AgentView, now: number): boolean {
  const a = agent.approval;
  if (!a) return false;
  if (!a.resolved) {
    // Grace window past the nominal expiry so the UI can show a clearly
    // labelled, disabled "expired" state instead of vanishing the instant
    // the countdown hits zero — post_approval_response (the real
    // confirmation) usually arrives within this window.
    return now < a.expiresAt + APPROVAL_RESOLVED_FLASH_MS;
  }
  if (a.resolvedAt == null) return false;
  return now - a.resolvedAt < APPROVAL_RESOLVED_FLASH_MS;
}

function freshAgent(id: string): AgentView {
  return {
    id,
    name: id,
    activeTaskId: null,
    claimed: false,
    lastTaskId: null,
    lastTerminalKind: null,
    lastTerminalAt: null,
    lastActivityAt: null,
    lastHeartbeatAt: null,
    recentEvents: [],
    fineState: null,
    activity: null,
    subagent: null,
    approval: null,
    runId: null,
    independent: false,
    source: null,
    cwd: null,
    lastActiveAt: null,
    preview: null,
    estimatedCostUsd: null,
  };
}

function parseSubagent(raw: unknown): SubagentView | null {
  const s = raw as
    | {
        role: string | null;
        status: string;
        active: boolean;
        started_at: number;
        ended_at: number | null;
        title: string | null;
      }
    | null
    | undefined;
  if (!s) return null;
  return {
    role: s.role ?? null,
    status: s.status ?? "active",
    active: Boolean(s.active),
    startedAt: Number(s.started_at) * 1000,
    endedAt: s.ended_at != null ? Number(s.ended_at) * 1000 : null,
    title: s.title ?? null,
  };
}

function parseApproval(raw: unknown): ApprovalView | null {
  const a = raw as
    | {
        pattern_key: string | null;
        description: string | null;
        requested_at: number;
        expires_at: number;
        resolved: boolean;
        choice: string | null;
        resolved_at: number | null;
      }
    | null
    | undefined;
  if (!a) return null;
  return {
    patternKey: a.pattern_key ?? null,
    description: a.description ?? null,
    requestedAt: Number(a.requested_at) * 1000,
    expiresAt: Number(a.expires_at) * 1000,
    resolved: Boolean(a.resolved),
    choice: a.choice ?? null,
    resolvedAt: a.resolved_at != null ? Number(a.resolved_at) * 1000 : null,
  };
}

function seedAgent(ag: Agent): AgentView {
  const a = freshAgent(String(ag.id));
  a.name = String(ag.name ?? ag.id);
  const endedMs = ag.last_ended_at ? Number(ag.last_ended_at) * 1000 : null;
  const hbMs = ag.last_heartbeat_at ? Number(ag.last_heartbeat_at) * 1000 : null;
  a.lastHeartbeatAt = hbMs;
  a.lastActivityAt = endedMs ?? hbMs;
  a.fineState = (ag.fine_state as string | null) ?? null;
  a.activity = (ag.activity as string | null) ?? null;
  a.subagent = parseSubagent(ag.subagent);
  a.approval = parseApproval(ag.approval);
  a.runId = ag.run_id != null ? Number(ag.run_id) : null;
  a.independent = Boolean(ag.independent);
  a.source = (ag.source as string | null) ?? null;
  a.cwd = (ag.cwd as string | null) ?? null;
  a.lastActiveAt = ag.last_active != null ? Number(ag.last_active) * 1000 : null;
  a.preview = (ag.preview as string | null) ?? null;
  a.estimatedCostUsd = ag.estimated_cost_usd != null ? Number(ag.estimated_cost_usd) : null;
  const ownedTask = (ag.current_task_id as string | null) ?? null;
  a.lastTaskId = ownedTask;
  // The backend now reports current_task_id for owned tasks that are queued
  // (ready) too, not just running ones — so a not-yet-claimed task still shows
  // as current. `claimed` is true only when the agent is actually working.
  a.activeTaskId = ownedTask;
  switch (ag.state) {
    case "working":
      a.claimed = true;
      break;
    case "done":
      a.activeTaskId = null;
      a.lastTerminalKind = "completed";
      a.lastTerminalAt = endedMs;
      break;
    case "blocked":
      a.lastTerminalKind = "blocked";
      a.lastTerminalAt = endedMs;
      break;
    case "error":
      a.activeTaskId = null;
      a.lastTerminalKind = "crashed";
      a.lastTerminalAt = endedMs;
      break;
  }
  return a;
}

export interface Fleet {
  agents: AgentView[];
  tasks: Record<string, TaskLite>;
  connected: boolean;
  costs: CostSummary | null;
}

export function useFleet(source: DataSource): Fleet {
  const [agents, setAgents] = useState<Record<string, AgentView>>({});
  const [tasks, setTasks] = useState<Record<string, TaskLite>>({});
  const [connected, setConnected] = useState(false);
  const [costs, setCosts] = useState<CostSummary | null>(null);
  const [, setTick] = useState(0); // forces cooldown re-evaluation each second

  // Latest tasks map, readable inside event handler without re-subscribing.
  const tasksRef = useRef(tasks);
  tasksRef.current = tasks;

  const refreshTasks = useCallback(async () => {
    try {
      const list = await source.getTasks();
      const map: Record<string, TaskLite> = {};
      for (const t of list) {
        map[t.id] = { id: t.id, title: t.title, status: t.status, assignee: t.assignee ?? null };
      }
      setTasks(map);
    } catch {
      /* leave previous tasks; connection indicator reflects trouble */
    }
  }, [source]);

  const handleEvent = useCallback(
    (ev: AgentEvent) => {
      if (ev.kind === "__open") return setConnected(true);
      if (ev.kind === "__error") return setConnected(false);

      // Step 10: synthetic, id-less frame from the backend's hook-derived
      // state engine — not a task_events row, so it's handled separately from
      // the task/assignee derivation below and never touches activeTaskId etc.
      if (ev.kind === "activity") {
        const p = ev.payload as
          | { assignee?: string; fine_state?: string | null; activity?: string | null }
          | null
          | undefined;
        if (!p?.assignee) return;
        const assignee = p.assignee;
        setAgents((prev) => {
          const a: AgentView = prev[assignee] ? { ...prev[assignee] } : freshAgent(assignee);
          a.fineState = p.fine_state ?? null;
          a.activity = p.activity ?? null;
          return { ...prev, [assignee]: a };
        });
        return;
      }

      // Step 11: another synthetic, id-less frame — the delegated-subagent
      // sprite. Same handling shape as "activity" above: keyed by assignee,
      // never touches activeTaskId/task derivation.
      if (ev.kind === "subagent") {
        const p = ev.payload as { assignee?: string } | null | undefined;
        if (!p?.assignee) return;
        const assignee = p.assignee;
        setAgents((prev) => {
          const a: AgentView = prev[assignee] ? { ...prev[assignee] } : freshAgent(assignee);
          a.subagent = parseSubagent(p);
          return { ...prev, [assignee]: a };
        });
        return;
      }

      // Step 14: global (id-less, no assignee) cost/token HUD frame.
      if (ev.kind === "costs") {
        setCosts(parseCosts(ev.payload));
        return;
      }

      // Step 12: same synthetic-frame handling for the pending-approval
      // indicator — keyed by assignee, never touches activeTaskId/task state.
      if (ev.kind === "approval") {
        const p = ev.payload as { assignee?: string } | null | undefined;
        if (!p?.assignee) return;
        const assignee = p.assignee;
        setAgents((prev) => {
          const a: AgentView = prev[assignee] ? { ...prev[assignee] } : freshAgent(assignee);
          a.approval = parseApproval(p);
          return { ...prev, [assignee]: a };
        });
        return;
      }

      const taskId = (ev.taskId ?? (ev as Record<string, unknown>).task_id) as string | undefined;
      const payload = ev.payload as { assignee?: string } | null | undefined;
      const assignee = payload?.assignee ?? (taskId ? tasksRef.current[taskId]?.assignee : null);

      // A new card needs its title/status; refetch the (small) task list.
      if (ev.kind === "created") void refreshTasks();

      if (taskId) {
        if (ev.kind === "archived") {
          setTasks((prev) => { const n = { ...prev }; delete n[taskId]; return n; });
        } else {
          setTasks((prev) => {
            const cur =
              prev[taskId] ?? { id: taskId, title: taskId, status: "?", assignee: assignee ?? null };
            const next: TaskLite = { ...cur };
            if (assignee) next.assignee = assignee;
            if (ev.kind === "completed") next.status = "done";
            else if (ev.kind === "claimed" || ev.kind === "spawned") next.status = "running";
            else if (BLOCKED_KINDS.has(ev.kind)) next.status = "blocked";
            return { ...prev, [taskId]: next };
          });
        }
      }

      if (!assignee) return;
      const now = Date.now();
      setAgents((prev) => {
        const a: AgentView = prev[assignee] ? { ...prev[assignee] } : freshAgent(assignee);
        a.recentEvents = [{ id: ev.id, kind: ev.kind, taskId, at: now }, ...a.recentEvents].slice(0, 12);
        a.lastActivityAt = now;
        if (taskId) a.lastTaskId = taskId;
        // Tracks whether THIS event hands the task to `assignee`, so we can strip
        // it from any previous owner (a task has exactly one owner — see reassign).
        let tookOwnership = false;
        if (WORKING_KINDS.has(ev.kind)) {
          a.activeTaskId = taskId ?? a.activeTaskId;
          a.claimed = true;
          a.lastTerminalKind = null;
          a.lastTerminalAt = null;
          if (ev.kind === "heartbeat") a.lastHeartbeatAt = now;
          // Step 13: task_events.run_id mirrors task_runs.id for this attempt
          // (DISCOVERY.md) — kept live here even though no current action reads it.
          const runId = (ev as Record<string, unknown>).run_id;
          if (typeof runId === "number") a.runId = runId;
          tookOwnership = true;
        } else if (ev.kind === "created" || ev.kind === "assigned") {
          // A ready/queued task this agent now owns but no worker has claimed.
          // Don't clobber a task currently being worked.
          if (!a.claimed) {
            a.activeTaskId = taskId ?? a.activeTaskId;
            a.lastTerminalKind = null;
            a.lastTerminalAt = null;
            tookOwnership = true;
          }
        } else if (ev.kind === "completed") {
          a.activeTaskId = null;
          a.claimed = false;
          a.lastTerminalKind = "completed";
          a.lastTerminalAt = now;
          a.runId = null;
        } else if (BLOCKED_KINDS.has(ev.kind)) {
          a.activeTaskId = null;
          a.claimed = false;
          a.lastTerminalKind = ev.kind;
          a.lastTerminalAt = now;
          a.runId = null;
        } else if (RELEASE_KINDS.has(ev.kind)) {
          a.activeTaskId = null;
          a.claimed = false;
          a.lastTerminalKind = null;
          a.lastTerminalAt = null;
          a.runId = null;
        } else if (ev.kind === "archived") {
          if (a.activeTaskId === taskId) {
            a.activeTaskId = null;
            a.claimed = false;
            a.lastTerminalKind = null;
            a.lastTerminalAt = null;
          }
        }
        const next = { ...prev, [assignee]: a };
        // Reassignment moved the task here; clear it from its previous owner so
        // the same task isn't shown as "current" on two characters at once.
        if (taskId && tookOwnership) {
          for (const id of Object.keys(next)) {
            if (id !== assignee && next[id].activeTaskId === taskId) {
              next[id] = { ...next[id], activeTaskId: null, claimed: false };
            }
          }
        }
        return next;
      });
    },
    [refreshTasks],
  );

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [ags, tks] = await Promise.all([source.getAgents(), source.getTasks()]);
        if (cancelled) return;
        const tmap: Record<string, TaskLite> = {};
        for (const t of tks) {
          tmap[t.id] = { id: t.id, title: t.title, status: t.status, assignee: t.assignee ?? null };
        }
        setTasks(tmap);
        const amap: Record<string, AgentView> = {};
        for (const ag of ags) amap[ag.id] = seedAgent(ag);
        setAgents(amap);
      } catch {
        /* start empty; live events will populate */
      }
    })();

    const unsub = source.subscribe(handleEvent);
    const ticker = window.setInterval(() => setTick((t) => t + 1), 1000);
    return () => {
      cancelled = true;
      unsub();
      window.clearInterval(ticker);
    };
  }, [source, handleEvent]);

  return {
    agents: Object.values(agents).sort((a, b) => a.id.localeCompare(b.id)),
    tasks,
    connected,
    costs,
  };
}
