import type { AgentView, TaskLite } from "../fleet";
import Character from "./Character";

// Step 13v2: Character is now a single shared PixiJS canvas for the whole
// room (never one per agent), so it takes the full agents list instead of
// being mapped once per agent. `tasks` is accepted here for prop-signature
// compatibility with App.tsx but no longer forwarded — the Pixi renderer only
// needs each agent's own visibleState(), not task details.
export default function Room({
  agents,
  tasks: _tasks,
  selectedId,
  onSelect,
}: {
  agents: AgentView[];
  tasks: Record<string, TaskLite>;
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  return <Character agents={agents} selectedId={selectedId} onSelect={onSelect} />;
}
