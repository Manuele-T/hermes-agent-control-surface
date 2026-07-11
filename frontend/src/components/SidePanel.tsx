import { useEffect, useRef, useState } from "react";
import type { DataSource, TaskDetail, WorkerActivity, WorkerActivityItem } from "../datasource";
import {
  activityLabel,
  approvalVisible,
  subagentVisible,
  visibleState,
  type AgentView,
  type TaskLite,
} from "../fleet";

function ago(ms: number): string {
  const s = Math.max(0, Math.round((Date.now() - ms) / 1000));
  if (s < 60) return `${s}s ago`;
  return `${Math.round(s / 60)}m ago`;
}

// created_at on task_events/tasks rows is Unix seconds, unlike the live SSE
// reducer's `at` (Date.now() ms) — ago() above expects ms, so this converts.
function agoFromUnixSeconds(sec: number): string {
  return ago(sec * 1000);
}

interface ApprovalFinding {
  severity: string;
  title: string;
  description: string;
}

// Hermes's tirith_security integration flattens structured findings into one
// string — "Security scan — [SEV] Title: desc; [SEV] Title: desc" (built by
// approval.py's _format_tirith_description). That flattened string is ALL
// this app ever receives over telemetry: the raw command/URL text is
// deliberately never sent (see fleet.ts's ApprovalView comment) — so this
// re-parses the same string purely for a nicer display, it does not add any
// new data source or network call.
const TIRITH_DESC_PREFIX = "Security scan — ";

function parseApprovalFindings(description: string | null): ApprovalFinding[] {
  if (!description || !description.startsWith(TIRITH_DESC_PREFIX)) return [];
  const body = description.slice(TIRITH_DESC_PREFIX.length);
  // Split on "; " only where it precedes the next "[SEV]" tag, since a
  // finding's own description text can itself contain "; ".
  const parts = body.split(/;\s+(?=\[[A-Za-z]+\])/);
  const findings: ApprovalFinding[] = [];
  for (const part of parts) {
    const m = /^\[([A-Za-z]+)\]\s*([^:]+):\s*(.*)$/s.exec(part.trim());
    if (m) findings.push({ severity: m[1], title: m[2].trim(), description: m[3].trim() });
  }
  return findings;
}

// pattern_key for a tirith-sourced approval is "tirith:<rule_id>" (see
// approval.py) — the rule id backing the FIRST/primary finding only; extra
// findings in the flattened string carry no rule_id of their own.
function ruleIdFromPatternKey(patternKey: string | null): string | null {
  return patternKey?.startsWith("tirith:") ? patternKey.slice("tirith:".length) : null;
}

// Hand-written from `tirith explain --rule <id>` output — a short "why this
// matters here" gloss for the rule ids this app is most likely to see.
const RISK_NOTES: Record<string, string> = {
  schemeless_to_sink:
    "Read-only fetch, but a bare host with no scheme can silently fall back to plain HTTP if intercepted.",
  lookalike_tld:
    "TLD resembles a common file extension — worth a glance that this is the real service, not a lookalike domain.",
  plain_http_to_sink:
    "Unencrypted transport into a download/execute sink — contents can be tampered with in transit. Treat as high-risk.",
  pipe_to_interpreter:
    "Output is piped straight into an interpreter — this is code execution, not just a fetch. Treat as high-risk.",
};

// Fallback when the specific rule isn't one we've written a note for —
// still gives a severity-shaped read rather than nothing.
function genericRiskNote(severity: string): string {
  const s = severity.toUpperCase();
  if (s === "HIGH" || s === "CRITICAL") {
    return "High severity — likely an execution or filesystem risk. Read the description below before approving.";
  }
  if (s === "MEDIUM" || s === "MODERATE") {
    return "Medium severity — often a transport/hostname heuristic rather than active execution, but check the domain/action below.";
  }
  return "Lower severity — usually informational; still gated here since Hermes never auto-approves.";
}

const ACTIVITY_POLL_MS = 1500;

