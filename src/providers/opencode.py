"""opencode provider, speaking to a per-session `opencode serve` instance.

One server process is spawned per provider (cwd = the session's working
directory) with its own random basic-auth password, so sessions are isolated
and die with the provider. A turn posts the prompt to the session and then
follows the server's SSE event stream, folding message parts into our
provider-agnostic Blocks until the session reports idle.

Permission prompts and questions arrive as events too; they are routed through
the same PermissionBroker the Claude provider uses, so Discord polls look and
behave identically regardless of which agent is behind the thread.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx

from src.providers.base import (
    Block,
    BlockKind,
    ContextState,
    PermissionBroker,
    Provider,
    TaskStatus,
)

log = logging.getLogger("src")

NAME = "opencode"

#: Modes this provider accepts, in our normalized naming. `plan` maps to the
#: plan agent; acceptEdits auto-approves edit-shaped permission asks here.
MODES = ("default", "acceptEdits", "plan")
_AGENTS = {"default": "build", "acceptEdits": "build", "plan": "plan"}

_EDIT_PERMISSIONS = frozenset({"edit", "write", "patch", "file"})

_IDLE_TIMEOUT = 600.0
_DRAIN_QUIET = 1.0

_SERVER_BOOT_TIMEOUT = 30.0


@dataclass
class SessionRef:
    """Duck-typed for render.session_pages (custom_title/summary/cwd)."""

    session_id: str
    title: str | None = None
    directory: str | None = None

    @property
    def custom_title(self) -> str | None:
        return self.title

    @property
    def cwd(self) -> str:
        return self.directory or ""


def _tool_output_text(output: Any) -> str:
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        parts = []
        for item in output:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            else:
                parts.append(json.dumps(item, ensure_ascii=False))
        return "\n".join(parts)
    if output is None:
        return ""
    return json.dumps(output, ensure_ascii=False)


def part_blocks(
    part: dict[str, Any],
    seen_tools: set[str],
) -> list[Block]:
    """Map one opencode message part onto Blocks.

    Tool calls emit once when work starts and once when it ends; text and
    reasoning only emit when their part completes, matching how the Claude
    provider surfaces whole sections rather than deltas.
    """
    ptype = part.get("type")
    pid = str(part.get("id") or "")

    if ptype == "text":
        if not part.get("time", {}).get("end"):
            return []
        text = str(part.get("text") or "").strip()
        return [Block(BlockKind.TEXT, body=text)] if text else []

    if ptype == "reasoning":
        if not part.get("time", {}).get("end"):
            return []
        text = str(part.get("text") or "").strip()
        return [Block(BlockKind.THINKING, body=text)] if text else []

    if ptype == "tool":
        state = part.get("state") or {}
        status = state.get("status")
        name = str(part.get("tool") or "tool")
        call_id = str(part.get("callID") or pid)
        if status in ("pending", "running"):
            if call_id in seen_tools:
                return []
            seen_tools.add(call_id)
            title = str(state.get("title") or "").strip() or name
            body = ""
            try:
                body = json.dumps(state.get("input"), ensure_ascii=False, indent=2)
            except (TypeError, ValueError):
                body = str(state.get("input"))
            block = Block(
                BlockKind.TOOL_CALL,
                title=title[:120],
                body=body,
                meta={"tool": name},
            )
            return [block]
        if status == "completed":
            output = _tool_output_text(state.get("output"))
            return [
                Block(
                    BlockKind.TOOL_RESULT,
                    body=str(state.get("title") or "").strip()
                    or output
                    or "(done)",
                    meta={"tool": name},
                )
            ]
        if status == "error":
            error = state.get("error")
            detail = error if isinstance(error, str) else json.dumps(error, ensure_ascii=False)
            return [
                Block(
                    BlockKind.TOOL_RESULT,
                    body=detail or "(failed)",
                    is_error=True,
                    meta={"tool": name},
                )
            ]
        return []

    return []


def todo_block(session_id: str, todos: list[dict[str, Any]]) -> Block | None:
    """The whole todo list as one live task-board entry."""
    if not todos:
        return None
    marks = {"completed": "x", "in_progress": " ", "pending": " ", "cancelled": "-"}
    lines = [f"[{marks.get(str(t.get('status')), ' ')}] {t.get('content', '')}" for t in todos]
    running = any(t.get("status") == "in_progress" for t in todos)
    return Block(
        BlockKind.TASK,
        title="Todos",
        body="\n".join(lines),
        task_id=f"{session_id}:todos",
        task_status=TaskStatus.RUNNING if running else TaskStatus.DONE,
    )


def event_error_text(error: dict[str, Any]) -> tuple[str, bool]:
    """(message, is_abort) for a session.error payload."""
    name = str(error.get("name") or "")
    if name == "MessageAbortedError" or "aborted" in name.lower():
        return "Turn interrupted.", True
    message = str(error.get("message") or error.get("toString") or name or "unknown error")
    return message, False


def create(
    *,
    cwd: str,
    channel_id: int,
    broker: PermissionBroker,
    model: str | None = None,
    resume: str | None = None,
    session_id: str | None = None,
    mode: str = "default",
    options: dict[str, Any] | None = None,
) -> "OpenCodeProvider":
    """Registry entry point; `options` may carry `binary` for tests."""
    extra = options or {}
    if mode not in MODES:
        mode = "default"
    return OpenCodeProvider(
        cwd=cwd,
        channel_id=channel_id,
        broker=broker,
        model=model,
        resume=resume,
        session_id=session_id,
        permission_mode=mode,
        binary=str(extra.get("binary") or "opencode"),
    )


class OpenCodeProvider(Provider):
    def __init__(
        self,
        *,
        cwd: str,
        channel_id: int,
        broker: PermissionBroker,
        model: str | None = None,
        resume: str | None = None,
        session_id: str | None = None,
        permission_mode: str = "default",
        binary: str = "opencode",
    ) -> None:
        self.cwd = cwd
        self.channel_id = channel_id
        self.broker = broker
        self.model = model  # "provider/model", e.g. anthropic/claude-opus-4-6
        self._resume = resume
        self.session_id = resume or session_id
        self.permission_mode = permission_mode
        self._binary = binary
        self._password = secrets.token_urlsafe(18)
        self._http: httpx.AsyncClient | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._base_url: str | None = None
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._pump_task: asyncio.Task[None] | None = None
        self._stdout_task: asyncio.Task[None] | None = None
        # Cumulative cost/tokens as of the last Done line, for deltas.
        self._cost_seen = 0.0
        self.last_context: ContextState | None = None
        self._seen_tools: set[str] = set()
        self._limit_hint = 0

    # ---- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        await self._start_server()
        assert self._http is not None and self._base_url is not None
        if self._resume:
            resp = await self._http.get(f"/session/{self._resume}")
            if resp.status_code != 200:
                raise RuntimeError(f"opencode session {self._resume} not found.")
            record = resp.json()
            self.session_id = record.get("id") or self._resume
            title = record.get("title")
            if title and record.get("parentID") is None:
                pass
        else:
            resp = await self._http.post("/session", json={"title": ""})
            resp.raise_for_status()
            self.session_id = resp.json()["id"]
        self._pump_task = asyncio.create_task(self._pump())

    async def _start_server(self) -> None:
        env = dict(**__import__("os").environ)
        env["OPENCODE_SERVER_PASSWORD"] = self._password
        self._proc = await asyncio.create_subprocess_exec(
            self._binary, "serve", "--port", "0", "--hostname", "127.0.0.1",
            cwd=self.cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        port = await self._read_port()
        self._base_url = f"http://127.0.0.1:{port}"
        auth = ("opencode", self._password)
        self._http = httpx.AsyncClient(base_url=self._base_url, auth=auth, timeout=30.0)
        deadline = time.monotonic() + _SERVER_BOOT_TIMEOUT
        while time.monotonic() < deadline:
            try:
                resp = await self._http.get("/global/health")
                if resp.status_code == 200 and resp.json().get("healthy"):
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.3)
        raise RuntimeError("opencode server did not become healthy in time.")

    async def _read_port(self) -> int:
        """Parse the listen address off the server banner."""
        import re

        assert self._proc is not None and self._proc.stdout is not None
        deadline = time.monotonic() + _SERVER_BOOT_TIMEOUT

        async def drain() -> None:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    return

        pattern = re.compile(r"http://[\d.]+:(\d+)")
        while time.monotonic() < deadline:
            line = await asyncio.wait_for(self._proc.stdout.readline(), timeout=5)
            match = pattern.search(line.decode("utf-8", errors="replace"))
            if match:
                self._stdout_task = asyncio.create_task(drain())
                return int(match.group(1))
            await asyncio.sleep(0)
        raise RuntimeError("Could not read the opencode server port.")

    async def _pump(self) -> None:
        """Stream the server's SSE feed into our queue.

        Mirrors the Claude provider's pump design: the stream belongs to this
        task alone, so turn-level timeouts cancel only our *wait*, never the
        subscription.
        """
        assert self._http is not None
        try:
            async with self._http.stream("GET", "/event", timeout=None) as resp:
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    try:
                        event = json.loads(line[len("data:") :].strip())
                    except ValueError:
                        continue
                    await self._queue.put(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - surfaced to the consumer
            await self._queue.put({"type": "__transport_error", "error": repr(exc)})

    # ---- turns -----------------------------------------------------------

    async def run_turn(self, text: str) -> AsyncIterator[Block]:
        if self._http is None or self.session_id is None:
            raise RuntimeError("Provider not started")

        await self._drain_stale_events()

        body: dict[str, Any] = {
            "parts": [{"type": "text", "text": text}],
        }
        if self.model and "/" in self.model:
            provider_id, _, model_id = self.model.partition("/")
            body["model"] = {"providerID": provider_id, "modelID": model_id}
        agent = _AGENTS.get(self.permission_mode)
        if agent:
            body["agent"] = agent

        resp = await self._post_prompt(body)
        if resp.status_code >= 400:
            yield Block(
                BlockKind.ERROR,
                title="Turn failed",
                body=f"opencode rejected the prompt ({resp.status_code}): "
                f"{resp.text[:400]}",
                is_error=True,
            )
            return

        seen_terminal = False
        while True:
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=_IDLE_TIMEOUT)
            except TimeoutError:
                yield Block(
                    BlockKind.ERROR,
                    title="Turn stalled",
                    body=f"No output for {int(_IDLE_TIMEOUT)}s; releasing the session.",
                    is_error=True,
                )
                return
            etype = event.get("type") or ""
            props = event.get("properties") or {}

            if etype == "__transport_error":
                yield Block(
                    BlockKind.ERROR,
                    title="Connection lost",
                    body=str(event.get("error")),
                    is_error=True,
                )
                return

            if props.get("sessionID") != self.session_id:
                continue

            if etype == "message.part.updated":
                for block in part_blocks(props.get("part") or {}, self._seen_tools):
                    yield block
            elif etype == "todo.updated":
                block = todo_block(self.session_id, props.get("todos") or [])
                if block is not None:
                    yield block
            elif etype == "permission.asked":
                await self._handle_permission(props)
            elif etype == "question.asked":
                await self._handle_question(props)
            elif etype == "session.error":
                message, aborted = event_error_text(props.get("error") or {})
                if not aborted:
                    yield Block(
                        BlockKind.ERROR,
                        title="Turn failed",
                        body=message,
                        is_error=True,
                    )
                seen_terminal = True
            elif etype == "session.idle":
                seen_terminal = True

            if seen_terminal:
                break

        snap = await self._snapshot()
        self._limit_hint = await self._model_context_limit()
        done = self._done_block(snap)
        if done:
            yield done

    async def _post_prompt(self, body: dict[str, Any]) -> httpx.Response:
        assert self._http is not None and self.session_id is not None
        resp = await self._http.post(
            f"/session/{self.session_id}/prompt_async", json=body
        )
        if resp.status_code == 404:
            resp = await self._http.post(
                f"/session/{self.session_id}/message", json=body
            )
        return resp

    async def _drain_stale_events(self) -> None:
        """Drop leftovers from an earlier turn so boundaries stay aligned."""
        while True:
            try:
                event = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            props = event.get("properties") or {}
            if props.get("sessionID") == self.session_id and event.get(
                "type"
            ) == "permission.asked":
                # An approval that outlived its turn still deserves an answer.
                async for _block in self._handle_permission(props):
                    pass

    async def _handle_permission(self, props: dict[str, Any]) -> None:
        """Route a permission.asked through the broker and answer the server."""
        request_id = str(props.get("id") or "")
        permission = str(props.get("permission") or "")
        metadata = props.get("metadata") or {}
        tool_name = str(metadata.get("tool") or permission or "tool").strip().title()

        allowed = False
        reason = None
        if self.permission_mode == "acceptEdits" and permission in _EDIT_PERMISSIONS:
            allowed = True
        elif self.broker is not None:
            allowed, reason = await self.broker.request(
                self.channel_id, tool_name, metadata
            )
        reply = "once" if allowed else "reject"
        try:
            await self._http.post(
                f"/permission/{request_id}/reply",
                json={"reply": reply, **({"message": reason} if reason else {})},
            )
        except (httpx.HTTPError, TypeError) as exc:
            log.warning("Could not answer opencode permission: %s", exc)

    async def _handle_question(self, props: dict[str, Any]) -> None:
        """Route a question.asked through the broker and send answers back."""
        request_id = str(props.get("id") or "")
        questions = props.get("questions") or []
        picks: list[list[str]] = [[] for _ in questions]
        asker = getattr(self.broker, "ask_structured", None)
        if asker is not None and questions:
            picks = await asker(self.channel_id, questions)
        elif self.broker is not None and questions:
            joined = await self.broker.ask(self.channel_id, {"questions": questions})
            picks = [[joined]] if joined else picks
        try:
            await self._http.post(
                f"/question/{request_id}/reply",
                json={"answers": picks},
            )
        except httpx.HTTPError as exc:
            log.warning("Could not answer opencode question: %s", exc)

    # ---- status ------------------------------------------------------------

    async def _snapshot(self) -> dict[str, Any]:
        assert self._http is not None and self.session_id is not None
        try:
            resp = await self._http.get(f"/session/{self.session_id}")
            if resp.status_code == 200:
                return resp.json() or {}
        except httpx.HTTPError:
            pass
        return {}

    async def _model_context_limit(self) -> int:
        """The active model's window size, from the model catalog."""
        assert self._http is not None
        snap = await self._snapshot()
        active = snap.get("model") if isinstance(snap.get("model"), dict) else {}
        provider_id = str(active.get("providerID") or "")
        model_id = str(active.get("id") or "")
        try:
            resp = await self._http.get("/api/model")
            items = (resp.json() or {}).get("data") or []
            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get("id") == model_id and (
                    not provider_id or item.get("providerID") == provider_id
                ):
                    ctx = ((item.get("limit") or {}).get("context")) or 0
                    if ctx > 0:
                        return int(ctx)
        except (httpx.HTTPError, ValueError):
            pass
        return 0

    def _done_block(self, snap: dict[str, Any]) -> Block | None:
        """Cost/token status after a run, from cumulative session figures.

        The session record carries lifetime totals; the difference against
        what previous Done lines showed is this run's share. The same figures
        update `last_context`: input plus cache writes are what the window
        must hold for the next request.
        """
        tokens = snap.get("tokens") or {}
        cache = tokens.get("cache") or {}
        used = (
            int(tokens.get("input") or 0)
            + int(cache.get("read") or 0)
            + int(cache.get("write") or 0)
        )
        cost = float(snap.get("cost") or 0.0)
        turn_cost = max(cost - self._cost_seen, 0.0) if cost > self._cost_seen else 0.0
        self._cost_seen = max(self._cost_seen, cost)

        bits: list[str] = []
        if cost > 0:
            bits.append(
                f"${turn_cost:.4f} this run"
                + (f" · ${cost:.2f} session" if abs(turn_cost - cost) >= 0.005 else "")
            )
        limit = getattr(self, "_limit_hint", 0) or 0
        if limit > 0 and used > 0:
            state = ContextState(used=min(used, limit), limit=limit)
            self.last_context = state
            bits.append(f"{state.pct:.0f}% ctx")
        elif used > 0:
            bits.append(f"{used:,} tokens")
        if not bits:
            return None
        return Block(BlockKind.STATUS, title="Done", body=" · ".join(bits))

    # ---- provider interface --------------------------------------------------

    def drain_pending(self, first_wait: float = 0.0) -> AsyncIterator[Block]:
        return self._drain_pending(first_wait)

    async def _drain_pending(self, first_wait: float = 0.0) -> AsyncIterator[Block]:
        """Surface events that landed outside any turn (late tasks etc.)."""
        backlog = not self._queue.empty()
        if not backlog and not first_wait:
            return
        saw_any = False
        while True:
            timeout = _DRAIN_QUIET if (backlog or saw_any) else first_wait
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=timeout)
            except TimeoutError:
                return
            saw_any = True
            etype = event.get("type") or ""
            props = event.get("properties") or {}
            if props.get("sessionID") != self.session_id:
                continue
            if etype == "message.part.updated":
                for block in part_blocks(props.get("part") or {}, self._seen_tools):
                    yield block
            elif etype == "todo.updated":
                block = todo_block(self.session_id, props.get("todos") or [])
                if block is not None:
                    yield block
            elif etype == "permission.asked":
                await self._handle_permission(props)

    async def context_usage(self) -> dict[str, Any] | None:
        """A normalized breakdown shaped like what render.context_embed reads."""
        snap = await self._snapshot()
        tokens = snap.get("tokens") or {}
        cache = tokens.get("cache") or {}
        used = (
            int(tokens.get("input") or 0)
            + int(cache.get("read") or 0)
            + int(cache.get("write") or 0)
        )
        limit = await self._model_context_limit()
        if not limit and not used:
            return None
        pct = (used / limit * 100) if limit else 0.0
        return {
            "totalTokens": used,
            "maxTokens": limit,
            "rawMaxTokens": limit,
            "percentage": round(pct, 2),
            "model": self.model
            or ((snap.get("model") or {}).get("id") if isinstance(snap.get("model"), dict) else None)
            or "opencode",
            "isAutoCompactEnabled": False,
            "memoryFiles": [],
            "categories": [],
        }

    async def list_commands(self) -> list[dict[str, Any]]:
        if self._http is None:
            return []
        try:
            resp = await self._http.get("/command")
            items = resp.json()
        except (httpx.HTTPError, ValueError):
            return []
        out = []
        for cmd in items or []:
            if isinstance(cmd, dict):
                out.append(
                    {
                        "name": cmd.get("name") or cmd.get("trigger") or "",
                        "description": cmd.get("description") or "",
                    }
                )
        return out

    async def title(self) -> str | None:
        snap = await self._snapshot()
        title = snap.get("title")
        return str(title) if title else None

    async def set_mode(self, mode: str) -> None:
        agent = _AGENTS.get(mode)
        if agent is None:
            raise RuntimeError(
                f"opencode supports these modes: {', '.join(MODES)}."
            )
        if mode == "acceptEdits":
            self.permission_mode = mode
            return
        if self._http is not None and self.session_id is not None:
            resp = await self._http.post(
                f"/api/session/{self.session_id}/agent", json={"agent": agent}
            )
            if resp.status_code >= 400:
                raise RuntimeError(f"opencode refused the switch: {resp.text[:200]}")
        self.permission_mode = mode

    async def interrupt(self) -> None:
        if self._http is not None and self.session_id is not None:
            try:
                await self._http.post(f"/session/{self.session_id}/abort")
            except httpx.HTTPError as exc:
                log.warning("Interrupt failed: %s", exc)

    async def stop(self) -> None:
        if self._pump_task is not None:
            self._pump_task.cancel()
            try:
                await self._pump_task
            except (asyncio.CancelledError, Exception):
                pass
            self._pump_task = None
        if self._stdout_task is not None:
            self._stdout_task.cancel()
            try:
                await self._stdout_task
            except (asyncio.CancelledError, Exception):
                pass
            self._stdout_task = None
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:
                pass
            self._http = None
        if self._proc is not None and self._proc.returncode is None:
            try:
                self._proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                try:
                    self._proc.kill()
                except ProcessLookupError:
                    pass
            self._proc = None

    # ---- module-level session metadata ---------------------------------------


