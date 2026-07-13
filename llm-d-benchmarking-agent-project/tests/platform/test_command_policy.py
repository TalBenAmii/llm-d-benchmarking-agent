"""Security policy validation tests — the safety foundation.

These assert both that legitimate quickstart commands are permitted with the right
read-only/mutating classification, and that everything outside the policy is denied.
"""
from __future__ import annotations

import pytest

from app.security.policy import MUTATING, READ_ONLY, CommandPolicy, CommandPolicyError

# ---- permitted commands, correct classification ---------------------------

def test_standup_is_allowed_and_mutating(policy, catalog):
    d = policy.validate(
        ["llmdbenchmark", "--spec", "cicd/kind", "standup", "-p", "llmd-quickstart", "--skip-smoketest"],
        catalog=catalog,
    )
    assert d.allowed and d.mode == MUTATING and d.requires_approval


def test_plan_is_read_only(policy, catalog):
    d = policy.validate(["llmdbenchmark", "--spec", "cicd/kind", "plan"], catalog=catalog)
    assert d.allowed and d.mode == READ_ONLY and not d.requires_approval


def test_run_is_mutating(policy, catalog):
    d = policy.validate(
        ["llmdbenchmark", "--spec", "cicd/kind", "run", "-p", "ns", "-l", "inference-perf", "-w", "sanity_random.yaml"],
        catalog=catalog,
    )
    assert d.allowed and d.mode == MUTATING


def test_run_list_endpoints_downgrades_to_read_only(policy, catalog):
    d = policy.validate(
        ["llmdbenchmark", "--spec", "cicd/kind", "run", "-p", "ns", "--list-endpoints"],
        catalog=catalog,
    )
    assert d.allowed and d.mode == READ_ONLY


def test_run_dry_run_downgrades_to_read_only(policy, catalog):
    d = policy.validate(
        ["llmdbenchmark", "--spec", "cicd/kind", "run", "-p", "ns", "-l", "inference-perf",
         "-w", "sanity_random.yaml", "--dry-run"],
        catalog=catalog,
    )
    assert d.allowed and d.mode == READ_ONLY


def test_write_destinations_reject_path_traversal(policy, catalog):
    # Defense-in-depth behind the approval gate: the constraints on WHERE llmdbenchmark writes must
    # reject a '..' escape, so an approved benchmark command can't be aimed outside the workspace.
    # --workspace/--ws/-e/--experiments (output_dir):
    assert policy.validate(
        ["llmdbenchmark", "--spec", "cicd/kind", "--workspace", "workspace/exp", "plan"],
        catalog=catalog).allowed
    assert not policy.validate(
        ["llmdbenchmark", "--spec", "cicd/kind", "--workspace", "../../etc", "plan"],
        catalog=catalog).allowed
    # run -r/--output (results_sink): a local dest and an opt-in gs:// bucket are fine; a '..' is not.
    run = ["llmdbenchmark", "--spec", "cicd/kind", "run", "-p", "ns",
           "-l", "inference-perf", "-w", "sanity_random.yaml"]
    assert policy.validate([*run, "-r", "gs://bkt/prefix"], catalog=catalog).allowed
    assert not policy.validate([*run, "-r", "gs://bkt/../prefix"], catalog=catalog).allowed
    # results add <paths...> (store_paths): a workspace dir is fine; a '..' escape is not.
    assert policy.validate(["llmdbenchmark", "results", "add", "workspace/run-1"], catalog=catalog).allowed
    assert not policy.validate(["llmdbenchmark", "results", "add", "../../secret"], catalog=catalog).allowed


def test_version_standalone_is_read_only(policy, catalog):
    d = policy.validate(["llmdbenchmark", "--version"], catalog=catalog)
    assert d.allowed and d.mode == READ_ONLY


def test_version_after_other_global_flag_is_read_only(policy, catalog):
    # (c) --version is a GENUINE global trigger (argparse action="version" exits before any
    # action). It must stay honored as read-only even alongside another global flag, and the
    # bypass fix must not regress it.
    d = policy.validate(["llmdbenchmark", "-v", "--version"], catalog=catalog)
    assert d.allowed and d.mode == READ_ONLY and not d.requires_approval


