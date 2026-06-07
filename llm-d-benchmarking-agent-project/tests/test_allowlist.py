"""Security allowlist validation tests — the safety foundation.

These assert both that legitimate quickstart commands are permitted with the right
read-only/mutating classification, and that everything outside the policy is denied.
"""
from __future__ import annotations

import pytest

from app.security.allowlist import MUTATING, READ_ONLY, Allowlist, AllowlistError

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


# ---- Phase 64: `oc` is the kubectl-equivalent read-only mirror ------------
# oc must accept EXACTLY the same read-only subcommands as kubectl (same value constraints),
# and DENY the mutating/unknown subcommands kubectl gates behind approval. The per-provider
# playbook (which CLI/toleration/known-issue) is knowledge, not Python — the allowlist just
# proves oc and kubectl share the read-only surface.

# The read-only commands both CLIs must accept identically (argv after the executable).
_OC_KUBECTL_READONLY_CASES = [
    ["config", "current-context"],
    ["config", "view", "--minify", "-o", "json"],
    ["config", "get-contexts"],
    ["cluster-info"],
    ["version", "--output", "json"],
    ["version", "--client"],
    ["get", "pods", "-n", "llmd-quickstart", "-o", "json"],
    ["get", "nodes", "-o", "json"],
    ["get", "events", "-A"],
    ["get", "pods", "-l", "app=vllm", "--field-selector", "status.phase=Pending"],
    ["top", "pods", "-n", "llmd-quickstart"],
    ["top", "nodes", "--sort-by", "cpu"],
    ["logs", "-n", "llmd-quickstart", "-l", "function=load_generator", "--tail", "100"],
]


def test_oc_mirrors_kubectl_readonly_surface(allowlist):
    """Every read-only kubectl command is accepted under `oc` with the SAME read-only mode."""
    for tail in _OC_KUBECTL_READONLY_CASES:
        oc = allowlist.validate(["oc", *tail])
        kc = allowlist.validate(["kubectl", *tail])
        assert kc.allowed and kc.mode == READ_ONLY, f"kubectl {tail} should be read-only"
        assert oc.allowed and oc.mode == READ_ONLY, f"oc {tail} should be read-only like kubectl"
        assert not oc.requires_approval, f"oc {tail} must auto-run (read-only)"


def test_oc_value_constraints_match_kubectl(allowlist):
    """oc enforces the SAME shared value constraints as kubectl (it references the same refs)."""
    # Bad namespace (uppercase violates the RFC1123 label) is rejected on BOTH.
    assert not allowlist.validate(["oc", "get", "pods", "-n", "BadNS", "-o", "json"]).allowed
    assert not allowlist.validate(["kubectl", "get", "pods", "-n", "BadNS", "-o", "json"]).allowed
    # An off-enum resource is rejected on BOTH (kubectl_resource enum is shared).
    assert not allowlist.validate(["oc", "get", "secrets", "-o", "json"]).allowed
    assert not allowlist.validate(["kubectl", "get", "secrets", "-o", "json"]).allowed
    # A bad output format is rejected on BOTH (output_format enum is shared).
    assert not allowlist.validate(["oc", "get", "pods", "-o", "evil"]).allowed


def test_oc_denies_mutating_and_unknown_subcommands(allowlist):
    """oc is a strictly READ-ONLY mirror — kubectl's mutating subcommands are NOT mirrored,
    and unknown subcommands are denied (no apply/patch/delete surface added in this phase)."""
    # apply/delete ARE allowlisted under kubectl (mutating) but DELIBERATELY absent on oc.
    assert allowlist.validate(["kubectl", "apply", "-f", "job.yaml"]).allowed
    assert not allowlist.validate(["oc", "apply", "-f", "job.yaml"]).allowed
    assert not allowlist.validate(["oc", "delete", "job", "myjob"]).allowed
    # patch is allowlisted on NEITHER (provider toleration patches go via the workspace path).
    assert not allowlist.validate(["oc", "patch", "deployment", "x", "-p", "{}"]).allowed
    assert not allowlist.validate(["kubectl", "patch", "deployment", "x", "-p", "{}"]).allowed
    # An entirely unknown oc subcommand is denied.
    assert not allowlist.validate(["oc", "login", "https://api.cluster:6443"]).allowed
    # Shell metacharacters are screened on oc too.
    assert not allowlist.validate(["oc", "get", "pods", "-n", "ns; rm -rf /"]).allowed


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


