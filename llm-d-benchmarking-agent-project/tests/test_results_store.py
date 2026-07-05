"""Phase 50 — Results Store (the OPTIONAL git-like CLI result store: remotes/push/pull).

Hermetic, no cluster / GPU / network / real GCS transfer. Covers the MECHANISM this phase adds
(the WHEN-to-use-the-CLI-store-vs-the-local-history-store JUDGMENT lives in knowledge/history.md,
not in Python):

  * build_argv emits the upstream-EXACT nested store-command shape for
    init/remote/status/add/rm/ls/push/pull (verified against
    llm-d-benchmark/llmdbenchmark/interface/results.py), and emits NOTHING from the
    namespace/harness/model/run-flag path for a `results` invocation;
  * the allowlist (DATA) accepts every store-command at the right mode — init/status/ls/remote-ls
    READ-ONLY (auto-run), add/rm/push/pull/remote-add/remote-rm MUTATING (approval-gated, the
    spec's HERMETIC-TEST requirement) — and value-pins remote names / GCS-only URIs / run-uids;
  * publish/pull are approval-gated end-to-end through execute_llmdbenchmark (a denying approver
    refuses them; a read-only init/status auto-runs with NO approval prompt);
  * the LOCAL history store (result_history / app/storage/history.py) is UNCHANGED — its tool,
    its trend store, and its API are untouched by anything here (ACCEPTANCE: local store intact);
  * the knowledge guide + tool/schema descriptions point the agent at the two-stores judgment.
"""
from __future__ import annotations

import pytest

from app.security.allowlist import MUTATING, READ_ONLY
from app.tools.context import ApprovalRejected, ToolError
from app.tools.execute import _RESULTS_STORE_COMMANDS, build_argv, execute_llmdbenchmark
from app.tools.schemas import ExecuteInput
from tests._helpers import _capture_ctx
from tests.flows.harness import CaptureRunner

GS_URI = "gs://my-team-results/published"

# ---------------------------------------------------------------------------
# build_argv — emits the upstream-EXACT nested store-command shape (PURE MECHANISM)
# ---------------------------------------------------------------------------


def _store_argv(store: dict) -> list[str]:
    return build_argv("results", spec="cicd/kind", store=store)


def test_build_argv_init():
    assert _store_argv({"command": "init"})[3:] == ["results", "init"]


def test_build_argv_status():
    assert _store_argv({"command": "status"})[3:] == ["results", "status"]


def test_build_argv_remote_ls():
    assert _store_argv({"command": "remote", "remote_action": "ls"})[3:] == [
        "results", "remote", "ls",
    ]


def test_build_argv_remote_add():
    argv = _store_argv({"command": "remote", "remote_action": "add", "name": "prod", "uri": GS_URI})
    assert argv[3:] == ["results", "remote", "add", "prod", GS_URI]


def test_build_argv_remote_rm():
    argv = _store_argv({"command": "remote", "remote_action": "rm", "name": "prod"})
    assert argv[3:] == ["results", "remote", "rm", "prod"]


def test_build_argv_add_multiple_paths():
    argv = _store_argv({"command": "add", "paths": ["workspaces/run1", "workspaces/run2"]})
    assert argv[3:] == ["results", "add", "workspaces/run1", "workspaces/run2"]


def test_build_argv_rm():
    argv = _store_argv({"command": "rm", "paths": ["workspaces/run1"]})
    assert argv[3:] == ["results", "rm", "workspaces/run1"]


def test_build_argv_paths_must_be_a_list():
    # Regression: a non-iterable `paths` (scalar) or a non-list mapping must raise a clean
    # ToolError, not a raw TypeError at argv-build time (before the allowlist could reject it).
    with pytest.raises(ToolError):
        _store_argv({"command": "add", "paths": 5})
    with pytest.raises(ToolError):
        _store_argv({"command": "rm", "paths": {"not": "a list"}})


def test_build_argv_ls_remote_with_filters():
    argv = _store_argv({"command": "ls", "remote": "prod", "model": "meta-llama/Llama-3.1-8B", "hardware": "a100"})
    assert argv[3:] == ["results", "ls", "prod", "-m", "meta-llama/Llama-3.1-8B", "-w", "a100"]


def test_build_argv_ls_remote_bare():
    assert _store_argv({"command": "ls", "remote": "prod"})[3:] == ["results", "ls", "prod"]


