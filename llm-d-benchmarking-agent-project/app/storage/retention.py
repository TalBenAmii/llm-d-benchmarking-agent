"""Workspace lifecycle — retention/GC over scratch + a startup configuration self-check.

Two concerns, both pure mechanism (the *policy* is DATA on ``Settings``; the judgment about
what the verdicts MEAN lives in ``knowledge/workspace_lifecycle.md`` — thin code, thick agent):

1. **Retention / GC** (:func:`run_gc`). Per-session state (``workspace/sessions/<id>``),
   per-run scratch (``workspace/runs/<id>`` and the orchestrator's ``workspace/jobs``), and the
   cross-session history store (``workspace/history/*.json``) all grow without bound on a
   long-lived server. The GC walks each area, sorts its items oldest-first by mtime, and removes
   the oldest beyond the configured caps (``RETENTION_MAX_AGE_DAYS`` / ``RETENTION_MAX_ITEMS`` /
   ``RETENTION_MAX_BYTES``; 0/None = unlimited). It NEVER removes an item belonging to an
   active/running session — those ids are passed in and skipped before any cap is applied.

2. **Startup self-check** (:func:`self_check`). Validates that the workspace paths are writable,
   the configured LLM provider is coherent, and the read-only sibling repos resolve — returning a
   STRUCTURED :class:`SelfCheckResult` (pass/fail + per-check reasons). :func:`readiness` folds
   that into a minimal readiness contribution the ``/readyz`` endpoint (Phase 16) can compose; if
   ``/readyz`` is not yet present, this function is the seam the integrator wires it through.

Everything here is filesystem + settings only: no network, no cluster, no GPU. The GC counts and
compares against DATA caps; it embeds no per-area judgment in ``if/elif`` branches.
"""
from __future__ import annotations

import contextlib
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config import Settings


# ---------------------------------------------------------------------------
# Managed scratch areas. Each is (logical-name, subpath-under-workspace, item-kind).
# ``item-kind`` selects how an area's items are enumerated:
#   "dir"  -> each immediate child DIRECTORY is one item (sessions/, runs/)
#   "file" -> each immediate child FILE is one item        (jobs/*.yaml, history/*.json)
# The active-session guard only applies to the "sessions" area (only sessions can be "running").
# This is DATA describing WHERE scratch lives — not decision logic; the walk below is uniform.
#
# ``jobs`` is the orchestrator's per-run scratch: app/orchestrator/controller.py writes the
# rendered Job manifest to workspace/jobs/<run_id>.yaml (one FILE per run), so the area is
# file-kind. ``runs`` is reserved for any future per-run directory scratch (no code creates it
# today); keeping it declared means it's GC'd the moment something starts using it.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ManagedArea:
    name: str
    subpath: str
    item_kind: str          # "dir" | "file"
    is_sessions: bool = False


MANAGED_AREAS: tuple[ManagedArea, ...] = (
    ManagedArea("sessions", "sessions", "dir", is_sessions=True),
    ManagedArea("runs", "runs", "dir"),
    ManagedArea("jobs", "jobs", "file"),
    ManagedArea("history", "history", "file"),
)


# ---------------------------------------------------------------------------
# Retention caps (resolved from Settings DATA; 0/None -> unlimited).
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RetentionCaps:
    max_age_seconds: float | None = None   # remove items older than this; None = unlimited
    max_items: int | None = None           # keep at most this many per area; None = unlimited
    max_bytes: int | None = None           # keep an area under this total; None = unlimited

    @classmethod
    def from_settings(cls, settings: Settings) -> RetentionCaps:
        # Treat 0 / 0.0 / None uniformly as "unlimited" for that dimension (documented default
        # policy). The conversion is the ONLY place env numbers become caps — pure normalization.
        age_days = settings.retention_max_age_days or 0.0
        items = settings.retention_max_items or 0
        nbytes = settings.retention_max_bytes or 0
        return cls(
            max_age_seconds=(age_days * 86400.0) if age_days > 0 else None,
            max_items=items if items > 0 else None,
            max_bytes=nbytes if nbytes > 0 else None,
        )


@dataclass
class _Item:
    """One prunable thing in an area: a path, its mtime, and its on-disk size (bytes)."""
    path: Path
    mtime: float
    size: int
    item_id: str            # the session/run id (dir name) or record id (file stem)
    active: bool = False     # an active/running session — never prunable


