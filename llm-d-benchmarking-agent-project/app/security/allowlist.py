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
    # --- governance, sourced PURELY from security/allowlist.yaml (Phase 13) ---
    # The per-command execution deadline (seconds) declared in the policy data, or None
    # when the command carries no `timeout_s` (the runner then applies its global default).
    # This is the ONE place timeouts come from — there is no Python per-command table.
    timeout_s: int | None = None
    # The usage-quota CAPS (data) for this command, or None when uncapped. The mechanism
    # (a per-session / per-day counter) lives in ToolContext; only the LIMIT is here.
    # ``quota_key`` is the stable identity a counter increments against (the
    # executable[+subcommand]); the caps are the integer ceilings from the YAML.
    quota_key: str | None = None
    quota_per_session: int | None = None
    quota_per_day: int | None = None

    @property
    def requires_approval(self) -> bool:
        return self.allowed and self.mode == MUTATING


def _deny(argv: list[str], reason: str) -> Decision:
    return Decision(allowed=False, mode=MUTATING, reason=reason, argv=list(argv))


class AllowlistError(RuntimeError):
    pass


# ----------------------------------------------------------------------------
# Governance fields (Phase 13): per-command timeouts + usage quotas live in the
# YAML as DATA. The two helpers below are the only things that read those fields —
# one validates their SHAPE at load, the other extracts them for a Decision. No
# per-command knowledge: both operate uniformly over whatever the policy declares.
# ----------------------------------------------------------------------------
_QUOTA_KEYS = ("per_session", "per_day")


def _check_positive_int(value: Any, where: str) -> None:
    # bool is an int subclass — reject it explicitly so `timeout_s: true` can't slip through.
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AllowlistError(f"{where} must be a positive integer, got {value!r}")


def _validate_one_governance_block(block: dict[str, Any], where: str) -> None:
    """Validate the optional ``timeout_s`` / ``quota`` fields on a single executable or
    subcommand entry. Raises :class:`AllowlistError` on any malformed value."""
    if "timeout_s" in block:
        _check_positive_int(block["timeout_s"], f"{where}.timeout_s")
    if "quota" in block:
        quota = block["quota"]
        if not isinstance(quota, dict):
            raise AllowlistError(f"{where}.quota must be a mapping, got {type(quota).__name__}")
        unknown = set(quota) - set(_QUOTA_KEYS)
        if unknown:
            raise AllowlistError(
                f"{where}.quota has unknown key(s) {sorted(unknown)}; allowed: {list(_QUOTA_KEYS)}"
            )
        if not quota:
            raise AllowlistError(f"{where}.quota must declare at least one of {list(_QUOTA_KEYS)}")
        for k in _QUOTA_KEYS:
            if k in quota:
                _check_positive_int(quota[k], f"{where}.quota.{k}")


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


class Allowlist:
    """Loads the policy once and validates argv lists against it."""

    def __init__(self, policy: dict[str, Any]):
        self._policy = policy
        self._executables: dict[str, Any] = policy.get("executables", {})
        self._value_constraints: dict[str, Any] = policy.get("value_constraints", {})
        # Schema-validate the governance fields (timeout_s / quota) AT STARTUP so a
        # malformed allowlist fails loudly here instead of mis-enforcing at run time.
        _validate_governance_schema(self._executables)

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
                return self._allow(argv, mode, exe, entry, None, None)

            # Executable with subcommands.
            global_flags = entry.get("global_flags", {})
            subcommands = entry.get("subcommands", {})

            sub_idx = self._find_subcommand_index(rest, global_flags)
            if sub_idx is None:
                # Allow standalone read-only global flags (e.g. `llmdbenchmark --version`).
                if self._has_read_only_trigger(rest, global_flags):
                    self._walk(rest, flags=global_flags, positionals=[], base_mode=READ_ONLY, catalog=catalog)
                    return self._allow(argv, READ_ONLY, exe, entry, None, None)
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
            return self._allow(argv, mode, exe, entry, subname, sub)
        except _Reject as exc:
            return _deny(argv, str(exc))

    def _allow(
        self,
        argv: list[str],
        mode: str,
        exe: str,
        entry: dict[str, Any],
        subname: str | None,
        sub: dict[str, Any] | None,
    ) -> Decision:
        """Build an allowed Decision and attach the governance limits (timeout + quota)
        that the policy DATA declares for this command. A subcommand's own field overrides
        the executable's; absence means 'no limit declared' (None). No per-command Python
        knowledge — the values are read straight out of the matched YAML entries."""
        timeout_s = None
        per_session = per_day = None
        # Subcommand-level fields take precedence over the executable-level ones.
        for block in (entry, sub):
            if not block:
                continue
            if block.get("timeout_s") is not None:
                timeout_s = block["timeout_s"]
            quota = block.get("quota")
            if isinstance(quota, dict):
                if quota.get("per_session") is not None:
                    per_session = quota["per_session"]
                if quota.get("per_day") is not None:
                    per_day = quota["per_day"]
        quota_key = f"{exe}:{subname}" if subname else exe
        has_quota = per_session is not None or per_day is not None
        return Decision(
            allowed=True,
            mode=mode,
            reason="ok",
            argv=list(argv),
            timeout_s=timeout_s,
            quota_key=quota_key if has_quota else None,
            quota_per_session=per_session,
            quota_per_day=per_day,
        )

    # ---- internals --------------------------------------------------------
    def _find_subcommand_index(self, tokens: list[str], global_flags: dict) -> int | None:
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if tok.startswith("-"):
                spec = global_flags.get(tok)
                if spec is None:
                    # Unknown global flag: accepted (policy allows any flag once the
                    # executable is allowlisted). Treat as boolean so we don't swallow
                    # what might be the subcommand token.
                    i += 1
                    continue
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
                    # Policy: any flag is accepted once the executable + subcommand are
                    # allowlisted. The unknown flag's arity is unknown, so greedily consume
                    # a following non-option token as its value (handles `-l inference-perf`);
                    # `--flag=value` is self-contained. Every token is already metachar-screened
                    # in validate(), and unknown flags never downgrade the mode — so mutating
                    # commands keep their approval gate. (Unknown-flag VALUES are not checked
                    # against value_constraints; known flags still are.)
                    if "=" not in tok and i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
                        i += 2
                    else:
                        i += 1
                    continue
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
