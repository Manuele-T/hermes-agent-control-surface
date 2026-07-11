import { API_BASE } from "../config";
import type {
  Agent,
  AgentEvent,
  DataSource,
  Task,
  TaskDetail,
  Unsubscribe,
  WorkerActivity,
} from "../datasource";

// Concrete adapter: the ONLY place that knows the transport (HTTP + SSE). UI
// components never see this — they depend on the DataSource interface only.
export class HttpDataSource implements DataSource {
  async getAgents(): Promise<Agent[]> {
    const r = await fetch(`${API_BASE}/agents`);
    if (!r.ok) throw new Error(`GET /agents -> ${r.status}`);
    return r.json();
  }

  async getTasks(): Promise<Task[]> {
    const r = await fetch(`${API_BASE}/tasks`);
    if (!r.ok) throw new Error(`GET /tasks -> ${r.status}`);
    return r.json();
  }

  subscribe(onEvent: (event: AgentEvent) => void): Unsubscribe {
    const es = new EventSource(`${API_BASE}/events`);
    // Connection state is surfaced through the same channel as synthetic events
    // (kind "__open"/"__error") so we don't have to widen the interface.
    es.onopen = () => onEvent({ kind: "__open" });
    es.onerror = () => onEvent({ kind: "__error" }); // EventSource auto-reconnects
    es.onmessage = (m) => {
      try {
        onEvent(JSON.parse(m.data) as AgentEvent);
      } catch {
        // keepalive comments never reach onmessage; ignore any stray parse error
      }
    };
    return () => es.close();
  }

  async act(
    agentId: string,
    action: string,
    payload?: Record<string, unknown>,
  ): Promise<{ ok: boolean; [k: string]: unknown }> {
    const r = await fetch(`${API_BASE}/act`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ agentId, action, payload: payload ?? {} }),
    });
    return r.json(); // wired in Step 4
  }

  async getTaskDetail(taskId: string): Promise<TaskDetail> {
    const r = await fetch(`${API_BASE}/tasks/${encodeURIComponent(taskId)}`);
    if (!r.ok) {
      const body = await r.json().catch(() => ({}));
      throw new Error((body as { detail?: string }).detail ?? `GET /tasks/${taskId} -> ${r.status}`);
    }
    return r.json();
  }

  async getWorkerActivity(agentId: string): Promise<WorkerActivity> {
    const r = await fetch(`${API_BASE}/agents/${encodeURIComponent(agentId)}/activity`);
    if (!r.ok) {
      const body = await r.json().catch(() => ({}));
      throw new Error(
        (body as { detail?: string }).detail ?? `GET /agents/${agentId}/activity -> ${r.status}`,
      );
    }
    return r.json();
  }
}