@dataclass
class AreaResult:
    """What the GC did to one area."""
    area: str
    scanned: int = 0
    removed: list[str] = field(default_factory=list)
    kept: int = 0
    protected_active: int = 0
    bytes_before: int = 0
    bytes_after: int = 0
    errors: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "area": self.area,
            "scanned": self.scanned,
            "removed": list(self.removed),
            "removed_count": len(self.removed),
            "kept": self.kept,
            "protected_active": self.protected_active,
            "bytes_before": self.bytes_before,
            "bytes_after": self.bytes_after,
            "reclaimed_bytes": self.bytes_before - self.bytes_after,
            "errors": list(self.errors),
        }


@dataclass
class GCResult:
    """Aggregate of a full GC pass across every managed area."""
    ran: bool
    caps: dict[str, Any]
    areas: list[AreaResult] = field(default_factory=list)

    @property
    def total_removed(self) -> int:
        return sum(len(a.removed) for a in self.areas)

    @property
    def total_reclaimed_bytes(self) -> int:
        return sum(a.bytes_before - a.bytes_after for a in self.areas)

    def to_json(self) -> dict[str, Any]:
        return {
            "ran": self.ran,
            "caps": self.caps,
            "total_removed": self.total_removed,
            "total_reclaimed_bytes": self.total_reclaimed_bytes,
            "areas": [a.to_json() for a in self.areas],
        }


def _dir_size(path: Path) -> int:
    """Total on-disk size of a path (bytes). A file -> its size; a dir -> sum of its files.
    Best-effort: an unreadable entry contributes 0 rather than raising (GC must not crash)."""
    try:
        if path.is_file():
            return path.stat().st_size
        total = 0
        for child in path.rglob("*"):
            try:
                if child.is_file():
                    total += child.stat().st_size
            except OSError:
                continue
        return total
    except OSError:
        return 0


def _remove(path: Path) -> None:
    if path.is_dir():
        import shutil

        shutil.rmtree(path, ignore_errors=True)
    else:
        with contextlib.suppress(OSError):
            path.unlink()


def _enumerate(area: ManagedArea, root: Path, active_ids: set[str]) -> list[_Item]:
    """List an area's prunable items (oldest-relevant fields filled). Missing area -> empty."""
    base = root / area.subpath
    if not base.is_dir():
        return []
    items: list[_Item] = []
    want_dir = area.item_kind == "dir"
    for child in base.iterdir():
        try:
            is_dir = child.is_dir()
        except OSError:
            continue
        if want_dir != is_dir:
            continue  # area expects dirs xor files; skip the other kind (e.g. a stray temp file)
        item_id = child.name if want_dir else child.stem
        try:
            mtime = child.stat().st_mtime
        except OSError:
            continue
        items.append(
            _Item(
                path=child,
                mtime=mtime,
                size=_dir_size(child),
                item_id=item_id,
                active=area.is_sessions and item_id in active_ids,
            )
        )
    return items


def _select_for_removal(items: list[_Item], caps: RetentionCaps, now: float) -> list[_Item]:
    """Pure selection: given an area's items + the caps, return which items to remove.

    Mechanism, not judgment. Active items are dropped from consideration up front (never
    prunable). The remaining items are ordered oldest-first; an item is removed when ANY cap
    says it must:
      * age:   its age exceeds ``max_age_seconds``
      * count: it is among the oldest beyond the newest ``max_items`` survivors
      * bytes: it must go so the survivors' total size fits under ``max_bytes``
    All thresholds are DATA on ``caps``; ``None`` disables that dimension. We always remove the
    OLDEST first (the spec's requirement). The set union of the three predicates is the result.
    """
    prunable = [it for it in items if not it.active]
    prunable.sort(key=lambda it: it.mtime)  # oldest first
    n = len(prunable)
    remove: set[int] = set()

    # age: anything strictly older than the age cap.
    if caps.max_age_seconds is not None:
        for i, it in enumerate(prunable):
            if (now - it.mtime) > caps.max_age_seconds:
                remove.add(i)

    # count: keep only the newest ``max_items`` survivors; the oldest overflow goes.
    if caps.max_items is not None and n > caps.max_items:
        overflow = n - caps.max_items
        for i in range(overflow):
            remove.add(i)

    # bytes: from oldest forward, remove until the survivors fit under ``max_bytes``.
    if caps.max_bytes is not None:
        surviving_bytes = sum(it.size for i, it in enumerate(prunable) if i not in remove)
        i = 0
        while surviving_bytes > caps.max_bytes and i < n:
            if i not in remove:
                remove.add(i)
                surviving_bytes -= prunable[i].size
            i += 1

    return [prunable[i] for i in sorted(remove)]


