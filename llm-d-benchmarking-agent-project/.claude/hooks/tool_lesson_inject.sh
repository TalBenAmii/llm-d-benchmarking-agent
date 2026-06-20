#!/usr/bin/env bash
# PreToolUse(all tools) hook — the injecting half of the self-improving loop. If past failures for
# THIS tool were recorded by tool_error_capture.sh, surface them as additionalContext so the model
# avoids repeating the mistake before the tool runs. Read-only: it NEVER blocks (always exit 0) and
# caps the injected text small. Disable the whole loop: TOOL_LESSONS_OFF=1.
set -u
[ "${TOOL_LESSONS_OFF:-0}" = "1" ] && exit 0
CTX="${CLAUDE_PROJECT_DIR:-$PWD}/context/tool_lessons"
INPUT=$(cat)
HOOK_INPUT="$INPUT" CTX="$CTX" python3 - <<'PY' 2>/dev/null || exit 0
import json, sys, os, re
ctx = os.environ["CTX"]
try:
    d = json.loads(os.environ.get("HOOK_INPUT", "") or "{}")
except Exception:
    sys.exit(0)
tool = d.get("tool_name") or ""
if not re.match(r'^[A-Za-z0-9_]+$', tool):
    sys.exit(0)
path = os.path.join(ctx, tool + ".md")
if not os.path.exists(path):
    sys.exit(0)
content = open(path, encoding="utf-8", errors="replace").read()
blocks = [b.strip() for b in re.split(r'(?=^- \[)', content, flags=re.M) if b.strip().startswith("- [")]
if not blocks:
    sys.exit(0)
msg = "Past %s failures in this project (avoid repeating these mistakes):\n%s" % (tool, "\n".join(blocks[-6:]))
msg = msg[:1200]
# Maintenance nudge (self-triggering): when raw auto-captured errors pile up across ALL tools,
# remind the user to have them DEBLOATED & GENERALIZED into guardrails — i.e. turn raw error text
# into solution-bearing lessons, merge duplicates, drop stale ones. Tied to the actual condition
# (volume of accumulated lessons), not an arbitrary clock.
import glob
total = 0
for f in glob.glob(os.path.join(ctx, "*.md")):
    try:
        # Count only RAW auto-captured signatures (hex-hash ids like `- [829d44c65e]`),
        # NOT curated `- [named-slug]` guardrails — once raw errors are generalized into
        # named lessons (or retired into the dedup comment) they should stop tripping this.
        total += len(re.findall(r'^- \[[0-9a-f]{6,}\]', open(f, encoding="utf-8", errors="replace").read(), flags=re.M))
    except Exception:
        pass
if total >= 12:
    msg += ("\n\n[maintenance] %d raw tool-error lessons have accumulated across tools. Surface this to "
            "the user: suggest they ask you to DEBLOAT & GENERALIZE the captured errors into guardrails "
            "(capture the fix/solution, merge duplicates, drop stale ones)." % total)
out = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": msg}}
print(json.dumps(out))
PY
exit 0
