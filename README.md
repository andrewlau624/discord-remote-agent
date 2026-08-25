<img width="1370" height="1152" alt="discord-profile-preview" src="https://github.com/user-attachments/assets/fe0aa3da-ed3c-46cb-a4ac-39dd529f8794" />

## Overview

Drive CLI coding agents from Discord. Run it on your machine and a private
server becomes the interface. Each session is a forum post, the agent's output
shows up as blocks (thinking, tool calls, tool results, text), and anything that
could change your files waits for you to vote Approve or Deny.

Providers are pluggable and normalized: **claude** (via the Claude Agent SDK)
and **opencode** (via a per-session `opencode serve` instance) ship today, and
each gets its own forum — `sessions-claude`, `sessions-opencode`. Commands like
`!mode`, `!context`, `!list` and `!handoff` work the same against either.

Send files with your message: small text files are inlined into the prompt,
images and other files are saved to disk and handed to the agent by path (both
agents can read images that way).

## Setup

1. Install Claude Code and sign in. The SDK shells out to it.
2. Install [opencode](https://opencode.ai) if you want the opencode provider.
3. Install deps:

   ```
   make install
   ```

4. Make a Discord app and bot at https://discord.com/developers/applications.
   Under Bot, turn on the Message Content Intent. Invite it with the `bot` scope
   and give it Manage Channels (to create the sessions forums), Manage Messages
   (to clear page reactions), plus Send Messages, Create Posts, and Send Messages
   in Threads.
5. Copy the env file and set your token:

   ```
   cp .env.example .env
   ```

   Set `DISCORD_TOKEN`. Only the server owner can use the bot, so there is no
   allowlist to configure.
6. Run it:

   ```
   make run
   ```

7. Or run it as a login service so it survives closing the terminal, and
   auto-restarts if it crashes:

   ```
   mkdir -p ~/Library/LaunchAgents
   cp launchd/com.discord-remote-agent.plist ~/Library/LaunchAgents/
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.discord-remote-agent.plist
   ```

   Logs land in `~/Library/Logs/discord-remote-agent.log`. Restart with
   `launchctl kickstart -k gui/$(id -u)/com.discord-remote-agent`; stop with
   `launchctl bootout gui/$(id -u)/com.discord-remote-agent`. To also keep it
   running with the MacBook lid closed: `sudo pmset -c disablesleep 1`
   (revert with `sudo pmset -c disablesleep 0`); keep it plugged in.

## Anthropic accounts from anywhere

`!login [name]` starts Claude's OAuth login right from Discord: the bot posts
the authorize link, you open it on any device, approve, and paste the code back
into the thread. The long-lived token is saved under `name` in `auth.json`
(gitignored, mode 0600) and becomes the account new sessions use. Keep several
accounts around and flip between them with `!account <name>`; `!logout <name>`
forgets one. Running sessions are unaffected until restarted.

## Config

Secrets live in `.env`. Everything else is in `config.toml`:

- `prefix` for commands (default `!`)
- `model` for Claude Code (blank uses the default)
- `providers.opencode.model`, e.g. `"anthropic/claude-opus-4-6"` (blank uses opencode's default)
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

- `!new [repo:<name>] [branch:<branch>] [mode:<mode>] [provider:<name>]` start a
  session, opens a thread under `sessions-<provider>`. A repo name resolves
  under `default_cwd`, or pass a full path. A bare first argument is the repo,
  so `!new myrepo` still works. `branch:` runs the session in a git worktree for
  that branch (reused if one exists, created under `<repo>/.worktrees/` if not,
  branch created on demand). Gitignored `.env*` files are carried over from the
  main checkout, existing files are never overwritten. `mode:` starts in that
  permission mode; modes are provider-specific — claude supports
  `default/acceptEdits/auto/plan/bypassPermissions`, opencode supports
  `default/acceptEdits/plan`.
- `!repos` list repos under the base path with their branches and existing
  worktrees, so you can reuse instead of creating new ones
- `!resume [session_id] [cwd]` with no argument, restart the session this
  thread is already bound to — that is how a thread stopped with `!stop` comes
  back. With an id, resume that session (in its existing thread if it still
  has one, otherwise a new one).
- `!list` show resumable sessions across all providers (paged): 📌 open,
  ⏸️ stopped but still bound to a thread, ▫️ unbound
- `!provider <name>` pick the provider for the next `!new`
- `!login [name]` / `!accounts` / `!account <name>` / `!logout <name>` manage
  Anthropic accounts remotely — see above
- `!skills` list available skills and commands (paged)
- `!skill <name> [args]` run a skill in this session
- `!mode <name>` switch permission mode; validated against the thread's
  provider and saved across restarts. Switching to `bypassPermissions`
  relaunches a claude session, since the CLI only honors bypass at launch.
- `!context` show how full this session's context window is: a breakdown by
  category for claude (messages, system prompt, tools), token totals for both,
  and whether autocompact is on. Every turn's `Done` line also carries the
  percentage plus an accurate dollar figure: cost shown is *that run's* share,
  with the session total alongside once they meaningfully differ.
- `!handoff` carry this session into a fresh thread. The outgoing session
  writes its own handoff brief; that brief, the recent exchange, and the
  repo's actual state seed the new session. The old thread is stopped,
  archived, and linked to the new one.
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

## Live output

A turn holds the typing indicator while it works, including while it waits on
your approval. Messages sent mid-turn are queued and tell you so ("⏳ Queued
behind work still running here") instead of vanishing silently.

When a turn ends but background work keeps going — subagents finishing after
their result frame, continuations waking the parent — a watcher drains that
output and posts it as it lands, so nothing waits for your next message to
drag it out.

## Tests

```
make test
```

Installs anything missing into `.venv` and runs pytest. No Discord connection
and no API calls — the SDK message types are constructed directly and the
Discord objects are stubbed, so the suite runs offline in a couple of seconds.

What it pins is mostly the non-obvious behaviour that was expensive to get
right: where a turn ends when a subagent settles before its result frame, that
context math reads the *last request's* input side rather than the session's
cumulative totals, that the Done line reports each run's cost as the delta of
the cumulative figure, that late output is drained without waiting for another
message, that `acceptEdits` waves through edits without consulting the broker,
and that an auto-approve toggle follows the tool name rather than its row on
the page.

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
