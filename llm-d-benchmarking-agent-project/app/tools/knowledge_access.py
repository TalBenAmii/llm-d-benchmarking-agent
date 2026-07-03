"""Read-only knowledge & repo-doc access tools.

Read a file from inside one of the read-only repos, fetch the LIVE content of the
authoritative repo docs pinned in knowledge/key_docs.yaml, read one knowledge guide by
basename, and lexically search the knowledge corpus + the curated upstream-doc index. None of
these mutate anything, so the agent loop runs them automatically (no approval).

Split out of app/tools/probe.py (which had grown into a ~1,100-line module spanning three
unrelated tool families) so the doc/knowledge surface is independently navigable. probe.py
re-exports these names for backwards compatibility; new code should import them from here.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

from app.agent.tool_result_budget import DEFAULT_TOOL_RESULT_BUDGET
from app.paths import is_within
from app.tools.context import ToolContext, ToolError

# --- per-session doc de-duplication (context budget) ----------------------------------------
# A repeated fetch of the SAME doc within a session re-injects its full text into the replayed
# transcript on every subsequent turn — pure waste, since the content is identical. The first
# fetch returns full content AND records the doc's identity on ``ctx.fetched_docs``; an EXACT
# repeat is short-circuited to a tiny back-reference so the agent knows the content was already
# provided and where to find it. Only EXACT repeats are short-circuited; a different doc (or the
# first fetch in a resumed process) always returns full content. Mechanism only — no judgment.

# A back-reference is itself bounded; do not bother de-duplicating trivially small docs (the
# saving would be smaller than the reference, and tiny docs are cheap to re-send verbatim).
_DEDUP_OVER_CHARS = 600


def _doc_seen(ctx: ToolContext, key: str) -> bool:
    """True if ``key`` was already fetched this session (and recorded). First call records it
    and returns False; later calls return True. ``ctx.fetched_docs`` is the session-scoped set."""
    if key in ctx.fetched_docs:
        return True
    ctx.fetched_docs.add(key)
    return False


def _already_provided(kind: str, identity: str, *, reload_hint: str) -> dict[str, Any]:
    """The tiny back-reference returned for an EXACT repeat fetch — it clearly tells the agent the
    content was already provided earlier this session, so it is never surprised by the omission."""
    return {
        "already_provided": True,
        kind: identity,
        "note": (f"already provided the {kind} '{identity}' earlier this session — its full "
                 "content is in the conversation above; not re-sending it to save context. "
                 f"{reload_hint}"),
    }


def _read_repo_doc_raw(ctx: ToolContext, *, path: str, max_bytes: int = 40_000) -> dict[str, Any]:
    """Resolve + read a repo doc, returning the full {path, content, truncated} payload. The
    UNDEDUPED core: callers that need the real content every time (e.g. fetch_key_docs, which
    runs its own per-doc dedup) use this directly; the deduping read_repo_doc tool wraps it."""
    repos = ctx.settings.repo_paths
    candidate = Path(path)
    resolved: Path | None = None
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        # Try "<repo-name>/<rel>" first, then each repo root.
        first = candidate.parts[0] if candidate.parts else ""
        if first in repos:
            resolved = (repos[first].parent / candidate).resolve()
        else:
            for root in repos.values():
                trial = (root / candidate).resolve()
                if trial.exists():
                    resolved = trial
                    break
    if resolved is None:
        raise ToolError(f"could not resolve repo path {path!r}")

    roots = [r.resolve() for r in repos.values()]
    if not any(is_within(resolved, root) for root in roots):
        raise ToolError(f"path {path!r} resolves outside the read-only repos — refused")
    if not resolved.is_file():
        raise ToolError(f"not a file: {resolved}")

    data = resolved.read_text(errors="replace")
    truncated = len(data.encode()) > max_bytes
    return {
        "path": str(resolved),
        "content": data[:max_bytes],
        "truncated": truncated,
    }


def read_repo_doc(ctx: ToolContext, *, path: str, max_bytes: int = 40_000) -> dict[str, Any]:
    """Read a file from inside one of the three read-only repos (incl. the llm-d-skills
    procedure library). Path traversal is blocked.

    Per-session de-dup: the FIRST read of a given resolved doc returns its full content; an EXACT
    repeat within the session returns a tiny back-reference instead of re-injecting identical text
    (the agent is told the content is already in the conversation above)."""
    doc = _read_repo_doc_raw(ctx, path=path, max_bytes=max_bytes)
    # Key on the RESOLVED path so different spellings of the same file de-dup together. Only
    # de-dup non-trivial docs that aren't already truncated mid-content (a truncated read may be
    # re-fetched with a larger max_bytes for the rest).
    resolved = doc["path"]
    dedup_eligible = len(doc["content"]) >= _DEDUP_OVER_CHARS and not doc["truncated"]
    if dedup_eligible and _doc_seen(ctx, f"repo_doc:{resolved}"):
        return _already_provided(
            "doc", resolved,
            reload_hint="Pass a larger max_bytes or a more specific path only if you need a "
                        "different portion.",
        )
    return doc


def fetch_key_docs(
    ctx: ToolContext,
    *,
    task: str | None = None,
    max_bytes_each: int = 20_000,
) -> dict[str, Any]:
    """Fetch the LIVE content of the authoritative repo docs pinned in
    knowledge/key_docs.yaml (optionally filtered to one task, e.g. 'quickstart').

    The *list* of docs is hard-coded (in key_docs.yaml); the *content* is read live from
    the cloned repos, so it is never a stale vendored copy. Read-only. Call this before
    proposing a deployment plan so the flow/flags come from the real procedure."""
    kfile = ctx.settings.knowledge_dir / "key_docs.yaml"
    if not kfile.is_file():
        return {"docs": [], "note": f"key_docs.yaml not found at {kfile}"}
    try:
        spec = yaml.safe_load(kfile.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ToolError(f"key_docs.yaml is not valid YAML: {exc}") from exc

    entries = spec.get("docs", []) if isinstance(spec, dict) else []
    if task:
        entries = [e for e in entries if e.get("task") == task]

    fetched: list[dict[str, Any]] = []
    for entry in entries:
        rel = entry.get("path", "")
        item: dict[str, Any] = {"path": rel, "task": entry.get("task"), "why": entry.get("why")}
        try:
            # Use the UNDEDUPED core so this tool controls dedup per-doc itself (the dedup wrapper
            # would return a content-less back-reference that breaks the item shape below).
            doc = _read_repo_doc_raw(ctx, path=rel, max_bytes=max_bytes_each)
            resolved = doc["path"]
            dedup_eligible = len(doc["content"]) >= _DEDUP_OVER_CHARS and not doc["truncated"]
            if dedup_eligible and _doc_seen(ctx, f"repo_doc:{resolved}"):
                # Already sent this doc's full text earlier this session — keep the metadata but
                # omit the body so the same doc isn't re-injected every later turn.
                item.update(found=True, resolved=resolved, already_provided=True,
                            note="already provided earlier this session — see the previous fetch "
                                 "of this doc above; body omitted to save context.")
            else:
                item.update(found=True, resolved=resolved, content=doc["content"],
                            truncated=doc["truncated"])
        except ToolError as exc:
            item.update(found=False, reason=str(exc))
        fetched.append(item)

    tasks: set[str] = {
        str(e["task"])
        for e in spec.get("docs", [])
        if isinstance(e, dict) and e.get("task")
    }
    available = sorted(tasks)
    return {
        "task": task,
        "available_tasks": available,
        "docs": fetched,
        "found_count": sum(1 for d in fetched if d.get("found")),
    }


# Files that physically live in knowledge/ but are NOT agent knowledge — they are
# editor-facing meta docs (e.g. CLAUDE.md guidance for whoever edits the knowledge files).
# They must never be inlined into the system prompt, indexed on-demand, or returned by
# read_knowledge, or they leak into the runtime agent's brain. app/agent/prompt.py imports
# and applies this same set when building the prompt.
EXCLUDED_KNOWLEDGE_FILES = frozenset({"CLAUDE.md", "README.md"})


def _knowledge_files(ctx: ToolContext) -> list[Path]:
    """Every knowledge file (basename order), or empty if the dir is missing. Editor-facing
    meta docs (EXCLUDED_KNOWLEDGE_FILES) are dropped — they are not agent knowledge."""
    kdir = ctx.settings.knowledge_dir
    if not kdir.is_dir():
        return []
    files = list(kdir.glob("*.md")) + list(kdir.glob("*.yaml")) + list(kdir.glob("*.yml"))
    files = [f for f in files if f.name not in EXCLUDED_KNOWLEDGE_FILES]
    return sorted(files, key=lambda p: p.name)


# --- markdown section addressing (truncation UX) -------------------------------------------
# A large knowledge guide overflows the loop's per-tool-result feed-back budget and is clamped
# (app/agent/tool_result_budget.py) to a leading PREVIEW before the MODEL sees it — so the LATER
# sections silently vanish and the model never learns they exist. Two mechanisms fix that, both
# pure: (1) when a guide won't fit the budget, read_knowledge annotates its result with the ##
# headings that fall PAST the clamp's cut (a short signal string the clamp preserves intact) plus
# a note, so the model knows what it is missing; (2) a `section` arg returns just one named section
# verbatim, so the model can re-fetch a dropped section. Crucially the FULL content stays in the
# returned dict — only the model-facing clamped COPY is bounded — so the UI/persistence and callers
# still get the whole guide. No judgment here — WHICH section to read is the model's call.
_ATX_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(\S.*?)[ \t]*#*[ \t]*$")
# Headings past this raw-content offset are reported as dropped. The clamp's preview shows roughly
# the leading ``budget`` chars of the SERIALIZED result minus its envelope overhead; this reserve
# is deliberately generous so the cut estimate is conservative (better to flag a borderline section
# as dropped than to let one vanish unmentioned).
_DROPPED_CUT_RESERVE = 1500
# Keep the dropped-heading list a short signal scalar so the loop's clamp preserves it verbatim
# (a longer string would be treated as bulk payload and clipped away exactly when it is needed).
_DROPPED_LIST_MAX_CHARS = 450


def _markdown_headings(text: str) -> list[tuple[int, int, str]]:
    """(level, char-offset, heading-text) for every ATX markdown heading in ``text``, in order.
    Pure mechanism — the outline used to address a section and to name the dropped ones."""
    out: list[tuple[int, int, str]] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        m = _ATX_HEADING_RE.match(line.rstrip("\n"))
        if m:
            out.append((len(m.group(1)), offset, m.group(2).strip()))
        offset += len(line)
    return out


def _extract_section(text: str, wanted: str) -> tuple[str, str] | None:
    """Return (heading-text, section-body) for the ATX heading whose text matches ``wanted``
    (case-insensitive; a leading '#'/'##' in the request is ignored). The body runs from the
    matched heading up to the next heading of the SAME-or-shallower level (or EOF), so a '##'
    section carries its own '###' subsections. None when no heading matches."""
    headings = _markdown_headings(text)
    target = wanted.lstrip("#").strip().lower()
    for i, (level, start, htext) in enumerate(headings):
        if htext.lower() == target:
            end = len(text)
            for level2, start2, _h2 in headings[i + 1:]:
                if level2 <= level:
                    end = start2
                    break
            return htext, text[start:end].strip()
    return None


def _join_dropped(headings: list[str]) -> str:
    """Join dropped-heading names into ONE short signal string (capped so the loop's clamp keeps
    it intact). If too many to fit, keep the first that fit and count the remainder."""
    out: list[str] = []
    used = 0
    for i, h in enumerate(headings):
        add = (3 if out else 0) + len(h)  # ' · ' separator + heading
        if used + add > _DROPPED_LIST_MAX_CHARS and out:
            return " · ".join(out) + f" · …(+{len(headings) - i} more)"
        out.append(h)
        used += add
    return " · ".join(out)


def _annotate_budget_overflow(result: dict[str, Any], content: str, budget: int) -> None:
    """The guide's serialized result exceeds the feed-back budget, so the loop's clamp will show
    the model only a leading preview. Annotate the result IN PLACE — WITHOUT touching ``content``
    (callers/UI still get the whole guide) — with the ## headings that fall past the clamp's cut
    plus a re-fetch note, both short signal strings the clamp preserves. Pure formatting."""
    cut = max(0, budget - _DROPPED_CUT_RESERVE)
    dropped = [htext for _lvl, off, htext in _markdown_headings(content) if off >= cut]
    if dropped:
        result["dropped_sections"] = _join_dropped(dropped)
        result["note"] = (
            f"guide '{result['topic']}' exceeds the tool-result feed-back budget, so only a "
            f"leading preview reaches you; the sections in 'dropped_sections' fall past the cut — "
            f"re-fetch any ONE with read_knowledge(name='{result['topic']}', section='<heading>')."
        )
    else:
        result["note"] = (
            f"guide '{result['topic']}' exceeds the tool-result feed-back budget, so only a "
            f"leading preview reaches you; fetch a specific section with "
            f"read_knowledge(name='{result['topic']}', section='<heading>')."
        )


def read_knowledge(
    ctx: ToolContext, *, name: str, section: str | None = None,
) -> dict[str, Any]:
    """Return the FULL text of ONE knowledge guide by its basename (e.g. 'capacity' or
    'capacity.md'), or — with ``section`` — just that one named markdown section of it. The
    system prompt inlines the core guides and indexes the rest; call this to load an on-demand
    guide BEFORE interpreting that kind of result. Read-only, auto-runs. Strictly validated: no
    path traversal, no absolute paths, no '..'.

    The FULL guide text is always returned, but a large one is clamped to a leading preview before
    the MODEL sees it; when that happens the result also lists the ``dropped_sections`` (the ##
    headings past the cut) so nothing vanishes silently — re-fetch any one by passing
    ``section='<heading>'`` (which returns just that section, never clamped)."""
    files = _knowledge_files(ctx)
    valid = [f.name for f in files]
    requested = (name or "").strip()
    if not requested:
        return {"error": "missing 'name'", "valid_topics": valid}

    # Reject any path-bearing or traversal input outright — only a bare basename is allowed.
    if "/" in requested or "\\" in requested or ".." in requested or Path(requested).is_absolute():
        return {"error": f"invalid knowledge name {name!r}: pass a bare topic basename, "
                         f"not a path", "valid_topics": valid}

    # Match on exact basename, or on the stem (so 'capacity' -> 'capacity.md').
    match: Path | None = None
    for f in files:
        if f.name == requested or f.stem == requested:
            match = f
            break
    if match is None:
        return {"error": f"unknown knowledge topic {name!r}", "valid_topics": valid}

    content = match.read_text()

    # A targeted section fetch returns just that one section verbatim (always small, never
    # clamped) — the escape hatch for a section the whole-guide read dropped. Not de-duped: it is
    # a different, narrower payload than the whole guide, and re-fetching a section is cheap.
    if section is not None and section.strip():
        found = _extract_section(content, section)
        if found is None:
            return {
                "error": f"no section {section!r} in knowledge topic {match.stem!r}",
                "name": match.name, "topic": match.stem,
                "available_sections": [h for _lvl, _off, h in _markdown_headings(content)],
            }
        heading, body = found
        return {"name": match.name, "topic": match.stem, "section": heading, "content": body}

    # Per-session de-dup: a guide already loaded this session is in the conversation above, so an
    # EXACT repeat returns a back-reference rather than re-injecting the full guide every turn.
    if len(content) >= _DEDUP_OVER_CHARS and _doc_seen(ctx, f"knowledge:{match.name}"):
        return _already_provided(
            "topic", match.stem,
            reload_hint="Its full text is unchanged; re-load it only if you truly need it again.",
        )

    result: dict[str, Any] = {"name": match.name, "topic": match.stem, "content": content}
    # The FULL content stays put; if it would overflow the loop's feed-back budget (and be clamped
    # to a blind preview for the model), add the clamp-surviving 'dropped_sections' + note so the
    # model still learns which sections it is missing and how to re-fetch them.
    if len(json.dumps(result)) > DEFAULT_TOOL_RESULT_BUDGET:
        _annotate_budget_overflow(result, content, DEFAULT_TOOL_RESULT_BUDGET)
    return result


# --- search_knowledge: lexical search over knowledge/ + the curated repo-doc index ---------
# DETERMINISTIC mechanism only: tokenize the query and the corpus, score by weighted term
# overlap (filename/heading hits weigh more than body hits), rank, and return the best
# snippets/pointers. No embeddings, no model call, no per-topic special-casing — WHEN to reach
# for this (the troubleshooting/problem moments) is JUDGMENT in knowledge/conversation_style.md
# + the tool description, NOT a branch here.

# Drop the highest-frequency English/markdown filler so it never dominates the overlap score.
# Pure mechanism — a stop list is not domain judgment.
_SEARCH_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "are", "be", "with",
    "how", "do", "i", "my", "it", "this", "that", "when", "what", "why", "can", "you", "your",
    "if", "at", "as", "by", "from", "not", "but", "so", "we", "me", "no", "yes", "use", "using",
})
_WORD_RE = re.compile(r"[a-z0-9]+")


def _search_tokens(text: str) -> list[str]:
    """Lowercase alnum tokens with the filler words dropped (short tokens kept only if numeric
    or >=2 chars). Pure mechanism shared by the query and the corpus so scoring is symmetric."""
    return [
        t for t in _WORD_RE.findall(text.lower())
        if t not in _SEARCH_STOPWORDS and (len(t) >= 2 or t.isdigit())
    ]


def _knowledge_headings(text: str) -> str:
    """Concatenated markdown/yaml-comment headings of a knowledge doc — the high-signal field
    a query term should weigh more in than the prose body."""
    out: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#"):
            out.append(s.lstrip("#").strip())
    return "\n".join(out)


def _best_snippet(text: str, query_tokens: set[str], *, width: int = 280) -> str:
    """The most query-dense ~`width`-char window of the doc (a line-anchored excerpt), or the
    leading text when nothing matches. Deterministic: ties resolve to the earliest line."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return ""
    best_i, best_hits = 0, -1
    for i, ln in enumerate(lines):
        hits = sum(1 for t in _search_tokens(ln) if t in query_tokens)
        if hits > best_hits:
            best_i, best_hits = i, hits
    snippet = lines[best_i]
    j = best_i + 1
    while j < len(lines) and len(snippet) + 1 + len(lines[j]) <= width:
        snippet += " " + lines[j]
        j += 1
    return snippet[:width]