// Polls source.getWorkerActivity(agentId) — the ONLY way this component talks
// to the backend, per the architecture rule (no fetch() here). Enabled only
// while the caller says so (agent selected + working/done); cleanly stops
// (interval cleared, in-flight guard reset) on agentId/enabled change and on
// unmount. A `cancelled` flag + `inFlightRef` reset in cleanup together mean:
// a straggling response from a PREVIOUS agent/disabled period can never land
// on top of the current one, and a slow poll can never overlap the next tick.
function useWorkerActivity(
  source: DataSource,
  agentId: string | null,
  enabled: boolean,
): WorkerActivity | null {
  const [activity, setActivity] = useState<WorkerActivity | null>(null);
  const inFlightRef = useRef(false);

  useEffect(() => {
    setActivity(null); // never show a stale/previous agent's feed while switching
    if (!agentId || !enabled) return;
    let cancelled = false;

    const poll = async () => {
      if (inFlightRef.current) return; // skip this tick — previous call still in flight
      inFlightRef.current = true;
      try {
        const result = await source.getWorkerActivity(agentId);
        if (!cancelled) setActivity(result);
      } catch {
        // Failed/aborted poll: keep the last frame on screen, no error state.
      } finally {
        inFlightRef.current = false;
      }
    };

    void poll(); // first poll immediately on selection, don't wait a full interval
    const interval = window.setInterval(() => {
      void poll();
    }, ACTIVITY_POLL_MS);

    return () => {
      cancelled = true;
      inFlightRef.current = false;
      window.clearInterval(interval);
    };
  }, [source, agentId, enabled]);

  return activity;
}

// "→ calling <tool>" for tool_call ALWAYS (never shows raw args — a clean
// one-liner regardless of what's in `text`); tool_result shows a short
// preview when there's text, else just the tool name; assistant shows its
// text as-is. Never returns a string that would render an empty/placeholder
// row — callers filter out any blank result.
function activityItemLine(item: WorkerActivityItem): string {
  if (item.kind === "tool_call") return `→ calling ${item.label ?? "tool"}`;
  if (item.kind === "tool_result") {
    const label = item.label ?? "tool";
    if (!item.text) return label;
    const preview = item.text.length > 100 ? `${item.text.slice(0, 100)}…` : item.text;
    return `${label}: ${preview}`;
  }
  return item.text ?? ""; // assistant
}

