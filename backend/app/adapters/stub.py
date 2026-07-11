"""StubAdapter — empty returns. Proves the interface and lets the app boot
before the Kanban adapter lands (Step 2)."""
from __future__ import annotations

from typing import Any

from app.datasource import Agent, DataSource, EventHandler, Task, Unsubscribe


class StubAdapter(DataSource):
    async def get_agents(self) -> list[Agent]:
        return []

    async def get_tasks(self) -> list[Task]:
        return []

    async def subscribe(self, on_event: EventHandler) -> Unsubscribe:
        def _unsubscribe() -> None:
            return None

        return _unsubscribe

    async def act(
        self, agent_id: str, action: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return {"ok": False, "detail": "stub adapter: not implemented"}
