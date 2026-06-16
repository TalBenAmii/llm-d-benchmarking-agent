#!/usr/bin/env bash
# Shared recorder for the self-improving tool-lessons loop. Appends ONE failure lesson for
# <LTOOL> into ${CLAUDE_PROJECT_DIR}/context/tool_lessons/<LTOOL>.md, where the sibling
# PreToolUse injector (tool_lesson_inject.sh) surfaces it before that tool runs again.
#
# Why this exists: in some harnesses PostToolUse does NOT deliver tool_response.is_error on a
# failed tool, so the PostToolUse-only capture path (tool_error_capture.sh) silently records
# nothing. PreToolUse *deny* hooks (worktree_gate, git_add_guard) reliably run, so they record
# their own block here — the failures that matter most are captured at the source.
#
# De-dup key is (tool, error) — the ARGUMENT (file/command) is shown for context but is NOT part
# of the key, so the same failure MODE is recorded once per tool regardless of which file or
# command triggered it. Fail-open: never blocks, never errors. Disable: TOOL_LESSONS_OFF=1.
# Inputs via env: LTOOL (required), LINPUT (offending arg, shown only), LERROR (message, required).
set -u
[ "${TOOL_LESSONS_OFF:-0}" = "1" ] && exit 0
[ -n "${LTOOL:-}" ] || exit 0
CTX="${CLAUDE_PROJECT_DIR:-$PWD}/context/tool_lessons" \
LTOOL="$LTOOL" LINPUT="${LINPUT:-}" LERROR="${LERROR:-}" python3 - <<'PY' 2>/dev/null || exit 0
import os, re, hashlib, time
ctx = os.environ["CTX"]; tool = os.environ["LTOOL"]
if not re.match(r'^[A-Za-z0-9_]+$', tool):
    raise SystemExit(0)
inp = " ".join(str(os.environ.get("LINPUT", "")).split())[:200]
err = " ".join(str(os.environ.get("LERROR", "")).split())[:240]
if not err:
    raise SystemExit(0)
# argument-independent: dedup on (tool, error) only — the input is context, not identity.
sig = hashlib.sha1(("%s|%s" % (tool, err[:80])).encode()).hexdigest()[:10]
os.makedirs(ctx, exist_ok=True)
path = os.path.join(ctx, tool + ".md")
existing = open(path, encoding="utf-8", errors="replace").read() if os.path.exists(path) else ""
if sig in existing:
    raise SystemExit(0)
ts = time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime())
entry = "- [%s] %s\n  - input: `%s`\n  - error: %s\n" % (sig, ts, inp, err)
blocks = [b for b in re.split(r'(?=^- \[)', existing, flags=re.M) if b.strip().startswith("- [")]
blocks.append(entry)
blocks = blocks[-8:]  # keep only the most recent 8 failure modes per tool
open(path, "w", encoding="utf-8").write("# %s — past failures (auto-captured; avoid repeating)\n\n" % tool + "".join(blocks))
PY
exit 0
