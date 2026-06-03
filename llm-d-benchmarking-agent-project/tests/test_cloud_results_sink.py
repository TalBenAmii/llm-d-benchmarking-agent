"""Phase 39 — cloud results sink for the run flag (-r gs://, s3://).

Hermetic, no cluster / GPU / network / real upload. Covers the MECHANISM this phase adds
(the "do you have a bucket?" / default-stays-local JUDGMENT lives in
knowledge/cloud_results_sink.md, not in Python):

  * build_argv emits ``-r <output>`` verbatim, so an explicit gs://.../s3://... destination
    passes straight through, while an UNSET output still defaults to ``local`` for a run;
  * the allowlist permits the cloud scheme on ``run``'s ``-r/--output`` ONLY via the dedicated
    ``results_sink`` constraint (OPT-IN), while a local path is still accepted as the default;
  * the cloud scheme is NOT silently widened elsewhere: --workspace/-e (output_dir) and the
    ``experiment`` subcommand's -r STILL reject gs://.../s3://... (cloud stays run-only, MVP);
  * the knowledge guide + tool/schema descriptions point the agent at the judgment.
"""
from __future__ import annotations

import pytest

from app.config import Settings
from app.security.allowlist import MUTATING, READ_ONLY, Allowlist
from app.tools.context import ToolContext
from app.tools.execute import build_argv, execute_llmdbenchmark
from app.tools.schemas import ExecuteInput
from tests.flows.catalog_snapshot import frozen_catalog
from tests.flows.harness import CaptureRunner

GS_URI = "gs://my-bucket/benchmarks/run1"
S3_URI = "s3://my-bucket/benchmarks/run1"

# ---------------------------------------------------------------------------
# build_argv — emits -r <destination> verbatim (PURE MECHANISM)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("uri", [GS_URI, S3_URI])
def test_build_argv_emits_cloud_sink_on_run(uri):
    argv = build_argv("run", spec="cicd/kind", flags={"output": uri})
    assert "-r" in argv
    # -r is immediately followed by the exact bucket URI the agent chose (verbatim).
    assert argv[argv.index("-r") + 1] == uri


def test_build_argv_emits_local_when_chosen():
    argv = build_argv("run", spec="cicd/kind", flags={"output": "local"})
    assert argv[argv.index("-r") + 1] == "local"


def test_build_argv_omits_r_when_output_unset():
    # build_argv itself emits nothing for an unset output; the LOCAL default is applied by
    # execute_llmdbenchmark (see test below), never a silent cloud destination.
    argv = build_argv("run", spec="cicd/kind", flags={})
    assert "-r" not in argv


def test_cloud_sink_does_not_disturb_other_flags():
    argv = build_argv(
        "run", spec="cicd/kind", harness="vllm-benchmark", workload="sanity_random.yaml",
        flags={"output": GS_URI, "monitoring": True},
    )
    assert argv[:3] == ["llmdbenchmark", "--spec", "cicd/kind"]
    assert argv[argv.index("-r") + 1] == GS_URI
    assert "--monitoring" in argv
    assert "-l" in argv and "vllm-benchmark" in argv


# ---------------------------------------------------------------------------
# execute_llmdbenchmark — LOCAL stays the default (ACCEPTANCE: default local)
# ---------------------------------------------------------------------------


async def _approve_all(kind, payload):
    return True


def _ctx(tmp_path):
    settings = Settings(_env_file=None, repos_dir=tmp_path / "repos", workspace_dir=tmp_path / "ws")
    runner = CaptureRunner(settings.repo_paths)
    ctx = ToolContext(
        settings=settings,
        allowlist=Allowlist.from_file(settings.allowlist_path),
        runner=runner,
        workspace=tmp_path / "ws",
        request_approval=_approve_all,
    )
    frozen = frozen_catalog()
    ctx._catalog = frozen
    ctx.catalog = lambda *, refresh=False: frozen
    return ctx, runner


def _last_run_call(runner: CaptureRunner):
    return next(c for c in reversed(runner.calls) if c["argv"][:1] == ["llmdbenchmark"])


async def test_run_defaults_to_local_output(tmp_path):
    ctx, runner = _ctx(tmp_path)
    await execute_llmdbenchmark(
        ctx, subcommand="run", spec="cicd/kind", namespace="llmd",
        harness="inference-perf", workload="sanity_random.yaml",
    )  # no output flag
    argv = _last_run_call(runner)["argv"]
    # The UNSET output defaulted to local — NOT to any cloud bucket.
    assert argv[argv.index("-r") + 1] == "local"
    assert "gs://" not in " ".join(argv) and "s3://" not in " ".join(argv)


async def test_run_opt_in_cloud_sink_passes_through(tmp_path):
    ctx, runner = _ctx(tmp_path)
    await execute_llmdbenchmark(
        ctx, subcommand="run", spec="cicd/kind", namespace="llmd",
        harness="inference-perf", workload="sanity_random.yaml", flags={"output": GS_URI},
    )
    argv = _last_run_call(runner)["argv"]
    # An explicit opt-in destination overrides the local default and passes straight to -r.
    assert argv[argv.index("-r") + 1] == GS_URI


