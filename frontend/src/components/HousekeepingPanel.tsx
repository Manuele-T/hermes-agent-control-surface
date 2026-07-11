import { useCallback, useEffect, useState } from "react";
import type { DataSource, Task } from "../datasource";

// Bulk sweep target: done/blocked only — running is never included here, and
// never will be (archiving a live task's row can orphan its worker; see
// DO-NOT-DO.md and KanbanAdapter._archive_guarded's server-side backstop).
const BULK_ARCHIVABLE_STATUSES = new Set(["done", "blocked"]);

function summaryOf(t: Task): string | null {
  const s = t.summary as string | null | undefined;
  return s && s.trim() !== "" ? s : null;
}

export default function HousekeepingPanel({ source }: { source: DataSource }) {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [busyBulk, setBusyBulk] = useState(false);
  const [result, setResult] = useState<string | null>(null);
  // Display-only filter — never affects which tasks are fetched or which are
  // eligible for archive/archive-all below (those always read `tasks`, the
  // full list). Default ON: archived clutter stays out of the way until
  // toggled back, and toggling never re-fetches or re-writes anything.
  const [hideArchived, setHideArchived] = useState(true);

  // Loads through the DataSource interface only — no fetch() here, same rule
  // every other panel in this app follows.
  const refresh = useCallback(async () => {
    setLoadError(null);
    try {
      const list = await source.getTasks();
      setTasks([...list].sort((a, b) => Number(b.created_at ?? 0) - Number(a.created_at ?? 0)));
    } catch (e) {
      setLoadError(String(e instanceof Error ? e.message : e));
    } finally {
      setLoading(false);
    }
  }, [source]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const busy = busyId !== null || busyBulk;

  async function archiveOne(task: Task): Promise<void> {
    if (!window.confirm(`Archive "${task.title}" (${task.id})?\n\nThis removes it from the board.`)) {
      return;
    }
    setBusyId(task.id);
    setResult(null);
    try {
      const r = await source.act(task.assignee ?? "unassigned", "archive", { task_id: task.id });
      if (r.ok === false) {
        setResult(`✗ ${(r.detail as string) ?? "archive failed"}`);
      } else {
        setResult(`✓ archived ${task.id}`);
        await refresh();
      }
    } catch (e) {
      setResult(`✗ ${String(e)}`);
    } finally {
      setBusyId(null);
    }
  }

  async function archiveAllCompleted(): Promise<void> {
    const targets = tasks.filter((t) => BULK_ARCHIVABLE_STATUSES.has(t.status));
    if (targets.length === 0) return;
    const confirmMsg =
      `Archive all ${targets.length} completed task(s)?\n\n` +
      "This archives every done/blocked task on the board right now. Running tasks are never touched.";
    if (!window.confirm(confirmMsg)) return;

    setBusyBulk(true);
    setResult(null);
    let succeeded = 0;
    const errors: string[] = [];
    for (const task of targets) {
      try {
        const r = await source.act(task.assignee ?? "unassigned", "archive", { task_id: task.id });
        if (r.ok === false) {
          errors.push(`${task.id}: ${(r.detail as string) ?? "failed"}`);
        } else {
          succeeded += 1;
        }
      } catch (e) {
        errors.push(`${task.id}: ${String(e)}`);
      }
    }
    setResult(
      errors.length === 0
        ? `✓ archived ${succeeded} task(s)`
        : `✗ archived ${succeeded}, failed ${errors.length}: ${errors.join("; ")}`,
    );
    setBusyBulk(false);
    await refresh();
  }

  const bulkCount = tasks.filter((t) => BULK_ARCHIVABLE_STATUSES.has(t.status)).length;
  const visibleTasks = hideArchived ? tasks.filter((t) => t.status !== "archived") : tasks;
  const hiddenCount = tasks.length - visibleTasks.length;

  return (
    <section className="housekeeping">
      <div className="housekeeping-head">
        <h2>Board housekeeping</h2>
        <div className="housekeeping-head-controls">
          <label className="housekeeping-toggle">
            <input
              type="checkbox"
              checked={hideArchived}
              onChange={(e) => setHideArchived(e.target.checked)}
            />
            Hide archived{hiddenCount > 0 ? ` (${hiddenCount})` : ""}
          </label>
          <button
            type="button"
            className="btn-danger"
            disabled={busy || bulkCount === 0}
            title="Archive every done/blocked task at once — running tasks are never touched"
            data-tooltip="Archive every done/blocked task at once — running tasks are never touched"
            onClick={() => void archiveAllCompleted()}
          >
            {busyBulk ? "Archiving…" : `Archive all completed (${bulkCount})`}
          </button>
        </div>
      </div>
      {result && <p className={`action-result ${result.startsWith("✓") ? "ok" : "err"}`}>{result}</p>}
      {loading && <p className="muted">loading…</p>}
      {loadError && <p className="action-result err">✗ {loadError}</p>}
      {!loading && !loadError && tasks.length === 0 && <p className="muted">no tasks on the board</p>}
      {!loading && tasks.length > 0 && visibleTasks.length === 0 && (
        <p className="muted">
          all {tasks.length} task(s) are archived and hidden — toggle "Hide archived" off to view them
        </p>
      )}
      {!loading && visibleTasks.length > 0 && (
        <ul className="housekeeping-list">
          {visibleTasks.map((t) => {
            const isRunning = t.status === "running";
            const isArchived = t.status === "archived";
            const summary = summaryOf(t);
            return (
              <li key={t.id} className="housekeeping-row">
                <div className="housekeeping-main">
                  <span className={`badge state-${t.status}`}>{t.status}</span>
                  <span className="housekeeping-id">{t.id}</span>
                  <span className="muted housekeeping-assignee">{t.assignee ?? "unassigned"}</span>
                </div>
                <p className="housekeeping-title">{t.title}</p>
                <p className="muted housekeeping-summary">{summary ?? "no result summary yet"}</p>
                {isArchived ? (
                  <span className="muted housekeeping-archived-tag">archived</span>
                ) : (
                  <button
                    type="button"
                    className="btn-danger"
                    disabled={busy || isRunning}
                    title={
                      isRunning
                        ? "Running tasks can't be archived here — use Cancel from the agent panel instead"
                        : "Archive this task"
                    }
                    data-tooltip={
                      isRunning
                        ? "Running tasks can't be archived here — use Cancel from the agent panel instead"
                        : "Archive this task"
                    }
                    onClick={() => void archiveOne(t)}
                  >
                    {busyId === t.id ? "Archiving…" : "Archive"}
                  </button>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
