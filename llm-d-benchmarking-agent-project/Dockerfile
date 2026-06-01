# Production image for the llm-d Benchmarking Assistant agent service.
#
# Two stages: a builder that resolves the Python dependencies into a wheel cache + venv, and a
# slim runtime that carries only the venv, the app, and the `kubectl` binary the agent shells
# out to (the orchestrator and the observe_run_metrics tool run allowlisted `kubectl`). The
# image runs as a non-root user; secrets are NEVER baked in (they come from the .env / a K8s
# Secret at runtime — see deploy/helm and deploy/kustomize). `.dockerignore` keeps `.env`, the
# workspace scratch, and the local venv out of the build context.
#
# Build:   docker build -t llm-d-benchmarking-agent:0.1.0 .
# Run:     docker run --rm -p 8000:8000 --env-file .env llm-d-benchmarking-agent:0.1.0
#
# The same image serves as the ORCHESTRATOR_IMAGE for in-cluster benchmark Jobs only when the
# benchmark CLI is available on PATH; for the kind/sim quickstart the local CLI path
# (execute_llmdbenchmark) is used instead. See knowledge/packaging.md.

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

# Install dependencies first (better layer caching), then the app itself.
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install .

# ---- runtime ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

ARG KUBECTL_VERSION
ENV VENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 \
    PORT=8000

# kubectl: the agent shells out to it (orchestrator + observability), so it must be on PATH.
# curl is used only here to fetch the pinned kubectl, then removed to keep the image lean.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl ca-certificates; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in amd64) karch=amd64 ;; arm64) karch=arm64 ;; *) echo "unsupported arch $arch" >&2; exit 1 ;; esac; \
    curl -fsSL -o /usr/local/bin/kubectl \
        "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${karch}/kubectl"; \
    chmod +x /usr/local/bin/kubectl; \
    apt-get purge -y curl; \
    apt-get autoremove -y; \
    rm -rf /var/lib/apt/lists/*

# Non-root runtime user (uid:gid 10001). The K8s securityContext also enforces this.
RUN groupadd --gid 10001 agent \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin agent

WORKDIR /app

# The resolved venv from the builder + the app source and the data files it reads at runtime
# (the allowlist, the knowledge files, the static UI). The two sibling repos and .env are NOT
# baked in — they are mounted / configured at deploy time.
COPY --from=builder /opt/venv /opt/venv
COPY app ./app
COPY security ./security
COPY knowledge ./knowledge
COPY ui ./ui

USER 10001:10001

EXPOSE 8000

# Container-level liveness check (Kubernetes uses its own httpGet probe; this helps `docker run`).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request,sys; urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('PORT','8000')}/healthz\"); sys.exit(0)"]

# Bind on all interfaces inside the container; the env HOST/PORT above can override.
CMD ["sh", "-c", "exec uvicorn app.main:app --host \"$HOST\" --port \"$PORT\""]
