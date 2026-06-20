#!/usr/bin/env python3
"""Trace where context tokens go in a Claude Code session transcript.

Usage:
    python3 context/tools/trace_tokens.py [session.jsonl]

With no arg it picks the NEWEST transcript under the project's Claude dir.
Prints: per-turn context growth curve, cache hit/miss ratio, and a breakdown
of injected (non-typed) content — hook blocks, system-reminders, tool output —
that silently rides along in context.
"""
import json, sys, glob, os, re

PROJ_DIR = os.path.expanduser(
    "~/.claude/projects/-home-tal-llm-d-benchmarking-agent")

def newest_transcript():
    files = glob.glob(os.path.join(PROJ_DIR, "*.jsonl"))
    return max(files, key=os.path.getmtime) if files else None

def tok(chars):           # ~4 chars/token heuristic
    return chars // 4

# Markers for content the harness INJECTS (you never typed it).
INJECT_MARKERS = [
    ("HOOK: tool-error lessons", "hook additional context"),
    ("HOOK: maintenance nag",    "[maintenance]"),
    ("INJECT: system-reminder",  "<system-reminder>"),
]

def main():
    path = sys.argv[1] if len(sys.argv) > 1 else newest_transcript()
    if not path or not os.path.exists(path):
        sys.exit(f"no transcript found (looked in {PROJ_DIR})")
    print(f"transcript: {os.path.basename(path)}\n")

    raw = open(path).read().splitlines()

    # --- 1. Authoritative per-turn usage, deduped by message id ---
    seen, turns = set(), []
    for line in raw:
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        if o.get("type") != "assistant":
            continue
        msg = o.get("message", {}) or {}
        mid = msg.get("id")
        u = msg.get("usage", {}) or {}
        if not u or mid in seen:
            continue
        seen.add(mid)
        turns.append(u)

    print("Context growth — resident tokens re-sent each turn (deduped):")
    print(f'{"turn":>4} {"resident":>9} {"cacheR":>8} {"cacheW":>7} {"fresh":>6} {"out":>6}')
    print("-" * 46)
    tcr = tcw = tin = to = 0
    for i, u in enumerate(turns, 1):
        cr = u.get("cache_read_input_tokens", 0)
        cw = u.get("cache_creation_input_tokens", 0)
        inp = u.get("input_tokens", 0)
        out = u.get("output_tokens", 0)
        resident = cr + cw + inp
        tcr += cr; tcw += cw; tin += inp; to += out
        print(f"{i:4d} {resident:9d} {cr:8d} {cw:7d} {inp:6d} {out:6d}")
    print("-" * 46)

    billed_in = tcr + tcw + tin
    print(f"\nUNIQUE turns: {len(turns)}")
    if turns:
        last = turns[-1]
        peak = last.get("cache_read_input_tokens", 0) + \
            last.get("cache_creation_input_tokens", 0) + \
            last.get("input_tokens", 0)
        print(f"Peak resident context (last turn): {peak:,} tokens")
    print(f"Total input billed: {billed_in:,}")
    print(f"  cache READ  (0.1x, cheap): {tcr:,}  ({100*tcr/max(1,billed_in):.0f}%)")
    print(f"  cache WRITE (1.25x):       {tcw:,}  ({100*tcw/max(1,billed_in):.0f}%)")
    print(f"  fresh       (1.0x):        {tin:,}  ({100*tin/max(1,billed_in):.0f}%)")
    print(f"Total output: {to:,}")

    # --- 2. Injected (non-typed) content footprint, counted ONCE ---
    print("\nInjected content (harness-added, measured once in the log):")
    counts = {name: [0, 0] for name, _ in INJECT_MARKERS}
    for line in raw:
        for name, marker in INJECT_MARKERS:
            n = line.count(marker)
            if n:
                counts[name][0] += n
                counts[name][1] += len(line)  # whole record as upper bound
                break
    print(f'{"source":28} {"hits":>5} {"~tokens(once)":>14}')
    print("-" * 50)
    for name, (cnt, ch) in sorted(counts.items(), key=lambda kv: -kv[1][1]):
        print(f"{name:28} {cnt:5d} {tok(ch):14d}")
    print("\nNote: injected blocks are stored ONCE here but RE-SENT every turn")
    print("they stay resident — multiply by remaining turns for true cost.")

if __name__ == "__main__":
    main()
