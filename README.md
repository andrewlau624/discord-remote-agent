<img width="1370" height="1152" alt="discord-profile-preview" src="https://github.com/user-attachments/assets/fe0aa3da-ed3c-46cb-a4ac-39dd529f8794" />

## Overview

Drive CLI coding agents from Discord. Run it on your machine and a private
server becomes the interface. Each session is a forum post, the agent's output
shows up as blocks (thinking, tool calls, tool results, text), and anything that
could change your files waits for you to vote Approve or Deny.

Currently, Claude is the only supported provider.

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
- `tools.auto_approve`, the tools that run without a poll — only used to seed
  the first run; after that `!auto-approve` owns it (see below)
- `context.warn_at` / `context.warn_again_at`, the context-fullness
  percentages that trigger a warning (defaults 75 and 90; `warn_at = 0` off)

## Commands

Type them in any channel with the prefix (default `!`).

- `!new [repo:<name>] [branch:<branch>] [mode:<mode>]` start a session, opens a
  thread. A repo name resolves under `default_cwd`, or pass a full path. A bare
  first argument is the repo, so `!new myrepo` still works. `branch:` runs the
  session in a git worktree for that branch (reused if one exists, created
  under `<repo>/.worktrees/` if not, branch created on demand). Gitignored
  `.env*` files are carried over from the main checkout, existing files are
  never overwritten. `mode:` starts
  in that permission mode; `mode:bypassPermissions` at launch is the only way
  to get bypass, the CLI refuses to switch into it later.
- `!repos` list repos under the base path with their branches and existing
  worktrees, so you can reuse instead of creating new ones
- `!resume [session_id] [cwd]` with no argument, restart the session this
  thread is already bound to — that is how a thread stopped with `!stop` comes
  back. With an id, resume that session (in its existing thread if it still
  has one, otherwise a new one).
- `!list` show resumable sessions (paged): 📌 open, ⏸️ stopped but still bound
  to a thread, ▫️ unbound
- `!provider <name>` pick the provider for the next `!new`
- `!skills` list available skills and commands (paged)
- `!skill <name> [args]` run a skill in this session
- `!mode <name>` switch permission mode: `default`, `acceptEdits`, `auto`,
  `plan`, `bypassPermissions`. The mode is saved per thread and survives bot
  restarts. Switching to `bypassPermissions` relaunches the session, since the
  CLI only honors bypass at launch.
- `!context` show how full this session's context window is: a breakdown by
  category (messages, system prompt, tools), the memory files loaded, and
  whether autocompact is on. Every turn's `Done` line also carries the
  percentage, which costs nothing extra — it comes from usage the turn already
  reports.
- `!handoff` carry this session into a fresh thread. The outgoing session
  writes its own handoff brief (it still has the full context, so nothing else
  summarizes it as well); that brief, the recent exchange, and the repo's
  actual state — branch, HEAD, uncommitted diffstat — seed the new session.
  The old thread is stopped, archived, and linked to the new one.
- `!auto-approve` (also `!auto`) open a panel to choose which tools run
  without asking — see Approvals below
- `!view` open a panel to toggle what shows (thinking, tool calls, tool
  results, task progress)
- `!interrupt` stop the current turn
- `!stop [forget]` end this session and archive its thread. The binding is
  kept, so `!resume` in that thread starts it again; the thread is archived
  but not locked, so you can still post there. `!stop forget` drops the
  binding for good.
- `!help` list commands

## Tests

```
make test
```

Installs anything missing into `.venv` and runs pytest. No Discord connection
and no API calls — the SDK message types are constructed directly and the
Discord objects are stubbed, so the suite runs offline in about a second.

What it pins is mostly the non-obvious behaviour that was expensive to get
right: where a turn ends when a subagent settles before its result frame, that
the context warning is dispatched outside the session lock (inside it, the
handoff action deadlocks on its own `ask`), that `acceptEdits` waves through
edits without consulting the broker, and that an auto-approve toggle follows
the tool name rather than its row on the page.

## Context management

A session that fills its context window starts losing the earlier half of the
conversation. The bot watches for that instead of letting it happen quietly:
when fullness crosses `warn_at` (and again at `warn_again_at`, both in
`config.toml`), it posts a warning offering three things:

- 🗜️ **Compact** — run `/compact` in place and carry on in the same thread
- 🔄 **Hand off** — the `!handoff` flow above
- ✖️ **Ignore** — carry on; the CLI's own autocompact still applies

Each threshold fires at most once per session, so a session that sits at 78%
for twenty turns warns once, not twenty times. Set `warn_at = 0` to turn the
warnings off entirely.

## Approvals

Any tool that is not auto-approved pops a poll. `!auto-approve` opens a panel
listing the tools, one per row, and a tap toggles each:

```
1️⃣ ✅ `Bash`          ✅ runs unattended
2️⃣ ⬜ `Edit`          ⬜ asks first
```

`◀`/`▶` page through the list — Discord allows only twenty reactions per
message, and MCP servers push the tool count past that. `⚡` turns on **accept
all**, which approves everything and ignores the per-tool settings while it is
on.

You do not have to know tool names in advance. The panel starts with Claude
Code's built-ins, and every tool the bot is ever asked to approve is added to
the list automatically — so anything that prompts you once can be switched off
from then on. Settings live in `approvals.json` next to the database and apply
across all threads.

Auto-approval is enforced by the bot, not by the CLI's `allowed_tools`. That is
deliberate: an `allowed_tools` entry is honored by the SDK *before* the bot's
permission callback runs, which would make the tool invisible to the bot —
unlearnable, and impossible to switch back off without a restart.

`!mode acceptEdits` is the per-thread alternative: it waves through file edits
(`Edit`, `Write`, `MultiEdit`, `NotebookEdit`) while still asking about
everything else, and unlike the panel it applies to one thread rather than all
of them. It is likewise handled by the bot, since the CLI's own `acceptEdits`
never takes effect while a permission callback is installed.

> [!WARNING]
> Accept all is `bypassPermissions` in all but name: shell commands and file
> writes run with no prompt, in every thread, and it survives restarts. The
> panel turns orange and says so while it is on.
