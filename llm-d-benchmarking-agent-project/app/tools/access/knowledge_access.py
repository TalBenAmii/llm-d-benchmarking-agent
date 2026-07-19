"""Read-only knowledge & repo-doc access tools.

Read a file from inside one of the read-only repos, fetch the LIVE content of the
authoritative repo docs pinned in knowledge/key_docs.yaml, read one knowledge guide by
basename, and lexically search the knowledge corpus + the curated upstream-doc index. None of
these mutate anything, so the agent loop runs them automatically (no approval).

Split out of app/tools/setup/probe.py (which had grown into a ~1,100-line module spanning three
unrelated tool families) so the doc/knowledge surface is independently navigable. probe.py
re-exports these names for backwards compatibility; new code should import them from here.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from app.paths import is_within
from app.tools.context import ToolContext, ToolError


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
    procedure library). Path traversal is blocked."""
    return _read_repo_doc_raw(ctx, path=path, max_bytes=max_bytes)


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
    kfile = ctx.settings.knowledge_dir / "reference" / "key_docs.yaml"
    if not kfile.is_file():
        return {"docs": [], "note": f"key_docs.yaml not found at {kfile}"}
    try:
        spec = yaml.safe_load(kfile.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ToolError(f"key_docs.yaml is not valid YAML: {exc}") from exc

    entries = spec.get("docs", []) if isinstance(spec, dict) else []
    if task:
        entries = [e for e in entries if e.get("task") == task]
        # Record the task as CONSULTED the instant it is requested — keyed on the task ARG,
        # independent of whether the docs actually resolve (an absent skills repo must not defeat
        # the skill-grounding gate; see app/tools/run/skill_gate.py). Mechanism only.
        ctx.consulted_skills.add(task)

    fetched: list[dict[str, Any]] = []
    for entry in entries:
        rel = entry.get("path", "")
        item: dict[str, Any] = {"path": rel, "task": entry.get("task"), "why": entry.get("why")}
        is_knowledge = entry.get("kind") == "knowledge"
        try:
            # A `kind: knowledge` entry (e.g. our quickstart runbook) reads from knowledge/
            # instead of the read-only repos — same {path, content, truncated} shape, so the
            # logic is shared.
            doc = (_read_knowledge_doc(ctx, rel, max_bytes=max_bytes_each) if is_knowledge
                   else _read_repo_doc_raw(ctx, path=rel, max_bytes=max_bytes_each))
            item.update(found=True, resolved=doc["path"], content=doc["content"],
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
    files = list(kdir.rglob("*.md")) + list(kdir.rglob("*.yaml")) + list(kdir.rglob("*.yml"))
    files = [f for f in files if f.name not in EXCLUDED_KNOWLEDGE_FILES]
    return sorted(files, key=lambda p: p.name)


def _match_knowledge_basename(name: str, files: list[Path]) -> Path | None:
    """The file in ``files`` whose basename OR stem equals ``name``, after rejecting any path /
    traversal / absolute input; None otherwise. The single basename-safety check shared by
    read_knowledge and fetch_key_docs' ``kind: knowledge`` branch (no path traversal, ever)."""
    requested = (name or "").strip()
    if "/" in requested or "\\" in requested or ".." in requested or Path(requested).is_absolute():
        return None
    return next((f for f in files if f.name == requested or f.stem == requested), None)


def _read_knowledge_doc(ctx: ToolContext, path: str, *, max_bytes: int) -> dict[str, Any]:
    """Read a knowledge/ guide by BASENAME for fetch_key_docs' ``kind: knowledge`` entries,
    returning the SAME {path, content, truncated} shape as _read_repo_doc_raw so the caller's
    dedup / item-building logic is shared. Raises ToolError on an unsafe name or missing file,
    which the caller catches into found=False (fail-open, like a missing repo doc)."""
    match = _match_knowledge_basename(path, _knowledge_files(ctx))
    if match is None:
        raise ToolError(f"knowledge file {path!r} not found (must be a bare basename)")
    data = match.read_text(errors="replace")
    return {"path": str(match), "content": data[:max_bytes], "truncated": len(data.encode()) > max_bytes}


# --- markdown section addressing --------------------------------------------------------------
# A `section` arg on read_knowledge returns just one named markdown section verbatim, so the
# model can pull a specific part of a large guide instead of the whole thing. Pure mechanism —
# WHICH section to read is the model's call.
# CommonMark allows an ATX heading up to 3 leading spaces of indentation; keep that latitude so an
# indented '### Sub' (as under capacity.md's provisioning block) is still seen as a heading.
_ATX_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]+(\S.*?)[ \t]*#*[ \t]*$")
# A fenced code block opens/closes on a line of >=3 backticks or tildes (up to 3 leading spaces).
# Heading-lookalike lines INSIDE a fence (e.g. a shell '# comment') are NOT headings, so the
# outline walk must track fences and skip their contents.
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def _markdown_headings(text: str) -> list[tuple[int, int, str]]:
    """(level, char-offset, heading-text) for every ATX markdown heading in ``text``, in order.
    Pure mechanism — the outline used to address a section. Fenced code blocks are tracked so a
    heading-lookalike line inside a ``` / ~~~ fence (a shell comment, a commented-out
    '# heading') is not mis-parsed as a real heading."""
    out: list[tuple[int, int, str]] = []
    offset = 0
    fence: str | None = None  # the fence char (` or ~) while inside a code block, else None
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\n")
        fm = _FENCE_RE.match(stripped)
        if fence is None:
            if fm:
                fence = fm.group(1)[0]  # a fence opens; suspend heading detection until it closes
            else:
                m = _ATX_HEADING_RE.match(stripped)
                if m:
                    out.append((len(m.group(1)), offset, m.group(2).strip()))
        elif fm and fm.group(1)[0] == fence:
            fence = None  # a same-marker fence line closes the block
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


def read_knowledge(
    ctx: ToolContext, *, name: str, section: str | None = None,
) -> dict[str, Any]:
    """Return the FULL text of ONE knowledge guide by its basename (e.g. 'capacity' or
    'capacity.md'), or — with ``section`` — just that one named markdown section of it. The
    system prompt inlines the core guides and indexes the rest; call this to load an on-demand
    guide BEFORE interpreting that kind of result. Read-only, auto-runs. Strictly validated: no
    path traversal, no absolute paths, no '..'."""
    files = _knowledge_files(ctx)
    valid = [f.name for f in files]
    requested = (name or "").strip()
    if not requested:
        return {"error": "missing 'name'", "valid_topics": valid}

    # Basename-safety + match (shared with fetch_key_docs): rejects any path/traversal/absolute
    # input, then matches on exact basename or stem ('capacity' -> 'capacity.md').
    match = _match_knowledge_basename(requested, files)
    if match is None:
        return {"error": f"unknown knowledge topic {name!r} (pass a bare basename, not a path)",
                "valid_topics": valid}

    content = match.read_text()

    # A targeted section fetch returns just that one section verbatim — the cheap way to pull a
    # specific part of a large guide.
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

    return {"name": match.name, "topic": match.stem, "content": content}


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
    idx = ctx.settings.knowledge_dir / "reference" / "useful_repo_docs.md"
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
