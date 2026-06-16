#!/usr/bin/env bash
# PostToolUse(all tools) hook — best-effort capture of tool FAILURES into a per-tool "lessons" file
# at ./context/tool_lessons/<Tool>.md, so the sibling PreToolUse injector (tool_lesson_inject.sh)
# can warn before the same tool runs again. This is the recording half of the self-improving loop.
#
# Fail-open by design: it never blocks a tool and never errors out (a hook that breaks tools is
# worse than no hook). It records ONLY when the harness set `tool_response.is_error` — scanning
# response TEXT for error words is unreliable in a codebase (a successful Read/Grep whose CONTENT
# contains "error:" etc. would be falsely flagged). Entries are de-duped by signature and capped.
# Disable the whole loop: TOOL_LESSONS_OFF=1.
set -u
[ "${TOOL_LESSONS_OFF:-0}" = "1" ] && exit 0
CTX="${CLAUDE_PROJECT_DIR:-$PWD}/context/tool_lessons"
INPUT=$(cat)
HOOK_INPUT="$INPUT" CTX="$CTX" python3 - <<'PY' 2>/dev/null || exit 0
import json, sys, os, re, hashlib, time
ctx = os.environ["CTX"]
try:
    d = json.loads(os.environ.get("HOOK_INPUT", "") or "{}")
except Exception:
    sys.exit(0)
tool = d.get("tool_name") or ""
if not re.match(r'^[A-Za-z0-9_]+$', tool):
    sys.exit(0)
resp = d.get("tool_response")
# Trust ONLY the harness's authoritative failure flag. Scanning response TEXT for error words is
# unreliable in a codebase: a successful Read/Grep whose CONTENT contains "error:" (etc.) would be
# falsely recorded. is_error is set when the tool actually failed.
is_err = isinstance(resp, dict) and resp.get("is_error") is True
if not is_err:
    sys.exit(0)
if isinstance(resp, dict):
    src = resp.get("content") or resp.get("error") or resp.get("stderr") or json.dumps(resp, default=str)
    text = src if isinstance(src, str) else json.dumps(src, default=str)
else:
    text = str(resp)
ti = d.get("tool_input", {}) or {}
inp = ti.get("command") or ti.get("file_path") or json.dumps(ti, default=str)
inp = " ".join(str(inp).split())[:200]
err = " ".join(text.split())[:240]
# argument-independent: dedup on (tool, error) only — the input is shown for context but is NOT
# part of the identity, so the same failure MODE is recorded once per tool regardless of which
# file/command triggered it (matches record_lesson.sh).
sig = hashlib.sha1(("%s|%s" % (tool, err[:80])).encode()).hexdigest()[:10]
os.makedirs(ctx, exist_ok=True)
path = os.path.join(ctx, tool + ".md")
existing = open(path, encoding="utf-8", errors="replace").read() if os.path.exists(path) else ""
if sig in existing:
    sys.exit(0)  # this exact failure mode is already on record
ts = time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime())
entry = "- [%s] %s\n  - input: `%s`\n  - error: %s\n" % (sig, ts, inp, err)
blocks = [b for b in re.split(r'(?=^- \[)', existing, flags=re.M) if b.strip().startswith("- [")]
blocks.append(entry)
blocks = blocks[-8:]  # keep only the most recent 8 failure modes per tool
body = "# %s — past failures (auto-captured; avoid repeating)\n\n" % tool + "".join(blocks)
open(path, "w", encoding="utf-8").write(body)
PY
exit 0
