# Device bootstrap — for the Claude agent on a NEW machine (`roots`)

You are Claude Code, freshly launched on the user's **second** device (`roots`). The user's
primary device (`tal`) is already fully set up. Your job: reproduce a working environment here.
Work through this top-to-bottom. Stop and ask the user only where this doc says to.

## The split you must respect
- **CODE → git/GitHub.** Never sync code via Google Drive (it corrupts `.git`).
- **Claude's "brain" (config, skills, memory, transcripts, the project `.env`) → Google Drive**,
  via the `claude-sync` tool. Run it; don't reinvent it.

There is a **chicken-and-egg**: the tools that pull the brain (`rclone`, `claude-sync`, and the
rclone config) cannot themselves come from the pull they enable. So step 1 is a **manual copy**
from `tal` (USB / `scp -p` / any file transfer that preserves bytes — NOT a Windows drag-copy,
which strips the Unix exec bit). Everything after that is automated.

---

## Step 0 — preconditions (ask the user to confirm)
- Drive access: the same Google account used on `tal`.
- The transfer of 3 files from `tal` (next step) has happened, OR ask the user to do it.

## Step 1 — get the sync tooling onto this device (manual, one-time)
Copy these **3 paths from `tal`** to the **same paths here** (preserve exec bit — use `scp -p`,
`tar`, or `rclone`; the user can also re-download rclone):
```
~/.local/bin/rclone                # rclone v1.74.3 (or newer)
~/.local/bin/claude-sync           # the sync script (also lives on Drive at gdrive:claude-sync/bin/claude-sync)
~/.config/rclone/rclone.conf       # type=drive, scope=drive, NO token (token is added by reconnect below)
```
Make the two bin files executable: `chmod +x ~/.local/bin/{rclone,claude-sync}` and ensure
`~/.local/bin` is on `PATH`.

> If you can only get ONE file across, get `rclone` + `rclone.conf`; you can then
> `rclone copy gdrive:claude-sync/bin/claude-sync ~/.local/bin/` after auth (step 2).

## Step 2 — authenticate rclone to Google Drive (one command, interactive)
The remote `gdrive:claude-sync` is pre-defined in `rclone.conf` but has no token. Ask the user
to run (it opens a browser for Google OAuth — they must do it, not you):
```
rclone config reconnect gdrive:
```
Verify: `rclone lsd gdrive:claude-sync` should list `config/ skills/ projects/ secrets/ bin/`.

## Step 3 — hydrate the brain from Drive
```
claude-sync pull          # FORCE Drive -> local; asks for confirmation
```
This pulls: global `~/.claude/CLAUDE.md`, `settings.json`, statusline, keybindings, `skills/`,
the project memory + transcripts (username auto-remapped `-home-tal- → -home-roots-` via the
`USERHOST` token), **and the project `.env`** (lands at
`~/llm-d-benchmarking-agent/llm-d-benchmarking-agent-project/.env`).
From here on the day-to-day command is `claude-sync sync` (safe two-way merge).

## Step 4 — get the CODE (git, not Drive)
Clone the monorepo **into `~/llm-d-benchmarking-agent`** (the path must match — the project was
renamed to the repo name on `tal`, and `.env`/venv/workflow paths assume it):
```
git clone https://github.com/TalBenAmii/llm-d-benchmarking-agent.git ~/llm-d-benchmarking-agent
cd ~/llm-d-benchmarking-agent
git config core.fileMode false     # device transfers strip exec bits; stops false "modified" churn
```
> ⚠️ **Staleness check:** as of 2026-06-19 the user's local `main` on `tal` was **3 commits
> ahead of origin/main** (unpushed work). If those aren't pushed, this clone is missing them.
> Ask the user to confirm they pushed `tal`'s `main`, or `git log origin/main -1` and compare.

## Step 5 — restore the local-only files git does NOT carry
A clone gives tracked code only. You still need:

1. **Sibling READ-ONLY upstream repos** (untracked nested repos — required for catalog/report
   tests to pass; `llm-d/` and `llm-d-benchmark/` are READ-ONLY, never edit them):
   ```
   git clone https://github.com/llm-d/llm-d.git           ~/llm-d-benchmarking-agent/llm-d
   git clone https://github.com/llm-d/llm-d-benchmark.git ~/llm-d-benchmarking-agent/llm-d-benchmark
   ```
2. **`.env`** — already hydrated by `claude-sync pull` (step 3). Confirm it exists at
   `~/llm-d-benchmarking-agent/llm-d-benchmarking-agent-project/.env`. If not, run
   `claude-sync sync` again. (It carries real API keys — keep it off any public surface.)
3. **Python env** — this repo uses `uv` (pyproject.toml + uv.lock). Don't hand-build a venv;
   run the suite through uv, which materializes `.venv` from the `[dev]` extra:
   ```
   cd ~/llm-d-benchmarking-agent/llm-d-benchmarking-agent-project
   uv run --extra dev python -m pytest        # ~14s green = environment OK
   ```
   Never invoke bare `python` or bare `pytest` (system python lacks the deps; bare `python`
   isn't even on PATH). Caches (`__pycache__`, `.ruff_cache`, `*.egg-info`) regenerate — ignore.

## Step 6 — sanity check
- `claude-sync status` → all sections "0 differences" (incl. `.env`).
- pytest green (step 5.3).
- Read `~/llm-d-benchmarking-agent/llm-d-benchmarking-agent-project/CLAUDE.md` — that's the real
  project brain (architecture, rules, doc map). The monorepo-root `CLAUDE.md` is just a pointer.

---

## Ongoing discipline (every session, both devices)
- **On arrival:** `claude-sync sync` (pull the other device's latest brain).
- **Before leaving:** `claude-sync sync` (push this session's transcripts/memory up).
- Conflicts (same file changed on both sides) prompt `[l]ocal / [r]emote / [b]oth / [s]kip` with a diff.
- Code moves through git as usual (commit / push / pull) — independent of `claude-sync`.

## claude-sync command reference
| Command | Effect |
|---|---|
| `claude-sync sync` | SAFE two-way merge; prompts on real conflicts (daily driver) |
| `claude-sync status` | Show diffs vs Drive; changes nothing |
| `claude-sync pull` | FORCE Drive → local (first hydration); asks first |
| `claude-sync push` | FORCE local → Drive (mirror); asks first |
| `claude-sync manifest [out]` | Device-independent file inventory |
| `claude-sync compare OTHER.manifest` | only-here / only-there / differ report |

Add `--dry-run` to preview any of them.
