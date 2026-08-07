# Agent Town

A local dashboard for running several [Claude Code](https://docs.claude.com/en/docs/claude-code) agents against an Odoo addons repository at once. Each agent gets its own git worktree, so multiple agents can write code in parallel without stepping on each other. You review the diff and decide what gets pushed — agents never commit or push on their own.

The whole thing is two files (`fleet.py` + `index.html`): no database, no cloud, everything runs locally on your machine.

![status: alpha](https://img.shields.io/badge/status-alpha-yellow)

## How it works

- **Desks** — 5 general-purpose desks plus a 2-desk **Urgent Room** that's always kept free for critical work.
- Assigning a task spins up a `claude -p ...` subprocess in a dedicated git worktree (`fleet/<desk>` branch), scoped to a minimal, read-mostly toolset.
- AI automatically picks a model (haiku/sonnet/opus) and flags whether a task sounds urgent, based on the task text.
- When an agent finishes, you can **Review** (a separate read-only reviewer agent checks the diff against your repo's `CLAUDE.md` conventions), then **Push** to one or more branches, or **Discard**.
- Optional local voice input (via [whisper.cpp](https://github.com/ggerganov/whisper.cpp)) lets you speak a task instead of typing it; non-English speech is translated to English automatically before it lands in the task box.

## Requirements

| Requirement | Check |
|---|---|
| Python 3 | `python3 --version` |
| git | `git --version` |
| [Claude Code CLI](https://docs.claude.com/en/docs/claude-code), logged in | `claude --version` |
| An Odoo repo checked out locally | the `custom_addons` folder you work in |

Install the Claude Code CLI if you don't have it: `npm install -g @anthropic-ai/claude-code`, then run `claude` once to log in.

## Setup

```bash
git clone https://github.com/<your-username>/agent-town.git
cd agent-town
python3 fleet.py --selftest   # should print "selftest ok"
```

Point it at your Odoo repo and base branch:

```bash
export FLEET_PROJECT=/path/to/your/custom_addons
export FLEET_BASE=main        # or stage, develop, etc.
```

Add those exports to your `~/.bashrc` / `~/.zshrc` to make them permanent. Without `FLEET_PROJECT` set, it defaults to `~/odoo/custom_addons`; without `FLEET_BASE`, it defaults to `main`.

Run it:

```bash
python3 fleet.py
```

Open **http://127.0.0.1:8765** — you should see 5 desks + an Urgent Room, all "empty".

To keep it running after closing the terminal:

```bash
nohup python3 fleet.py > server.log 2>&1 &
```

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `FLEET_PROJECT` | `~/odoo/custom_addons` | Path to the Odoo addons repo to work on |
| `FLEET_BASE` | `main` | Branch each desk's worktree resets to. Can also be changed at runtime from the dashboard's base-branch picker, which stays in sync with your repo's actual branches. |
| `FLEET_PORT` | `8765` | Port the dashboard listens on |
| `FLEET_ODOO_VERSION` | `17` | Odoo version referenced in agent prompts |
| `FLEET_REVIEW_AGENT` | *(none)* | Name of a custom subagent (`.claude/agents/*.md`) to use for reviews, if you have one. Reviews use the default agent otherwise. |

## Voice input (optional)

Only needed if you want to speak tasks instead of typing them. The rest of the dashboard works fully without it.

```bash
bash setup-voice.sh
```

This script:
1. Checks for `cmake` (installs via `sudo` if available, otherwise into an isolated venv — never touches the system without permission).
2. Clones and builds `whisper.cpp` (~2–5 minutes, needs internet).
3. Downloads the speech model (~141MB, one time).

Everything runs offline/locally — audio never leaves your machine. If you speak in a language other than English, it's automatically translated to English before it's inserted into the task box.

## Troubleshooting

| Problem | Fix |
|---|---|
| `claude: command not found` | `npm install -g @anthropic-ai/claude-code`, then open a new terminal |
| Server runs but agents don't do anything | Run `claude --version` to check you're logged in |
| `port 8765 already in use` | `FLEET_PORT=8766 python3 fleet.py` |
| Voice setup fails on cmake | `sudo apt install cmake`, then re-run `bash setup-voice.sh` |
| Mic button always disabled | Browser may be blocking mic permission — check the lock icon in the address bar |
| Worktrees look stale/broken | `rm -rf wt` and `git -C /path/to/custom_addons worktree prune` — they're recreated automatically next run. (`fleet.py` also self-heals this on the next task if you forget the `prune` step.) |

## Daily use

```bash
# start
python3 fleet.py

# stop
pkill -f "python3 fleet.py"   # or Ctrl+C in the terminal it's running in

# check everything's OK
python3 fleet.py --selftest
```

## Safety model

- Agents get `Read`/`Grep`/`Glob` and read-only git tools always; `Edit`/`Write` only for tasks that need to write code. No agent ever gets a shell, a git-write tool, or a commit/push tool.
- All writes happen in an isolated git worktree per desk (`fleet/<desk>` branch) — your base branch checkout is never touched.
- Pushing to `main`/`master` directly is blocked; you push to another branch and open a PR as usual.

## Development

```bash
python3 fleet.py --selftest   # backend tests
node test_ui.js               # frontend logic tests
```

## License

[MIT](LICENSE)
