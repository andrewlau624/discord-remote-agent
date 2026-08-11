# discord-remote-agent

Control CLI coding agents from Discord. Run it on your machine and a dedicated
server becomes the interface. Each session is a forum post, agent output shows up
as blocks (thinking, tool calls, tool results, text), and tool calls that can
change things wait for you to vote Approve or Deny in a poll.

Claude Code is the first provider. The provider layer is pluggable, so Codex,
Gemini, and opencode can slot in behind the same interface later.

## How it works

- Sessions live in a forum channel called `sessions`, created the first time you
  need it. Each session is its own forum post (thread). The post is named after
  the Claude session and holds the repo, branch, working directory, and id.
- `/new` and `/resume` work from anywhere and open (or reopen) a thread. Type in
  that thread to talk to the agent.
- Run as many sessions as you want, each in its own thread.
- Read-only tools run on their own. Bash, Write, Edit, and anything else not in
  the allowlist post an Approve/Deny poll first.
- `/stop` ends a session and archives its thread. Threads survive restarts, so
  sending a message in one resumes its session, or use `/resume <id>`.
- Commands work as slash commands or as chat commands with a prefix you set in
  config (default `!`), so `/list` and `!list` both work.
- Long lists like `/list` and `/skills` page with ◀ ▶ reactions.

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
   `applications.commands` and `bot` scopes, and give it Manage Channels (to
   create the sessions forum), Manage Messages (to clear page reactions), plus
   Send Messages, Create Posts, and Send Messages in Threads.
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

- `prefix` for chat commands (default `!`)
- `model` for Claude Code (blank uses the default)
- `approval_timeout` in seconds
- `db_path` for the pin database
- `skills` to load ("all", "none", or a list)
- `tools.auto_approve`, the tools that run without a poll

## Commands

Every command works as `/name` or `<prefix>name` (default `!name`).

- `new [cwd]` start a session, opens a thread (cwd defaults to where the bot runs)
- `resume <session_id> [cwd]` resume a session in a thread
- `list` show resumable sessions (paged), 📌 marks open ones
- `provider <name>` pick the provider for the next `new`
- `skills` list available skills and commands (paged)
- `skill <name> [args]` run a skill in this session
- `mode <name>` switch permission mode: `default`, `acceptEdits`, `plan`, `bypassPermissions`
- `interrupt` stop the current turn
- `stop` end this session and archive its thread
- `help` list commands (chat command only)

## Security

This runs commands on your machine. Only the Discord server owner can drive the
bot or vote on approvals, everyone else is ignored. Keep the server private.
Auto-approved read tools can still read files, so tune `tools.auto_approve` if
you want tighter control.

## Layout

```
run.py             entrypoint
config.toml        behavior settings
src/config.py      env + toml config
src/store.py       sqlite thread bindings
src/session.py     per-thread turn loop
src/forum.py       sessions forum + thread creation
src/permissions.py approval polls
src/paginator.py   emoji-react pagination
src/render.py      blocks to embeds
src/bot.py         commands and message handling
src/providers/     provider interface + claude
```
