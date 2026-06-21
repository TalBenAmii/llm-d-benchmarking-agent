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
        self._policy = policy
        self._executables: dict[str, Any] = policy.get("executables", {})
        self._value_constraints: dict[str, Any] = policy.get("value_constraints", {})
        # Schema-validate the governance fields (timeout_s / quota) AT STARTUP so a
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

            # Validate the subcommand region; global flags remain acceptable here too. The pre
            # region is tagged with the flag-dict that is EFFECTIVE there (the executable's
            # global_flags) so a read_only_trigger is only honored where the flag actually takes
            # effect — see _walk's region-aware read-only detection.
            mode = self._walk_subcommand(
                subname, sub, post, global_flags=global_flags,
                pre_regions=[(pre, global_flags)], catalog=catalog,
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
    def _walk_subcommand(
        self,
        subname: str,
        sub: dict[str, Any],
        tokens: list[str],
        *,
        global_flags: dict,
        pre_regions: list[tuple[list[str], dict]],
        catalog: dict | None,
    ) -> str:
        """Validate the region AFTER a matched subcommand token and return the effective mode.

        Most subcommands are a single level: ``tokens`` is walked against the subcommand's own
        flags (merged with the still-acceptable global flags) and positionals. But a subcommand
        may itself declare ``subcommands:`` — a NESTED command group (e.g. ``llmdbenchmark
        results <store-command>`` whose ``init``/``status``/``remote``/``add``/``push``/``pull``
        each have their own mode + positionals, exactly mirroring the upstream argparse
        sub-subparsers). In that case the FIRST positional after this subcommand selects the
        nested subcommand and we recurse one level deeper with the same uniform rules. This is
        PURE MECHANISM driven entirely by the YAML shape: a subcommand with no ``subcommands``
        key is single-level (every pre-existing entry), so this is strictly additive. The mode
        of the DEEPEST matched (leaf) subcommand wins, while any ``read_only_trigger`` flag at
        any level still downgrades to read-only and global flags stay acceptable throughout.

        ``pre_regions`` is the list of leading-flag regions BEFORE this subcommand token, each
        paired with the flag-dict that was EFFECTIVE in that region. A ``read_only_trigger`` in a
        pre-region is honored ONLY if the flag is a trigger in *that region's own* flag-dict — so
        a subcommand-OWN trigger (e.g. ``standup``'s ``-n``/``--dry-run``) sitting in the GLOBAL
        pre-region (where only ``global_flags`` are effective) does NOT downgrade the command. This
        closes an approval-gate bypass: upstream registers ``-n``/``--dry-run`` on BOTH the
        top-level parser and each subparser, and whether a global-position ``-n`` actually takes
        effect depends on an upstream ``default=argparse.SUPPRESS`` detail we don't control — so a
        security gate must NOT treat a flag as a dry-run in a region where its effect is not
        guaranteed. The INTENTIONAL nested propagation is preserved because each intermediate
        region carries the flag-dict (``merged_flags``) effective at THAT level."""
        merged_flags = {**global_flags, **sub.get("flags", {})}
        nested = sub.get("subcommands")
        if nested:
            # A nested command group: the next positional is the nested subcommand token. Flags
            # belonging to THIS level (and globals) may precede it; we reuse the same scanner that
            # finds the top-level subcommand so a leading flag never gets mistaken for the token.
            idx = self._find_subcommand_index(tokens, merged_flags)
            if idx is None:
                raise _Reject(f"no subcommand provided for {subname!r}")
            nested_name = tokens[idx]
            nested_sub = nested.get(nested_name)
            if nested_sub is None:
                raise _Reject(f"subcommand {nested_name!r} is not allowlisted for {subname!r}")
            pre = tokens[:idx]
            post = tokens[idx + 1:]
            # Validate this level's leading flags (flags only — no positionals before the nested
            # token); read_only_triggers among them propagate down via pre_regions, tagged with
            # THIS level's effective flags (merged_flags) so an intermediate-level trigger still
            # counts — that is the intentional nested propagation.
            self._walk(pre, flags=merged_flags, positionals=[], base_mode=READ_ONLY, catalog=catalog)
            return self._walk_subcommand(
                nested_name, nested_sub, post,
                global_flags=merged_flags,
                pre_regions=[*pre_regions, (pre, merged_flags)], catalog=catalog,
            )
        return self._walk(
            tokens,
            flags=merged_flags,
            positionals=sub.get("positionals", []),
            base_mode=sub.get("mode", MUTATING),
            catalog=catalog,
            # read_only_triggers from the outer region(s) matter too — but each is judged against
            # the flags that were EFFECTIVE where it appeared (see pre_regions above).
            pre_regions=pre_regions,
        )

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
        pre_regions: list[tuple[list[str], dict]] | None = None,
    ) -> str:
        """Walk a token region. Returns the effective mode. Raises _Reject on any
        unrecognized flag/positional or bad value.

        ``pre_regions`` carries the leading-flag regions from OUTER levels, each paired with the
        flag-dict effective there; a read_only_trigger in a pre-region is honored only against its
        OWN region's flags (so a subcommand-own trigger in the global pre-region is NOT honored)."""
        read_only_triggered = any(
            self._has_read_only_trigger(tokens_, flags_) for tokens_, flags_ in (pre_regions or [])
        )
        pos_specs = list(positionals)
        repeated_matched = False  # did a head `repeated` spec consume at least one token?
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
            pspec = pos_specs[0]
            self._check_value(tok, pspec.get("value"), catalog, ctx="positional")
            # A posspec marked `repeated: true` (DATA) is the LAST spec and matches one-or-more
            # remaining positionals against the same constraint (e.g. the Results Store
            # `add <paths...>` / `pull <remotes...>`, mapping to argparse nargs='+'/'*'). It stays
            # on the stack so every following positional is validated against it. A normal spec is
            # consumed once. PURE MECHANISM — the variable arity is the YAML's, not Python's.
            if pspec.get("repeated"):
                repeated_matched = True
            else:
                pos_specs.pop(0)
            i += 1

        # Any positional specs still unconsumed are acceptable only if they need not appear: a
        # spec marked `optional: true` (zero-or-one); a `repeated` spec is zero-or-more when also
        # marked `optional` (nargs='*'), otherwise one-or-more (nargs='+') and so still REQUIRED
        # unless it already matched at least one token. This lets a subcommand take a VARIABLE
        # number of positionals (e.g. `push [remote] [path]`, `add <paths...>`) without
        # per-command Python. A required, unmatched spec is rejected as before. PURE MECHANISM
        # from the YAML shape.
        leftover_required = [
            p for p in pos_specs
            if not p.get("optional") and not (p.get("repeated") and repeated_matched)
        ]
        if leftover_required:
            raise _Reject(f"missing required positional argument(s): {len(leftover_required)}")

        return READ_ONLY if read_only_triggered else base_mode

    @staticmethod
    def _has_read_only_trigger(tokens: list[str], flags: dict) -> bool:
        return any(flags.get(t, {}).get("read_only_trigger") for t in tokens if t.startswith("-"))

    def _check_value(self, value: str, constraint: Any, catalog: dict | None, *, ctx: str) -> None:
        if constraint is None:
            return  # any value (already metachar-screened)
        constraint = self._resolve(constraint)
        # Alternation: the value satisfies the flag if it satisfies ANY listed sub-constraint.
        # Pure DATA — used so e.g. `--spec` can be EITHER a live-catalog name OR a
        # workspace-confined authored spec path, without baking either rule into Python.
        if "any_of" in constraint:
            alternatives = constraint["any_of"]
            errs: list[str] = []
            for alt in alternatives:
                try:
                    self._check_value(value, alt, catalog, ctx=ctx)
                    return
                except _Reject as exc:
                    errs.append(str(exc))
            raise _Reject(
                f"value {value!r} for {ctx} matched no allowed form ({'; '.join(errs)})"
            )
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