async def _probe_servers():
    """Spawn one short-lived opencode server for metadata queries.

    Session records live in opencode's own storage, not ours, and the bot
    needs cwd/title/history for sessions whose server is long gone. A server
    can list sessions regardless of which project it was started in.
    """
    proc: asyncio.subprocess.Process | None = None
    client: httpx.AsyncClient | None = None
    try:
        import os as _os
        import re

        password = secrets.token_urlsafe(18)
        env = dict(**_os.environ)
        env["OPENCODE_SERVER_PASSWORD"] = password
        proc = await asyncio.create_subprocess_exec(
            "opencode", "serve", "--port", "0", "--hostname", "127.0.0.1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        assert proc.stdout is not None
        pattern = re.compile(r"http://[\d.]+:(\d+)")
        deadline = time.monotonic() + _SERVER_BOOT_TIMEOUT
        port = None
        while time.monotonic() < deadline:
            line = await proc.stdout.readline()
            if not line:
                break
            match = pattern.search(line.decode("utf-8", errors="replace"))
            if match:
                port = int(match.group(1))
                break
        if port is None:
            return
        client = httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            auth=("opencode", password),
            timeout=15.0,
        )
        yield client
    finally:
        if client is not None:
            await client.aclose()
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=5)
            except (ProcessLookupError, asyncio.TimeoutError):
                pass


