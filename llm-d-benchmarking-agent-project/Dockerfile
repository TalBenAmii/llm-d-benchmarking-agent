# Production image for the llm-d Benchmarking Assistant agent service.
#
# SELF-CONTAINED "full-bake": beyond the FastAPI app, this image carries everything the agent's
# verified local-CLI benchmark path needs so that path works with NO host mounts when the agent
# runs in-cluster:
#   * the `llmdbenchmark` CLI (upstream llm-d-benchmark) in its OWN venv at
#     /repos/llm-d-benchmark/.venv — the app resolves it at that ABSOLUTE path (policy "venv-bin"
#     runner, cwd = /repos/llm-d-benchmark). It is ALSO symlinked onto PATH so the SAME image can
#     double as the ORCHESTRATOR_IMAGE for in-cluster benchmark Jobs (which resolve `llmdbenchmark`
#     via the container PATH, not the policy).
#   * all THREE sibling upstream repos under /repos — llm-d, llm-d-benchmark, llm-d-skills — because
#     the app's readiness self-check AND the skill-grounding gate both require every one present on
#     disk under $REPOS_DIR.
#   * the client toolchain the CLI shells out to at runtime: kubectl, helm (+ the helm-diff plugin
#     helmfile calls as `helm diff`), helmfile, kustomize, yq, jq, skopeo, crane, and git — all
#     pinned for reproducible builds.
#
# COST / REQUIREMENTS (know before you build):
#   * LARGE image (~1 GB): the CLI venv alone is ~700 MB of transformers/scipy/pandas/numpy/
#     kubernetes, on top of the repos and the toolchain.
#   * The build needs NETWORK EGRESS: it git-clones the three repos (pinned refs) and pip-installs
#     from PyPI + GitHub. The sibling repos live OUTSIDE the build context (context = the project
#     dir) and are read-only / empty in a git worktree, so they CANNOT be COPYed — they are cloned
#     at build time instead.
#
# Two stages: a builder that resolves the APP's Python deps into /opt/venv, and a runtime that adds
# the CLI venv, the repos, and the toolchain. Runs as a non-root user (uid 10001) under a read-only
# root filesystem — /repos and both venvs are made world-readable; only /workspace and /tmp are
# writable at runtime (helm's cache/config are pointed at /tmp for that reason). Secrets are NEVER
# baked in — they come from the .env / a K8s Secret at runtime (see deploy/helm). `.dockerignore`
# keeps `.env`, the workspace scratch, and the local venv out of the build context.
#
# Build:   docker build -t llm-d-benchmarking-agent:0.1.0 .
# Run:     docker run --rm -p 8000:8000 --env-file .env llm-d-benchmarking-agent:0.1.0

ARG PYTHON_VERSION=3.11
ARG KUBECTL_VERSION=v1.31.0

# ---- builder ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    VENV=/opt/venv

WORKDIR /build

# Create an isolated venv we can copy wholesale into the runtime stage.
RUN python -m venv "$VENV"
ENV PATH="$VENV/bin:$PATH"

# Resolve the app + its dependencies into the venv. NOTE: `COPY app` precedes this RUN, so an
# app-only edit busts the cache here and re-runs the full dependency install (deps + app together).
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install .

# ---- runtime ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

# Re-declare the ARGs this stage consumes (ARGs do not cross a FROM boundary). The client-toolchain
# pins go here (used by the toolchain layer below); the repo refs are declared LATER, just before
# the clone layer, so bumping a ref (the common case) does NOT bust the expensive toolchain layer.
ARG KUBECTL_VERSION
ARG HELM_VERSION=3.19.0
ARG HELMFILE_VERSION=1.5.1
ARG HELM_DIFF_VERSION=3.13.0
ARG KUSTOMIZE_VERSION=5.8.1
ARG YQ_VERSION=4.44.6
ARG JQ_VERSION=1.7.1
ARG SKOPEO_VERSION=1.20.1
ARG CRANE_VERSION=0.21.7

# REPOS_DIR: the app resolves the CLI + the three sibling repos under it; it defaults to
#   PROJECT_ROOT.parent (which would be "/" here), so pin it to where we clone the repos.
# HOME: useradd's default /home/agent is on the read-only root fs and HOME passes through to
#   subprocesses, so git/kubectl cache writes to ~ would fail — point it at writable /tmp.
# HELM_DATA_HOME: where the baked helm-diff plugin lives (world-readable, read-only at runtime).
# HELM_CACHE_HOME/HELM_CONFIG_HOME: helm needs these WRITABLE, but the root fs is read-only at
#   runtime, so point them at /tmp (a writable mount).
# CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: bundles DISABLE_AUTOUPDATER + telemetry-off; the baked
#   `claude` CLI (installed below) sits on the read-only root fs, so its self-updater — which writes a
#   new version under HOME — would fail. Disable it (and non-essential network) outright.
ENV VENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    REPOS_DIR=/repos \
    HOME=/tmp \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    HELM_DATA_HOME=/opt/helm/data \
    HELM_CACHE_HOME=/tmp/helm/cache \
    HELM_CONFIG_HOME=/tmp/helm/config