def gc_area(
    area: ManagedArea,
    root: Path,
    caps: RetentionCaps,
    active_ids: set[str],
    *,
    now: float | None = None,
    dry_run: bool = False,
) -> AreaResult:
    """Apply the caps to ONE area and (unless ``dry_run``) delete the selected items."""
    now = time.time() if now is None else now
    items = _enumerate(area, root, active_ids)
    result = AreaResult(area=area.name, scanned=len(items))
    result.bytes_before = sum(it.size for it in items)
    result.protected_active = sum(1 for it in items if it.active)

    to_remove = _select_for_removal(items, caps, now)
    removed_ids: set[str] = set()
    for it in to_remove:
        if not dry_run:
            try:
                _remove(it.path)
            except Exception as exc:  # noqa: BLE001 — one bad entry must not abort the pass
                result.errors.append(f"{it.item_id}: {exc}")
                continue
        result.removed.append(it.item_id)
        removed_ids.add(it.item_id)

    result.kept = len(items) - len(removed_ids)
    result.bytes_after = sum(it.size for it in items if it.item_id not in removed_ids)
    return result


def run_gc(
    settings: Settings,
    *,
    active_session_ids: Iterable[str] | None = None,
    now: float | None = None,
    dry_run: bool = False,
) -> GCResult:
    """Run a full retention GC pass over every managed scratch area in the workspace.

    ``active_session_ids`` are sessions currently held in memory / running a turn — their data
    is NEVER pruned (the active-run safety the spec mandates). The caller (the lifespan / a
    periodic hook) passes ``app.state.sessions`` live ids. ``dry_run`` reports what WOULD be
    removed without touching disk (used by the self-check / tests)."""
    caps = RetentionCaps.from_settings(settings)
    active = set(active_session_ids or ())
    root = settings.resolved_workspace_dir
    areas = [
        gc_area(area, root, caps, active, now=now, dry_run=dry_run) for area in MANAGED_AREAS
    ]
    return GCResult(
        ran=True,
        caps={
            "max_age_seconds": caps.max_age_seconds,
            "max_items": caps.max_items,
            "max_bytes": caps.max_bytes,
        },
        areas=areas,
    )


# ===========================================================================
# Startup configuration self-check
# ===========================================================================
@dataclass
class CheckOutcome:
    """One self-check probe's structured result."""
    name: str
    ok: bool
    detail: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail, **({"data": self.data} if self.data else {})}


@dataclass
class SelfCheckResult:
    """Structured pass/fail of the whole startup self-check, with per-check reasons."""
    checks: list[CheckOutcome] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    @property
    def failures(self) -> list[CheckOutcome]:
        return [c for c in self.checks if not c.ok]

    @property
    def reasons(self) -> list[str]:
        return [f"{c.name}: {c.detail}" for c in self.failures]

    def to_json(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checks": [c.to_json() for c in self.checks],
            "failures": [c.name for c in self.failures],
            "reasons": self.reasons,
        }


def _check_workspace_writable(settings: Settings) -> CheckOutcome:
    """The workspace root must exist (creatable) and accept a write — else every session
    snapshot, run manifest, and history write fails opaquely at request time."""
    root = settings.resolved_workspace_dir
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".selfcheck_write_probe"
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        return CheckOutcome("workspace_writable", False, f"cannot write under {root}: {exc}",
                            {"path": str(root)})
    return CheckOutcome("workspace_writable", True, f"writable at {root}", {"path": str(root)})


# How to determine which secret a configured provider needs. DATA, keyed by the normalized
# provider name — NOT decision logic; the self-check below just looks the requirement up.
_PROVIDER_KEY_ATTR: dict[str, str] = {
    "anthropic": "anthropic_api_key",
    "openai": "openai_api_key",
    "openai-compatible": "openai_api_key",
    "vllm": "openai_api_key",
}