def test_install_metrics_server_allowed_and_mutating(allowlist):
    # The per-cluster metrics-server installer is mutating (touches kube-system) → approval-gated.
    d = allowlist.validate(["install_metrics_server.sh", "--kubelet-insecure-tls"])
    assert d.allowed and d.mode == MUTATING and d.requires_approval


def test_install_metrics_server_version_allowed(allowlist):
    d = allowlist.validate(["install_metrics_server.sh", "--version", "v0.7.2"])
    assert d.allowed and d.mode == MUTATING


def test_install_metrics_server_has_governance_timeout(allowlist):
    # The mutating installer declares a per-command deadline (DATA, not Python).
    d = allowlist.validate(["install_metrics_server.sh"])
    assert d.timeout_s == 300


def test_install_deps_allowed_and_mutating(allowlist):
    # UPSTREAM llm-d guide client-prereq installer (helm/helmfile/kustomize/yq/kubectl).
    d = allowlist.validate(["install-deps.sh"])
    assert d.allowed and d.mode == MUTATING and d.requires_approval


def test_install_deps_dev_flag_allowed(allowlist):
    d = allowlist.validate(["install-deps.sh", "--dev"])
    assert d.allowed and d.mode == MUTATING


def test_install_deps_has_governance_timeout(allowlist):
    # The mutating guide installer declares a per-command deadline (DATA, not Python).
    d = allowlist.validate(["install-deps.sh"])
    assert d.timeout_s == 900


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


def test_install_metrics_server_bad_version_denied(allowlist):
    # metrics_server_version must look like vX.Y.Z — no shell injection through --version.
    assert not allowlist.validate(["install_metrics_server.sh", "--version", "latest; rm -rf /"]).allowed


def test_install_deps_metachar_arg_denied(allowlist):
    # The guide installer's args are still metachar-screened — no shell injection.
    assert not allowlist.validate(["install-deps.sh", "--dev; rm -rf /"]).allowed


# ---- positional shape invariant: a `repeated` spec must be LAST ------------
# The positional walker keeps a `repeated` spec on the stack so it matches every following token;
# a `repeated` spec placed before another positional would silently swallow the next positional's
# tokens. The loader enforces "repeated must be last" LOUDLY so a future allowlist edit can't slip
# that past — these tests pin both the load-time rejection and that a legitimate trailing-repeated
# spec (mirroring the live `results add <paths...>` shape) still loads.

def test_real_allowlist_loads_with_repeated_last(allowlist):
    # The shipped security/allowlist.yaml has trailing `repeated` positionals (results store);
    # loading it via the fixture must not raise — the invariant holds for the real policy.
    assert allowlist is not None


def test_repeated_positional_before_another_is_rejected_at_load():
    policy = {
        "executables": {
            "demo": {
                "flat": True,
                "mode": READ_ONLY,
                # A `repeated` spec FOLLOWED by another positional — the invariant violation.
                "positionals": [
                    {"value": None, "repeated": True},
                    {"value": None},
                ],
            }
        }
    }
    with pytest.raises(AllowlistError, match="repeated"):
        Allowlist(policy)


def test_repeated_positional_last_is_accepted_at_load():
    policy = {
        "executables": {
            "demo": {
                "flat": True,
                "mode": READ_ONLY,
                "positionals": [
                    {"value": None},
                    {"value": None, "repeated": True},  # trailing repeated — legitimate (nargs='+')
                ],
            }
        }
    }
    # Loads without raising, and still validates a multi-token tail against the repeated spec.
    al = Allowlist(policy)
    assert al.validate(["demo", "a", "b", "c"]).allowed


def test_repeated_positional_before_another_in_nested_subcommand_rejected():
    # The validation must also reach NESTED subcommands (the `results <store-command>` shape).
    policy = {
        "executables": {
            "demo": {
                "subcommands": {
                    "group": {
                        "subcommands": {
                            "leaf": {
                                "mode": READ_ONLY,
                                "positionals": [
                                    {"value": None, "repeated": True},
                                    {"value": None},
                                ],
                            }
                        }
                    }
                }
            }
        }
    }
    with pytest.raises(AllowlistError, match="repeated"):
        Allowlist(policy)
