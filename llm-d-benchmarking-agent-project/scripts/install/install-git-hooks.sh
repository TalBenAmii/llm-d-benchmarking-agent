#!/usr/bin/env bash
# Install the main-branch merge gate into the shared git hooks dir (idempotent).
#
# The hooks (`pre-commit` + `pre-merge-commit`) block commits/merges to `main` unless
# `ruff check` + `mypy` + `pytest` pass, plus a dangling-skill-reference check. They live in
# `.git/hooks`, which is NOT version-controlled, so a fresh clone / new machine has no gate
# until this runs. Re-running it just overwrites the two hooks with the current gate.
set -euo pipefail

hooks_dir="$(git rev-parse --git-common-dir)/hooks"
mkdir -p "$hooks_dir"

cat > "$hooks_dir/pre-commit" <<'HOOK'
#!/usr/bin/env bash
# Lint + test gate — blocks commits/merges to `main` unless `ruff check` AND `pytest` pass.
#
# Scope: ONLY the `main` branch. Feature/worktree branches are NOT gated (fast WIP iteration),
# so there is NO need to run a "green baseline" when you branch out — green is verified HERE,
# when the worktree feature lands on `main` (this hook is reused by `pre-merge-commit`).
# Mirrors the project gates `make lint` (= ruff check .), `make typecheck` (= mypy app) and
# `make test` (= pytest tests/).
#
# Local-only (lives in .git/hooks, not version-controlled) — (re)create with scripts/install/install-git-hooks.sh.
# Bypass once with:  git commit --no-verify   /   git merge --no-verify
set -u

branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)
[ "$branch" = "main" ] || exit 0   # only gate main; skip every other branch

root=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
proj="$root/llm-d-benchmarking-agent-project"

# ── ruff lint gate ──────────────────────────────────────────────────────────
ruff="$proj/.venv/bin/ruff"
[ -x "$ruff" ] || ruff=$(command -v ruff 2>/dev/null) || ruff=""
if [ -n "$ruff" ]; then
  if ! out=$(cd "$proj" && "$ruff" check . 2>&1); then
    echo "──────── ruff lint gate (main) ────────" >&2
    echo "$out" >&2
    echo "───────────────────────────────────────" >&2
    echo "Commit/merge to main BLOCKED: fix lint (try 'ruff check --fix .'), or bypass once with --no-verify." >&2
    exit 1
  fi
else
  echo "lint gate: ruff not found (project .venv missing?) — skipping lint (run 'make lint' manually)." >&2
fi

# ── mypy typecheck gate ─────────────────────────────────────────────────────
py="$proj/.venv/bin/python"
[ -x "$py" ] || py=$(command -v python3 2>/dev/null) || py=""
if [ -n "$py" ] && "$py" -c 'import mypy' >/dev/null 2>&1; then
  if ! out=$(cd "$proj" && "$py" -m mypy app 2>&1); then
    echo "──────── mypy typecheck gate (main) ────────" >&2
    echo "$out" | tail -40 >&2
    echo "────────────────────────────────────────────" >&2
    echo "Commit/merge to main BLOCKED: type errors. Fix them, or bypass once with --no-verify." >&2
    exit 1
  fi
  echo "typecheck gate: clean." >&2
else
  echo "typecheck gate: mypy/venv not found — skipping (run 'make typecheck' manually)." >&2
fi

# ── pytest test gate ────────────────────────────────────────────────────────
py="$proj/.venv/bin/python"
[ -x "$py" ] || py=$(command -v python3 2>/dev/null) || py=""
if [ -n "$py" ] && "$py" -c 'import pytest' >/dev/null 2>&1; then
  TIMEOUT=""
  command -v timeout >/dev/null 2>&1 && TIMEOUT="timeout 600"
  # Parallelize with pytest-xdist when present (full suite ~6.5x faster); else serial.
  XDIST=""; "$py" -c 'import xdist' >/dev/null 2>&1 && XDIST="-n auto"
  echo "test gate (main): running pytest tests/ ${XDIST:+$XDIST }…" >&2
  if ! out=$(cd "$proj" && $TIMEOUT "$py" -m pytest tests/ -q $XDIST 2>&1); then
    echo "──────────── pytest gate (main) ────────────" >&2
    echo "$out" | tail -40 >&2
    echo "────────────────────────────────────────────" >&2
    echo "Commit/merge to main BLOCKED: tests are red. Fix them, or bypass once with --no-verify." >&2
    exit 1
  fi
  echo "test gate: green." >&2
else
  echo "test gate: pytest/venv not found — skipping tests (run 'make test' manually)." >&2
fi

# ── dangling-skill-reference gate ───────────────────────────────────────────
# Archived skills must not still be pointed at by the always-loaded "map" files.
# The archive dir IS the denylist: any backticked mention of an archived skill in
# a map file is a stale pointer that will mislead a future session. Local-only,
# best-effort (skips cleanly when the archive dir isn't present on a clone).
archive_dir="$root/.claude/archive/skills"
if [ -d "$archive_dir" ]; then
  maps=("$root/CLAUDE.md" "$proj/CLAUDE.md" "$proj/CONTEXT.md")
  dangling=""
  for sk in "$archive_dir"/*/; do
    [ -d "$sk" ] || continue
    name=$(basename "$sk")
    for m in "${maps[@]}"; do
      [ -f "$m" ] || continue
      grep -qF "\`$name\`" "$m" && dangling="$dangling
  $m  ->  \`$name\`"
    done
  done
  if [ -n "$dangling" ]; then
    echo "──────── dangling-skill-reference gate (main) ────────" >&2
    echo "Map files still point at archived skills:$dangling" >&2
    echo "Remove the pointer (or un-archive the skill), then re-commit; bypass once with --no-verify." >&2
    exit 1
  fi
fi
exit 0
HOOK

cat > "$hooks_dir/pre-merge-commit" <<'HOOK'
#!/usr/bin/env bash
# Ruff/mypy/pytest gate for MERGE commits. Git runs `pre-merge-commit` (not `pre-commit`) when a
# merge creates a commit, so a `--no-ff` merge into main is gated here. Delegates to the
# pre-commit hook (same main-only gate). Bypass: git merge --no-verify.
exec "$(dirname "$0")/pre-commit" "$@"
HOOK

chmod +x "$hooks_dir/pre-commit" "$hooks_dir/pre-merge-commit"
echo "installed main-branch merge gate: $hooks_dir/{pre-commit,pre-merge-commit}"