# ---- approval-gate bypass (BUG): a subcommand-OWN read_only_trigger placed in the GLOBAL /
# pre-subcommand region must NOT downgrade a mutating subcommand to read-only. Upstream
# llmdbenchmark registers `-n`/`--dry-run` on BOTH the top-level parser AND each subparser, so
# whether a GLOBAL-position `-n` actually takes effect hinges on an upstream
# `default=argparse.SUPPRESS` detail we do not control — a security gate must therefore NOT treat
# such a flag as a dry-run where its effect is not guaranteed. These pin the FAIL-SAFE direction:
# global-position trigger -> stays mutating -> still requires approval.

@pytest.mark.parametrize(
    "argv",
    [
        # `-n` / `--dry-run` BEFORE the mutating subcommand token (the bypass).
        ["llmdbenchmark", "-n", "standup", "-p", "ns"],
        ["llmdbenchmark", "--dry-run", "standup", "-p", "ns"],
        ["llmdbenchmark", "--spec", "cicd/kind", "--dry-run", "run", "-p", "ns",
         "-l", "inference-perf", "-w", "sanity_random.yaml"],
        ["llmdbenchmark", "-n", "run", "-p", "ns", "-l", "inference-perf", "-w", "sanity_random.yaml"],
        ["llmdbenchmark", "-n", "smoketest", "-p", "ns"],
        ["llmdbenchmark", "--dry-run", "teardown", "-p", "ns"],
        ["llmdbenchmark", "-n", "experiment", "-e", "exp.yaml"],
    ],
)
def test_global_position_dry_run_does_not_bypass_approval(policy, catalog, argv):
    d = policy.validate(argv, catalog=catalog)
    # Command is still permitted, but stays MUTATING -> approval-gated (NOT auto-run).
    assert d.allowed, f"{argv} should still be allowed"
    assert d.mode == MUTATING, f"{argv} must stay mutating (global-position -n is not a dry-run)"
    assert d.requires_approval, f"{argv} must still require approval"


@pytest.mark.parametrize(
    ("argv", "expected_mode"),
    [
        # (a) The SAME trigger flag in the subcommand's OWN region still downgrades correctly.
        (["llmdbenchmark", "standup", "--dry-run", "-p", "ns"], READ_ONLY),
        (["llmdbenchmark", "standup", "-n", "-p", "ns"], READ_ONLY),
        (["llmdbenchmark", "--spec", "cicd/kind", "run", "-p", "ns", "-l", "inference-perf",
          "-w", "sanity_random.yaml", "--dry-run"], READ_ONLY),
        (["llmdbenchmark", "--spec", "cicd/kind", "run", "-p", "ns", "--list-endpoints"], READ_ONLY),
        (["llmdbenchmark", "--spec", "cicd/kind", "run", "-p", "ns", "-z"], READ_ONLY),
        (["llmdbenchmark", "--spec", "cicd/kind", "run", "-p", "ns", "--generate-config"], READ_ONLY),
        (["llmdbenchmark", "--spec", "cicd/kind", "plan", "-n"], READ_ONLY),
        (["llmdbenchmark", "-n", "experiment", "-e", "exp.yaml"], MUTATING),  # contrast: global -> mutating
    ],
)
def test_subcommand_region_trigger_still_downgrades(policy, catalog, argv, expected_mode):
    # (a) preserved: a read_only_trigger flag in the subcommand's own region still controls the
    # mode exactly as before — only the GLOBAL-position misuse is closed.
    d = policy.validate(argv, catalog=catalog)
    assert d.allowed and d.mode == expected_mode


