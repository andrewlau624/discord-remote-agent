"""Anthropic account login and switching, driven from Discord.

`claude setup-token` performs the subscription OAuth dance: it prints an
authorize link, you open it anywhere, approve, and paste the resulting code
back; it then prints a long-lived token (`sk-ant-oat01-…`). That token is what
the CLI itself accepts as `CLAUDE_CODE_OAUTH_TOKEN`, so storing several of them
means switching accounts without touching the machine -- start a login here,
tap the link on your phone, send the code back to this chat.

The CLI is an interactive TUI that renders nothing without a terminal, so the
flow runs it under a pseudo-terminal and scrapes the rendered text. Accounts
persist in `auth.json` (mode 0600, next to the database); the active account's
token rides into every newly launched provider session.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import pty
import re
import struct
import termios
from dataclasses import dataclass
from pathlib import Path

#: The authorize link the CLI prints, possibly split across visual lines.
_URL_RE = re.compile(r"https://\S*oauth/authorize\S+")

#: Long-lived OAuth tokens as printed by a successful exchange.
_TOKEN_RE = re.compile(r"sk-ant-oat01-[A-Za-z0-9_\-]{8,}")

_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?<>=]*[A-Za-z]"  # CSI sequences (cursor movement, colors)
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC sequences (hyperlinks)
    r"|[\x00-\x08\x0b-\x1a\x1c-\x1f]"  # control bytes except \n
)

#: How long to wait for the CLI to produce its authorize link / accept a code.
_URL_TIMEOUT = 60.0
_CODE_TIMEOUT = 90.0

_POLL = 0.25


def clean(text: str) -> str:
    return _ANSI_RE.sub("", text)


def find_url(text: str) -> str | None:
    match = _URL_RE.search(clean(text))
    return match.group(0).rstrip("\\") if match else None


def find_token(text: str) -> str | None:
    # A wide-enough pty keeps the token on one line, but unwrap anyway so a
    # narrow terminal cannot corrupt the match.
    flat = clean(text).replace("\r", "").replace("\n", "")
    match = _TOKEN_RE.search(flat)
    return match.group(0) if match else None


def code_from_reply(text: str) -> str | None:
    """Accept either the bare code or the whole callback URL pasted back."""
    value = text.strip().strip("`<>")
    match = re.search(r"[?&]code=([A-Za-z0-9_\-\.]+)", value)
    if match:
        return match.group(1)
    if re.fullmatch(r"[A-Za-z0-9_\-\.]{10,}", value):
        return value
    return None


class AuthStore:
    """Named Anthropic accounts and which one new sessions should use."""

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self.active: str | None = None
        self.accounts: dict[str, str] = {}

    @classmethod
    def load(cls, path: str) -> "AuthStore":
        store = cls(path)
        try:
            data = json.loads(store._path.read_text(encoding="utf-8"))
            store.active = data.get("active") or None
            store.accounts = {
                str(k): str(v) for k, v in (data.get("accounts") or {}).items()
            }
        except (OSError, ValueError):
            pass
        return store

    def save(self) -> None:
        try:
            self._path.write_text(
                json.dumps(
                    {"active": self.active, "accounts": self.accounts}, indent=2
                ),
                encoding="utf-8",
            )
            os.chmod(self._path, 0o600)
        except OSError:
            pass

    def set_account(self, name: str, token: str) -> None:
        self.accounts[name] = token
        self.active = name
        self.save()

    def switch(self, name: str) -> bool:
        if name not in self.accounts:
            return False
        self.active = name
        self.save()
        return True

    @property
    def token(self) -> str | None:
        return self.accounts.get(self.active) if self.active else None

    def forget(self, name: str) -> bool:
        if name not in self.accounts:
            return False
        del self.accounts[name]
        if self.active == name:
            self.active = next(iter(self.accounts), None)
        self.save()
        return True


@dataclass
class _PendingLogin:
    """One running `claude setup-token` under its own pseudo-terminal."""

    name: str
    channel_id: int
    proc: asyncio.subprocess.Process
    master: int
    buffer: str = ""
    code_from: int = 0


class LoginManager:
    """One login flow at a time, awaiting its code from a Discord channel."""

    def __init__(self, store: AuthStore) -> None:
        self.store = store
        self._pending: _PendingLogin | None = None

    @property
    def waiting_channel(self) -> int | None:
        """The channel whose next owner message should be read as a code."""
        return self._pending.channel_id if self._pending else None

    async def start(self, channel_id: int, name: str) -> str:
        """Launch the flow; returns the authorize link to show the user."""
        if self._pending is not None:
            raise RuntimeError(
                "A login is already in progress; finish or cancel it first."
            )
        master, slave = pty.openpty()
        # A very wide window keeps long URLs and tokens unsplit.
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 60, 500, 0, 0))
        proc = await asyncio.create_subprocess_exec(
            "claude", "setup-token",
            stdin=slave, stdout=slave, stderr=slave,
            preexec_fn=os.setsid,
        )
        os.close(slave)
        self._pending = _PendingLogin(name=name, channel_id=channel_id, proc=proc, master=master)
        loop = asyncio.get_running_loop()

        def _read() -> None:
            try:
                data = os.read(master, 65536)
            except OSError:
                data = b""
            if data:
                self._pending.buffer += data.decode("utf-8", errors="replace")

        loop.add_reader(master, _read)

        deadline = loop.time() + _URL_TIMEOUT
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(_POLL)
            url = find_url(self._pending.buffer)
            if url:
                return url
            if proc.returncode is not None:
                await self.cancel()
                raise RuntimeError("The Claude CLI exited before showing a link.")
        await self.cancel()
        raise RuntimeError("Timed out waiting for the sign-in link.")

    async def submit(self, reply: str) -> str:
        """Feed the user's code in; returns a result message."""
        pending = self._pending
        if pending is None:
            return "No login is in progress."
        code = code_from_reply(reply)
        if code is None:
            return (
                "That does not look like a login code. Open the link, approve "
                "access, and paste the code it shows you."
            )
        pending.code_from = len(pending.buffer)
        os.write(pending.master, code.encode() + b"\r")

        deadline = asyncio.get_running_loop().time() + _CODE_TIMEOUT
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(_POLL)
            fresh = pending.buffer[pending.code_from:]
            token = find_token(fresh)
            if token:
                name = pending.name
                self.store.set_account(name, token)
                await self._cleanup()
                return (
                    f"Logged in as **{name}**. New sessions will use this "
                    "account."
                )
            lowered = clean(fresh).lower()
            if any(word in lowered for word in ("invalid", "failed", "error")):
                lines = [ln.strip() for ln in clean(fresh).splitlines() if ln.strip()]
                detail = lines[-1][:300] if lines else "unknown error"
                await self._cleanup()
                return f"Login failed: {detail}"
            if pending.proc.returncode is not None:
                await self._cleanup()
                return "The Claude CLI exited before finishing the login."
        await self._cleanup()
        return "Login timed out waiting for the code to be accepted."

    async def cancel(self) -> None:
        if self._pending is not None:
            await self._cleanup()

    async def _cleanup(self) -> None:
        pending, self._pending = self._pending, None
        if pending is None:
            return
        try:
            asyncio.get_running_loop().remove_reader(pending.master)
        except (ValueError, OSError):
            pass
        try:
            os.close(pending.master)
        except OSError:
            pass
        if pending.proc.returncode is None:
            try:
                pending.proc.kill()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(pending.proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass
