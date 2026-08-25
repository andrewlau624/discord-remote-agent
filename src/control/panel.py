"""The one live message in #control: a screenshot, plus buttons.

ControlPanel keeps a single message and normally edits it in place with a fresh
screenshot after every action, the same way TaskBoard keeps one message per
task.

The wrinkle is that Discord leaves an edited message where it was posted, so
anything said in the channel afterwards pushes the panel up and out of view.
So render() checks whether the panel is still the newest message and, if it is
not, deletes and reposts it. The panel therefore always sits at the bottom of
the channel, and the cheap edit path is still what runs whenever nothing else
has been said -- which, because ControlManager deletes commands as it reads
them, is most of the time.

ControlView is the button row; it only forwards Actions, it never touches the
browser.
"""

from __future__ import annotations

import io
from typing import Awaitable, Callable

import discord

from src.control.types import Action, ActionKind, PageState

_COLOR = 0x5865F2
_ERROR_COLOR = 0xED4245
_FOOTER = "type a number to click · `go <url>` · `type <text>` · `key <name>`"

# (label, emoji, ActionKind, row) for each button, in display order.
_BUTTONS = [
    ("Back", "◀️", ActionKind.BACK, 0),
    ("Forward", "▶️", ActionKind.FORWARD, 0),
    ("Reload", "🔄", ActionKind.RELOAD, 0),
    ("Shot", "📷", ActionKind.REFRESH, 0),
    ("Up", "⬆️", ActionKind.SCROLL_UP, 1),
    ("Down", "⬇️", ActionKind.SCROLL_DOWN, 1),
    ("Enter", "↩️", ActionKind.ENTER, 1),
]


class ControlView(discord.ui.View):
    """Fixed actions, owner-gated.

    Not persistent across restarts on purpose: a bot restart kills the browser
    process, and a live button pointing at a dead browser is worse than a dead
    button.
    """

    def __init__(
        self, owner_id: int, on_action: Callable[[Action], Awaitable[None]]
    ) -> None:
        super().__init__(timeout=None)
        self._owner_id = owner_id
        self._on_action = on_action
        for label, emoji, kind, row in _BUTTONS:
            self.add_item(_ActionButton(label, emoji, kind, row, self._dispatch))
        self.add_item(_ActionButton("End", "⏹️", ActionKind.END, 1, self._dispatch,
                                    style=discord.ButtonStyle.danger))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self._owner_id:
            return True
        await interaction.response.send_message(
            "This panel belongs to the server owner.", ephemeral=True
        )
        return False

    async def _dispatch(
        self, interaction: discord.Interaction, kind: ActionKind
    ) -> None:
        # Ack first: Discord kills an un-acked interaction after 3s, and a page
        # load plus screenshot routinely takes longer. The panel is edited
        # out-of-band once the action finishes.
        await interaction.response.defer()
        await self._on_action(Action(kind=kind))


class _ActionButton(discord.ui.Button):
    def __init__(
        self,
        label: str,
        emoji: str,
        kind: ActionKind,
        row: int,
        dispatch: Callable[[discord.Interaction, ActionKind], Awaitable[None]],
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
    ) -> None:
        super().__init__(label=label, emoji=emoji, row=row, style=style)
        self._kind = kind
        self._dispatch = dispatch

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._dispatch(interaction, self._kind)


class ControlPanel:
    def __init__(
        self,
        channel: discord.TextChannel,
        owner_id: int,
        on_action: Callable[[Action], Awaitable[None]],
    ) -> None:
        self._channel = channel
        self._view = ControlView(owner_id, on_action)
        self._message: discord.Message | None = None
        self._seq = 0

    async def render(self, state: PageState, *, status: str = "") -> None:
        """Draw the current page, keeping the panel at the bottom of the channel."""
        self._seq += 1
        name = f"page-{self._seq}.png"

        embed = discord.Embed(
            title=(state.title or "(untitled)")[:250],
            description=f"{state.url[:400]}\n{status}".strip(),
            color=_ERROR_COLOR if status else _COLOR,
        )
        embed.set_image(url=f"attachment://{name}")
        embed.set_footer(text=f"{len(state.hints)} hints · {_FOOTER}")

        def attachment() -> discord.File:
            # A File wraps a stream that is consumed on send, so a retry after
            # a failed edit needs a fresh one rather than the spent original.
            return discord.File(io.BytesIO(state.png), filename=name)

        if self._is_at_bottom():
            # Passing only new File objects replaces the previous attachment,
            # and edit() returns a new Message, so it has to be reassigned.
            self._message = await self._message.edit(
                embed=embed, attachments=[attachment()], view=self._view
            )
            return
        await self._repost(embed, attachment())

    def _is_at_bottom(self) -> bool:
        """Is the panel already the newest message in the channel?

        last_message_id is kept current by the gateway, so this costs nothing:
        no API call on the hot path just to decide how to draw. Editing does
        not move a message, so the id only changes when something new is said.
        """
        if self._message is None:
            return False
        last = self._channel.last_message_id
        # An unknown id means nothing has been seen since startup; editing is
        # the cheaper guess and self-corrects on the next message.
        return last is None or last == self._message.id

    async def _repost(self, embed: discord.Embed, file: discord.File) -> None:
        """Send a new panel at the bottom and drop the stale one."""
        stale = self._message
        self._message = await self._channel.send(
            embed=embed, file=file, view=self._view
        )
        if stale is None:
            return
        try:
            await stale.delete()
        except discord.HTTPException:
            # Already gone, or we lack Manage Messages. The new panel is up
            # either way, which is the part that matters.
            pass

    async def close(self, reason: str) -> None:
        """Drop the buttons and say why the session ended."""
        self._view.stop()
        if self._message is None:
            await self._channel.send(f"⏹️ Control session ended ({reason}).")
            return
        try:
            await self._message.edit(
                content=f"⏹️ Control session ended ({reason}).", view=None
            )
        except discord.HTTPException:
            pass
