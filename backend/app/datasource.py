"""The DataSource interface — the ONLY contract between the UI and any backend.

Every adapter (stub, Kanban, synthetic, later hook/subagent) implements exactly
these four operations. Adding a new source must not change the UI. The UI never
reads the DB or calls Hermes directly. (See .claude/rules/architecture.md.)
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable

# Loose dict shapes for v1; concrete adapters populate them. The UI only relies
# on the four methods below, not on Hermes-specific structures.
Agent = dict[str, Any]
Task = dict[str, Any]
Event = dict[str, Any]
EventHandler = Callable[[Event], None]
Unsubscribe = Callable[[], None]


class DataSource(ABC):
    @abstractmethod
    async def get_agents(self) -> list[Agent]:
        ...

    @abstractmethod
    async def get_tasks(self) -> list[Task]:
        ...

    @abstractmethod
    async def subscribe(self, on_event: EventHandler) -> Unsubscribe:
        """Register a callback for live events. Returns an unsubscribe fn."""
        ...

    @abstractmethod
    async def act(
        self, agent_id: str, action: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Perform a control action on an agent. Returns an outcome dict."""
        ...
