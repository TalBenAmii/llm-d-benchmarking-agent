# Device bootstrap — move this setup to another machine (manual export/import)

Reproduce a working environment (Claude's "brain" + this project) on a **new device**, using a
**manual export zip** carried over by hand (USB / `scp` / cloud upload). This replaces the old
`claude-sync` (rclone → Google Drive) flow, which has been removed.

## The split you must respect
- **CODE → git/GitHub.** The project is a normal git repo
  (`github.com/TalBenAmii/llm-d-benchmarking-agent`). Clone it; never carry `.git` through a
  zip/Drive copy (it corrupts the repo).
- **Everything git does NOT carry → the export zip:** Claude's brain (global `CLAUDE.md`,
  `settings.json`, statusline, keybindings, `skills/`, and this project's **memory + transcripts**)
  plus the project **`.env`** (real secrets, gitignored).

---

## A. Create the export (on the SOURCE device — the one that's set up)

The export is a single self-contained zip. Build it with:

```bash
bash <(cat <<'EOF'
set -euo pipefail
DATE=$(date +%Y-%m-%d)
OUT="$HOME/claude-export"; STAGE="$OUT/staging"
rm -rf "$STAGE"; mkdir -p "$STAGE/brain" "$STAGE/project"
C="$HOME/.claude"
# --- brain: only the curated, non-secret, non-device-specific set ---
for f in CLAUDE.md settings.json statusline-command.sh keybindings.json; do
  [ -e "$C/$f" ] && cp -a "$C/$f" "$STAGE/brain/"; done
[ -d "$C/skills" ] && cp -a "$C/skills" "$STAGE/brain/skills"
# project memory + transcripts (the path key is username-encoded — see import notes)
PROJ_KEY="-home-$USER-llm-d-benchmarking-agent"
[ -d "$C/projects/$PROJ_KEY" ] && mkdir -p "$STAGE/brain/projects" && \
  cp -a "$C/projects/$PROJ_KEY" "$STAGE/brain/projects/$PROJ_KEY"
# --- project: tracked code (git archive) + the gitignored .env ---
REPO="$HOME/llm-d-benchmarking-agent"
mkdir -p "$STAGE/project/code"
git -C "$REPO" archive --format=tar HEAD | tar -x -C "$STAGE/project/code"
cp -a "$REPO/llm-d-benchmarking-agent-project/.env" "$STAGE/project/.env"
# --- zip it (python's zipfile — no `zip` binary needed) ---
python3 -c "import shutil; shutil.make_archive('$OUT/claude-export-'+'$DATE','zip','$STAGE')"
rm -rf "$STAGE"
echo "Export ready: $OUT/claude-export-$DATE.zip"
du -h "$OUT/claude-export-$DATE.zip"
EOF
)
```

> ⚠️ **The zip is sensitive.** It contains `.env` (real API keys) and chat transcripts (which may
> include secrets printed in past sessions). Keep it private; delete it after import.

**What is intentionally NOT exported** (device-specific or secret-by-policy — set up fresh on the
new device): `~/.claude/.credentials.json` (Claude Code login — you re-login), `sessions/`,
`daemon*`, caches, `history.jsonl`, `plugins/`, the `.venv`, and the three READ-ONLY upstream repos
(`llm-d/`, `llm-d-benchmark/`, `llm-d-skills/` — re-cloned from GitHub).

---

## B. Import on the NEW device

### Step 1 — get the brain in place
Unzip the export somewhere temporary, then copy the brain into `~/.claude`:

```bash
unzip claude-export-<date>.zip -d ~/claude-import
cp -a ~/claude-import/brain/CLAUDE.md ~/claude-import/brain/settings.json \
      ~/claude-import/brain/statusline-command.sh ~/.claude/ 2>/dev/null
[ -e ~/claude-import/brain/keybindings.json ] && cp -a ~/claude-import/brain/keybindings.json ~/.claude/
cp -a ~/claude-import/brain/skills/. ~/.claude/skills/ 2>/dev/null || cp -a ~/claude-import/brain/skills ~/.claude/skills
```

**⚠️ Username/path remap (the one real gotcha).** Claude stores per-project memory + transcripts
under a folder whose name encodes the project's absolute path. The export folder is
`brain/projects/-home-tal-llm-d-benchmarking-agent`. On the new device the key must match **this
device's** home path and the path you clone to in step 2:

- **Same path** (`/home/<you>/llm-d-benchmarking-agent`, same username `tal`): copy as-is →
  `cp -a ~/claude-import/brain/projects/-home-tal-llm-d-benchmarking-agent ~/.claude/projects/`
- **Different username** (e.g. `roots`): rename the `-home-tal-` segment to `-home-roots-` →
  `cp -a ~/claude-import/brain/projects/-home-tal-llm-d-benchmarking-agent ~/.claude/projects/-home-roots-llm-d-benchmarking-agent`

(If you clone the repo to a path other than `~/llm-d-benchmarking-agent`, the key must encode that
full path with `/` → `-`.)

### Step 2 — get the code (git, authoritative)
Clone to the **same relative path** the memory key assumes:

```bash
git clone https://github.com/TalBenAmii/llm-d-benchmarking-agent.git ~/llm-d-benchmarking-agent
cd ~/llm-d-benchmarking-agent
git config core.fileMode false     # cross-device copies strip exec bits; stops false "modified" churn
```

> The zip also carries a snapshot of the tracked code at `project/code/` — use it only if you can't
> reach GitHub; `git clone` is the source of truth.

### Step 3 — restore the local-only files git does NOT carry
1. **`.env`** (real secrets) from the zip:
   ```bash
   cp -a ~/claude-import/project/.env ~/llm-d-benchmarking-agent/llm-d-benchmarking-agent-project/.env
   ```
2. **The three READ-ONLY upstream repos** (untracked nested repos; required for catalog/report tests):
   ```bash
   git clone https://github.com/llm-d/llm-d.git                    ~/llm-d-benchmarking-agent/llm-d
   git clone https://github.com/llm-d/llm-d-benchmark.git          ~/llm-d-benchmarking-agent/llm-d-benchmark
   git clone https://github.com/llm-d-incubation/llm-d-skills.git  ~/llm-d-benchmarking-agent/llm-d-skills
   ```
3. **Python env** (`uv run` builds/syncs `.venv` from `pyproject.toml` on first use; never hand-build it):
   ```bash
   cd ~/llm-d-benchmarking-agent/llm-d-benchmarking-agent-project
   uv run --extra dev python -m pytest        # ~14s green = environment OK
   ```
4. **Git hooks** (local-only, not version-controlled — recreate the three in `.git/hooks/`:
   `pre-commit`, `pre-merge-commit`, `post-commit`; confirm `git config core.hooksPath` is empty).
5. **Claude Code login** — start `claude` and authenticate normally (the export omits credentials).

### Step 4 — sanity check
- `pytest` green (step 3.3).
- Read `~/llm-d-benchmarking-agent/llm-d-benchmarking-agent-project/CLAUDE.md` — the real project
  brain (architecture, rules, doc map). The monorepo-root `CLAUDE.md` is just a pointer.
- `rm -rf ~/claude-import` and delete the zip once verified.

---

## Keeping devices in sync afterward
There is no automatic sync anymore. To move later changes, **re-run the export (section A)** and
re-import the parts that changed. Code always moves through **git** (commit / push / pull),
independent of this export.
