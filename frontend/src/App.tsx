import { useState } from "react";
import type { DataSource } from "./datasource";
import { useFleet } from "./fleet";
import Room from "./components/Room";
import SidePanel from "./components/SidePanel";
import CostHud from "./components/CostHud";
import HousekeepingPanel from "./components/HousekeepingPanel";

// App depends only on the DataSource interface; the concrete adapter is injected
// from main.tsx (the composition root).
export default function App({ source }: { source: DataSource }) {
  const { agents, tasks, connected, costs } = useFleet(source);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const selected = agents.find((a) => a.id === selectedId) ?? null;

  return (
    <div className="app">
      <header className="topbar">
        <h1>Hermes Agent Control Surface</h1>
        <span className={`conn ${connected ? "live" : "down"}`}>
          {connected ? "live" : "connecting…"}
        </span>
        <span className="count">{agents.length} agents</span>
        <CostHud costs={costs} />
      </header>
      <div className="layout">
        <Room agents={agents} tasks={tasks} selectedId={selectedId} onSelect={setSelectedId} />
        <SidePanel agent={selected} tasks={tasks} source={source} onClose={() => setSelectedId(null)} />
      </div>
      <HousekeepingPanel source={source} />
    </div>
  );
}