# --- client toolchain (pinned) ----------------------------------------------------------
# The `llmdbenchmark` CLI shells out to all of these at runtime; the agent also runs policy-allowed
# kubectl directly (orchestrator + observability). curl is installed only to fetch the binaries and
# is purged afterwards; git stays (the CLI + readiness/provenance shell out to it). Binaries land in
# /usr/local/bin (on PATH). Kept in one layer so the whole toolchain caches together and only one
# apt install/purge cycle runs.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends git ca-certificates curl tar gzip; \
    arch="$(dpkg --print-architecture)"; \
    # crane's release assets use uname-style arch tokens; everything else uses the dpkg token.
    case "$arch" in \
        amd64) crane_arch=x86_64 ;; \
        arm64) crane_arch=arm64 ;; \
        *) echo "unsupported arch: $arch" >&2; exit 1 ;; \
    esac; \
    tmp="$(mktemp -d)"; cd "$tmp"; \
    # kubectl (reused from the previous image revision)
    curl -fsSL -o /usr/local/bin/kubectl \
        "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${arch}/kubectl"; \
    # helm
    curl -fsSL "https://get.helm.sh/helm-v${HELM_VERSION}-linux-${arch}.tar.gz" | tar xz; \
    install -m 0755 "linux-${arch}/helm" /usr/local/bin/helm; \
    # helmfile
    curl -fsSL "https://github.com/helmfile/helmfile/releases/download/v${HELMFILE_VERSION}/helmfile_${HELMFILE_VERSION}_linux_${arch}.tar.gz" \
        | tar xz helmfile; \
    install -m 0755 helmfile /usr/local/bin/helmfile; \
    # kustomize (the tag path is URL-encoded: "kustomize/vX" -> "kustomize%2FvX")
    curl -fsSL "https://github.com/kubernetes-sigs/kustomize/releases/download/kustomize%2Fv${KUSTOMIZE_VERSION}/kustomize_v${KUSTOMIZE_VERSION}_linux_${arch}.tar.gz" \
        | tar xz kustomize; \
    install -m 0755 kustomize /usr/local/bin/kustomize; \
    # crane (go-containerregistry bundle; we only need the `crane` binary)
    curl -fsSL "https://github.com/google/go-containerregistry/releases/download/v${CRANE_VERSION}/go-containerregistry_Linux_${crane_arch}.tar.gz" \
        | tar xz crane; \
    install -m 0755 crane /usr/local/bin/crane; \
    # yq / jq / skopeo are single static binaries. NOTE: containers/skopeo ships NO upstream Linux
    # binary, so skopeo comes from the lework/skopeo-binary static-build mirror (pinned to the same
    # version) — see NOTICE / the build risks noted in the PR.
    curl -fsSL -o /usr/local/bin/yq "https://github.com/mikefarah/yq/releases/download/v${YQ_VERSION}/yq_linux_${arch}"; \
    curl -fsSL -o /usr/local/bin/jq "https://github.com/jqlang/jq/releases/download/jq-${JQ_VERSION}/jq-linux-${arch}"; \
    curl -fsSL -o /usr/local/bin/skopeo "https://github.com/lework/skopeo-binary/releases/download/v${SKOPEO_VERSION}/skopeo-linux-${arch}"; \
    chmod 0755 /usr/local/bin/kubectl /usr/local/bin/yq /usr/local/bin/jq /usr/local/bin/skopeo; \
    # helm-diff is a helm PLUGIN (helmfile invokes `helm diff`): unpack it into HELM_DATA_HOME/plugins.
    # The tarball's top-level dir is `diff/` (plugin.yaml + bin/diff), so extract straight in.
    mkdir -p "${HELM_DATA_HOME}/plugins"; \
    curl -fsSL "https://github.com/databus23/helm-diff/releases/download/v${HELM_DIFF_VERSION}/helm-diff-linux-${arch}.tgz" \
        | tar xz -C "${HELM_DATA_HOME}/plugins"; \
    chmod -R a+rX "${HELM_DATA_HOME}"; \
    cd /; rm -rf "$tmp"; \
    apt-get purge -y curl; apt-get autoremove -y; rm -rf /var/lib/apt/lists/*

# --- claude CLI (native, pinned) --------------------------------------------------------
# The cluster-service deploy path defaults to the `claude-agent-sdk` LLM provider, whose Python
# package spawns THIS `claude` binary as a subprocess — so it must sit on PATH for uid 10001. The
# official native installer drops a single self-contained glibc binary (NO Node.js/npm) and symlinks
# ~/.local/bin/claude -> ~/.local/share/claude/versions/<v>; HOME=/tmp is a fresh emptyDir at runtime,
# so we copy the RESOLVED binary to a fixed system path and discard the HOME-side install tree. Its
# own layer (curl re-added just for the fetch, then purged; git + ca-certificates stay from above) so
# a version bump doesn't bust the toolchain layer. `claude --version` fails the build if install broke.
ARG CLAUDE_CODE_VERSION=2.1.193
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl; \
    cd /tmp; \
    curl -fsSL https://claude.ai/install.sh | bash -s "${CLAUDE_CODE_VERSION}"; \
    install -m 0755 "$(readlink -f "$HOME/.local/bin/claude")" /usr/local/bin/claude; \
    rm -rf "$HOME/.local" "$HOME/.claude"; \
    claude --version; \
    apt-get purge -y curl; apt-get autoremove -y; rm -rf /var/lib/apt/lists/*

# Non-root runtime user (uid:gid 10001). The K8s securityContext also enforces this.
RUN groupadd --gid 10001 agent \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin agent

# --- bundled upstream repos + the benchmark CLI venv ------------------------------------
# Pinned refs for the three sibling repos (overridable via --build-arg so the Makefile/CI can bump
# them). llm-d + llm-d-skills are pinned to exact SHAs (NOT "main") so the build is reproducible;
# llm-d-benchmark to its release TAG. Declared here (not up top) to keep the toolchain layer cached
# across ref bumps.
ARG BENCH_REF=v0.7.0
ARG LLMD_REF=0227212d78661ef184446c875adb4972fda2237c
ARG SKILLS_REF=84c100c6ec6f4055b4d8132a6eb9bfa326e6c1d4

# Clone all three sibling repos at their pinned refs, then build the CLI venv the app expects at
# /repos/llm-d-benchmark/.venv. We install the package editable off the cloned source and add the
# pinned planner extra (a git dep the CLI needs that llm-d-benchmark does NOT declare). This is the
# deterministic path — we do NOT run the upstream install.sh, which can be interactive/non-hermetic.
RUN set -eux; \
    mkdir -p /repos; \
    # SHA-pinned repos: can't `--branch` a bare SHA, so init + shallow-fetch the exact commit.
    # (GitHub allows fetching an arbitrary reachable SHA, so --depth 1 stays cheap.)
    fetch_sha() { \
        mkdir -p "$1"; \
        git -C "$1" init -q; \
        git -C "$1" remote add origin "$2"; \
        git -C "$1" fetch -q --depth 1 origin "$3"; \
        git -C "$1" checkout -q FETCH_HEAD; \
    }; \
    fetch_sha /repos/llm-d        https://github.com/llm-d/llm-d.git                     "${LLMD_REF}"; \
    fetch_sha /repos/llm-d-skills https://github.com/llm-d-incubation/llm-d-skills.git   "${SKILLS_REF}"; \
    # Tag-pinned repo: `--branch <tag> --depth 1` is the simplest shallow clone.
    git clone --depth 1 --branch "${BENCH_REF}" https://github.com/llm-d/llm-d-benchmark.git /repos/llm-d-benchmark; \
    # The CLI venv the app resolves at $REPOS_DIR/llm-d-benchmark/.venv/bin/llmdbenchmark.
    python -m venv /repos/llm-d-benchmark/.venv; \
    /repos/llm-d-benchmark/.venv/bin/pip install --no-cache-dir --upgrade pip setuptools wheel; \
    /repos/llm-d-benchmark/.venv/bin/pip install --no-cache-dir -e /repos/llm-d-benchmark; \
    /repos/llm-d-benchmark/.venv/bin/pip install --no-cache-dir \
        "git+https://github.com/llm-d-incubation/llm-d-planner.git@v0.1.0"; \
    # uid 10001 must be able to read the repos + venv under a read-only root fs.
    chmod -R a+rX /repos; \
    # Put the CLI on PATH so this image can also serve as the ORCHESTRATOR_IMAGE (the Job path
    # resolves `llmdbenchmark` via PATH). The console-script's shebang points at the venv python.
    ln -s /repos/llm-d-benchmark/.venv/bin/llmdbenchmark /usr/local/bin/llmdbenchmark

WORKDIR /app

# The resolved APP venv from the builder + the app source (which carries the static UI at
# app/ui/) and the data files it reads at runtime (the policy, the knowledge files, and
# scripts/ — the policy wires scripts/bridges/capacity_check.py + aggregate_runs.py +
# provision_hf_secret.py to run via the CLI venv). The .env is NOT baked in — it is configured
# at deploy time.
COPY --from=builder /opt/venv /opt/venv
COPY app ./app
COPY security ./security
COPY knowledge ./knowledge
COPY scripts ./scripts

# Attribution for the bundled Apache-2.0 upstreams this image redistributes.
COPY NOTICE ./NOTICE

USER 10001:10001

EXPOSE 8000

# Container-level liveness check (Kubernetes uses its own httpGet probe; this helps `docker run`).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request,sys; urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('PORT','8000')}/healthz\"); sys.exit(0)"]

# Bind on all interfaces inside the container; the env HOST/PORT above can override.
CMD ["sh", "-c", "exec uvicorn app.main:app --host \"$HOST\" --port \"$PORT\""]
