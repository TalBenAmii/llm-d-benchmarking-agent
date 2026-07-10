"""Read-only workload-profile inspection tools.

Two tools let a non-expert PREVIEW a workload before running it:

  * ``inspect_workload_profile`` — locate a profile on disk (under the read-only benchmark
    repo's ``workload/profiles/<harness>/``), parse its YAML, and return a NORMALIZED factual
    summary of what it sends: token shape (input/output length distributions, shared/system
    prefix reuse), load shape (rate/concurrency/QPS, sweep stages, duration), and the
    prompt/dataset source. Every normalized fact carries the raw key snippet it came from so the
    summary is auditable.

  * ``estimate_run_duration`` — REUSE that reader, then compute a clearly-labeled HEURISTIC
    wall-clock estimate from the load shape (sum of sweep-stage durations, or request-count /
    rate, …). It states its assumption and flags itself approximate; when the load fields are
    missing it says what's missing rather than fabricating a number.

THIN MECHANISM ONLY. Profile layouts differ per harness (inference-perf nests ``load.stages``;
guidellm is flat ``rate``/``max_seconds``; vllm-benchmark uses ``max-concurrency``/``num-prompts``;
aiperf uses ``concurrency``/``request-count``/``isl``/``osl``) — we normalize DEFENSIVELY and never
assume a key exists. No recommendation/verdict text is baked in here: WHICH workload to pick and
whether an estimate is "long" is the agent's judgment over knowledge/, not this code.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from app.dig import dict_or_empty as _dict
from app.tools.context import ToolContext, ToolError
from app.tools.setup.catalog import build_catalog

# Harness search order when the caller omits the harness. inference-perf is the default harness
# the agent reaches for first, so try it first; the rest are the remaining profile dirs.
_HARNESS_SEARCH_ORDER = (
    "inference-perf",
    "guidellm",
    "vllm-benchmark",
    "aiperf",
    "inferencemax",
    "nop",
)


# --- profile location + parse (shared by both tools) ---------------------------------------

def _profiles_root(ctx: ToolContext) -> Path:
    """``<bench-repo>/workload/profiles`` — the one place real profiles live (REUSES the same
    layout app/tools/setup/catalog.py discovers, so the two never drift)."""
    return ctx.settings.bench_repo / "workload" / "profiles"


def _candidate_files(root: Path, harness: str, workload: str) -> list[Path]:
    """The on-disk filenames a workload name can resolve to for one harness. The CLI takes a
    ``<name>.yaml`` name but the file on disk is usually ``<name>.yaml.in`` (a template), and a
    few profiles ship as a bare ``<name>.yaml``. We try every spelling, in a stable order."""
    base = workload.removesuffix(".in").removesuffix(".yaml")
    hdir = root / harness
    # e.g. base="sanity_random" -> sanity_random.yaml.in, sanity_random.yaml, then the raw name.
    names = [f"{base}.yaml.in", f"{base}.yaml", workload, f"{workload}.in"]
    seen: set[str] = set()
    out: list[Path] = []
    for n in names:
        if n in seen:
            continue
        seen.add(n)
        out.append(hdir / n)
    return out


def _read_profile(
    ctx: ToolContext, *, workload: str, harness: str | None,
) -> dict[str, Any]:
    """Locate + parse a workload profile, returning ``{harness, path, raw}`` where ``raw`` is the
    parsed YAML mapping. The SHARED core both tools call — neither duplicates the search/parse.

    Raises ToolError with a clear, enumerated "not found" message (the names that DO exist for the
    searched harness(es)) so the agent can correct the workload name. Mechanism only — no judgment.
    """
    root = _profiles_root(ctx)
    if not root.is_dir():
        raise ToolError(
            f"workload profiles dir not found at {root} — the benchmark repo is absent or empty; "
            "clone it (ensure_repos) first."
        )

    harnesses: list[str]
    if harness:
        harnesses = [harness]
    else:
        # Search inference-perf first, then the rest; include any extra dirs on disk we don't
        # know about (defensive — the repo may add a harness).
        on_disk = sorted(d.name for d in root.iterdir() if d.is_dir())
        ordered = [h for h in _HARNESS_SEARCH_ORDER if h in on_disk]
        harnesses = ordered + [h for h in on_disk if h not in ordered]

    for h in harnesses:
        for candidate in _candidate_files(root, h, workload):
            if candidate.is_file():
                try:
                    data = yaml.safe_load(candidate.read_text(errors="replace"))
                except yaml.YAMLError as exc:
                    raise ToolError(f"{candidate} is not valid YAML: {exc}") from exc
                if not isinstance(data, dict):
                    raise ToolError(f"{candidate} did not parse into a YAML mapping")
                return {"harness": h, "path": str(candidate), "raw": data}

    # Not found — enumerate what DOES exist for the searched harness(es), via the catalog so the
    # listed names are exactly the ones the agent would use elsewhere.
    catalog = build_catalog(ctx.settings.bench_repo)
    by_harness = catalog.get("workloads_by_harness", {})
    available: dict[str, list[str]] = {h: by_harness.get(h, []) for h in harnesses}
    scope = harness or "any harness (searched inference-perf first)"
    raise ToolError(
        f"workload profile {workload!r} not found for {scope}. "
        f"Available profiles: {available}"
    )


# --- normalization helpers (pure; no judgment) ---------------------------------------------
# ``_dict`` = ``dig.dict_or_empty`` (imported as ``_dict``): coerce a possibly-absent/oddly-shaped
# profile field to a mapping so callers can ``.get(...)`` defensively without re-asserting the type.

def _num(value: Any) -> float | int | None:
    """A YAML scalar coerced to a number, or None for anything non-numeric (templated
    REPLACE_ENV_* placeholders, null, strings). Booleans are NOT numbers here."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    return None


