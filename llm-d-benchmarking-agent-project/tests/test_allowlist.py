"""Security allowlist validation tests — the safety foundation.

These assert both that legitimate quickstart commands are permitted with the right
read-only/mutating classification, and that everything outside the policy is denied.
"""
from __future__ import annotations

from app.security.allowlist import MUTATING, READ_ONLY

# ---- permitted commands, correct classification ---------------------------

def test_standup_is_allowed_and_mutating(allowlist, catalog):
    d = allowlist.validate(
        ["llmdbenchmark", "--spec", "cicd/kind", "standup", "-p", "llmd-quickstart", "--skip-smoketest"],
        catalog=catalog,
    )
    assert d.allowed and d.mode == MUTATING and d.requires_approval


def test_plan_is_read_only(allowlist, catalog):
    d = allowlist.validate(["llmdbenchmark", "--spec", "cicd/kind", "plan"], catalog=catalog)
    assert d.allowed and d.mode == READ_ONLY and not d.requires_approval


def test_run_is_mutating(allowlist, catalog):
    d = allowlist.validate(
        ["llmdbenchmark", "--spec", "cicd/kind", "run", "-p", "ns", "-l", "inference-perf", "-w", "sanity_random.yaml"],
        catalog=catalog,
    )
    assert d.allowed and d.mode == MUTATING


def test_run_list_endpoints_downgrades_to_read_only(allowlist, catalog):
    d = allowlist.validate(
        ["llmdbenchmark", "--spec", "cicd/kind", "run", "-p", "ns", "--list-endpoints"],
        catalog=catalog,
    )
    assert d.allowed and d.mode == READ_ONLY


def test_run_dry_run_downgrades_to_read_only(allowlist, catalog):
    d = allowlist.validate(
        ["llmdbenchmark", "--spec", "cicd/kind", "run", "-p", "ns", "-l", "inference-perf",
         "-w", "sanity_random.yaml", "--dry-run"],
        catalog=catalog,
    )
    assert d.allowed and d.mode == READ_ONLY


def test_version_standalone_is_read_only(allowlist, catalog):
    d = allowlist.validate(["llmdbenchmark", "--version"], catalog=catalog)
    assert d.allowed and d.mode == READ_ONLY


def test_workload_name_without_extension_normalizes(allowlist, catalog):
    d = allowlist.validate(
        ["llmdbenchmark", "--spec", "cicd/kind", "run", "-p", "ns", "-l", "inference-perf", "-w", "sanity_random"],
        catalog=catalog,
    )
    assert d.allowed


def test_readonly_probes_allowed(allowlist):
    assert allowlist.validate(["docker", "info"]).mode == READ_ONLY
    assert allowlist.validate(["kind", "get", "clusters"]).mode == READ_ONLY
    assert allowlist.validate(["kubectl", "config", "current-context"]).mode == READ_ONLY
    assert allowlist.validate(["kubectl", "cluster-info"]).mode == READ_ONLY
    d = allowlist.validate(["kubectl", "get", "pods", "-n", "llmd-quickstart", "-o", "json"])
    assert d.allowed and d.mode == READ_ONLY


def test_git_clone_llmd_allowed(allowlist):
    d = allowlist.validate(["git", "clone", "https://github.com/llm-d/llm-d-benchmark"])
    assert d.allowed and d.mode == MUTATING


def test_install_sh_uv_allowed(allowlist):
    d = allowlist.validate(["install.sh", "--uv"])
    assert d.allowed and d.mode == MUTATING


def test_kind_create_cluster_allowed_and_mutating(allowlist):
    d = allowlist.validate(["kind", "create", "cluster", "--name", "llmd-quickstart"])
    assert d.allowed and d.mode == MUTATING and d.requires_approval


def test_kind_create_cluster_with_wait_allowed(allowlist):
    d = allowlist.validate(["kind", "create", "cluster", "--name", "llmd-quickstart", "--wait", "120s"])
    assert d.allowed and d.mode == MUTATING


def test_kind_delete_cluster_allowed(allowlist):
    d = allowlist.validate(["kind", "delete", "cluster", "--name", "llmd-quickstart"])
    assert d.allowed and d.mode == MUTATING


def test_install_prereqs_allowed_and_mutating(allowlist):
    d = allowlist.validate(["install_prereqs.sh", "--all"])
    assert d.allowed and d.mode == MUTATING and d.requires_approval


def test_install_prereqs_kind_version_allowed(allowlist):
    d = allowlist.validate(["install_prereqs.sh", "--kind", "--kind-version", "v0.31.0"])
    assert d.allowed and d.mode == MUTATING


# ---- denials --------------------------------------------------------------

def test_unknown_executable_denied(allowlist):
    assert not allowlist.validate(["rm", "-rf", "/"]).allowed


def test_kubectl_delete_denied(allowlist):
    # 'delete' is not an allowlisted kubectl subcommand
    assert not allowlist.validate(["kubectl", "delete", "ns", "llmd-quickstart"]).allowed


