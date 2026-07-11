import type { CostSummary } from "../fleet";

// Step 14: small global token/cost HUD, rendered in the React layer (outside
// the PixiJS canvas). Fed entirely by the "costs" SSE frame already flowing
// through the existing subscribe() channel (see fleet.ts) — no new
// DataSource method, per architecture.md.
function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

export default function CostHud({ costs }: { costs: CostSummary | null }) {
  if (!costs || !costs.available) return null;
  return (
    <div className="cost-hud" title={`${costs.sessionCount} sessions · ${costs.activeSessionCount} active`}>
      <span className="cost-hud-item">${costs.totalCostUsd.toFixed(2)}</span>
      <span className="cost-hud-sep">·</span>
      <span className="cost-hud-item">
        {formatTokens(costs.inputTokens + costs.outputTokens)} tok
      </span>
    </div>
  );
}
