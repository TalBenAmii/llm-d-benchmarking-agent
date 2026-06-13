#!/usr/bin/env bash
# UserPromptSubmit hook — inject the local-GPU caveat ONLY when the prompt is about GPU/cluster
# work. Replaces the always-eligible `rtx5070-gpu-cluster` memory: keeps ~600 tokens out of every
# unrelated turn, surfaces the hard-won Blackwell caveat exactly when it matters.
#
# stdout from a UserPromptSubmit hook is appended to the model's context. On no keyword match we
# print nothing (exit 0) and the turn pays zero tokens for it. Disable: GPU_CONTEXT_HOOK_OFF=1.
set -u
[ "${GPU_CONTEXT_HOOK_OFF:-0}" = "1" ] && exit 0

INPUT=$(cat)
PROMPT=$(printf '%s' "$INPUT" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("prompt",""))' 2>/dev/null) || exit 0

# Case-insensitive keyword gate. Broad enough to catch GPU/cluster standup talk, narrow enough
# to stay quiet on ordinary code work.
if printf '%s' "$PROMPT" | grep -Eiq 'gpu|blackwell|sm_120|minikube|nvidia|cuda|vllm|standup|smoketest|device-plugin|kind cluster|cluster'; then
  PAYLOAD="$(dirname "$0")/gpu_context.md"
  [ -f "$PAYLOAD" ] && cat "$PAYLOAD"
fi
exit 0
