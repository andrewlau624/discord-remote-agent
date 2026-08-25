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
    launch_cwd: str
    prefix: str = "!"
    default_cwd: str = ""
    model: str | None = None
    approval_timeout: int = 300
    db_path: str = "sessions.db"
    skills: str | list[str] | None = "all"
    auto_approve_tools: list[str] = field(default_factory=list)
    #: Per-provider settings from [providers.<name>] tables. Only `model` is
    # read today: [providers.opencode] model = "anthropic/claude-opus-4-6".
    provider_models: dict[str, str | None] = field(default_factory=dict)
    #: Context fullness (percent) at which to warn, then warn again. Set
    #: context_warn_at to 0 to disable the warnings entirely.
    context_warn_at: int = 75
    context_warn_again_at: int = 90

    @classmethod
    def load(cls, dotenv_path: str = ".env", toml_path: str = "config.toml") -> "Config":
        _load_dotenv(dotenv_path)

        token = os.environ.get("DISCORD_TOKEN", "").strip()
        if not token:
            raise SystemExit(
                "DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in."
            )

        toml = _read_toml(toml_path)
        bot = toml.get("bot", {})
        tools = toml.get("tools", {})
        ctx = toml.get("context", {})

        model = str(bot.get("model", "")).strip() or None

        skills_raw = bot.get("skills", "all")
        if isinstance(skills_raw, str):
            skills: str | list[str] | None = None if skills_raw.lower() in ("none", "") else skills_raw
        elif isinstance(skills_raw, list):
            skills = [str(s) for s in skills_raw]
        else:
            skills = "all"

        provider_models: dict[str, str | None] = {}
        for name, table in (toml.get("providers") or {}).items():
            if isinstance(table, dict) and str(table.get("model", "")).strip():
                provider_models[str(name)] = str(table["model"]).strip()

        return cls(
            token=token,
            launch_cwd=os.getcwd(),
            prefix=str(bot.get("prefix", "!")) or "!",
            default_cwd=str(bot.get("default_cwd", "")).strip(),
            model=model,
            approval_timeout=int(bot.get("approval_timeout", 300)),
            db_path=str(bot.get("db_path", "sessions.db")) or "sessions.db",
            skills=skills,
            auto_approve_tools=[str(t) for t in tools.get("auto_approve", [])],
            provider_models=provider_models,
            context_warn_at=int(ctx.get("warn_at", 75)),
            context_warn_again_at=int(ctx.get("warn_again_at", 90)),
        )