def test_nested_pre_token_propagation_is_region_aware():
    # (b) preserved AND the bypass closed at EVERY level. The intentional nested read-only
    # propagation honors a trigger in the region BEFORE a nested subcommand token ONLY when the
    # flag is effective THERE (the intermediate level's merged flags) — a deeper level's OWN
    # trigger flag does NOT downgrade when it appears in an OUTER region where it is not effective.
    policy = {
        "executables": {
            "tool": {
                "global_flags": {"--gver": {"read_only_trigger": True}},  # genuine global trigger
                "subcommands": {
                    "group": {
                        "mode": READ_ONLY,
                        "flags": {"--gdry": {"read_only_trigger": True}},  # GROUP-level trigger
                        "subcommands": {
                            "leaf": {
                                "mode": MUTATING,
                                "flags": {"--ldry": {"read_only_trigger": True}},  # LEAF-own trigger
                                "positionals": [{"value": None, "optional": True}],
                            },
                        },
                    },
                },
            },
        },
    }
    al = CommandPolicy(policy)

    def mode(argv):
        return al.validate(argv).mode

    assert mode(["tool", "group", "leaf"]) == MUTATING
    # (a) leaf-own trigger in the leaf's own region downgrades.
    assert mode(["tool", "group", "leaf", "--ldry"]) == READ_ONLY
    # (b) an INTERMEDIATE (group)-level trigger in the region before the nested leaf token still
    # propagates down — the nested-propagation mechanism is intact.
    assert mode(["tool", "group", "--gdry", "leaf"]) == READ_ONLY
    # (c) a genuine GLOBAL trigger before the group still downgrades.
    assert mode(["tool", "--gver", "group", "leaf"]) == READ_ONLY
    # BUG closed: a leaf-own trigger in the GLOBAL region does NOT downgrade.
    assert mode(["tool", "--ldry", "group", "leaf"]) == MUTATING
    # And a leaf-own trigger in the GROUP region (also not effective there) does NOT downgrade.
    assert mode(["tool", "group", "--ldry", "leaf"]) == MUTATING


def test_workload_name_without_extension_normalizes(policy, catalog):
    d = policy.validate(
        ["llmdbenchmark", "--spec", "cicd/kind", "run", "-p", "ns", "-l", "inference-perf", "-w", "sanity_random"],
        catalog=catalog,
    )
    assert d.allowed


def test_readonly_probes_allowed(policy):
    assert policy.validate(["docker", "info"]).mode == READ_ONLY
    assert policy.validate(["kind", "get", "clusters"]).mode == READ_ONLY
    assert policy.validate(["kubectl", "config", "current-context"]).mode == READ_ONLY
    assert policy.validate(["kubectl", "cluster-info"]).mode == READ_ONLY
    d = policy.validate(["kubectl", "get", "pods", "-n", "llmd-quickstart", "-o", "json"])
    assert d.allowed and d.mode == READ_ONLY


# ---- Phase 64: `oc` is the kubectl-equivalent read-only mirror ------------
# oc must accept EXACTLY the same read-only subcommands as kubectl (same value constraints),
# and DENY the mutating/unknown subcommands kubectl gates behind approval. The per-provider
# playbook (which CLI/toleration/known-issue) is knowledge, not Python — the policy just
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


def test_oc_mirrors_kubectl_readonly_surface(policy):
    """Every read-only kubectl command is accepted under `oc` with the SAME read-only mode."""
    for tail in _OC_KUBECTL_READONLY_CASES:
        oc = policy.validate(["oc", *tail])
        kc = policy.validate(["kubectl", *tail])
        assert kc.allowed and kc.mode == READ_ONLY, f"kubectl {tail} should be read-only"
        assert oc.allowed and oc.mode == READ_ONLY, f"oc {tail} should be read-only like kubectl"
        assert not oc.requires_approval, f"oc {tail} must auto-run (read-only)"


def test_oc_value_constraints_match_kubectl(policy):
    """oc enforces the SAME shared value constraints as kubectl (it references the same refs)."""
    # Bad namespace (uppercase violates the RFC1123 label) is rejected on BOTH.
    assert not policy.validate(["oc", "get", "pods", "-n", "BadNS", "-o", "json"]).allowed
    assert not policy.validate(["kubectl", "get", "pods", "-n", "BadNS", "-o", "json"]).allowed
    # An off-enum resource is rejected on BOTH (kubectl_resource enum is shared).
    assert not policy.validate(["oc", "get", "secrets", "-o", "json"]).allowed
    assert not policy.validate(["kubectl", "get", "secrets", "-o", "json"]).allowed
    # A bad output format is rejected on BOTH (output_format enum is shared).
    assert not policy.validate(["oc", "get", "pods", "-o", "evil"]).allowed


