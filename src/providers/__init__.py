"""Agent-provider adapters and the registry that normalizes them.

Every provider module exposes the same surface: `NAME`, `MODES` (its accepted
permission-mode names), `create(...)` to build a live provider, and functions
for session metadata (recent_sessions, session_cwd, session_title,
textual_history) -- sync or async, both fine. The bot talks only to this
surface, so commands like `!list`, `!resume` and `!handoff` work identically
no matter which agent is behind the thread.
"""

from __future__ import annotations

import importlib
import inspect
from typing import Any

PROVIDERS = ("claude", "opencode")


def module(name: str):
    """The provider module for `name`; raises ValueError if unknown."""
    if name not in PROVIDERS:
        raise ValueError(f"Provider '{name}' is not one of: {', '.join(PROVIDERS)}.")
    return importlib.import_module(f"src.providers.{name}")


#: Alias for callers that prefer a self-documenting name.
provider_module = module


def modes_for_provider(name: str) -> tuple[str, ...]:
    return tuple(getattr(module(name), "MODES", ()))


def models_for_provider(name: str) -> tuple[str, ...]:
    return tuple(getattr(module(name), "MODELS", ()))


async def _maybe(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def create_provider(
    name: str,
    *,
    cwd: str,
    channel_id: int,
    broker: Any,
    model: str | None = None,
    resume: str | None = None,
    session_id: str | None = None,
    mode: str = "default",
    options: dict[str, Any] | None = None,
):
    """Build a live provider of `name`; `options` carries provider extras."""
    factory = getattr(module(name), "create")
    out = factory(
        cwd=cwd,
        channel_id=channel_id,
        broker=broker,
        model=model,
        resume=resume,
        session_id=session_id,
        mode=mode,
        options=options or {},
    )
    return await out if inspect.isawaitable(out) else out


async def recent_sessions(name: str, limit: int = 100) -> list[Any]:
    return await _maybe(module(name).recent_sessions(limit))


async def session_cwd(name: str, session_id: str) -> str | None:
    func = getattr(module(name), "session_cwd")
    return await _maybe(func(session_id))


async def session_title(name: str, session_id: str) -> str | None:
    func = getattr(module(name), "session_title")
    return await _maybe(func(session_id))


async def textual_history(name: str, session_id: str) -> list[tuple[str, str]]:
    return await _maybe(module(name).textual_history(session_id))


__all__ = [
    "PROVIDERS",
    "module",
    "provider_module",
    "modes_for_provider",
    "create_provider",
    "recent_sessions",
    "session_cwd",
    "session_title",
    "textual_history",
]

