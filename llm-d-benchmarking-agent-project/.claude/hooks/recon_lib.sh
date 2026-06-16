#!/usr/bin/env bash
# recon_lib.sh — reconciliation primitives shared by the auto-merge Stop hook and the
# `reconcile-before-merge` skill. Deterministic git analysis only; NO judgment (that's the
# agent's job). Subcommands:
#
#   recon_lib.sh detect    <WT_ROOT> <TARGET> <BRANCH>
#       Print a human-readable report of concurrent-session activity that should be
#       reconciled BEFORE merging <BRANCH> into <TARGET>. Empty output ⇒ nothing to
#       reconcile (safe to merge directly). Signals reported:
#         (a) <TARGET> advanced since this branch forked (other sessions committed),
#             with the subset of files BOTH sides touched (semantic-breakage risk),
#         (b) a trial merge into <TARGET> would CONFLICT (textual),
#         (c) other still-unmerged sibling worktree branches edit files this branch edits.
#
#   recon_lib.sh signature <WT_ROOT> <TARGET> <BRANCH>
#       Print a stable signature of the *current* concurrent state (<TARGET> tip + the tips
#       of every other managed worktree branch). The marker written by `mark` stores this;
#       the hook re-merges only while the signature is unchanged, so if another session
#       commits AFTER you reconcile, reconciliation is re-triggered automatically.
#
#   recon_lib.sh mark      <WT_ROOT> <TARGET> <BRANCH>
#       Record that reconciliation is complete: writes the current signature to
#       <WT_ROOT>/.reconciled. Run this from the skill AFTER you've resolved everything and
#       committed on <BRANCH>. (Left untracked; the worktree is removed on merge anyway.)
set -u

SUB="${1:-}"; WT_ROOT="${2:-}"; TARGET="${3:-}"; BRANCH="${4:-}"
[ -n "$SUB" ] && [ -n "$WT_ROOT" ] && [ -n "$TARGET" ] && [ -n "$BRANCH" ] || {
  echo "usage: recon_lib.sh {detect|signature|mark} <WT_ROOT> <TARGET> <BRANCH>" >&2; exit 2; }

SUB="$SUB" WT_ROOT="$WT_ROOT" TARGET="$TARGET" BRANCH="$BRANCH" python3 - <<'PY'
import os, sys, subprocess, hashlib

sub    = os.environ["SUB"]
wt     = os.environ["WT_ROOT"]
target = os.environ["TARGET"]
branch = os.environ["BRANCH"]

def git(root, *a):
    return subprocess.run(["git", "-C", root, *a],
                          capture_output=True, text=True).stdout.strip()

def main_root(any_wt):
    # The primary checkout is the first entry of `git worktree list`.
    for line in git(any_wt, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            return line[len("worktree "):]
    return any_wt

MAIN = main_root(wt)

def pending_siblings(exclude):
    """Other branches checked out in managed worktrees (i.e. other live sessions)."""
    res, cur = [], None
    for line in git(MAIN, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            cur = line[len("worktree "):]
        elif line.startswith("branch "):
            br = line[len("branch "):].replace("refs/heads/", "")
            if cur and "/.claude/worktrees/" in cur and br != exclude:
                res.append(br)
    return res

def changed(root, a, b):
    return set(f for f in git(root, "diff", "--name-only", f"{a}...{b}").splitlines() if f)

def signature():
    parts = [git(MAIN, "rev-parse", target)]
    parts += sorted(f"{br}:{git(MAIN, 'rev-parse', br)}" for br in pending_siblings(branch))
    return hashlib.sha1("\n".join(parts).encode()).hexdigest()

def detect():
    lines = []
    ours = changed(MAIN, target, branch)

    # (a) target advanced since fork AND both sides touched the same files. A bare advance with
    # no shared files is the safe disjoint case (worktree isolation working as intended) — it is
    # deliberately NOT reported, so it merges straight through. Hidden semantic breakage from a
    # disjoint advance is left to the optional AUTO_MERGE_TEST_CMD gate.
    base   = git(MAIN, "merge-base", target, branch)
    behind = int(git(MAIN, "rev-list", "--count", f"{branch}..{target}") or 0)
    if behind > 0:
        tgt_changed = set(f for f in git(MAIN, "diff", "--name-only", f"{base}..{target}").splitlines() if f)
        shared = sorted(ours & tgt_changed)
        if shared:
            lines.append(f"• `{target}` advanced by {behind} commit(s) from concurrent session(s), and your "
                         f"branch and that concurrent work changed the SAME files (review for semantic "
                         f"breakage even if git merges cleanly):")
            lines += [f"    - {f}" for f in shared]

    # (b) trial merge — textual conflicts? merge-tree returns 1 on conflict, 0 on clean. But a
    # returncode of 1 ALSO covers errors like a missing ref (e.g. a sibling branch that a
    # concurrent session just deleted) — those put a message on stderr and leave stdout empty.
    # A genuine conflict always writes the merged-tree OID + conflict detail to stdout, so we
    # require non-empty stdout to treat it as a conflict; errors are ignored.
    mt = subprocess.run(["git", "-C", MAIN, "merge-tree", "--write-tree", target, branch],
                        capture_output=True, text=True)
    if mt.returncode == 1 and mt.stdout.strip():
        lines.append(f"• A trial merge into `{target}` reports textual CONFLICTS — manual resolution required:")
        # First stdout line is the merged tree's OID; the rest is the conflict detail.
        for l in mt.stdout.splitlines()[1:41]:
            lines.append("    " + l)

    # (c) other unmerged sibling worktree branches that overlap our files.
    for br in pending_siblings(branch):
        shared = sorted(ours & changed(MAIN, target, br))
        if shared:
            lines.append(f"• Unmerged sibling worktree branch `{br}` (another live session) also edits files you changed:")
            lines += [f"    - {f}" for f in shared]

    return "\n".join(lines)

if sub == "signature":
    print(signature())
elif sub == "detect":
    out = detect()
    if out:
        print(out)
elif sub == "mark":
    with open(os.path.join(wt, ".reconciled"), "w") as fh:
        fh.write(signature() + "\n")
    print(f"recorded reconciliation signature in {os.path.join(wt, '.reconciled')}")
else:
    print(f"unknown subcommand: {sub}", file=sys.stderr)
    sys.exit(2)
PY