async def recent_sessions(limit: int = 100) -> list[SessionRef]:
    """Resumable opencode sessions across every project, newest first."""
    refs: list[SessionRef] = []
    async for client in _probe_servers():
        try:
            resp = await client.get("/session")
            if resp.status_code != 200:
                continue
            records = resp.json() or []
        except (httpx.HTTPError, ValueError):
            continue
        for record in records[:limit]:
            if not isinstance(record, dict) or record.get("parentID"):
                continue  # subtasks are not resumable conversations
            refs.append(
                SessionRef(
                    session_id=str(record.get("id")),
                    title=record.get("title") or None,
                    directory=record.get("directory"),
                )
            )
    return refs


async def fetch_commands(
    cwd: str | None = None, *, skills: Any = None, model: str | None = None
) -> list[dict[str, Any]]:
    """Available commands/skills, read via a throwaway server."""
    out: list[dict[str, Any]] = []
    async for client in _probe_servers():
        try:
            resp = await client.get("/command")
            if resp.status_code != 200:
                continue
            for cmd in (resp.json() or []):
                if isinstance(cmd, dict):
                    out.append(
                        {
                            "name": cmd.get("name") or cmd.get("trigger") or "",
                            "description": cmd.get("description") or "",
                        }
                    )
        except (httpx.HTTPError, ValueError):
            continue
    return out