def _repo_doc_pointers(ctx: ToolContext) -> list[tuple[str, str]]:
    """(repo-relative-path, line) pairs parsed from the curated knowledge/useful_repo_docs.md
    index. Each bulleted/numbered line that names a `repo/...` doc becomes a searchable pointer
    so a problem-driven query can surface the right UPSTREAM doc by topic, not exact basename."""
    idx = ctx.settings.knowledge_dir / "useful_repo_docs.md"
    if not idx.is_file():
        return []
    pointers: list[tuple[str, str]] = []
    seen: set[str] = set()
    # A doc reference looks like `repo/path/to/doc.md` inside a backtick-quoted span.
    ref_re = re.compile(r"`((?:llm-d|llm-d-benchmark)/[^`]+?\.(?:md|yaml|yml|json))`")
    for line in idx.read_text().splitlines():
        s = line.strip()
        if not s:
            continue
        for m in ref_re.finditer(s):
            path = m.group(1)
            if path in seen:
                continue
            seen.add(path)
            pointers.append((path, s.lstrip("#*-0123456789. ").strip()))
    return pointers


def search_knowledge(
    ctx: ToolContext, *, query: str, limit: int = 5, include_repo_docs: bool = True,
) -> dict[str, Any]:
    """Lexically SEARCH every knowledge guide (and, optionally, the curated upstream repo-doc
    index) by keyword/topic and return the most relevant guides + a snippet from each, so the
    agent finds the right help WITHOUT knowing the exact basename. Read-only, auto-runs,
    deterministic (weighted keyword overlap; no model call). Use it at a TROUBLESHOOTING /
    'I don't know which doc' moment; once you know the topic, load the full guide with
    read_knowledge('<topic>') or the upstream doc with read_repo_doc('<path>')."""
    q = (query or "").strip()
    if not q:
        return {"error": "missing 'query'", "query": query}
    query_tokens = _search_tokens(q)
    if not query_tokens:
        return {"error": "query has no searchable terms", "query": query}
    qset = set(query_tokens)
    limit = max(1, min(int(limit), 20))

    knowledge_hits: list[dict[str, Any]] = []
    for f in _knowledge_files(ctx):
        try:
            text = f.read_text()
        except OSError:
            continue
        headings = _knowledge_headings(text)
        # Score by DISTINCT-term COVERAGE per field, not raw prose frequency: a query term that
        # appears in the filename (x12) or a heading (x5) signals the doc is ABOUT that topic and
        # must outrank one that merely mentions the word many times in passing (body coverage x2
        # per distinct term + a small, capped frequency tie-breaker). This keeps a focused guide
        # above a long doc that happens to repeat a common word. Deterministic — pure counting.
        name_blob = f.stem.replace("_", " ").replace("-", " ").lower()
        body_lower = text.lower()
        heading_lower = headings.lower()
        score = 0
        matched: list[str] = []
        for t in qset:
            in_name = t in name_blob
            in_head = t in heading_lower
            body_hits = body_lower.count(t)
            if in_name or in_head or body_hits:
                matched.append(t)
            score += (12 if in_name else 0) + (5 if in_head else 0)
            score += (2 if body_hits else 0) + min(body_hits, 4)
        if score <= 0:
            continue
        knowledge_hits.append({
            "kind": "knowledge",
            "topic": f.stem,
            "name": f.name,
            "score": score,
            "matched_terms": sorted(matched),
            "snippet": _best_snippet(text, qset),
            "load_with": f"read_knowledge('{f.stem}')",
        })

    repo_hits: list[dict[str, Any]] = []
    if include_repo_docs:
        for path, line in _repo_doc_pointers(ctx):
            path_tokens = set(_search_tokens(path))
            line_lower = line.lower()
            matched = [t for t in qset if t in path_tokens or t in line_lower]
            if not matched:
                continue
            # Curated pointers are terse one-liners, so a query term that appears in the doc PATH
            # itself (e.g. 'quickstart' in docs/quickstart.md) is the strongest signal — weight it
            # filename-tier (x12). A term only in the one-line blurb is heading-tier (x4).
            score = sum(12 if t in path_tokens else 4 for t in matched)
            repo_hits.append({
                "kind": "repo_doc",
                "path": path,
                "score": score,
                "matched_terms": sorted(matched),
                "snippet": line[:280],
                "load_with": f"read_repo_doc('{path}')",
            })

    # Stable, deterministic per-kind ranking (score desc, then name/path).
    knowledge_hits.sort(key=lambda r: (-r["score"], r["name"]))
    repo_hits.sort(key=lambda r: (-r["score"], r["path"]))

    # The agent's OWN guides are the primary help, so they lead; but curated upstream pointers
    # would otherwise be crowded out by the guides' longer prose, so RESERVE a slice of the page
    # for them (up to a third, >=1 when any matched) so a problem-driven query still surfaces the
    # right upstream doc. Mechanism only — the split is a fixed budget, not a per-topic decision.
    repo_quota = min(len(repo_hits), max(1, limit // 3)) if repo_hits else 0
    # ...but the reserved slice must never EVICT the top-ranked knowledge guide: at a small limit
    # (e.g. limit=1) max(1, limit//3) would leave 0 knowledge slots, returning only a (typically
    # lower-scoring) repo pointer and dropping the single best guide. Keep at least one knowledge
    # slot whenever knowledge matched, so the primary help always leads. The backfill below still
    # fills any slot the other kind can't.
    if knowledge_hits:
        repo_quota = min(repo_quota, limit - 1)
    k_take = min(len(knowledge_hits), limit - repo_quota)
    chosen = knowledge_hits[:k_take] + repo_hits[:repo_quota]
    # Backfill any unused slots from whichever kind still has matches (deterministic order).
    if len(chosen) < limit:
        rest = knowledge_hits[k_take:] + repo_hits[repo_quota:]
        rest.sort(key=lambda r: (-r["score"], r.get("name") or r.get("path") or ""))
        chosen += rest[: limit - len(chosen)]
    # Present the page in a single score-ranked order (ties: knowledge before repo_doc, then name).
    chosen.sort(key=lambda r: (-r["score"], r["kind"], r.get("name") or r.get("path") or ""))

    results = chosen
    valid_topics = [f.stem for f in _knowledge_files(ctx)]
    return {
        "query": q,
        "terms": query_tokens,
        "match_count": len(knowledge_hits) + len(repo_hits),
        "results": results[:limit],
        "valid_topics": valid_topics,
    }