def test_build_argv_push_defaults_and_options():
    assert _store_argv({"command": "push"})[3:] == ["results", "push"]
    argv = _store_argv({"command": "push", "remote": "staging", "path": "workspaces/run1", "group": "team"})
    assert argv[3:] == ["results", "push", "staging", "workspaces/run1", "-g", "team"]


def test_build_argv_push_path_without_remote_does_not_skip_positional():
    # Regression (positional-skip): `push` has TWO optional positionals — `remote` THEN `path`
    # (interface/results.py: remote nargs='?' default 'staging', path nargs='?'). The agent may
    # push an ad-hoc dir WITHOUT naming a remote (the schema/knowledge document `remote` as
    # optional with default `staging`). If the builder appends only `path`, upstream argparse
    # binds that path to the `remote` positional (verified) — the run dir becomes the remote name,
    # so the WRONG store op runs (push to a remote named after the local dir, or a get_remote
    # failure). The remote default MUST be emitted so `path` lands in the second slot.
    argv = _store_argv({"command": "push", "path": "workspaces/run1"})
    assert argv[3:] == ["results", "push", "staging", "workspaces/run1"], (
        "a path-only push must emit the default remote first so the path is not "
        "mis-bound to the remote positional"
    )
    # With a group too, ordering stays remote→path→-g.
    argv = _store_argv({"command": "push", "path": "workspaces/run1", "group": "team"})
    assert argv[3:] == ["results", "push", "staging", "workspaces/run1", "-g", "team"]
    # A bare push (no remote, no path) still emits nothing extra (staged runs → default remote).
    assert _store_argv({"command": "push"})[3:] == ["results", "push"]
    # A push naming ONLY a remote (no path) is unchanged.
    assert _store_argv({"command": "push", "remote": "prod"})[3:] == ["results", "push", "prod"]


def test_build_argv_pull_requires_run_uid():
    argv = _store_argv({"command": "pull", "remote": "prod", "run_uid": "c6bc210e"})
    assert argv[3:] == ["results", "pull", "prod", "--run-uid", "c6bc210e"]
    # remote is optional (default prod handled by the CLI)
    assert _store_argv({"command": "pull", "run_uid": "c6bc210e"})[3:] == [
        "results", "pull", "--run-uid", "c6bc210e",
    ]


def test_build_argv_spec_precedes_results_even_for_store():
    # The upstream CLI errors without --spec even for a results-store op (cli.py:1794), so the
    # global --spec must still be emitted BEFORE `results`.
    argv = _store_argv({"command": "init"})
    assert argv[:3] == ["llmdbenchmark", "--spec", "cicd/kind"]


def test_results_emits_no_namespace_or_model_path():
    # A `results` invocation takes NONE of the namespace/harness/model/run-flag emission — those
    # would be rejected by the upstream `results` parser. The early-return guards that.
    argv = build_argv(
        "results", spec="cicd/kind", namespace="llmd", harness="inference-perf",
        models="meta-llama/Llama-3.1-8B", store={"command": "status"},
    )
    assert argv[3:] == ["results", "status"]
    assert "-p" not in argv and "-l" not in argv and "-m" not in argv


def test_results_without_store_emits_bare_subcommand():
    # Back-compat: no store => bare `results` (no positionals), unchanged from before this phase.
    assert build_argv("results", spec="cicd/kind")[3:] == ["results"]


def test_build_argv_rejects_unknown_store_command():
    with pytest.raises(ToolError):
        _store_argv({"command": "frobnicate"})


def test_store_command_set_matches_upstream():
    # Lockstep with the upstream results sub-parsers (interface/results.py).
    assert frozenset(
        {"init", "remote", "status", "add", "rm", "ls", "push", "pull"}
    ) == _RESULTS_STORE_COMMANDS


# ---------------------------------------------------------------------------
# allowlist (DATA) — every store-command at the right mode + value-pinned
# ---------------------------------------------------------------------------


def _argv(*rest):
    return ["llmdbenchmark", "--spec", "cicd/kind", "results", *rest]


READ_ONLY_CASES = [
    ("init",),
    ("status",),
    ("ls", "prod"),
    ("ls", "prod", "-m", "meta-llama/Llama-3.1-8B", "-w", "a100"),
    ("remote", "ls"),
]