def _check_provider_coherent(settings: Settings) -> CheckOutcome:
    """The LLM provider name must be known AND its required key present. Surfaces the most
    common misconfiguration (provider set, key forgotten) at startup rather than on first chat.
    The check OBSERVES config only — it never contacts the provider (hermetic)."""
    provider = (settings.llm_provider or "anthropic").lower()
    key_attr = _PROVIDER_KEY_ATTR.get(provider)
    if key_attr is None:
        return CheckOutcome(
            "provider_coherent", False, f"unknown LLM_PROVIDER {settings.llm_provider!r}",
            {"provider": provider, "known": sorted(_PROVIDER_KEY_ATTR)},
        )
    has_key = bool(getattr(settings, key_attr, None))
    return CheckOutcome(
        "provider_coherent", has_key,
        f"provider {provider!r} configured with {key_attr}" if has_key
        else f"provider {provider!r} requires {key_attr.upper()} but it is unset",
        {"provider": provider, "key_attr": key_attr, "has_key": has_key},
    )


def _check_repos_resolvable(settings: Settings) -> CheckOutcome:
    """The two read-only sibling repos must resolve on disk — the agent reads their specs,
    schemas, and CLI live. A missing bench repo means catalog/report/capacity paths fail."""
    present = {name: path.is_dir() for name, path in settings.repo_paths.items()}
    missing = [name for name, ok in present.items() if not ok]
    return CheckOutcome(
        "repos_resolvable", not missing,
        "all repos resolvable" if not missing else f"missing repo(s): {', '.join(missing)}",
        {"repos": {n: str(p) for n, p in settings.repo_paths.items()}, "present": present},
    )


def _check_runner_ok(settings: Settings) -> CheckOutcome:
    """The command runner's PRECONDITION is a well-formed security policy: the deny-by-default
    allowlist must load + schema-validate (malformed YAML / bad governance limits raise at load).
    Without it the runner can validate nothing and every command would be refused. Phase 16 adds
    this as the ``runner ok`` readiness component the spec calls for — observing config only (it
    loads the policy file; it never executes anything), so it stays hermetic."""
    try:
        from app.security.allowlist import Allowlist

        al = Allowlist.from_file(settings.allowlist_path)
        n = len(getattr(al, "_executables", {}) or {})
    except Exception as exc:  # noqa: BLE001 — a bad policy is a readiness failure, not a crash
        return CheckOutcome("runner_ok", False, f"allowlist failed to load: {exc}",
                            {"allowlist_path": str(settings.allowlist_path)})
    return CheckOutcome("runner_ok", True, f"command runner ready ({n} allowlisted executables)",
                        {"allowlist_path": str(settings.allowlist_path), "executables": n})


def _check_auth_coherent(settings: Settings) -> CheckOutcome:
    """If Bearer auth is enabled it MUST have a token, else every request 401s silently. Mirrors
    the fail-loud guard in the lifespan, but as a structured readiness signal rather than a crash."""
    bad = settings.auth_enabled and not settings.auth_token
    return CheckOutcome(
        "auth_coherent", not bad,
        "auth config coherent" if not bad else "AUTH_ENABLED is set but AUTH_TOKEN is empty",
        {"auth_enabled": settings.auth_enabled},
    )


# The ordered set of self-check probes. DATA (a list of callables) — adding a probe is a list
# edit, not a new branch. Each returns a structured CheckOutcome.
_CHECKS: tuple[Callable[[Settings], CheckOutcome], ...] = (
    _check_workspace_writable,
    _check_provider_coherent,
    _check_repos_resolvable,
    _check_runner_ok,
    _check_auth_coherent,
)


def self_check(settings: Settings) -> SelfCheckResult:
    """Run every startup self-check probe and return the structured aggregate result."""
    return SelfCheckResult(checks=[probe(settings) for probe in _CHECKS])


def readiness(settings: Settings) -> dict[str, Any]:
    """Minimal readiness contribution for the /readyz endpoint (Phase 16) to compose.

    Returns ``{"ready": bool, "self_check": {...}}``. Readiness honors the STARTUP_SELF_CHECK
    toggle: when disabled, the contribution reports ready with the self-check marked skipped (so
    an operator who deliberately turned it off isn't held un-ready). When enabled, ``ready``
    mirrors the self-check's pass/fail and the structured reasons ride along for diagnosis."""
    if not settings.startup_self_check:
        return {"ready": True, "self_check": {"skipped": True}}
    result = self_check(settings)
    return {"ready": result.ok, "self_check": result.to_json()}
