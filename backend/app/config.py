"""App config — loaded from env with DISCOVERY.md defaults.

The session token is read from env ONLY and never logged or returned to clients
(see /health, which exposes a boolean, not the value).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    # Load a gitignored .env (repo root) so HERMES_DASHBOARD_SESSION_TOKEN and
    # friends are picked up without exporting them by hand. Optional dependency.
    from dotenv import find_dotenv, load_dotenv

    load_dotenv(find_dotenv(usecwd=True))
except ImportError:
    pass


@dataclass(frozen=True)
class Config:
    kanban_db_path: str  # absolute, expanduser+resolve'd at load time
    dashboard_base_url: str
    board_slug: str
    session_token: str | None
    data_source: str  # "auto" (default) | "synthetic" — see HERMES_DATA_SOURCE

    @property
    def has_token(self) -> bool:
        return bool(self.session_token)

    @property
    def kanban_db_exists(self) -> bool:
        return Path(self.kanban_db_path).exists()


def load_config() -> Config:
    # Var names match Hermes' own env vars where they exist, so a single
    # variable configures both Hermes and this app (e.g. the session token and
    # board slug are read by Hermes too — see DISCOVERY.md).
    raw_db = os.environ.get("HERMES_KANBAN_DB") or os.path.join("~", ".hermes", "kanban.db")
    return Config(
        kanban_db_path=str(Path(raw_db).expanduser().resolve()),
        dashboard_base_url=os.environ.get("HERMES_DASHBOARD_URL", "http://127.0.0.1:9119"),
        board_slug=os.environ.get("HERMES_KANBAN_BOARD", "default"),
        session_token=os.environ.get("HERMES_DASHBOARD_SESSION_TOKEN") or None,
        data_source=os.environ.get("HERMES_DATA_SOURCE", "auto").strip().lower(),
    )