MUTATING_CASES = [
    ("remote", "add", "prod", GS_URI),
    ("remote", "rm", "prod"),
    ("add", "workspaces/run1"),
    ("add", "workspaces/run1", "workspaces/run2", "workspaces/run3"),
    ("rm", "workspaces/run1"),
    ("push",),
    ("push", "staging"),
    ("push", "staging", "workspaces/run1", "-g", "team"),
    ("pull", "prod", "--run-uid", "c6bc210e"),
    ("pull", "--run-uid", "c6bc210e"),
]


@pytest.mark.parametrize("rest", READ_ONLY_CASES)
def test_allowlist_read_only_store_commands_auto_run(allowlist, catalog, rest):
    d = allowlist.validate(_argv(*rest), catalog=catalog)
    assert d.allowed, f"results {' '.join(rest)} should be allowed: {d.reason}"
    assert d.mode == READ_ONLY and not d.requires_approval, f"results {' '.join(rest)} should auto-run"


@pytest.mark.parametrize("rest", MUTATING_CASES)
def test_allowlist_mutating_store_commands_need_approval(allowlist, catalog, rest):
    d = allowlist.validate(_argv(*rest), catalog=catalog)
    assert d.allowed, f"results {' '.join(rest)} should be allowed: {d.reason}"
    assert d.mode == MUTATING and d.requires_approval, (
        f"results {' '.join(rest)} must be approval-gated (spec HERMETIC-TEST)"
    )


def test_allowlist_push_and_pull_are_approval_gated(allowlist, catalog):
    # The spec calls this out explicitly: push/pull must be approval-gated + allowlisted.
    push = allowlist.validate(_argv("push", "staging"), catalog=catalog)
    pull = allowlist.validate(_argv("pull", "prod", "--run-uid", "c6bc210e"), catalog=catalog)
    assert push.allowed and push.requires_approval
    assert pull.allowed and pull.requires_approval


def test_allowlist_remote_uri_is_gcs_only(allowlist, catalog):
    # s3:// is deliberately NOT accepted for a remote URI (GCS-only, matching upstream defaults).
    ok = allowlist.validate(_argv("remote", "add", "prod", GS_URI), catalog=catalog)
    assert ok.allowed, ok.reason
    bad = allowlist.validate(_argv("remote", "add", "prod", "s3://bucket/x"), catalog=catalog)
    assert not bad.allowed


def test_allowlist_rejects_unknown_store_command(allowlist, catalog):
    d = allowlist.validate(_argv("frobnicate"), catalog=catalog)
    assert not d.allowed


def test_allowlist_rejects_missing_required_positionals(allowlist, catalog):
    # remote add needs name + uri; ls needs a remote; add needs >=1 path; remote needs an action.
    assert not allowlist.validate(_argv("remote", "add", "prod"), catalog=catalog).allowed
    assert not allowlist.validate(_argv("ls"), catalog=catalog).allowed
    assert not allowlist.validate(_argv("add"), catalog=catalog).allowed
    assert not allowlist.validate(_argv("remote"), catalog=catalog).allowed


def test_allowlist_rejects_wildcard_run_uid(allowlist, catalog):
    # Upstream supports `*` wildcards; we cannot (it is a blocked shell metacharacter). An exact
    # run-uid works; a wildcard is rejected by the blanket metachar screen.
    d = allowlist.validate(_argv("pull", "prod", "--run-uid", "c6bc*"), catalog=catalog)
    assert not d.allowed


def test_allowlist_rejects_injection_in_store_value(allowlist, catalog):
    d = allowlist.validate(_argv("remote", "add", "prod", "gs://evil/$(rm -rf /)"), catalog=catalog)
    assert not d.allowed


# ---------------------------------------------------------------------------
# execute_llmdbenchmark — read-only auto-runs; mutating is approval-gated end-to-end
# ---------------------------------------------------------------------------


def _last_call(runner: CaptureRunner):
    return next(c for c in reversed(runner.calls) if c["argv"][:1] == ["llmdbenchmark"])


async def test_execute_init_auto_runs_without_approval(tmp_path):
    approvals: list = []

    async def approver(kind, payload):
        approvals.append(kind)
        return True

    ctx, runner = _capture_ctx(tmp_path, approve=approver)
    res = await execute_llmdbenchmark(ctx, subcommand="results", spec="cicd/kind", store={"command": "init"})
    assert res["mode"] == READ_ONLY
    assert _last_call(runner)["argv"][3:] == ["results", "init"]
    assert approvals == []  # read-only never prompts


