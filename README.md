# discord-remote-agent

Drive CLI coding agents from Discord. Run it on your machine and a private
server becomes the interface. Each session is a forum post, the agent's output
shows up as blocks (thinking, tool calls, tool results, text), and anything that
could change your files waits for you to vote Approve or Deny.

Claude Code is the first provider. The provider layer is pluggable, so Codex,
Gemini, and opencode can slot in behind the same interface later.

## How it works

- Sessions live in a forum channel called `sessions`, made the first time it is
  needed. Each session is its own post (thread), named after the Claude session,
  holding the repo, branch, working directory, and id.
- `!new` and `!resume` work from anywhere and open a thread. Type in that thread
  to talk to the agent. Run as many sessions as you want, each in its own thread.
- Read-only tools run on their own. Bash, Write, Edit, and anything else not on
  the allowlist post an Approve/Deny poll first.
- When the agent asks you something, it shows up as a real poll with its options.
  Your pick goes back to the agent as the answer. Multi-select questions add a 🆗
  reaction you tap to submit. Auto accept skips these, so leave it off if you
  want to be asked.
- `!stop` ends a session and archives its thread. Threads survive restarts, so
  posting in one resumes its session, or use `!resume <id>`.
- Resuming into a fresh thread replays the prior conversation (the text, not the
  tool calls) so you have the context.
- Long lists like `!list` and `!skills` page with ◀ ▶ reactions.
- `!view` opens a panel where you react to toggle which blocks show (thinking,
  tool calls, tool results) and to flip auto accept, which runs every tool
  without a poll. The choices persist across restarts.

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
   Under Bot, turn on the Message Content Intent. Invite it with the `bot` scope
   and give it Manage Channels (to create the sessions forum), Manage Messages
   (to clear page reactions), plus Send Messages, Create Posts, and Send Messages
   in Threads.
4. Copy the env file and set your token:

   ```
   cp .env.example .env
   ```

   Set `DISCORD_TOKEN`. Only the server owner can use the bot, so there is no
   allowlist to configure.
5. Run it:

   ```
   make run
   ```

## Config

Secrets live in `.env`. Everything else is in `config.toml`:

- `prefix` for commands (default `!`)
- `model` for Claude Code (blank uses the default)
- `default_cwd`, the base path for new sessions (blank uses the launch dir)
- `approval_timeout` in seconds
- `db_path` for the pin database
- `skills` to load ("all", "none", or a list)
- `tools.auto_approve`, the tools that run without a poll

## Commands

Type them in any channel with the prefix (default `!`).

- `!new [repo]` start a session, opens a thread (a repo name resolves under
  `default_cwd`, or pass a full path)
- `!resume <session_id> [cwd]` resume a session in a thread
- `!list` show resumable sessions (paged), 📌 marks open ones
- `!provider <name>` pick the provider for the next `!new`
- `!skills` list available skills and commands (paged)
- `!skill <name> [args]` run a skill in this session
- `!mode <name>` switch permission mode: `default`, `acceptEdits`, `auto`,
  `plan`, `bypassPermissions`
- `!view` open a panel to toggle what shows and auto accept
- `!interrupt` stop the current turn
- `!stop` end this session and archive its thread
- `!help` list commands

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