def test_execute_schema_accepts_cloud_output_flag():
    m = ExecuteInput(subcommand="run", spec="cicd/kind", flags={"output": S3_URI})
    assert m.flags == {"output": S3_URI}


# ---------------------------------------------------------------------------
# allowlist — cloud scheme permitted on run -r/--output ONLY when opted in (DATA)
# ---------------------------------------------------------------------------


def _run_argv(*rest):
    return ["llmdbenchmark", "--spec", "cicd/kind", "run",
            "-l", "vllm-benchmark", "-w", "sanity_random.yaml", *rest]


@pytest.mark.parametrize("flag", ["-r", "--output"])
@pytest.mark.parametrize("uri", [GS_URI, S3_URI])
def test_allowlist_permits_cloud_sink_on_run(allowlist, catalog, flag, uri):
    d = allowlist.validate(_run_argv(flag, uri), catalog=catalog)
    assert d.allowed, f"{flag} {uri} should be allowed on run (opt-in): {d.reason}"
    assert d.mode == MUTATING  # a real run stays mutating (approval-gated)


@pytest.mark.parametrize("flag", ["-r", "--output"])
def test_allowlist_local_default_still_accepted_on_run(allowlist, catalog, flag):
    # The LOCAL default must keep working under the widened constraint.
    for value in ("local", "results/run1"):
        d = allowlist.validate(_run_argv(flag, value), catalog=catalog)
        assert d.allowed, f"{flag} {value} (local default) must stay allowed: {d.reason}"


def test_allowlist_rejects_injection_laden_sink_value(allowlist, catalog):
    # A metachar-laden destination is rejected by the blanket screen (defense in depth).
    d = allowlist.validate(_run_argv("-r", "gs://evil/$(rm -rf /)"), catalog=catalog)
    assert not d.allowed


def test_cloud_sink_keeps_read_only_preview(allowlist, catalog):
    # --dry-run still downgrades a cloud-sink-bearing run to a read-only preview (orthogonal).
    d = allowlist.validate(_run_argv("-r", GS_URI, "--dry-run"), catalog=catalog)
    assert d.allowed and d.mode == READ_ONLY


# ---------------------------------------------------------------------------
# the cloud scheme is NOT silently widened beyond run -r/--output
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flag", ["--workspace", "--ws", "-e", "--experiments"])
def test_filesystem_path_flags_still_reject_cloud_scheme(allowlist, catalog, flag):
    # output_dir is SHARED by genuine filesystem-path flags; widening output_dir would have
    # let a bucket URI slip into --workspace/-e. The dedicated results_sink keeps those denied.
    d = allowlist.validate(_run_argv(flag, GS_URI), catalog=catalog)
    assert not d.allowed, f"{flag} must NOT accept a {GS_URI!r} cloud scheme (it is a path)"


@pytest.mark.parametrize("uri", [GS_URI, S3_URI])
def test_experiment_output_still_local_only(allowlist, catalog, uri):
    # The phase scopes the cloud sink to the `run` flag; experiment's -r stays on output_dir
    # (cloud stores deliberately not permitted there for the MVP).
    argv = ["llmdbenchmark", "--spec", "cicd/kind", "experiment", "-e", "exp.yaml", "-r", uri]
    d = allowlist.validate(argv, catalog=catalog)
    assert not d.allowed, f"experiment -r must NOT accept {uri!r} (run-only for the MVP)"


def test_experiment_local_output_still_accepted(allowlist, catalog):
    argv = ["llmdbenchmark", "--spec", "cicd/kind", "experiment", "-e", "exp.yaml", "-r", "local"]
    d = allowlist.validate(argv, catalog=catalog)
    assert d.allowed, f"experiment -r local must stay allowed: {d.reason}"


# ---------------------------------------------------------------------------
# the JUDGMENT is in knowledge, and the agent is pointed at it
# ---------------------------------------------------------------------------


def test_cloud_results_sink_knowledge_is_discoverable():
    from pathlib import Path

    kfile = Path(__file__).resolve().parent.parent / "knowledge" / "cloud_results_sink.md"
    assert kfile.is_file(), "knowledge/cloud_results_sink.md must exist (auto-indexed by prompt glob)"
    text = kfile.read_text().lower()
    # It must carry the do-you-have-a-bucket / default-local judgment, not just mention the flag.
    assert "bucket" in text
    assert "local" in text
    assert "gs://" in text and "s3://" in text
    # First non-empty line is the H1 heading used verbatim by prompt._one_line_purpose.
    first = next(line.strip() for line in kfile.read_text().splitlines() if line.strip())
    assert first.startswith("# ")


def test_execute_tool_description_points_at_cloud_sink_knowledge():
    from app.tools.registry import _DESCRIPTIONS

    desc = _DESCRIPTIONS["execute_llmdbenchmark"]
    assert "cloud_results_sink" in desc
    assert "gs://" in desc or "s3://" in desc
