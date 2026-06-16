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
out = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": msg[:1200]}}
print(json.dumps(out))
PY
exit 0
