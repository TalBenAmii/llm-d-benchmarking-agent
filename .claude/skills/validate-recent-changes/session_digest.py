#!/usr/bin/env python3
"""session_digest.py — distill recent Claude Code session transcripts into a per-session digest
of what was REQUESTED (user prompts), what was CLAIMED done (assistant summaries), and what was
ACTUALLY TOUCHED (Edit/Write tool calls + git commits), plus a cross-session file-collision table.

Mechanism only — it reports facts from the transcripts. The `validate-recent-changes` skill applies
the judgment (did the claim match the code? is a promise unkept? do two sessions conflict?).

Usage:
  session_digest.py [--hours N] [--max-sessions N] [--branch BR] [--json]
Defaults: last 12h of sessions, capped at 12 most-recent. --branch filters to sessions that ran on
a branch matching BR (substring). --json emits the raw structure instead of the text report.
"""
import argparse, glob, json, os, re, sys
from collections import defaultdict

TRANSCRIPT_DIR = "/home/roots/.claude/projects/-home-roots-llm-d-benchmarking-agent"
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def logical(path):
    """Collapse a worktree-copy / absolute path to a repo-relative key, so the SAME logical file
    edited in different worktrees (or in the main checkout) collides in the cross-session table."""
    p = re.sub(r"^.*/\.claude/worktrees/[^/]+/", "", path)   # drop the worktree prefix
    for anchor in (r"(llm-d-benchmarking-agent-project/.*)$", r"(\.claude/.*)$"):
        m = re.search(anchor, p)
        if m:
            return m.group(1)
    return p


def text_blocks(content):
    if isinstance(content, str):
        return [content]
    out = []
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                out.append(b.get("text", ""))
    return out


def digest(path):
    sid = os.path.basename(path)[:-6]
    prompts, claims, commits = [], [], []
    edits = defaultdict(int)
    branches, cwds, ts = set(), set(), []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("gitBranch"):
                branches.add(d["gitBranch"])
            if d.get("cwd"):
                cwds.add(d["cwd"])
            if d.get("timestamp"):
                ts.append(d["timestamp"])
            m = d.get("message")
            if not isinstance(m, dict):
                continue
            role, content, t = m.get("role"), m.get("content"), d.get("type")
            if t == "user" and role == "user":
                for tx in text_blocks(content):
                    tx = tx.strip()
                    # keep real human prompts; drop tool_results and hook/system injections.
                    if tx and "<system-reminder>" not in tx and not tx.startswith("<") and "tool_use_id" not in tx:
                        if not prompts or prompts[-1] != tx:   # dedup repeats (resumes re-emit the prompt)
                            prompts.append(tx)
            elif t == "assistant" and role == "assistant":
                txt = [x for x in text_blocks(content) if x.strip()]
                if txt:
                    claims.append(" ".join(txt))
                if isinstance(content, list):
                    for b in content:
                        if not isinstance(b, dict) or b.get("type") != "tool_use":
                            continue
                        name, inp = b.get("name"), (b.get("input") or {})
                        if name in EDIT_TOOLS and inp.get("file_path"):
                            edits[inp["file_path"]] += 1
                        elif name == "Bash" and re.search(r"\bgit\s+commit\b", inp.get("command", "")):
                            commits.append(inp["command"].strip().splitlines()[0][:200])
    return dict(sid=sid, prompts=prompts, claims=claims, edits=dict(edits), commits=commits,
                branches=sorted(branches), cwds=sorted(cwds),
                start=min(ts) if ts else "", end=max(ts) if ts else "", mtime=os.path.getmtime(path))


def trunc(s, n):
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=12)
    ap.add_argument("--max-sessions", type=int, default=12)
    ap.add_argument("--branch", default="")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    files = sorted(glob.glob(os.path.join(TRANSCRIPT_DIR, "*.jsonl")), key=os.path.getmtime, reverse=True)
    if not files:
        print(f"no transcripts under {TRANSCRIPT_DIR}", file=sys.stderr)
        return 1
    newest = os.path.getmtime(files[0])
    cutoff = newest - a.hours * 3600  # window relative to the most-recent session (clock-free)

    sessions = []
    for f in files:
        if os.path.getmtime(f) < cutoff:
            break
        dg = digest(f)
        if a.branch and not any(a.branch in b for b in dg["branches"]):
            continue
        if not (dg["prompts"] or dg["edits"]):  # skip empty/idle transcripts
            continue
        sessions.append(dg)
        if len(sessions) >= a.max_sessions:
            break

    if a.json:
        print(json.dumps(sessions, indent=2))
        return 0

    if not sessions:
        print("No sessions with activity in the window. Widen with --hours.")
        return 0

    print(f"# Recent session digest — {len(sessions)} session(s), window ≈ last {a.hours:g}h\n")
    collide = defaultdict(list)
    for s in sessions:
        br = ", ".join(s["branches"]) or "(unknown)"
        print(f"## {s['sid']}\n  branch: {br}   {s['start']} … {s['end']}")
        if s["prompts"]:
            print("  requested:")
            for p in s["prompts"][:3]:
                print(f"    - {trunc(p, 200)}")
        if s["claims"]:
            print("  claimed (final summary):")
            print(f"    - {trunc(s['claims'][-1], 400)}")
        if s["edits"]:
            print("  files touched (edit count):")
            for fp, n in sorted(s["edits"].items(), key=lambda kv: -kv[1]):
                print(f"    - {fp} ({n})")
                collide[logical(fp)].append((s["sid"][:8], br))
        if s["commits"]:
            print("  commits:")
            for c in s["commits"][:5]:
                print(f"    - {trunc(c, 160)}")
        print()

    multi = {fp: ss for fp, ss in collide.items() if len({x[0] for x in ss}) > 1}
    print("## Cross-session file collisions (same file edited by >1 session — verify intents merged)")
    if not multi:
        print("  none — sessions edited disjoint files.")
    else:
        for fp, ss in sorted(multi.items()):
            who = ", ".join(f"{sid}[{br}]" for sid, br in dict.fromkeys(ss))
            print(f"  ⚠ {fp}\n      {who}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
