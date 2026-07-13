"""Generic token-walk engine for the command policy (private to ``app.security``).

This is the *validator engine* extracted from :mod:`app.security.policy`: the pure,
per-command-knowledge-free machinery that walks a region of argv tokens against the policy
shape (executables -> subcommands -> flags -> positionals -> value_constraints) and returns
the effective mode (``read_only`` / ``mutating``), raising :class:`_Reject` on any
unrecognized flag/positional or bad value.

It contains **no** ``if exe == "..."`` / ``if sub == "..."`` branches: every rule is read out
of the parsed YAML structures it is handed. The :class:`CommandPolicy` facade in
:mod:`app.security.policy` constructs a :class:`_Validator` per ``validate()`` call (holding
the resolved value-constraint table + the optional live catalog) and delegates the token walk
to it; it then builds the public :class:`~app.security.policy.Decision` from the returned
mode plus the policy's governance fields.

Import direction is one-way to avoid a cycle: ``policy.py`` imports FROM this module; this
module imports nothing from ``policy.py``. The mode constants and the :class:`_Reject`
control-flow exception therefore live HERE and are re-exported by ``policy.py`` so the
public names (``READ_ONLY`` / ``MUTATING``) stay where external code already imports them.
"""
from __future__ import annotations

import re
from typing import Any

READ_ONLY = "read_only"
MUTATING = "mutating"


class _Reject(Exception):
    """Internal control-flow exception used to short-circuit a denial."""


class _Validator:
    """The generic token-walk engine, constructed once per ``validate()`` call.

    Holds the per-call state the walk needs — the resolved ``value_constraints`` table (for
    ``ref`` resolution) and the optional live ``catalog`` (for ``ref_catalog`` membership) — so
    the walk methods don't have to thread it through every recursive call. The methods are a
    verbatim move of the former ``CommandPolicy._walk*`` family: pure mechanism over the YAML shape,
    with zero per-command knowledge.
    """

    def __init__(self, value_constraints: dict[str, Any], catalog: dict | None):
        self._value_constraints = value_constraints
        self._catalog = catalog

    # ---- token walk -------------------------------------------------------
    def walk_subcommand(
        self,
        subname: str,
        sub: dict[str, Any],
        tokens: list[str],
        *,
        global_flags: dict,
        pre_regions: list[tuple[list[str], dict]],
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
            idx = self.find_subcommand_index(tokens, merged_flags)
            if idx is None:
                raise _Reject(f"no subcommand provided for {subname!r}")
            nested_name = tokens[idx]
            nested_sub = nested.get(nested_name)
            if nested_sub is None:
                raise _Reject(f"subcommand {nested_name!r} is not policy-allowed for {subname!r}")
            pre = tokens[:idx]
            post = tokens[idx + 1:]
            # Validate this level's leading flags (flags only — no positionals before the nested
            # token); read_only_triggers among them propagate down via pre_regions, tagged with
            # THIS level's effective flags (merged_flags) so an intermediate-level trigger still
            # counts — that is the intentional nested propagation.
            self.walk(pre, flags=merged_flags, positionals=[], base_mode=READ_ONLY)
            return self.walk_subcommand(
                nested_name, nested_sub, post,
                global_flags=merged_flags,
                pre_regions=[*pre_regions, (pre, merged_flags)],
            )
        return self.walk(
            tokens,
            flags=merged_flags,
            positionals=sub.get("positionals", []),
            base_mode=sub.get("mode", MUTATING),
            # read_only_triggers from the outer region(s) matter too — but each is judged against
            # the flags that were EFFECTIVE where it appeared (see pre_regions above).
            pre_regions=pre_regions,
        )

    def find_subcommand_index(self, tokens: list[str], global_flags: dict) -> int | None:
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if tok.startswith("-"):
                spec = global_flags.get(tok)
                if spec is None:
                    # Unknown global flag: accepted (policy allows any flag once the
                    # executable is policy-allowed). Treat as boolean so we don't swallow
                    # what might be the subcommand token.
                    i += 1
                    continue
                i += 2 if spec.get("takes_value") else 1
                continue
            return i
        return None

    def walk(
        self,
        tokens: list[str],
        *,
        flags: dict,
        positionals: list,
        base_mode: str,
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
                    # policy-allowed. The unknown flag's arity is unknown, so greedily consume
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
                    self._check_value(tokens[i + 1], spec.get("value"), ctx=tok)
                    i += 2
                else:
                    i += 1
                continue
            # positional
            if not pos_specs:
                raise _Reject(f"unexpected positional argument {tok!r}")
            pspec = pos_specs[0]
            self._check_value(tok, pspec.get("value"), ctx="positional")
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

    # ---- value constraints ------------------------------------------------
    def _check_value(self, value: str, constraint: Any, *, ctx: str) -> None:
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
                    self._check_value(value, alt, ctx=ctx)
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
            if self._catalog is None:
                # Cannot verify membership without a catalog; charset already screened.
                return
            allowed = self._catalog.get(kind, [])
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
