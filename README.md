# discord-remote-agent

Control CLI coding agents from Discord. Run it on your machine and a dedicated
server becomes the interface. Each channel pins to one agent session, agent
output shows up as blocks (thinking, tool calls, tool results, text), and tool
calls that can change things wait for you to vote Approve or Deny in a poll.

Claude Code is the first provider. The provider layer is pluggable, so Codex,
Gemini, and opencode can slot in behind the same interface later.

## How it works

- One session per channel. `/new` pins a session, `/stop` unpins and frees the
  channel. You can run different sessions in different channels at the same time.
- Type in a pinned channel to talk to the agent.
- Read-only tools run on their own. Bash, Write, Edit, and anything else not in
  the allowlist post an Approve/Deny poll first.
- Pins survive restarts. Send a message in a pinned channel after a restart and
  it resumes on its own, or use `/resume <id>`.

Claude runs through the Claude Agent SDK, so blocks and session data come from
the SDK. Working directories and titles come from the agent's own session data,
so there is no default path to configure.

## Setup

1. Install Claude Code and sign in. The SDK shells out to it.
2. Install deps:

   ```
   make install
   ```

3. Make a Discord app and bot at https://discord.com/developers/applications.
   Under Bot, turn on the Message Content Intent. Invite it with the
   `applications.commands` and `bot` scopes.
4. Copy the env file and fill it in:

   ```
   cp .env.example .env
   ```

   Set `DISCORD_TOKEN` and `GUILD_ID` for your server. Only the server owner can
   use the bot, so there is no allowlist to configure.
5. Run it:

   ```
   make run
   ```

## Config

Secrets and identity live in `.env`. Everything else is in `config.toml`:

- `model` for Claude Code (blank uses the default)
- `approval_timeout` in seconds
- `db_path` for the pin database
- `skills` to load ("all", "none", or a list)
- `tools.auto_approve`, the tools that run without a poll

## Commands

- `/new [cwd]` start a session here (cwd defaults to where the bot runs)
- `/resume <session_id> [cwd]` resume a session and pin it here
- `/list` show pinned sessions
- `/provider <name>` pick the provider for the next `/new`
- `/skills` list available skills and commands
- `/skill <name> [args]` run a skill in this session
- `/interrupt` stop the current turn
- `/stop` end this channel's session and free it

## Security

This runs commands on your machine. Only the Discord server owner can drive the
bot or vote on approvals, everyone else is ignored. Keep the server private.
Auto-approved read tools can still read files, so tune `tools.auto_approve` if
you want tighter control.

## Layout

```
run.py             entrypoint
config.toml        behavior settings
dra/config.py      env + toml config
dra/store.py       sqlite channel pins
dra/session.py     per-channel turn loop
dra/permissions.py approval polls
dra/render.py      blocks to embeds
dra/bot.py         commands and message handling
dra/providers/     provider interface + claude
```