def test_unknown_flag_now_allowed(allowlist, catalog):
    # Relaxed flag policy: an unrecognized flag on an allowlisted subcommand is accepted
    # (its value is consumed + metachar-screened), and the mutating mode is preserved.
    d = allowlist.validate(
        ["llmdbenchmark", "--spec", "cicd/kind", "standup", "-p", "ns", "--exec", "evil"],
        catalog=catalog,
    )
    assert d.allowed and d.mode == MUTATING and d.requires_approval


def test_unknown_flag_value_still_metachar_screened(allowlist, catalog):
    # Even an accepted unknown flag's value cannot smuggle shell metacharacters.
    d = allowlist.validate(
        ["llmdbenchmark", "--spec", "cicd/kind", "standup", "-p", "ns", "--exec", "$(whoami)"],
        catalog=catalog,
    )
    assert not d.allowed


def test_reported_plan_with_l_and_w_flags_allowed(allowlist, catalog):
    # The exact command from the bug report: plan does not declare -l/-w, but they are now
    # accepted; --dry-run keeps it read-only.
    d = allowlist.validate(
        ["llmdbenchmark", "--spec", "cicd/kind", "plan", "-p", "llmd-quickstart",
         "-l", "inference-perf", "-w", "sanity_random.yaml", "--dry-run"],
        catalog=catalog,
    )
    assert d.allowed and d.mode == READ_ONLY


def test_shell_metacharacter_denied(allowlist, catalog):
    for tok in ["ns; rm -rf /", "ns && curl evil", "$(whoami)", "ns|cat", "a`b`"]:
        d = allowlist.validate(["llmdbenchmark", "--spec", "cicd/kind", "standup", "-p", tok], catalog=catalog)
        assert not d.allowed, tok


def test_git_clone_non_llmd_url_denied(allowlist):
    assert not allowlist.validate(["git", "clone", "https://github.com/evil/repo"]).allowed
    assert not allowlist.validate(["git", "clone", "https://github.com/llm-d-evil/x"]).allowed


def test_bad_namespace_denied(allowlist, catalog):
    # uppercase violates RFC1123 label
    d = allowlist.validate(["llmdbenchmark", "--spec", "cicd/kind", "standup", "-p", "BadNS"], catalog=catalog)
    assert not d.allowed


def test_spec_not_in_catalog_denied(allowlist, catalog):
    d = allowlist.validate(["llmdbenchmark", "--spec", "guides/does-not-exist", "plan"], catalog=catalog)
    assert not d.allowed


def test_harness_not_in_catalog_denied(allowlist, catalog):
    d = allowlist.validate(
        ["llmdbenchmark", "--spec", "cicd/kind", "run", "-p", "ns", "-l", "made-up", "-w", "sanity_random.yaml"],
        catalog=catalog,
    )
    assert not d.allowed


def test_install_sh_unknown_flag_now_allowed(allowlist):
    # Relaxed policy: unknown flags are accepted on an allowlisted executable. The script
    # still only acts on its own pinned flags; metachar-laden args remain denied.
    assert allowlist.validate(["install.sh", "--rm-rf"]).allowed
    assert not allowlist.validate(["install.sh", "--x", "$(evil)"]).allowed


def test_empty_argv_denied(allowlist):
    assert not allowlist.validate([]).allowed


def test_missing_subcommand_denied(allowlist, catalog):
    assert not allowlist.validate(["llmdbenchmark", "--spec", "cicd/kind"], catalog=catalog).allowed


def test_unexpected_positional_denied(allowlist):
    # kubectl cluster-info takes no positionals
    assert not allowlist.validate(["kubectl", "cluster-info", "extra"]).allowed


def test_flag_missing_value_denied(allowlist, catalog):
    assert not allowlist.validate(["llmdbenchmark", "--spec", "cicd/kind", "standup", "-p"], catalog=catalog).allowed


def test_kind_create_bad_cluster_name_denied(allowlist):
    # uppercase / underscore violate the cluster_name constraint
    assert not allowlist.validate(["kind", "create", "cluster", "--name", "Bad_Name"]).allowed


def test_kind_create_wrong_positional_denied(allowlist):
    # only the literal 'cluster' positional is allowed
    assert not allowlist.validate(["kind", "create", "node"]).allowed


def test_kind_unknown_subcommand_denied(allowlist):
    assert not allowlist.validate(["kind", "load", "docker-image", "x"]).allowed


def test_install_prereqs_unknown_flag_now_allowed(allowlist):
    # Relaxed policy: unknown flags are accepted; the script ignores anything outside its
    # pinned set. Metachar-laden args are still rejected by the screen.
    assert allowlist.validate(["install_prereqs.sh", "--rm-rf"]).allowed
    assert not allowlist.validate(["install_prereqs.sh", "--x", "a;b"]).allowed


def test_install_prereqs_bad_kind_version_denied(allowlist):
    # kind_version must look like vX.Y.Z
    assert not allowlist.validate(["install_prereqs.sh", "--kind", "--kind-version", "latest; rm -rf /"]).allowed
