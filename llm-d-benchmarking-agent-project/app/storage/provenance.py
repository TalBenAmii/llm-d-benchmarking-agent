"""Provenance bundle — capture the EXACT inputs that produced a validated benchmark result.

A benchmark number is only credible if someone can regenerate it. This module is the
**mechanism** that captures, into one content-addressed :class:`ProvenanceBundle`, everything
needed to reproduce a run: both read-only repo SHAs (+ dirty flags), the exact resolved
run-config the CLI itself wrote, an environment snapshot, the knowledge "brain" hash, the agent
version, and the *validated* Benchmark Report summary + digest (determinism gate d). It then
serializes that bundle to JSON under the per-session workspace.

It contains NO judgment — WHEN to offer a bundle, how to explain a dirty repo to a non-expert,
and how to sequence a reproduce live in ``knowledge/reproducibility.md`` and the agent's
reasoning (thin code, thick agent). Like ``history.py``:

* it **refuses to capture an unvalidated report** (only certifies a schema-valid run), and
* it **never fabricates a SHA**: a missing/empty repo degrades to ``unavailable: True`` rather
  than inventing one, so a worktree with empty sibling repos still produces an honest (but
  flagged non-reproducible-as-captured) bundle.

Storage mirrors history: bundles live under ``<workspace>/bundles/<bundle_id>.json`` (per the
session dir, GC'd with it) and the ``bundle_id`` + a compact ``provenance`` dict can be threaded
onto a :class:`~app.storage.history.HistoryRecord`. No new managed area, no retention change.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

# Filesystem-safe id length (we build paths from it). Mirrors history.compute_record_id.
_ID_LEN = 16

# Knowledge-hash glob: the SAME set the system prompt assembles its brain from
# (app/agent/prompt.py::_knowledge_sections). Captures the agent's judgment surface so a bundle
# records which "brain" produced the run. Any knowledge edit bumps the hash even if
# behavior-neutral — an accepted, coarse provenance signal (see knowledge/reproducibility.md).
_KNOWLEDGE_GLOBS = ("*.md", "*.yaml", "*.yml")
# Editor-facing meta docs are not agent knowledge — drop them so the hash matches the prompt's
# brain (mirrors knowledge_access.EXCLUDED_KNOWLEDGE_FILES).
_EXCLUDED_KNOWLEDGE_FILES = frozenset({"CLAUDE.md", "README.md"})


@dataclass
class ProvenanceBundle:
    """Everything needed to credibly reproduce one validated benchmark result.

    Pure data: assembled by :func:`build_bundle` from already-gathered, already-validated
    inputs. ``to_json`` is the only behavior (it serializes for the store + the HTML renderer).
    """

    bundle_id: str
    created_at: float
    agent_version: str
    knowledge_version: str
    repos: dict[str, Any]            # {"llm-d": {sha, dirty, ref|unavailable}, "llm-d-benchmark": {...}}
    resolved_config: dict[str, Any]  # {path, body} — the CLI-written run-config (or {found: False})
    spec: str | None
    harness: str | None
    workload: str | None
    namespace: str | None
    model: str | None
    slo: dict[str, Any] | None
    env_snapshot: dict[str, Any] | None
    report_digest: str
    report_summary: dict[str, Any]
    regenerate_command: str
    report_path: str | None = None
    label: str | None = None
    dirty: bool = False              # convenience: True if EITHER repo was dirty

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


# ---- repo state capture (read-only git, degrades gracefully) ----------------

class RunReadonly(Protocol):
    """The read-only runner the tool passes in. A structural type (kept abstract so this module
    never imports ToolContext — avoids a cycle): an awaitable call accepting an argv + an optional
    ``cwd`` and returning an object with ``.ok``/``.stdout`` (a RunResult)."""

    async def __call__(self, argv: list[str], *, cwd: str | None = ...) -> Any: ...


async def capture_repo_state(repo_path: str | Path, run_readonly: RunReadonly) -> dict[str, Any]:
    """Capture one repo's git state via the already-allowlisted ``git rev-parse`` /
    ``git status --porcelain``.

    Returns ``{sha, dirty}`` (short SHA + uncommitted-changes flag) for a present, readable repo.
    A missing/empty directory (the worktree case — empty sibling repos) or any failing/erroring
    git read degrades to ``{sha: None, dirty: None, unavailable: True}`` — it NEVER raises and
    NEVER fabricates a SHA. (Only the two already-allowlisted READ-ONLY reads are used; no new
    allowlist entry beyond the small ``git rev-parse --short`` addition.)
    """
    p = Path(repo_path)
    # Cheap, no-subprocess miss for the common worktree case (empty/absent sibling repo).
    if not p.is_dir() or not (p / ".git").exists():
        return {"sha": None, "dirty": None, "unavailable": True}

    try:
        # Only the two already-allowlisted READ-ONLY git reads (no new allowlist entry beyond
        # the small `git rev-parse --short` addition): a short SHA + the dirty flag.
        sha_res = await run_readonly(["git", "rev-parse", "--short", "HEAD"], cwd=str(p))
        status_res = await run_readonly(["git", "status", "--porcelain"], cwd=str(p))
    except Exception:
        # Any allowlist/runner error → honest "unavailable" rather than a crash or a fake SHA.
        return {"sha": None, "dirty": None, "unavailable": True}

    if not getattr(sha_res, "ok", False):
        return {"sha": None, "dirty": None, "unavailable": True}

    sha = (getattr(sha_res, "stdout", "") or "").strip().splitlines()
    dirty = bool((getattr(status_res, "stdout", "") or "").strip()) if getattr(status_res, "ok", False) else None
    return {
        "sha": sha[0] if sha else None,
        "dirty": dirty,
    }


# ---- knowledge hash (the agent's "brain") -----------------------------------


def knowledge_hash(knowledge_dir: str | Path) -> str:
    """A sha256 over the sorted knowledge ``*.md|*.yaml|*.yml`` glob (file name + bytes).

    Deterministic for a fixed knowledge dir; changes when any knowledge file's content (or its
    presence) changes. Editor-facing meta docs are excluded so it tracks the agent's actual
    brain, matching the system-prompt glob. Missing dir → the hash of an empty set (stable).
    """
    kdir = Path(knowledge_dir)
    files: list[Path] = []
    if kdir.is_dir():
        for pat in _KNOWLEDGE_GLOBS:
            files.extend(kdir.glob(pat))
    files = sorted(
        {f for f in files if f.name not in _EXCLUDED_KNOWLEDGE_FILES},
        key=lambda f: f.name,
    )
    h = hashlib.sha256()
    for f in files:
        try:
            data = f.read_bytes()
        except OSError:
            continue
        h.update(f.name.encode("utf-8"))
        h.update(b"\0")
        h.update(data)
        h.update(b"\0")
    return h.hexdigest()


# ---- the regenerate command -------------------------------------------------


def regenerate_command(run_config_path: str | None, namespace: str | None) -> str:
    """The single copy-paste CLI line that replays the run from its resolved run-config —
    the upstream round-trip (``llmdbenchmark run -c <run-config.yaml> -p <namespace>``;
    ``-c`` is run-only). Pure string assembly; never executed here. Honest when no config was
    captured (the agent must run ``--generate-config`` first)."""
    cfg = run_config_path or "<run-config.yaml — run `--generate-config` first>"
    ns = namespace or "<namespace>"
    return f"llmdbenchmark run -c {cfg} -p {ns}"


# ---- bundle assembly --------------------------------------------------------


def _report_digest(report_bytes: bytes, summary: dict[str, Any]) -> str:
    """sha256 of the validated report bytes + its summarize_report output — ties the bundle to
    the exact report it certifies (determinism gate d)."""
    h = hashlib.sha256()
    h.update(report_bytes)
    h.update(b"\0")
    h.update(json.dumps(summary, sort_keys=True, default=str).encode("utf-8"))
    return h.hexdigest()


def _compute_bundle_id(*, run_uid: str | None, report_path: str | None, repos: dict[str, Any]) -> str:
    """Content hash (sha256, 16 hex) of run_uid + report_path + repo SHAs — so the same run +
    same repos maps to one bundle (idempotent), but a genuinely different run/repo state does
    not collide (mirrors history.compute_record_id)."""
    repo_shas = {name: (state or {}).get("sha") for name, state in sorted(repos.items())}
    basis = json.dumps(
        {"run_uid": run_uid, "report_path": report_path, "repo_shas": repo_shas},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:_ID_LEN]


class InvalidReportError(RuntimeError):
    """build_bundle refuses to certify an unvalidated report (determinism gate d)."""


def build_bundle(
    *,
    report_bytes: bytes,
    report_summary: dict[str, Any],
    report_valid: bool,
    report_path: str | None,
    repos: dict[str, Any],
    resolved_config: dict[str, Any],
    agent_version: str,
    knowledge_version: str,
    spec: str | None = None,
    harness: str | None = None,
    workload: str | None = None,
    namespace: str | None = None,
    model: str | None = None,
    slo: dict[str, Any] | None = None,
    env_snapshot: dict[str, Any] | None = None,
    label: str | None = None,
) -> ProvenanceBundle:
    """Assemble a :class:`ProvenanceBundle` from already-gathered, already-validated inputs.

    Pure (no I/O beyond hashing the bytes the caller already read). REFUSES an unvalidated
    report (``report_valid`` False → :class:`InvalidReportError`) so a bundle only ever certifies
    a schema-valid run. The ``harness``/``model`` fall back to the report summary's own values.
    """
    if not report_valid:
        raise InvalidReportError(
            "refusing to build a provenance bundle for an unvalidated report "
            "(determinism gate d): the bundle only certifies a schema-valid run."
        )

    harness = harness or report_summary.get("harness")
    model = model or report_summary.get("model")
    run_uid = report_summary.get("run_uid")
    cfg_path = resolved_config.get("path") if isinstance(resolved_config, dict) else None

    digest = _report_digest(report_bytes, report_summary)
    bundle_id = _compute_bundle_id(run_uid=run_uid, report_path=report_path, repos=repos)
    dirty = any(bool((state or {}).get("dirty")) for state in repos.values())

    return ProvenanceBundle(
        bundle_id=bundle_id,
        created_at=time.time(),
        agent_version=agent_version,
        knowledge_version=knowledge_version,
        repos=repos,
        resolved_config=resolved_config,
        spec=spec,
        harness=harness,
        workload=workload,
        namespace=namespace,
        model=model,
        slo=slo,
        env_snapshot=env_snapshot,
        report_digest=digest,
        report_summary=report_summary,
        regenerate_command=regenerate_command(cfg_path, namespace),
        report_path=report_path,
        label=label,
        dirty=dirty,
    )


# ---- the bundle store (under <workspace>/bundles) ---------------------------


def _safe_id(bid: str | None) -> bool:
    """Filesystem-safe bundle id (we build paths from it). Same guard shape as
    history._safe_id — alphanumeric, bounded length — so traversal (``../``, ``a/b``) is rejected."""
    return isinstance(bid, str) and bid.isalnum() and 0 < len(bid) <= 64


def _as_num(v: Any) -> float:
    """Crash-proof sort key: the value if a real number, else 0.0. A bundle is read straight from
    on-disk JSON with no per-field type-check, so a forged/corrupt ``created_at`` (a truthy string)
    would make ``list()``'s ``sorted(...)`` raise ``TypeError`` and break the WHOLE bundle list.
    The old ``b.get('created_at') or 0.0`` only handled falsy values, not a truthy non-number.
    ``bool`` is excluded — an ``int`` subclass, never a valid timestamp."""
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0


class BundleStore:
    """Write/read provenance bundles under ``<workspace>/bundles/<bundle_id>.json``.

    All I/O is defensive: a corrupt/partial bundle file is skipped on read (returns None),
    never crashing the agent. Writes are atomic-ish (temp then replace) so a concurrent read
    never sees a half file. The store only ever writes under the per-session workspace — never
    the read-only repos.
    """

    def __init__(self, workspace: str | Path):
        self._dir = Path(workspace) / "bundles"

    @property
    def dir(self) -> Path:
        return self._dir

    def write(self, bundle: ProvenanceBundle | dict[str, Any]) -> Path:
        data = bundle.to_json() if isinstance(bundle, ProvenanceBundle) else dict(bundle)
        bid = data.get("bundle_id")
        if not _safe_id(bid):
            raise ValueError(f"unsafe bundle_id {bid!r}")
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / f"{bid}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, default=str))
        tmp.replace(path)
        return path

    def read(self, bundle_id: str) -> dict[str, Any] | None:
        if not _safe_id(bundle_id):
            return None
        path = self._dir / f"{bundle_id}.json"
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict) or "bundle_id" not in data:
            return None
        # Never trust an id baked into the file — the path is truth.
        data["bundle_id"] = path.stem
        return data

    def list(self) -> list[dict[str, Any]]:
        """All stored bundles under this workspace, newest first (best-effort)."""
        out: list[dict[str, Any]] = []
        if not self._dir.exists():
            return out
        for p in self._dir.glob("*.json"):
            b = self.read(p.stem)
            if b is not None:
                out.append(b)
        out.sort(key=lambda b: _as_num(b.get("created_at")), reverse=True)
        return out


def provenance_view(bundle: dict[str, Any]) -> dict[str, Any]:
    """A compact, list-friendly provenance dict to thread onto a HistoryRecord (the full bundle
    lives under the workspace; the record carries just enough to surface + locate it)."""
    return {
        "bundle_id": bundle.get("bundle_id"),
        "created_at": bundle.get("created_at"),
        "agent_version": bundle.get("agent_version"),
        "knowledge_version": bundle.get("knowledge_version"),
        "repos": bundle.get("repos"),
        "dirty": bundle.get("dirty"),
        "regenerate_command": bundle.get("regenerate_command"),
    }
