#!/usr/bin/env bash
# kind_egress_heal.sh — re-add the kind bridge's iptables FORWARD/CT/BRIDGE ACCEPT rules that a
# WSL2/Docker restart wipes (and Docker does NOT restore). Without them a RUNNING pod has no internet
# egress: the agent's first LLM call to api.anthropic.com hangs forever (UI "Thinking…", no error).
#
# Idempotent + best-effort (never fails the caller). The kind docker-network id — hence the bridge
# name — changes whenever the cluster is recreated, so we derive it at runtime, never hard-code it.
# A fresh `kind create cluster` already installs these rules, so this is a no-op there; it only heals
# the reused-cluster-after-a-restart case. Requires root (invoked via sudo from install.sh, or by the
# kind-egress-heal.service systemd unit that runs it on every Docker (re)start).
set -uo pipefail
have() { command -v "$1" >/dev/null 2>&1; }
have docker   || exit 0
have iptables || exit 0

# Bridge for the kind docker network (e.g. br-f5905eab2758); empty when no kind network exists yet.
B="$(docker network inspect kind -f '{{ printf "br-%s" (slice .Id 0 12) }}' 2>/dev/null || true)"
[[ -n "$B" ]] || exit 0
ip -o link show "$B" >/dev/null 2>&1 || exit 0   # network known to docker but bridge not up — skip

# DOCKER-CT / DOCKER-FORWARD / DOCKER-BRIDGE are the chains from Docker Engine 28's nftables refactor
# (2025+). On older Docker these chains don't exist, so every ensure_rule below no-ops harmlessly — this
# heal targets current Docker (the project's kind stack ships with it).
# Add a rule only if an identical one isn't already present (iptables -C), so repeated runs don't stack.
ensure_rule() { iptables -C "$@" 2>/dev/null || iptables -A "$@" 2>/dev/null || true; }
ensure_rule DOCKER-CT      -o "$B" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
ensure_rule DOCKER-FORWARD -i "$B" -j ACCEPT
ensure_rule DOCKER-BRIDGE  -o "$B" -j DOCKER
exit 0