def test_oc_denies_mutating_and_unknown_subcommands(policy):
    """oc is a strictly READ-ONLY mirror — kubectl's mutating subcommands are NOT mirrored,
    and unknown subcommands are denied (no apply/patch/delete surface added in this phase)."""
    # apply/delete ARE policy-allowed under kubectl (mutating) but DELIBERATELY absent on oc.
    assert policy.validate(["kubectl", "apply", "-f", "job.yaml"]).allowed
    assert not policy.validate(["oc", "apply", "-f", "job.yaml"]).allowed
    assert not policy.validate(["oc", "delete", "job", "myjob"]).allowed
    # patch is policy-allowed on NEITHER (provider toleration patches go via the workspace path).
    assert not policy.validate(["oc", "patch", "deployment", "x", "-p", "{}"]).allowed
    assert not policy.validate(["kubectl", "patch", "deployment", "x", "-p", "{}"]).allowed
    # An entirely unknown oc subcommand is denied.
    assert not policy.validate(["oc", "login", "https://api.cluster:6443"]).allowed
    # Shell metacharacters are screened on oc too.
    assert not policy.validate(["oc", "get", "pods", "-n", "ns; rm -rf /"]).allowed


def test_git_clone_llmd_allowed(policy):
    d = policy.validate(["git", "clone", "https://github.com/llm-d/llm-d-benchmark"])
    assert d.allowed and d.mode == MUTATING


def test_git_clone_skills_allowed(policy):
    # The incubation skills library is the third permitted clone target.
    d = policy.validate(["git", "clone", "https://github.com/llm-d-incubation/llm-d-skills"])
    assert d.allowed and d.mode == MUTATING


def test_git_rev_parse_short_head_allowed_read_only(policy):
    # Reproducibility provenance capture: a SHORT commit SHA. Read-only (inspects git state),
    # auto-runs — it must NOT widen any mutating capability.
    d = policy.validate(["git", "rev-parse", "--short", "HEAD"])
    assert d.allowed and d.mode == READ_ONLY and not d.requires_approval
    # Plain rev-parse HEAD + status --porcelain (dirty detection) stay read-only too.
    assert policy.validate(["git", "rev-parse", "HEAD"]).mode == READ_ONLY
    assert policy.validate(["git", "status", "--porcelain"]).mode == READ_ONLY


def test_git_run_config_replay_stays_mutating_and_approval_gated(policy, catalog):
    # The reproduce path's -c replay is the EXISTING mutating, approval-gated run — reproduction
    # adds no new mutation capability (the only policy change is the read-only --short flag).
    d = policy.validate(
        ["llmdbenchmark", "--spec", "cicd/kind", "run", "-c", "run-config.yaml", "-p", "test"],
        catalog=catalog,
    )
    assert d.allowed and d.mode == MUTATING and d.requires_approval


def test_install_sh_uv_allowed(policy):
    d = policy.validate(["install.sh", "--uv"])
    assert d.allowed and d.mode == MUTATING


def test_kind_create_cluster_allowed_and_mutating(policy):
    d = policy.validate(["kind", "create", "cluster", "--name", "llmd-quickstart"])
    assert d.allowed and d.mode == MUTATING and d.requires_approval


def test_kind_create_cluster_with_wait_allowed(policy):
    d = policy.validate(["kind", "create", "cluster", "--name", "llmd-quickstart", "--wait", "120s"])
    assert d.allowed and d.mode == MUTATING


def test_kind_delete_cluster_allowed(policy):
    d = policy.validate(["kind", "delete", "cluster", "--name", "llmd-quickstart"])
    assert d.allowed and d.mode == MUTATING


def test_install_prereqs_allowed_and_mutating(policy):
    d = policy.validate(["install_prereqs.sh", "--all"])
    assert d.allowed and d.mode == MUTATING and d.requires_approval


def test_install_prereqs_kind_version_allowed(policy):
    d = policy.validate(["install_prereqs.sh", "--kind", "--kind-version", "v0.31.0"])
    assert d.allowed and d.mode == MUTATING


