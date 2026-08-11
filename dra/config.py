"""Config from .env (secrets) and config.toml (behavior)."""

from __future__ import annotations

import os
import tomllib
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
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def _read_toml(path: str) -> dict:
    p = Path(path)
    if not p.is_file():
        return {}
    with p.open("rb") as fh:
        return tomllib.load(fh)


@dataclass(frozen=True)
class Config:
    token: str
    guild_id: int | None
    launch_cwd: str
    model: str | None = None
    approval_timeout: int = 300
    db_path: str = "sessions.db"
    skills: str | list[str] | None = "all"
    auto_approve_tools: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, dotenv_path: str = ".env", toml_path: str = "config.toml") -> "Config":
        _load_dotenv(dotenv_path)

        token = os.environ.get("DISCORD_TOKEN", "").strip()
        if not token:
            raise SystemExit(
                "DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in."
            )

        guild_raw = os.environ.get("GUILD_ID", "").strip()
        guild_id = int(guild_raw) if guild_raw else None

        toml = _read_toml(toml_path)
        bot = toml.get("bot", {})
        tools = toml.get("tools", {})

        model = str(bot.get("model", "")).strip() or None

        skills_raw = bot.get("skills", "all")
        if isinstance(skills_raw, str):
            skills: str | list[str] | None = None if skills_raw.lower() in ("none", "") else skills_raw
        elif isinstance(skills_raw, list):
            skills = [str(s) for s in skills_raw]
        else:
            skills = "all"

        return cls(
            token=token,
            guild_id=guild_id,
            launch_cwd=os.getcwd(),
            model=model,
            approval_timeout=int(bot.get("approval_timeout", 300)),
            db_path=str(bot.get("db_path", "sessions.db")) or "sessions.db",
            skills=skills,
            auto_approve_tools=[str(t) for t in tools.get("auto_approve", [])],
        )
