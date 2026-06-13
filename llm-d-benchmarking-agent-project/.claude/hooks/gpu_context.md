<gpu-host-context source="hooks/gpu_context.sh — injected because this prompt looks GPU/cluster-related">
This user's local GPU host (the part the agent can't introspect itself):

- **Windows + WSL2 (Ubuntu 26.04), RTX 5070 Laptop GPU** — Blackwell, compute capability
  **sm_120**, 8 GB VRAM, driver 581.80 / CUDA 13.0.
- **Standup that worked** (see `docs/GPU_CLUSTER_RUNBOOK.md`, written for 40-series Ada but
  applies): `scripts/install_prereqs.sh --docker --kubectl` → NVIDIA Container Toolkit
  (`nvidia-ctk runtime configure --runtime=docker`, restart docker; gate with
  `docker run --gpus all nvidia/cuda:...-base nvidia-smi`) → `minikube start --driver=docker
  --container-runtime=docker --gpus all` (auto-enables nvidia-device-plugin) →
  `minikube addons enable metrics-server`. GPU advertises `nvidia.com/gpu:1` ~30s after the
  device-plugin pod is Running.
- **Docker-group gotcha:** the live shell predates `usermod -aG docker` and `sg`/`newgrp`
  aren't installed → run minikube via `sudo -u roots env HOME=/home/roots minikube ...`
  (sudo re-resolves groups). kubectl/the agent don't need the docker group.

**⚠ Blackwell vLLM caveat (top risk, NOT in the runbook):** sm_120 needs a vLLM image built
with CUDA 12.8+. If the llm-d-deployed vLLM image lacks Blackwell kernels the serve pod dies
at startup with *"no kernel image is available for execution on the device"* (surfaces at
`standup`/`smoketest`). Mitigation: pin a newer vLLM image tag in the scenario, or fall back
to `SIMULATE=1`. FP8 IS available on Blackwell, but start fp16/bf16.

Agent config for GPU runs: `LLM_PROVIDER=claude-agent-sdk` (model claude-haiku-4-5)
authenticates via the local Claude Code session — **no ANTHROPIC_API_KEY needed in .env**.
</gpu-host-context>