def test_install_metrics_server_allowed_and_mutating(policy):
    # The per-cluster metrics-server installer is mutating (touches kube-system) → approval-gated.
    d = policy.validate(["install_metrics_server.sh", "--kubelet-insecure-tls"])
    assert d.allowed and d.mode == MUTATING and d.requires_approval


def test_install_metrics_server_version_allowed(policy):
    d = policy.validate(["install_metrics_server.sh", "--version", "v0.7.2"])
    assert d.allowed and d.mode == MUTATING


def test_install_metrics_server_has_governance_timeout(policy):
    # The mutating installer declares a per-command deadline (DATA, not Python).
    d = policy.validate(["install_metrics_server.sh"])
    assert d.timeout_s == 300


def test_install_deps_allowed_and_mutating(policy):
    # UPSTREAM llm-d guide client-prereq installer (helm/helmfile/kustomize/yq/kubectl).
    d = policy.validate(["install-deps.sh"])
    assert d.allowed and d.mode == MUTATING and d.requires_approval


def test_install_deps_dev_flag_allowed(policy):
    d = policy.validate(["install-deps.sh", "--dev"])
    assert d.allowed and d.mode == MUTATING


def test_install_deps_has_governance_timeout(policy):
    # The mutating guide installer declares a per-command deadline (DATA, not Python).
    d = policy.validate(["install-deps.sh"])
    assert d.timeout_s == 900


# ---- denials --------------------------------------------------------------

def test_unknown_executable_denied(policy):
    assert not policy.validate(["rm", "-rf", "/"]).allowed


def test_kubectl_delete_denied(policy):
    # 'delete' is not an policy-allowed kubectl subcommand
    assert not policy.validate(["kubectl", "delete", "ns", "llmd-quickstart"]).allowed


def test_unknown_flag_now_allowed(policy, catalog):
    # Relaxed flag policy: an unrecognized flag on an policy-allowed subcommand is accepted
    # (its value is consumed + metachar-screened), and the mutating mode is preserved.
    d = policy.validate(
        ["llmdbenchmark", "--spec", "cicd/kind", "standup", "-p", "ns", "--exec", "evil"],
        catalog=catalog,
    )
    assert d.allowed and d.mode == MUTATING and d.requires_approval


def test_unknown_flag_value_still_metachar_screened(policy, catalog):
    # Even an accepted unknown flag's value cannot smuggle shell metacharacters.
    d = policy.validate(
        ["llmdbenchmark", "--spec", "cicd/kind", "standup", "-p", "ns", "--exec", "$(whoami)"],
        catalog=catalog,
    )
    assert not d.allowed


def test_reported_plan_with_l_and_w_flags_allowed(policy, catalog):
    # The exact command from the bug report: plan does not declare -l/-w, but they are now
    # accepted; --dry-run keeps it read-only.
    d = policy.validate(
        ["llmdbenchmark", "--spec", "cicd/kind", "plan", "-p", "llmd-quickstart",
         "-l", "inference-perf", "-w", "sanity_random.yaml", "--dry-run"],
        catalog=catalog,
    )
    assert d.allowed and d.mode == READ_ONLY


def test_shell_metacharacter_denied(policy, catalog):
    for tok in ["ns; rm -rf /", "ns && curl evil", "$(whoami)", "ns|cat", "a`b`"]:
        d = policy.validate(["llmdbenchmark", "--spec", "cicd/kind", "standup", "-p", tok], catalog=catalog)
        assert not d.allowed, tok


def test_git_clone_non_llmd_url_denied(policy):
    assert not policy.validate(["git", "clone", "https://github.com/evil/repo"]).allowed
    assert not policy.validate(["git", "clone", "https://github.com/llm-d-evil/x"]).allowed
    # The incubation org is pinned to exactly llm-d-skills — no other repo under it is allowed.
    assert not policy.validate(["git", "clone", "https://github.com/llm-d-incubation/llm-d-other"]).allowed


def test_bad_namespace_denied(policy, catalog):
    # uppercase violates RFC1123 label
    d = policy.validate(["llmdbenchmark", "--spec", "cicd/kind", "standup", "-p", "BadNS"], catalog=catalog)
    assert not d.allowed