def _length_distribution(dist: Any) -> dict[str, Any] | None:
    """Normalize an inference-perf-style ``{min,max,mean,std_dev,total_count}`` length block to a
    common shape, keeping only the numeric fields actually present. Returns None if not a mapping."""
    if not isinstance(dist, dict):
        return None
    out: dict[str, Any] = {}
    for key in ("min", "max", "mean", "std_dev", "total_count"):
        v = _num(dist.get(key))
        if v is not None:
            out[key] = v
    return out or None


def _token_shape(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize input/output token-length shape + any shared/system-prefix reuse across the
    differing harness layouts. Records the raw key path each fact came from under ``_from``.
    Defensive: every lookup tolerates a missing/oddly-shaped key (returns what it can)."""
    shape: dict[str, Any] = {}
    sources: dict[str, str] = {}

    # inference-perf: data.input_distribution / data.output_distribution ; shared_prefix block.
    data = _dict(raw.get("data"))
    inp = _length_distribution(data.get("input_distribution"))
    if inp:
        shape["input_tokens"] = inp
        sources["input_tokens"] = "data.input_distribution"
    out = _length_distribution(data.get("output_distribution"))
    if out:
        shape["output_tokens"] = out
        sources["output_tokens"] = "data.output_distribution"
    sp = data.get("shared_prefix")
    if isinstance(sp, dict):
        prefix: dict[str, Any] = {}
        for src_key, norm_key in (
            ("num_groups", "num_groups"),
            ("num_prompts_per_group", "num_prompts_per_group"),
            ("system_prompt_len", "system_prompt_len"),
            ("question_len", "question_len"),
            ("output_len", "output_len"),
        ):
            v = _num(sp.get(src_key))
            if v is not None:
                prefix[norm_key] = v
        if prefix:
            shape["shared_prefix"] = prefix
            sources["shared_prefix"] = "data.shared_prefix"
        if "output_len" in prefix:
            shape.setdefault("output_tokens", {"mean": prefix["output_len"]})
            sources.setdefault("output_tokens", "data.shared_prefix.output_len")

    # guidellm: flat data.prompt_tokens* / output_tokens* ; data.prefix_tokens/prefix_count.
    if not shape.get("input_tokens") and isinstance(data, dict) and "prompt_tokens" in data:
        inp = {}
        for src_key, norm_key in (
            ("prompt_tokens_min", "min"), ("prompt_tokens_max", "max"),
            ("prompt_tokens", "mean"), ("prompt_tokens_stdev", "std_dev"),
        ):
            v = _num(data.get(src_key))
            if v is not None:
                inp[norm_key] = v
        if inp:
            shape["input_tokens"] = inp
            sources["input_tokens"] = "data.prompt_tokens*"
    if not shape.get("output_tokens") and isinstance(data, dict) and "output_tokens" in data:
        out = {}
        for src_key, norm_key in (
            ("output_tokens_min", "min"), ("output_tokens_max", "max"),
            ("output_tokens", "mean"), ("output_tokens_stdev", "std_dev"),
        ):
            v = _num(data.get(src_key))
            if v is not None:
                out[norm_key] = v
        if out:
            shape["output_tokens"] = out
            sources["output_tokens"] = "data.output_tokens*"
    if isinstance(data, dict) and ("prefix_tokens" in data or "prefix_count" in data):
        prefix = {}
        pt = _num(data.get("prefix_tokens"))
        pc = _num(data.get("prefix_count"))
        if pt is not None:
            prefix["system_prompt_len"] = pt
        if pc is not None:
            prefix["num_groups"] = pc
        if prefix:
            shape.setdefault("shared_prefix", prefix)
            sources.setdefault("shared_prefix", "data.prefix_tokens/prefix_count")

    # vllm-benchmark: flat random-input-len / random-output-len.
    ril = _num(raw.get("random-input-len"))
    if ril is not None and "input_tokens" not in shape:
        shape["input_tokens"] = {"mean": ril}
        sources["input_tokens"] = "random-input-len"
    rol = _num(raw.get("random-output-len"))
    if rol is not None and "output_tokens" not in shape:
        shape["output_tokens"] = {"mean": rol}
        sources["output_tokens"] = "random-output-len"

    # aiperf: flat isl (input sequence length) / osl (output sequence length).
    isl = _num(raw.get("isl"))
    if isl is not None and "input_tokens" not in shape:
        shape["input_tokens"] = {"mean": isl}
        sources["input_tokens"] = "isl"
    osl = _num(raw.get("osl"))
    if osl is not None and "output_tokens" not in shape:
        shape["output_tokens"] = {"mean": osl}
        sources["output_tokens"] = "osl"

    if sources:
        shape["_from"] = sources
    return shape


def _load_stages(load: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize an inference-perf ``load.stages`` list to ``[{rate,duration,num_requests,
    concurrency}]`` (only numeric fields kept). Defensive against a non-list / odd entries."""
    raw_stages = load.get("stages")
    if not isinstance(raw_stages, list):
        return []
    out: list[dict[str, Any]] = []
    for s in raw_stages:
        if not isinstance(s, dict):
            continue
        stage: dict[str, Any] = {}
        for src_key, norm_key in (
            ("rate", "rate"), ("duration", "duration"),
            ("num_requests", "num_requests"), ("concurrency_level", "concurrency"),
        ):
            v = _num(s.get(src_key))
            if v is not None:
                stage[norm_key] = v
        out.append(stage)
    return out


def _as_rate_list(value: Any) -> list[float | int]:
    """A guidellm ``rate`` may be a scalar or a list; normalize to a numeric list."""
    if isinstance(value, list):
        return [v for v in (_num(x) for x in value) if v is not None]
    v = _num(value)
    return [v] if v is not None else []


def _load_shape(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize the LOAD shape (request rate / concurrency / QPS, sweep stages, per-stage and
    total duration, request count) across harness layouts. Records the raw key path each fact
    came from. Pure mechanism — never assumes a key exists."""
    shape: dict[str, Any] = {}
    sources: dict[str, str] = {}

    load = raw.get("load") if isinstance(raw.get("load"), dict) else None
    if load is not None:
        ltype = load.get("type")
        if isinstance(ltype, str):
            shape["type"] = ltype
            sources["type"] = "load.type"
        stages = _load_stages(load)
        if stages:
            shape["stages"] = stages
            sources["stages"] = "load.stages"
            rates = [s["rate"] for s in stages if "rate" in s]
            if rates:
                shape["rates"] = rates
                sources["rates"] = "load.stages[].rate"
            durations = [s["duration"] for s in stages if "duration" in s]
            if durations:
                shape["total_stage_duration_s"] = sum(durations)
                sources["total_stage_duration_s"] = "sum(load.stages[].duration)"
        for src_key, norm_key in (
            ("num_workers", "num_workers"),
            ("worker_max_concurrency", "worker_max_concurrency"),
        ):
            v = _num(load.get(src_key))
            if v is not None:
                shape[norm_key] = v
                sources[norm_key] = f"load.{src_key}"

    # guidellm: flat profile / rate / max_seconds.
    if "rate" in raw and "stages" not in shape:
        rates = _as_rate_list(raw.get("rate"))
        if rates:
            shape["rates"] = rates
            sources["rates"] = "rate"
    ms = _num(raw.get("max_seconds"))
    if ms is not None:
        shape["max_seconds"] = ms
        sources["max_seconds"] = "max_seconds"
    if isinstance(raw.get("profile"), str) and "type" not in shape:
        shape["type"] = raw["profile"]
        sources["type"] = "profile"

    # vllm-benchmark: max-concurrency / num-prompts.
    mc = _num(raw.get("max-concurrency"))
    if mc is not None:
        shape["max_concurrency"] = mc
        sources["max_concurrency"] = "max-concurrency"
    npr = _num(raw.get("num-prompts"))
    if npr is not None:
        shape["num_requests"] = npr
        sources["num_requests"] = "num-prompts"

    # aiperf: concurrency / request-count.
    conc = _num(raw.get("concurrency"))
    if conc is not None and "max_concurrency" not in shape:
        shape["max_concurrency"] = conc
        sources["max_concurrency"] = "concurrency"
    rc = _num(raw.get("request-count"))
    if rc is not None and "num_requests" not in shape:
        shape["num_requests"] = rc
        sources["num_requests"] = "request-count"

    if sources:
        shape["_from"] = sources
    return shape


def _prompt_source(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize the PROMPT/DATASET source: synthetic vs a staged dataset, and whether a dataset
    file/url is required (so the agent knows a run needs a staged dataset). Records raw keys."""
    out: dict[str, Any] = {}
    sources: dict[str, str] = {}

    data = _dict(raw.get("data"))
    dtype = data.get("type")
    if isinstance(dtype, str):
        out["data_type"] = dtype
        sources["data_type"] = "data.type"

    # A staged dataset is signaled differently per harness; collect every signal we recognize.
    dataset_signals: dict[str, Any] = {}
    # vllm-benchmark: dataset-name / dataset-path. aiperf: input-file / custom-dataset-type.
    for key in ("dataset-name", "dataset-path", "input-file", "custom-dataset-type"):
        val = raw.get(key)
        if isinstance(val, str) and val:
            dataset_signals[key] = val
    if dataset_signals:
        out["dataset"] = dataset_signals
        sources["dataset"] = ", ".join(sorted(dataset_signals))

    # Does this profile REQUIRE a staged dataset file the user must provide?
    requires_dataset = False
    if isinstance(dtype, str) and dtype.lower() in {"sharegpt", "custom", "mooncake_trace"}:
        requires_dataset = True
    if any(
        isinstance(v, str) and "REPLACE_ENV_LLMDBENCH_RUN_DATASET" in v
        for v in dataset_signals.values()
    ):
        requires_dataset = True
    if str(raw.get("dataset-name", "")).lower() == "custom":
        requires_dataset = True
    out["requires_staged_dataset"] = requires_dataset

    if sources:
        out["_from"] = sources
    return out


# --- tool 1: inspect_workload_profile ------------------------------------------------------

def inspect_workload_profile(
    ctx: ToolContext, *, workload: str, harness: str | None = None,
) -> dict[str, Any]:
    """Locate + parse a workload profile on disk and return a NORMALIZED factual summary of what
    it sends (token shape, load shape, prompt/dataset source) so a non-expert can preview a
    workload BEFORE running it. Read-only; auto-runs. FACTS ONLY — no judgment.

    Raises ToolError (turned into a clean ``{"error": ...}`` by the loop) with the names that DO
    exist when the workload can't be found."""
    found = _read_profile(ctx, workload=workload, harness=harness)
    raw = found["raw"]
    harness_name = found["harness"]
    return {
        "workload": workload,
        "harness": harness_name,
        "path": found["path"],
        "token_shape": _token_shape(raw),
        "load_shape": _load_shape(raw),
        "prompt_source": _prompt_source(raw),
    }


# --- tool 2: estimate_run_duration ---------------------------------------------------------

def estimate_run_duration(
    ctx: ToolContext, *, workload: str, harness: str | None = None,
) -> dict[str, Any]:
    """Compute a rough, clearly-labeled HEURISTIC wall-clock estimate for one workload profile by
    READING the same profile (shares ``_read_profile`` with inspect_workload_profile). Read-only;
    auto-runs. The arithmetic + its stated assumption is all that lives here — no judgment about
    whether the duration is acceptable (that is the agent's, over knowledge/).

    Estimation precedence (first that applies):
      1. inference-perf sweep stages: sum of per-stage ``duration`` seconds.
      2. guidellm: ``max_seconds`` per rate stage  ×  number of rates.
      3. request-count / mean-rate: ``num_requests / rate`` seconds (closed-loop concurrency is
         only an upper bound on parallelism, so this is a coarse lower bound on time).
    If none of these fields is present, returns ``estimable=False`` and SAYS WHAT'S MISSING.
    """
    found = _read_profile(ctx, workload=workload, harness=harness)
    raw = found["raw"]
    harness_name = found["harness"]
    load = _load_shape(raw)

    base = {
        "workload": workload,
        "harness": harness_name,
        "path": found["path"],
        "approximate": True,
        "load_shape": load,
    }

    # 1) Sum of explicit sweep-stage durations (inference-perf).
    if "total_stage_duration_s" in load:
        secs = load["total_stage_duration_s"]
        return {
            **base,
            "estimable": True,
            "estimated_seconds": secs,
            "estimated_minutes": round(secs / 60.0, 1),
            "basis": "sum of per-stage durations (load.stages[].duration)",
            "assumption": "wall-clock ≈ the sum of each sweep stage's configured duration; "
                          "excludes standup/warmup/teardown and any inter-stage settle time.",
        }

    # 2) guidellm: max_seconds capped per rate stage.
    rates = load.get("rates")
    if "max_seconds" in load and isinstance(rates, list) and rates:
        per = load["max_seconds"]
        n = len(rates)
        secs = per * n
        return {
            **base,
            "estimable": True,
            "estimated_seconds": secs,
            "estimated_minutes": round(secs / 60.0, 1),
            "basis": f"max_seconds ({per}s) × number of rate stages ({n})",
            "assumption": "each of the rate stages runs up to max_seconds; actual time may be "
                          "shorter if a stage exhausts its samples first. Excludes "
                          "standup/warmup/teardown.",
        }

    # 3) request-count / mean rate.
    num_requests = load.get("num_requests")
    if isinstance(num_requests, (int, float)) and isinstance(rates, list) and rates:
        mean_rate = sum(rates) / len(rates)
        if mean_rate > 0:
            secs = num_requests / mean_rate
            return {
                **base,
                "estimable": True,
                "estimated_seconds": round(secs, 1),
                "estimated_minutes": round(secs / 60.0, 1),
                "basis": f"num_requests ({num_requests}) / mean request rate ({mean_rate:g}/s)",
                "assumption": "open-loop arrival at the mean configured rate; a coarse LOWER "
                              "bound (a saturated server serves slower than the offered rate). "
                              "Excludes standup/warmup/teardown.",
            }

    # Couldn't estimate — say exactly what's missing rather than fabricate a number.
    missing = []
    if "total_stage_duration_s" not in load and "max_seconds" not in load:
        missing.append("a per-stage duration (load.stages[].duration) or max_seconds")
    if not rates:
        missing.append("a request rate (load.stages[].rate or rate)")
    if num_requests is None:
        missing.append("a request count (num-prompts / request-count)")
    return {
        **base,
        "estimable": False,
        "reason": "the profile has no duration/rate/request-count fields to estimate from",
        "missing": missing or ["any explicit duration, rate, or request-count field"],
    }
