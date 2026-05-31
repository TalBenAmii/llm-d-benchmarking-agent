"""Deny-by-default command allowlist validator.

This module is a *pure validator* over ``security/allowlist.yaml``. It contains no
per-command knowledge: every rule lives in the YAML. Given a logical argv list
(e.g. ``["llmdbenchmark", "--spec", "cicd/kind", "standup", "-p", "ns"]``) it returns
a :class:`Decision` saying whether the command is permitted and whether it is
``read_only`` (auto-runnable) or ``mutating`` (requires user approval).

It never runs anything — see ``app/security/runner.py`` for execution.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Tokens we generate never need shell metacharacters. We reject them on every token
# as defense in depth, even though the runner uses shell=False (no shell to inject).
_DANGEROUS = set(";|&$><`\n\r\t\0\\!*?(){}[]'\"")

READ_ONLY = "read_only"
MUTATING = "mutating"


@dataclass
class Decision:
    allowed: bool
    mode: str = MUTATING  # conservative default
    reason: str = ""
    argv: list[str] = field(default_factory=list)

    @property
    def requires_approval(self) -> bool:
        return self.allowed and self.mode == MUTATING


def _deny(argv: list[str], reason: str) -> Decision:
    return Decision(allowed=False, mode=MUTATING, reason=reason, argv=list(argv))


class AllowlistError(RuntimeError):
    pass


class Allowlist:
    """Loads the policy once and validates argv lists against it."""

    def __init__(self, policy: dict[str, Any]):
        self._policy = policy
        self._executables: dict[str, Any] = policy.get("executables", {})
        self._value_constraints: dict[str, Any] = policy.get("value_constraints", {})

    # ---- construction -----------------------------------------------------
    @classmethod
    def from_file(cls, path: str | Path) -> "Allowlist":
        data = yaml.safe_load(Path(path).read_text())
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

        rest = argv[1:]
        try:
            if entry.get("flat"):
                mode = self._walk(
                    rest,
                    flags=entry.get("flags", {}),
                    positionals=entry.get("positionals", []),
                    base_mode=entry.get("mode", MUTATING),
                    catalog=catalog,
                )
                return Decision(allowed=True, mode=mode, reason="ok", argv=list(argv))

            # Executable with subcommands.
            global_flags = entry.get("global_flags", {})
            subcommands = entry.get("subcommands", {})

            sub_idx = self._find_subcommand_index(rest, global_flags)
            if sub_idx is None:
                # Allow standalone read-only global flags (e.g. `llmdbenchmark --version`).
                if self._has_read_only_trigger(rest, global_flags):
                    self._walk(rest, flags=global_flags, positionals=[], base_mode=READ_ONLY, catalog=catalog)
                    return Decision(allowed=True, mode=READ_ONLY, reason="ok", argv=list(argv))
                return _deny(argv, f"no subcommand provided for {exe!r}")

            subname = rest[sub_idx]
            sub = subcommands.get(subname)
            if sub is None:
                return _deny(argv, f"subcommand {subname!r} is not allowlisted for {exe!r}")

            pre = rest[:sub_idx]   # leading global flags
            post = rest[sub_idx + 1:]

            # Validate the global-flags region (flags only, no positionals).
            self._walk(pre, flags=global_flags, positionals=[], base_mode=READ_ONLY, catalog=catalog)

            # Validate the subcommand region; global flags remain acceptable here too.
            merged_flags = {**global_flags, **sub.get("flags", {})}
            mode = self._walk(
                post,
                flags=merged_flags,
                positionals=sub.get("positionals", []),
                base_mode=sub.get("mode", MUTATING),
                catalog=catalog,
                # read_only_triggers from the global region matter too:
                pre_tokens=pre,
            )
            return Decision(allowed=True, mode=mode, reason="ok", argv=list(argv))
        except _Reject as exc:
            return _deny(argv, str(exc))

    # ---- internals --------------------------------------------------------
    def _find_subcommand_index(self, tokens: list[str], global_flags: dict) -> int | None:
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if tok.startswith("-"):
                spec = global_flags.get(tok)
                if spec is None:
                    raise _Reject(f"unknown global flag {tok!r}")
                i += 2 if spec.get("takes_value") else 1
                continue
            return i
        return None

    def _walk(
        self,
        tokens: list[str],
        *,
        flags: dict,
        positionals: list,
        base_mode: str,
        catalog: dict | None,
        pre_tokens: list[str] | None = None,
    ) -> str:
        """Walk a token region. Returns the effective mode. Raises _Reject on any
        unrecognized flag/positional or bad value."""
        read_only_triggered = self._has_read_only_trigger(pre_tokens or [], flags)
        pos_specs = list(positionals)
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if tok.startswith("-"):
                spec = flags.get(tok)
                if spec is None:
                    raise _Reject(f"flag {tok!r} is not allowlisted here")
                if spec.get("read_only_trigger"):
                    read_only_triggered = True
                if spec.get("takes_value"):
                    if i + 1 >= len(tokens):
                        raise _Reject(f"flag {tok!r} expects a value")
                    self._check_value(tokens[i + 1], spec.get("value"), catalog, ctx=tok)
                    i += 2
                else:
                    i += 1
                continue
            # positional
            if not pos_specs:
                raise _Reject(f"unexpected positional argument {tok!r}")
            pspec = pos_specs.pop(0)
            self._check_value(tok, pspec.get("value"), catalog, ctx="positional")
            i += 1

        if pos_specs:
            raise _Reject(f"missing required positional argument(s): {len(pos_specs)}")

        return READ_ONLY if read_only_triggered else base_mode

    @staticmethod
    def _has_read_only_trigger(tokens: list[str], flags: dict) -> bool:
        return any(flags.get(t, {}).get("read_only_trigger") for t in tokens if t.startswith("-"))

    def _check_value(self, value: str, constraint: Any, catalog: dict | None, *, ctx: str) -> None:
        if constraint is None:
            return  # any value (already metachar-screened)
        constraint = self._resolve(constraint)
        if "enum" in constraint:
            if value not in constraint["enum"]:
                raise _Reject(f"value {value!r} for {ctx} not in {constraint['enum']}")
            return
        if "regex" in constraint:
            if not re.fullmatch(constraint["regex"], value):
                raise _Reject(f"value {value!r} for {ctx} does not match required pattern")
            return
        if "ref_catalog" in constraint:
            kind = constraint["ref_catalog"]
            if catalog is None:
                # Cannot verify membership without a catalog; charset already screened.
                return
            allowed = catalog.get(kind, [])
            # Workload values may be given as 'name' or 'name.yaml'; normalize.
            candidates = {value, value.removesuffix(".yaml"), f"{value}.yaml"}
            if not (candidates & set(allowed)):
                raise _Reject(f"value {value!r} is not in the live {kind} catalog")
            return
        raise _Reject(f"unrecognized constraint for {ctx}: {constraint!r}")

    def _resolve(self, constraint: Any, _depth: int = 0) -> dict:
        if _depth > 8:
            raise _Reject("constraint ref nesting too deep")
        if isinstance(constraint, dict) and "ref" in constraint:
            name = constraint["ref"]
            target = self._value_constraints.get(name)
            if target is None:
                raise _Reject(f"unknown value_constraints ref {name!r}")
            return self._resolve(target, _depth + 1)
        return constraint


class _Reject(Exception):
    """Internal control-flow exception used to short-circuit a denial."""