def test_spec_not_in_catalog_denied(policy, catalog):
    d = policy.validate(["llmdbenchmark", "--spec", "guides/does-not-exist", "plan"], catalog=catalog)
    assert not d.allowed


def test_harness_not_in_catalog_denied(policy, catalog):
    d = policy.validate(
        ["llmdbenchmark", "--spec", "cicd/kind", "run", "-p", "ns", "-l", "made-up", "-w", "sanity_random.yaml"],
        catalog=catalog,
    )
    assert not d.allowed


def test_install_sh_unknown_flag_now_allowed(policy):
    # Relaxed policy: unknown flags are accepted on an policy-allowed executable. The script
    # still only acts on its own pinned flags; metachar-laden args remain denied.
    assert policy.validate(["install.sh", "--rm-rf"]).allowed
    assert not policy.validate(["install.sh", "--x", "$(evil)"]).allowed


def test_empty_argv_denied(policy):
    assert not policy.validate([]).allowed


def test_missing_subcommand_denied(policy, catalog):
    assert not policy.validate(["llmdbenchmark", "--spec", "cicd/kind"], catalog=catalog).allowed


def test_unexpected_positional_denied(policy):
    # kubectl cluster-info takes no positionals
    assert not policy.validate(["kubectl", "cluster-info", "extra"]).allowed


def test_flag_missing_value_denied(policy, catalog):
    assert not policy.validate(["llmdbenchmark", "--spec", "cicd/kind", "standup", "-p"], catalog=catalog).allowed


def test_kind_create_bad_cluster_name_denied(policy):
    # uppercase / underscore violate the cluster_name constraint
    assert not policy.validate(["kind", "create", "cluster", "--name", "Bad_Name"]).allowed


def test_kind_create_wrong_positional_denied(policy):
    # only the literal 'cluster' positional is allowed
    assert not policy.validate(["kind", "create", "node"]).allowed


def test_kind_unknown_subcommand_denied(policy):
    assert not policy.validate(["kind", "load", "docker-image", "x"]).allowed


def test_install_prereqs_unknown_flag_now_allowed(policy):
    # Relaxed policy: unknown flags are accepted; the script ignores anything outside its
    # pinned set. Metachar-laden args are still rejected by the screen.
    assert policy.validate(["install_prereqs.sh", "--rm-rf"]).allowed
    assert not policy.validate(["install_prereqs.sh", "--x", "a;b"]).allowed


def test_install_prereqs_bad_kind_version_denied(policy):
    # kind_version must look like vX.Y.Z
    assert not policy.validate(["install_prereqs.sh", "--kind", "--kind-version", "latest; rm -rf /"]).allowed


def test_install_metrics_server_bad_version_denied(policy):
    # metrics_server_version must look like vX.Y.Z — no shell injection through --version.
    assert not policy.validate(["install_metrics_server.sh", "--version", "latest; rm -rf /"]).allowed


def test_install_deps_metachar_arg_denied(policy):
    # The guide installer's args are still metachar-screened — no shell injection.
    assert not policy.validate(["install-deps.sh", "--dev; rm -rf /"]).allowed


# ---- positional shape invariant: a `repeated` spec must be LAST ------------
# The positional walker keeps a `repeated` spec on the stack so it matches every following token;
# a `repeated` spec placed before another positional would silently swallow the next positional's
# tokens. The loader enforces "repeated must be last" LOUDLY so a future policy edit can't slip
# that past — these tests pin both the load-time rejection and that a legitimate trailing-repeated
# spec (mirroring the live `results add <paths...>` shape) still loads.

def test_real_policy_loads_with_repeated_last(policy):
    # The shipped security/command_policy.yaml has trailing `repeated` positionals (results store);
    # loading it via the fixture must not raise — the invariant holds for the real policy.
    assert policy is not None


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
    with pytest.raises(CommandPolicyError, match="repeated"):
        CommandPolicy(policy)


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
    al = CommandPolicy(policy)
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
    with pytest.raises(CommandPolicyError, match="repeated"):
        CommandPolicy(policy)
