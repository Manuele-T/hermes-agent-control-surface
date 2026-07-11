// The ONLY contract the UI knows about. No Hermes types, no DB access, no fetch
// details leak in here. Concrete adapters (Step 2+) implement this interface;
// swapping adapters must never require a UI change.

export type Agent = { id: string; name: string; state: string; [k: string]: unknown };
export type Task = {
  id: string;
  title: string;
  status: string;
  assignee?: string | null;
  [k: string]: unknown;
};
export type AgentEvent = {
  id?: number;
  kind: string;
  taskId?: string;
  payload?: unknown;
  [k: string]: unknown;
};
export type TaskDetailEvent = {
  id: number;
  kind: string;
  payload?: unknown;
  created_at: number;
  [k: string]: unknown;
};
export type TaskDetail = {
  id: string;
  title: string;
  status: string;
  result?: unknown;
  events: TaskDetailEvent[];
  [k: string]: unknown;
};
// Live "what is this worker doing right now" feed (DISCOVERY.md spike):
// newest item last, capped ~20, every text field already truncated (~300
// chars) and secret-scrubbed server-side before it ever reaches the UI —
// see backend/app/redact.py. `label` is the tool name for tool_call/
// tool_result items, null for assistant items. `text` can be null (e.g. a
// coarse hook-only fallback item, which carries no message text at all).
export type WorkerActivityItem = {
  ts: number;
  kind: "assistant" | "tool_call" | "tool_result";
  label: string | null;
  text: string | null;
};
export type WorkerActivity = {
  available: boolean;
  updatedAt: number | null;
  items: WorkerActivityItem[];
};
export type Unsubscribe = () => void;

export interface DataSource {
  getAgents(): Promise<Agent[]>;
  getTasks(): Promise<Task[]>;
  subscribe(onEvent: (event: AgentEvent) => void): Unsubscribe;
  act(
    agentId: string,
    action: string,
    payload?: Record<string, unknown>,
  ): Promise<{ ok: boolean; [k: string]: unknown }>;
  // Click-through detail for one task (title/status/result + the full
  // task_events timeline). Not push/live like the rest — a plain fetch keyed
  // by whichever task id the user clicks in the events list.
  getTaskDetail(taskId: string): Promise<TaskDetail>;
  // Live worker-activity feed for one agent's CURRENT running task. Plain
  // fetch (not push) — callers poll it. Never rejects for "nothing to show"
  // (idle agent, no readable source) — resolves to `{available: false}`.
  getWorkerActivity(agentId: string): Promise<WorkerActivity>;
}
