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
- `tools.auto_approve`, the tools that run without a poll

## Commands

Type them in any channel with the prefix (default `!`).

- `!new [repo:<name>] [branch:<branch>] [mode:<mode>]` start a session, opens a
  thread. A repo name resolves under `default_cwd`, or pass a full path. A bare
  first argument is the repo, so `!new myrepo` still works. `branch:` runs the
  session in a git worktree for that branch (reused if one exists, created
  under `<repo>/.worktrees/` if not, branch created on demand). `mode:` starts
  in that permission mode; `mode:bypassPermissions` at launch is the only way
  to get bypass, the CLI refuses to switch into it later.
- `!repos` list repos under the base path with their branches and existing
  worktrees, so you can reuse instead of creating new ones
- `!resume <session_id> [cwd]` resume a session in a thread
- `!list` show resumable sessions (paged), 📌 marks open ones
- `!provider <name>` pick the provider for the next `!new`
- `!skills` list available skills and commands (paged)
- `!skill <name> [args]` run a skill in this session
- `!mode <name>` switch permission mode: `default`, `acceptEdits`, `auto`,
  `plan`, `bypassPermissions`. The mode is saved per thread and survives bot
  restarts. Switching to `bypassPermissions` relaunches the session, since the
  CLI only honors bypass at launch.
- `!view` open a panel to toggle what shows (thinking, tool calls, tool results)
- `!interrupt` stop the current turn
- `!stop` end this session and archive its thread
- `!help` list commands