function TaskDetailModal({
  taskId,
  detail,
  loading,
  error,
  onClose,
}: {
  taskId: string;
  detail: TaskDetail | null;
  loading: boolean;
  error: string | null;
  onClose: () => void;
}) {
  // The `result` column on the tasks table is unpopulated in practice — the
  // real outcome summary lives in the terminal event's payload.summary (Hermes
  // writes it there, not to tasks.result). Prefer the column if it's ever
  // populated, else fall back to the last event carrying a summary.
  const lastSummary = detail?.events
    .slice()
    .reverse()
    .map((e) => (e.payload as { summary?: string } | null)?.summary)
    .find((s): s is string => !!s);
  const summary =
    detail?.result != null && detail.result !== "" ? detail.result : lastSummary;

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal task-detail-modal" onClick={(e) => e.stopPropagation()}>
        <div className="panel-head">
          <h2>{detail?.title ?? taskId}</h2>
          <button type="button" className="close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </div>
        <p className="muted">{taskId}</p>
        {loading && <p className="muted">loading…</p>}
        {error && <p className="action-result err">✗ {error}</p>}
        {detail && (
          <>
            <div className={`badge state-${detail.status}`}>{detail.status}</div>
            {summary != null && (
              <>
                <h3>Result</h3>
                <p className="task-result">
                  {typeof summary === "string" ? summary : JSON.stringify(summary, null, 2)}
                </p>
              </>
            )}
            <h3>Event timeline</h3>
            {detail.events.length === 0 ? (
              <p className="muted">no events recorded</p>
            ) : (
              <ul className="events timeline">
                {detail.events.map((e) => (
                  <li key={e.id}>
                    <span className="ev-kind">{e.kind}</span>
                    <span className="muted">{agoFromUnixSeconds(e.created_at)}</span>
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
      </div>
    </div>
  );
}

export default function SidePanel({
  agent,
  tasks,
  source,
  onClose,
}: {
  agent: AgentView | null;
  tasks: Record<string, TaskLite>;
  source: DataSource;
  onClose: () => void;
}) {
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [comment, setComment] = useState("");
  const [reassignProfile, setReassignProfile] = useState("");
  // Step 14: which action is in flight (not just a boolean) — so the exact
  // button clicked can show its own "…ing" label instantly, while every other
  // button just disables to prevent a double-fire.
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const busy = busyAction !== null;
  const [result, setResult] = useState<string | null>(null);
  // Optimistic task overrides: applied on top of the SSE-driven tasks map.
  // Rolled back if the API call fails; SSE confirms on success.
  const [taskOverrides, setTaskOverrides] = useState<Record<string, Partial<TaskLite>>>({});
  // Recent-events click-through: which task id's detail modal is open, plus
  // its fetched detail/loading/error state. Independent of the rest of this
  // panel's state — any event's task id can be inspected, not just the
  // agent's current task.
  const [detailTaskId, setDetailTaskId] = useState<string | null>(null);
  const [taskDetail, setTaskDetail] = useState<TaskDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const activityFeedRef = useRef<HTMLUListElement | null>(null);

  // Computed here (before the early `!agent` return below) so the hook itself
  // is always called — React's Rules of Hooks — and is simply a no-op while
  // nothing is selected. Poll while working AND through the brief "done"
  // flash (fleet.ts's cooldown window) so the feed still has a moment to
  // catch the backend's post-completion coarse-fallback frame instead of
  // freezing mid-run the instant the task finishes.
  const activityGateState = agent ? visibleState(agent, Date.now()) : null;
  const activityEnabled = activityGateState === "working" || activityGateState === "done";
  const workerActivity = useWorkerActivity(source, agent?.id ?? null, activityEnabled);

  useEffect(() => {
    const el = activityFeedRef.current;
    if (el) el.scrollTop = el.scrollHeight; // auto-scroll to the newest line
  }, [workerActivity]);

  async function openTaskDetail(id: string): Promise<void> {
    setDetailTaskId(id);
    setTaskDetail(null);
    setDetailError(null);
    setDetailLoading(true);
    try {
      const d = await source.getTaskDetail(id);
      setTaskDetail(d);
    } catch (e) {
      setDetailError(String(e instanceof Error ? e.message : e));
    } finally {
      setDetailLoading(false);
    }
  }

  if (!agent) {
    return (
      <aside className="panel panel-empty">
        <p>Click a character to see its live status and act on it.</p>
      </aside>
    );
  }

  const now = Date.now();
  const state = visibleState(agent, now);
  const activity = activityLabel(agent, state);
  const subVisible = subagentVisible(agent, now);
  const appr = agent.approval;
  const apprVisible = approvalVisible(agent, now);
  const apprExpired = !!appr && !appr.resolved && now >= appr.expiresAt;
  const apprSecondsLeft = appr ? Math.max(0, Math.ceil((appr.expiresAt - now) / 1000)) : 0;
  const apprFindings = appr ? parseApprovalFindings(appr.description) : [];
  const apprRuleId = appr ? ruleIdFromPatternKey(appr.patternKey) : null;
  const apprRiskNote = apprFindings.length
    ? (apprRuleId && RISK_NOTES[apprRuleId]) || genericRiskNote(apprFindings[0].severity)
    : null;
  const task = agent.activeTaskId ? tasks[agent.activeTaskId] : undefined;
  const targetTaskId = agent.activeTaskId ?? agent.lastTaskId;
  // Effective view for the action target — merges SSE state with optimistic override.
  const targetTask = targetTaskId
    ? { ...(tasks[targetTaskId] ?? {}), ...(taskOverrides[targetTaskId] ?? {}) }
    : undefined;

  async function run(
    action: string,
    payload: Record<string, unknown>,
    opts?: { optimistic?: { taskId: string; update: Partial<TaskLite> } },
  ): Promise<void> {
    setBusyAction(action);
    setResult(null);
    const opt = opts?.optimistic;
    if (opt) setTaskOverrides((prev) => ({ ...prev, [opt.taskId]: opt.update }));
    try {
      const r = await source.act(agent!.id, action, payload);
      const ok = r.ok !== false;
      setResult(
        ok
          ? `✓ ${action}${r.warning ? ` — ${r.warning}` : ""}`
          : `✗ ${(r.detail as string) ?? "failed"}`,
      );
      if (!ok && opt) {
        // Rollback: remove the optimistic override.
        setTaskOverrides((prev) => { const n = { ...prev }; delete n[opt.taskId]; return n; });
      }
      // On success, keep the override briefly; SSE will confirm and drive the real update.
    } catch (e) {
      setResult(`✗ ${String(e)}`);
      if (opt) {
        setTaskOverrides((prev) => { const n = { ...prev }; delete n[opt.taskId]; return n; });
      }
    } finally {
      setBusyAction(null);
    }
  }

  const isRunning = targetTask?.status === "running";
  const isArchived = targetTask?.status === "archived";
  const isBlocked = targetTask?.status === "blocked";
  const isReady = targetTask?.status === "ready";
  // Note-for-next-run is only meaningful while a task is still in flight
  // (running / ready / blocked) — a completed or archived task has no next
  // run to read it. `targetTask?.status` (not `agent.activeTaskId`, which
  // fleet.ts nulls out on a blocked-kind event) is the same terminal-vs-not
  // signal `isRunning`/`isBlocked`/`isArchived` above already use.
  const hasOpenTask = !!targetTaskId && (isRunning || isReady || isBlocked);

  return (
    <aside className="panel">
      <div className="panel-head">
        <h2>{agent.name}</h2>
        <button type="button" className="close" onClick={onClose} aria-label="Close">
          ×
        </button>
      </div>
      <div className={`badge state-${state}`}>{state}</div>
      {activity && <p className="muted activity-label">{activity}</p>}
      {subVisible && agent.subagent && (
        // Step 11: ephemeral — only shown while a delegate_task hook cycle is
        // in flight or briefly after subagent_stop (see subagentVisible()).
        <p className="muted subagent-info">
          ↳ delegated: {agent.subagent.title ?? agent.subagent.role ?? "sub-agent"} (
          {agent.subagent.active ? "running" : agent.subagent.status})
        </p>
      )}

      {apprVisible && appr && (
        // Step 12: a real dangerous-command approval, resolvable from here —
        // never auto-approved. See DISCOVERY.md for how this actually reaches
        // the blocked Hermes worker thread (hermesboard-sensor's poller).
        <div className="approval-panel">
          <h3>Approval requested</h3>
          <p className="approval-rule">
            Rule: <code>{apprRuleId ?? appr.patternKey ?? "unknown pattern"}</code>
          </p>
          {apprFindings.length > 0 ? (
            <>
              <div className="approval-findings">
                {apprFindings.map((f, i) => (
                  <div key={i} className="approval-finding">
                    <p className="approval-finding-head">
                      <span className={`approval-sev approval-sev-${f.severity.toLowerCase()}`}>
                        {f.severity}
                      </span>{" "}
                      {f.title}
                    </p>
                    <p className="approval-finding-desc">{f.description}</p>
                  </div>
                ))}
              </div>
              {apprRiskNote && <p className="approval-risk-note">Risk here: {apprRiskNote}</p>}
              <p className="muted approval-note">
                exact command/URL text isn't sent over telemetry — decide from the rule + findings above
              </p>
            </>
          ) : (
            <p className="approval-desc">{appr.description ?? "dangerous command"}</p>
          )}
          {!appr.resolved ? (
            <>
              <p className="muted">
                {apprExpired ? "expired — waiting for Hermes to time out" : `${apprSecondsLeft}s to respond`}
              </p>
              <div className="approval-actions">
                <button
                  type="button"
                  className="btn-approve"
                  disabled={busy || apprExpired}
                  title="Approve the pending command once and let the agent continue"
                  data-tooltip="Approve the pending command once and let the agent continue"
                  onClick={() => run("approve", {})}
                >
                  {apprExpired ? "Expired" : busyAction === "approve" ? "Approving…" : "Approve once"}
                </button>
                <button
                  type="button"
                  className="btn-danger"
                  disabled={busy || apprExpired}
                  title="Deny the pending command and block the agent from running it"
                  data-tooltip="Deny the pending command and block the agent from running it"
                  onClick={() => run("deny", {})}
                >
                  {apprExpired ? "Expired" : busyAction === "deny" ? "Denying…" : "Deny"}
                </button>
              </div>
            </>
          ) : (
            <p className="muted approval-outcome">
              {!appr.choice || appr.choice === "deny" || appr.choice === "timeout"
                ? `✕ ${appr.choice ?? "denied"}`
                : `✓ ${appr.choice}`}
            </p>
          )}
        </div>
      )}

      {agent.independent && (
        // Step 13: an active, non-Kanban Hermes session for this profile.
        // Point-in-time only — see SessionsAdapter's docstring (no live stream
        // for these, unlike the Kanban fleet).
        <div className="independent-panel">
          <h3>Independent session</h3>
          <p className="muted">
            source: {agent.source ?? "?"}
            {agent.lastActiveAt ? ` · active ${ago(agent.lastActiveAt)}` : ""}
          </p>
          {agent.cwd && <p className="muted independent-cwd">{agent.cwd}</p>}
          {agent.preview && <p className="independent-preview">{agent.preview}</p>}
          {agent.estimatedCostUsd != null && (
            <p className="muted">est. cost ${agent.estimatedCostUsd.toFixed(4)}</p>
          )}
        </div>
      )}

      <h3>Current task</h3>
      {task ? (
        <p className="task-current">
          <strong>{task.title}</strong>
          <br />
          <span className="muted">
            {task.id} · {task.status}
          </span>
        </p>
      ) : (
        <p className="muted">none{targetTaskId ? ` (last: ${targetTaskId})` : ""}</p>
      )}

      <h3>Live activity</h3>
      <p className="muted activity-subtitle">
        Worker output, ~1-2s delay — the agent's latest actions as it works. Not a live
        in-place edit, and never the model's hidden reasoning.
      </p>
      {(() => {
        const items = (workerActivity?.available ? workerActivity.items : [])
          .map((item) => ({ item, line: activityItemLine(item) }))
          .filter(({ line }) => line.trim() !== "");
        if (items.length === 0) {
          return <p className="muted">No live activity — agent idle</p>;
        }
        return (
          <ul className="activity-feed" ref={activityFeedRef}>
            {items.map(({ item, line }, i) => (
              <li key={`${item.ts}-${i}`} className={`activity-item activity-item-${item.kind}`}>
                <span className="activity-text">{line}</span>
                <span className="muted activity-ts">{agoFromUnixSeconds(item.ts)}</span>
              </li>
            ))}
          </ul>
        );
      })()}

      <h3>Actions</h3>
      <div className="actions">
        <input
          className="inp"
          placeholder="New task title"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
        />
        <textarea
          className="inp"
          placeholder="Task body / instructions (optional)"
          rows={4}
          value={body}
          onChange={(e) => setBody(e.target.value)}
        />
        <button
          type="button"
          disabled={busy || !title.trim()}
          title="Create a new task and assign it to this agent"
          data-tooltip="Create a new task and assign it to this agent"
          onClick={() => run("spawn", { title, body: body || undefined }).then(() => setTitle(""))}
        >
          {busyAction === "spawn" ? "Spawning…" : `Spawn task onto ${agent.name}`}
        </button>

        <input
          className="inp"
          placeholder="Leave a note for the next run"
          value={comment}
          disabled={!hasOpenTask}
          onChange={(e) => setComment(e.target.value)}
        />
        <p className="muted note-helper">
          The agent reads this on its next run, not mid-turn — a correction picked up when
          the task re-runs (reclaim/redispatch, unblock, or retry), not a live in-place edit.
        </p>
        <button
          type="button"
          disabled={busy || !comment.trim() || !hasOpenTask}
          title={
            hasOpenTask
              ? "Post a note the agent reads via kanban_show() on its next run"
              : "Only available while the agent has a running/ready/blocked task"
          }
          data-tooltip="Post a note the agent reads via kanban_show() on its next run"
          onClick={() =>
            run("comment", { task_id: targetTaskId, body: comment }).then(() => setComment(""))
          }
        >
          {busyAction === "comment" ? "Posting note…" : "Leave note for next run"}
        </button>

        <button
          type="button"
          disabled={busy || !isBlocked}
          title={!isBlocked ? "Only applicable to blocked tasks" : "Mark a blocked task as ready so the agent can continue"}
          data-tooltip="Mark a blocked task as ready so the agent can continue"
          onClick={() => run("unblock", { task_id: targetTaskId })}
        >
          {busyAction === "unblock" ? "Unblocking…" : "Unblock task"}
        </button>

        <hr className="divider" />

        <input
          className="inp"
          placeholder="Reassign to profile (e.g. writer)"
          value={reassignProfile}
          onChange={(e) => setReassignProfile(e.target.value)}
        />
        <button
          type="button"
          disabled={busy || !targetTaskId || !reassignProfile.trim() || isArchived}
          title="Move the current task to a different agent"
          data-tooltip="Move the current task to a different agent"
          onClick={() => {
            if (!targetTaskId || !reassignProfile.trim()) return;
            const profile = reassignProfile.trim();
            if (!window.confirm(`Reassign task "${targetTaskId}" to "${profile}"?`)) return;
            run(
              "reassign",
              { task_id: targetTaskId, profile },
              { optimistic: { taskId: targetTaskId, update: { assignee: profile } } },
            ).then(() => setReassignProfile(""));
          }}
        >
          {busyAction === "reassign" ? "Reassigning…" : `Reassign to ${reassignProfile.trim() || "…"}`}
        </button>

        <button
          type="button"
          className="btn-danger"
          disabled={busy || !targetTaskId || isArchived}
          title="Archive this task and remove it from the queue — stops the worker first if it's running"
          data-tooltip="Archive this task and remove it from the queue — stops the worker first if it's running"
          onClick={() => {
            if (!targetTaskId) return;
            const label = task?.title ?? targetTaskId;
            const confirmMsg = isRunning
              ? `Cancel "${label}"?\n\nThis stops the running worker and removes the task from the board.`
              : `Archive "${label}"?\n\nThis removes it from the board.`;
            if (!window.confirm(confirmMsg)) return;
            run(
              "cancel",
              { task_id: targetTaskId },
              { optimistic: { taskId: targetTaskId, update: { status: "archived" } } },
            );
          }}
        >
          {isArchived ? "Cancelling…" : "Cancel task"}
        </button>
      </div>
      {result && <p className={`action-result ${result.startsWith("✓") ? "ok" : "err"}`}>{result}</p>}

      <h3>Recent events</h3>
      {agent.recentEvents.length === 0 ? (
        <p className="muted">no live events yet</p>
      ) : (
        <ul className="events">
          {agent.recentEvents.map((e, i) => (
            <li key={`${e.id ?? "x"}-${i}`}>
              <span className="ev-kind">{e.kind}</span>
              <span className="muted">
                {" "}
                {e.taskId && (
                  <>
                    <button
                      type="button"
                      className="ev-taskid-link"
                      title="View task detail and full event timeline"
                      onClick={() => openTaskDetail(e.taskId!)}
                    >
                      {e.taskId}
                    </button>{" "}
                    ·{" "}
                  </>
                )}
                {ago(e.at)}
              </span>
            </li>
          ))}
        </ul>
      )}

      {detailTaskId && (
        <TaskDetailModal
          taskId={detailTaskId}
          detail={taskDetail}
          loading={detailLoading}
          error={detailError}
          onClose={() => setDetailTaskId(null)}
        />
      )}
    </aside>
  );
}