async def fetch_models() -> list[str]:
    """Available models as "providerID/modelID", read via a throwaway server."""
    out: list[str] = []
    async for client in _probe_servers():
        try:
            resp = await client.get("/api/model")
            items = ((resp.json() or {}).get("data")) or []
        except (httpx.HTTPError, ValueError):
            continue
        for item in items:
            if isinstance(item, dict) and item.get("id"):
                out.append(f"{item.get('providerID') or ''}/{item['id']}")
    return sorted(set(out))


async def session_cwd(session_id: str) -> str | None:
    """Resolve where an opencode session lives, via a throwaway server."""
    async for client in _probe_servers():
        try:
            resp = await client.get(f"/session/{session_id}")
            if resp.status_code == 200:
                directory = (resp.json() or {}).get("directory")
                return str(directory) if directory else None
        except httpx.HTTPError:
            continue
    return None


async def session_title(session_id: str) -> str | None:
    """The session's title from opencode's own records."""
    async for client in _probe_servers():
        try:
            resp = await client.get(f"/session/{session_id}")
            if resp.status_code == 200:
                title = (resp.json() or {}).get("title")
                return str(title) if title else None
        except httpx.HTTPError:
            continue
    return None


async def textual_history(session_id: str) -> list[tuple[str, str]]:
    """Prior conversation as (role, text), text parts only."""
    out: list[tuple[str, str]] = []
    async for client in _probe_servers():
        try:
            resp = await client.get(f"/session/{session_id}/message")
            if resp.status_code != 200:
                continue
            records = resp.json() or []
        except (httpx.HTTPError, ValueError):
            continue
        rows = records.get("data") if isinstance(records, dict) else records
        for row in rows or []:
            info = row.get("info") if isinstance(row, dict) else None
            role = str((info or {}).get("role") or row.get("role") or "")
            parts = row.get("parts") if isinstance(row, dict) else None
            texts = [
                str(p.get("text"))
                for p in (parts or [])
                if isinstance(p, dict)
                and p.get("type") == "text"
                and str(p.get("text") or "").strip()
            ]
            if texts:
                out.append((role, "\n".join(texts)))
    return out
