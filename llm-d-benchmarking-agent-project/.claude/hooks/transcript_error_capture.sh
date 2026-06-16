#!/usr/bin/env bash
# Stop hook — capture EVERY tool failure this session into ./context/tool_lessons/<Tool>.md,
# so the PreToolUse injector (tool_lesson_inject.sh) can warn before the same tool runs again.
#
# WHY this exists alongside the PostToolUse tool_error_capture.sh: in THIS harness, PostToolUse
# does NOT fire for a FAILED tool call (verified — a non-zero Bash exit produces no PostToolUse
# event at all, and is_error is never delivered), so the PostToolUse capture never actually
# recorded a failure. The transcript IS the authoritative record: every failure is a tool_result
# with is_error:true. We scan it once per turn (on Stop), map each errored result back to its
# tool_use for the tool name + input, and append a deduped lesson. Idempotent: re-scanning the
# whole transcript each turn is safe because entries dedup by (tool, error) signature.
#
# Fail-open by design: never blocks, never errors out. Disable the loop: TOOL_LESSONS_OFF=1.
set -u
[ "${TOOL_LESSONS_OFF:-0}" = "1" ] && exit 0
CTX="${CLAUDE_PROJECT_DIR:-$PWD}/context/tool_lessons"
INPUT=$(cat)
HOOK_INPUT="$INPUT" CTX="$CTX" python3 - <<'PY' 2>/dev/null || exit 0
import json, os, sys, re, hashlib, time
ctx = os.environ["CTX"]
try:
    d = json.loads(os.environ.get("HOOK_INPUT", "") or "{}")
except Exception:
    sys.exit(0)
tpath = d.get("transcript_path") or ""
if not tpath or not os.path.exists(tpath):
    sys.exit(0)

# Pass 1: index every tool_use by id -> (tool_name, input). Pass 2 (same loop): collect every
# tool_result flagged is_error. tool_use always precedes its result in the transcript, so a single
# forward pass resolves names for all errors.
uses = {}
errors = []  # list of (tool_use_id, error_text)
try:
    fh = open(tpath, encoding="utf-8", errors="replace")
except Exception:
    sys.exit(0)
for line in fh:
    line = line.strip()
    if not line:
        continue
    try:
        r = json.loads(line)
    except Exception:
        continue
    content = (r.get("message", {}) or {}).get("content")
    if not isinstance(content, list):
        continue
    for b in content:
        if not isinstance(b, dict):
            continue
        bt = b.get("type")
        if bt == "tool_use":
            uses[b.get("id")] = (b.get("name") or "", b.get("input") or {})
        elif bt == "tool_result" and b.get("is_error"):
            txt = b.get("content")
            if isinstance(txt, list):
                txt = " ".join(x.get("text", "") for x in txt if isinstance(x, dict))
            errors.append((b.get("tool_use_id"), str(txt)))
if not errors:
    sys.exit(0)
os.makedirs(ctx, exist_ok=True)

def input_repr(ti):
    if not isinstance(ti, dict):
        return " ".join(str(ti).split())[:200]
    v = ti.get("command") or ti.get("file_path") or ti.get("path") or json.dumps(ti, default=str)
    return " ".join(str(v).split())[:200]

ts = time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime())
for tuid, errtext in errors:
    name, ti = uses.get(tuid, ("", {}))
    if not re.match(r'^[A-Za-z0-9_]+$', name):
        continue  # can't attribute to a tool file — skip
    err = " ".join(errtext.split())
    # Skip USER-DRIVEN rejections — declining a tool is a deliberate choice, not a mistake to
    # learn from. Recording it would only make future-me hesitant to offer tools.
    if err.startswith("The user doesn't want to proceed") or "tool use was rejected" in err:
        continue
    err = err[:240]
    # argument-independent dedup on (tool, error[:80]) — same failure MODE recorded once per tool,
    # matching tool_error_capture.sh / record_lesson.sh so the two layers never double-record.
    sig = hashlib.sha1(("%s|%s" % (name, err[:80])).encode()).hexdigest()[:10]
    path = os.path.join(ctx, name + ".md")
    existing = open(path, encoding="utf-8", errors="replace").read() if os.path.exists(path) else ""
    if sig in existing:
        continue
    entry = "- [%s] %s\n  - input: `%s`\n  - error: %s\n" % (sig, ts, input_repr(ti), err)
    blocks = [b for b in re.split(r'(?=^- \[)', existing, flags=re.M) if b.strip().startswith("- [")]
    blocks.append(entry)
    blocks = blocks[-8:]  # keep only the most recent 8 failure modes per tool
    header = "# %s — past failures (auto-captured; avoid repeating)\n\n" % name
    open(path, "w", encoding="utf-8").write(header + "".join(blocks))
PY
exit 0
