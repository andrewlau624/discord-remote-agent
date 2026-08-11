"""Config from .env and the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: str = ".env") -> None:
    """Load a .env file without clobbering existing env vars."""
    p = Path(path)
    if not p.is_file():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass(frozen=True)
class Config:
    token: str
    owner_ids: frozenset[int]
    guild_id: int | None
    default_cwd: str
    auto_approve_tools: list[str] = field(default_factory=list)
    model: str | None = None
    db_path: str = "sessions.db"
    approval_timeout: int = 300

    @classmethod
    def load(cls, dotenv_path: str = ".env") -> "Config":
        _load_dotenv(dotenv_path)

        token = os.environ.get("DISCORD_TOKEN", "").strip()
        if not token:
            raise SystemExit(
                "DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in."
            )

        owner_raw = _split_csv(os.environ.get("OWNER_IDS", ""))
        try:
            owner_ids = frozenset(int(x) for x in owner_raw)
        except ValueError as exc:
            raise SystemExit(f"OWNER_IDS must be integer user IDs: {exc}") from exc
        if not owner_ids:
            raise SystemExit(
                "OWNER_IDS is empty. Set at least one Discord user ID or the bot "
                "will ignore everyone."
            )

        guild_raw = os.environ.get("GUILD_ID", "").strip()
        guild_id = int(guild_raw) if guild_raw else None

        default_cwd = os.environ.get("DEFAULT_CWD", os.getcwd()).strip() or os.getcwd()

        model = os.environ.get("MODEL", "").strip() or None

        try:
            approval_timeout = int(os.environ.get("APPROVAL_TIMEOUT", "300"))
        except ValueError:
            approval_timeout = 300

        return cls(
            token=token,
            owner_ids=owner_ids,
            guild_id=guild_id,
            default_cwd=default_cwd,
            auto_approve_tools=_split_csv(
                os.environ.get("AUTO_APPROVE_TOOLS", "Read,Glob,Grep")
            ),
            model=model,
            db_path=os.environ.get("DB_PATH", "sessions.db").strip() or "sessions.db",
            approval_timeout=approval_timeout,
        )
