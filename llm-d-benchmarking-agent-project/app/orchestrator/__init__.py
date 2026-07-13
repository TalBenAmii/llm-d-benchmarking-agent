"""Kubernetes-native benchmark orchestrator.

Mechanism only (thin code): submit benchmark runs as K8s Jobs, monitor their lifecycle,
classify failures (OOM / timeout / eviction / unschedulable), retry + dead-letter, and
reconstruct in-flight state from the cluster after a restart. All cluster access goes
through the policy-allowed ``kubectl`` runner (see :mod:`app.orchestrator.kube`) — never the
Python kubernetes client, which would bypass the deny-by-default policy + approval gate.
Judgment (which spec/harness/workload, sweep grid) stays with the agent + knowledge files.
"""
