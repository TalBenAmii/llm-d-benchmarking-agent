"""Deny-by-default command allowlist validator.

This module is a *pure validator* over ``security/allowlist.yaml``. It contains no
per-command knowledge: every rule lives in the YAML. Given a logical argv list
(e.g. ``["llmdbenchmark", "--spec", "cicd/kind", "standup", "-p", "ns"]``) it returns
a :class:`Decision` saying whether the command is permitted and whether it is
``read_only`` (auto-runnable) or ``mutating`` (requires user approval).

It never runs anything — see ``app/security/runner.py`` for execution.

Flag policy: the allowlist gates *which* executables and subcommands may run, whether a
command is read-only or mutating (and thus approval-gated), and screens every token for
shell metacharacters. It does **not** reject unrecognized *flags* — once an executable +
subcommand are allowlisted, any additional flag is accepted (its value is still
metachar-screened, but not checked against ``value_constraints``). Values consumed by
*known* flags are still validated against their declared constraint, and unknown flags
never downgrade a mutating command's mode. Positionals and subcommands remain
strictly validated.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# The generic token-walk engine + mode constants live in the private sibling module. This file
# imports FROM it (one-way; the engine imports nothing back) so there is no cycle. The mode
# constants are re-exported here because external callers import READ_ONLY/MUTATING from this
# module — keeping the public names stable.
from app.security._validator import MUTATING, READ_ONLY, _Reject, _Validator

__all__ = ["READ_ONLY", "MUTATING", "Decision", "AllowlistError", "Allowlist"]

# Tokens we generate never need shell metacharacters. We reject them on every token
# as defense in depth, even though the runner uses shell=False (no shell to inject).
_DANGEROUS = set(";|&$><`\n\r\t\0\\!*?(){}[]'\"")


@dataclass
class Decision:
    allowed: bool
    mode: str = MUTATING  # conservative default
    reason: str = ""
    argv: list[str] = field(default_factory=list)
    # --- governance, sourced PURELY from security/allowlist.yaml (Phase 13) ---
    # The per-command execution deadline (seconds) declared in the policy data, or None
    # when the command carries no `timeout_s` (the runner then applies its global default).
    # This is the ONE place timeouts come from — there is no Python per-command table.
    timeout_s: int | None = None

    @property
    def requires_approval(self) -> bool:
        return self.allowed and self.mode == MUTATING


def _deny(argv: list[str], reason: str) -> Decision:
    return Decision(allowed=False, mode=MUTATING, reason=reason, argv=list(argv))


class AllowlistError(RuntimeError):
    pass


# ----------------------------------------------------------------------------
# Governance fields (Phase 13): per-command timeouts live in the YAML as DATA. The
# two helpers below are the only things that read that field — one validates its
# SHAPE at load, the other extracts it for a Decision. No per-command knowledge:
# both operate uniformly over whatever the policy declares.
# ----------------------------------------------------------------------------


def _check_positive_int(value: Any, where: str) -> None:
    # bool is an int subclass — reject it explicitly so `timeout_s: true` can't slip through.
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AllowlistError(f"{where} must be a positive integer, got {value!r}")


def _validate_one_governance_block(block: dict[str, Any], where: str) -> None:
    """Validate the optional ``timeout_s`` field on a single executable or subcommand
    entry. Raises :class:`AllowlistError` on any malformed value."""
    if "timeout_s" in block:
        _check_positive_int(block["timeout_s"], f"{where}.timeout_s")


def _validate_governance_schema(executables: dict[str, Any]) -> None:
    """Walk every executable + subcommand and schema-validate their governance fields."""
    if not isinstance(executables, dict):
        raise AllowlistError("executables must be a mapping")
    for exe, entry in executables.items():
        if not isinstance(entry, dict):
            raise AllowlistError(f"executable {exe!r} must be a mapping")
        _validate_one_governance_block(entry, f"executables.{exe}")
        subs = entry.get("subcommands")
        if subs is not None:
            if not isinstance(subs, dict):
                raise AllowlistError(f"executables.{exe}.subcommands must be a mapping")
            for sub, sub_entry in subs.items():
                if not isinstance(sub_entry, dict):
                    raise AllowlistError(f"executables.{exe}.subcommands.{sub} must be a mapping")
                _validate_one_governance_block(sub_entry, f"executables.{exe}.subcommands.{sub}")


def _validate_one_positional_list(positionals: Any, where: str) -> None:
    """Enforce the invariant the positional walker (``_walk``) relies on: a ``repeated`` positional
    spec (nargs='+'/'*') is the LAST spec in its list. The walker keeps a ``repeated`` spec on the
    stack so it matches every following token; a ``repeated`` spec placed BEFORE another positional
    would therefore silently swallow the following positional's tokens. Catch that LOUDLY at LOAD
    time (startup/test) rather than mis-parsing a real command. A non-list ``positionals`` is left
    to the walker (which treats it as no positionals); we only police the repeated-ordering rule."""
    if not isinstance(positionals, list):
        return
    for idx, spec in enumerate(positionals):
        if isinstance(spec, dict) and spec.get("repeated") and idx != len(positionals) - 1:
            raise AllowlistError(
                f"{where}: a `repeated` positional spec must be LAST (it consumes all following "
                f"tokens), but one appears at index {idx} of {len(positionals)}"
            )


def _validate_positionals_schema(executables: dict[str, Any]) -> None:
    """Walk every positional list in the policy — flat executables, subcommands, and NESTED
    subcommands (the recursion ``_walk_subcommand`` supports) — and enforce the repeated-last
    invariant on each. No per-command knowledge: it police the SHAPE uniformly."""
    def _walk_entry(entry: dict[str, Any], where: str) -> None:
        _validate_one_positional_list(entry.get("positionals"), f"{where}.positionals")
        subs = entry.get("subcommands")
        if isinstance(subs, dict):
            for sub, sub_entry in subs.items():
                if isinstance(sub_entry, dict):
                    _walk_entry(sub_entry, f"{where}.subcommands.{sub}")

    for exe, entry in executables.items():
        if isinstance(entry, dict):
            _walk_entry(entry, f"executables.{exe}")


class Allowlist:
    """Loads the policy once and validates argv lists against it."""

    def __init__(self, policy: dict[str, Any]):
        self._executables: dict[str, Any] = policy.get("executables", {})
        self._value_constraints: dict[str, Any] = policy.get("value_constraints", {})
        # Schema-validate the governance fields (timeout_s) AT STARTUP so a
        # malformed allowlist fails loudly here instead of mis-enforcing at run time.
        _validate_governance_schema(self._executables)
        # Enforce the positional walker's `repeated`-must-be-last invariant at LOAD time, so a
        # future allowlist edit putting a repeated spec before another positional fails loudly here
        # rather than silently swallowing the following positional's tokens in the hot _walk path.
        _validate_positionals_schema(self._executables)

    # ---- construction -----------------------------------------------------
    @classmethod
    def from_file(cls, path: str | Path) -> Allowlist:
        try:
            data = yaml.safe_load(Path(path).read_text())
        except yaml.YAMLError as exc:
            raise AllowlistError(f"allowlist policy at {path} is not valid YAML: {exc}") from exc
        if not isinstance(data, dict) or "executables" not in data:
            raise AllowlistError(f"malformed allowlist policy at {path}")
        return cls(data)

    # ---- public API -------------------------------------------------------
    def executable(self, exe: str) -> dict[str, Any] | None:
        """Return the raw policy entry for an executable (runner hints etc.)."""
        return self._executables.get(exe)

    def validate(
        self,
        argv: list[str],
        *,
        catalog: dict[str, list[str]] | None = None,
    ) -> Decision:
        """Validate a logical argv. ``catalog`` maps 'specs'/'harnesses'/'workloads'
        to the live on-disk names; when provided, ``ref_catalog`` values must be
        members. When omitted, ref_catalog values are charset-checked only."""
        if not argv:
            return _deny(argv, "empty command")

        # Blanket metacharacter screen.
        for tok in argv:
            bad = _DANGEROUS & set(tok)
            if bad:
                return _deny(argv, f"token {tok!r} contains disallowed character(s): {''.join(sorted(bad))}")

        exe = argv[0]
        entry = self._executables.get(exe)
        if entry is None:
            return _deny(argv, f"executable {exe!r} is not allowlisted")

        # The generic token walk is delegated to the engine, constructed per call with the
        # value-constraint table (for `ref` resolution) and the optional live catalog (for
        # `ref_catalog` membership). No per-command knowledge crosses this boundary — only the
        # parsed YAML structures and the argv tokens do.
        v = _Validator(self._value_constraints, catalog)
        rest = argv[1:]
        try:
            if entry.get("flat"):
                mode = v.walk(
                    rest,
                    flags=entry.get("flags", {}),
                    positionals=entry.get("positionals", []),
                    base_mode=entry.get("mode", MUTATING),
                )
                return self._allow(argv, mode, entry, None)

            # Executable with subcommands.
            global_flags = entry.get("global_flags", {})
            subcommands = entry.get("subcommands", {})

            sub_idx = v.find_subcommand_index(rest, global_flags)
            if sub_idx is None:
                # Allow standalone read-only global flags (e.g. `llmdbenchmark --version`).
                if v._has_read_only_trigger(rest, global_flags):
                    v.walk(rest, flags=global_flags, positionals=[], base_mode=READ_ONLY)
                    return self._allow(argv, READ_ONLY, entry, None)
                return _deny(argv, f"no subcommand provided for {exe!r}")

            subname = rest[sub_idx]
            sub = subcommands.get(subname)
            if sub is None:
                return _deny(argv, f"subcommand {subname!r} is not allowlisted for {exe!r}")

            pre = rest[:sub_idx]   # leading global flags
            post = rest[sub_idx + 1:]

            # Validate the global-flags region (flags only, no positionals).
            v.walk(pre, flags=global_flags, positionals=[], base_mode=READ_ONLY)

            # Validate the subcommand region; global flags remain acceptable here too. The pre
            # region is tagged with the flag-dict that is EFFECTIVE there (the executable's
            # global_flags) so a read_only_trigger is only honored where the flag actually takes
            # effect — see _walk's region-aware read-only detection.
            mode = v.walk_subcommand(
                subname, sub, post, global_flags=global_flags,
                pre_regions=[(pre, global_flags)],
            )
            return self._allow(argv, mode, entry, sub)
        except _Reject as exc:
            return _deny(argv, str(exc))

    def _allow(
        self,
        argv: list[str],
        mode: str,
        entry: dict[str, Any],
        sub: dict[str, Any] | None,
    ) -> Decision:
        """Build an allowed Decision and attach the governance limit (timeout) that the
        policy DATA declares for this command. A subcommand's own field overrides the
        executable's; absence means 'no limit declared' (None). No per-command Python
        knowledge — the value is read straight out of the matched YAML entries."""
        timeout_s = None
        # Subcommand-level field takes precedence over the executable-level one.
        for block in (entry, sub):
            if not block:
                continue
            if block.get("timeout_s") is not None:
                timeout_s = block["timeout_s"]
        return Decision(
            allowed=True,
            mode=mode,
            reason="ok",
            argv=list(argv),
            timeout_s=timeout_s,
        )
