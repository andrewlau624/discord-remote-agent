# discord-remote-agent

Control CLI coding agents from Discord. Run it on your machine, and a dedicated
server becomes the interface: each channel is an agent session, agent output
shows up as blocks (thinking, tool calls, tool results, text), and tool calls
that can change things wait for you to tap Approve or Deny.

Claude Code is the first provider. The provider layer is pluggable, so Codex,
Gemini, and opencode can slot in behind the same interface later.

## How it works

- `/new` starts a session in the current channel.
- Type in that channel to talk to the agent.
- Read-only tools run on their own. Bash, Write, Edit, and anything else not in
  the allowlist post an Approve/Deny prompt first.
- Session ids are saved, so `/resume` picks a session back up after a restart.

Claude runs through the Claude Agent SDK, so blocks come straight from the SDK
instead of scraping a terminal. No tmux needed.

## Setup

1. Install Claude Code and sign in. The SDK shells out to it.
2. Install deps:

   ```
   pip install -r requirements.txt
   ```

3. Make a Discord app and bot at https://discord.com/developers/applications.
   Under Bot, turn on the Message Content Intent. Invite it to your server with
   the `applications.commands` and `bot` scopes.
4. Copy the config and fill it in:

   ```
   cp .env.example .env
   ```

   Set `DISCORD_TOKEN`, your user id in `OWNER_IDS`, and `GUILD_ID` for your
   server. `DEFAULT_CWD` is where new sessions start.
5. Run it:

   ```
   python run.py
   ```

## Commands

- `/new [cwd] [title]` start a session here
- `/resume <session_id> [cwd]` reattach an existing session
- `/list` show sessions
- `/provider <name>` pick the provider for the next `/new`
- `/interrupt` stop the current turn
- `/stop` end this channel's session

## Security

This runs commands on your machine. Only the ids in `OWNER_IDS` can drive the
bot or approve tools, everyone else is ignored. Keep the server private. Note
that even auto-approved read tools can read files, so tune `AUTO_APPROVE_TOOLS`
if you want tighter control.

## Layout

```
run.py            entrypoint
dra/config.py     env config
dra/store.py      sqlite session store
dra/session.py    per-channel turn loop
dra/permissions.py approval buttons
dra/render.py     blocks to embeds
dra/bot.py        commands and message handling
dra/providers/    provider interface + claude
```