async def test_execute_push_is_approval_gated(tmp_path):
    approvals: list = []

    async def deny(kind, payload):
        approvals.append(kind)
        return False  # user clicks "deny"

    ctx, runner = _capture_ctx(tmp_path, approve=deny)
    with pytest.raises(ApprovalRejected):
        await execute_llmdbenchmark(
            ctx, subcommand="results", spec="cicd/kind",
            store={"command": "push", "remote": "staging"},
        )
    assert approvals == ["command"]  # the publish prompted for approval (and was denied)
    # Denied => the CLI was never actually invoked.
    assert not any(c["argv"][:1] == ["llmdbenchmark"] for c in runner.calls)


async def test_execute_pull_runs_when_approved(tmp_path):
    async def approve(kind, payload):
        return True

    ctx, runner = _capture_ctx(tmp_path, approve=approve)
    res = await execute_llmdbenchmark(
        ctx, subcommand="results", spec="cicd/kind",
        store={"command": "pull", "remote": "prod", "run_uid": "c6bc210e"},
    )
    assert res["mode"] == MUTATING
    assert _last_call(runner)["argv"][3:] == ["results", "pull", "prod", "--run-uid", "c6bc210e"]


# ---------------------------------------------------------------------------
# schema accepts the store field
# ---------------------------------------------------------------------------


def test_execute_schema_accepts_store_field():
    m = ExecuteInput(subcommand="results", spec="cicd/kind", store={"command": "push", "remote": "staging"})
    assert m.store == {"command": "push", "remote": "staging"}


def test_execute_schema_store_defaults_none_for_other_subcommands():
    m = ExecuteInput(subcommand="run", spec="cicd/kind")
    assert m.store is None


# ---------------------------------------------------------------------------
# ACCEPTANCE: the LOCAL history store is UNCHANGED (the two stores are independent)
# ---------------------------------------------------------------------------


def test_local_history_store_is_untouched():
    # The result_history tool + its store must still exist and expose the same local-only API;
    # this phase adds the CLI store as a SEPARATE path and must NOT regress the local one.
    from pathlib import Path

    from app.storage import history as hist
    from app.tools import history as history_tool  # the result_history handler module

    # The local HistoryStore contract (add/get/list/delete) + the module-level trend() are intact.
    for method in ("add", "get", "list", "delete"):
        assert hasattr(hist.HistoryStore, method), f"local history store lost {method}()"
    assert callable(hist.trend), "module-level trend() must still exist for the local store"
    # The local store is purely local — no GCS / remote / push-pull concept leaked into it.
    store_src = Path(hist.__file__).read_text()
    assert "gs://" not in store_src and "results push" not in store_src and "remote add" not in store_src
    # The result_history tool module did not grow a results-store dependency either.
    tool_src = Path(history_tool.__file__).read_text()
    assert "results push" not in tool_src and "gs://" not in tool_src


# ---------------------------------------------------------------------------
# the JUDGMENT is in knowledge, and the agent is pointed at it
# ---------------------------------------------------------------------------


def test_history_knowledge_documents_the_two_stores():
    from pathlib import Path

    kfile = Path(__file__).resolve().parent.parent / "knowledge" / "history.md"
    text = kfile.read_text().lower()
    # It must explain the CLI store, its independence from the local store, and WHEN to use it.
    assert "results store" in text
    assert "result_history" in text  # names the local store it is distinct from
    assert "team" in text and "gs://" in text
    assert "push" in text and "pull" in text
    # the two stores are called out as separate / unchanged
    assert "separate" in text or "distinct" in text or "independent" in text


def test_execute_tool_description_points_at_results_store():
    from app.tools.registry import _DESCRIPTIONS

    desc = _DESCRIPTIONS["execute_llmdbenchmark"]
    assert "results store" in desc.lower() or "store=" in desc
    assert "history.md" in desc


def test_execute_schema_describes_store_field():
    field = ExecuteInput.model_fields["store"]
    d = (field.description or "").lower()
    assert "result_history" in d  # distinct from the local store
    assert "gs://" in d or "gcs" in d
    assert "push" in d and "pull" in d
