#!/usr/bin/env python3
"""HuggingFace gated-model secret provisioner — a thin, vetted bridge (Phase 30).

This materializes the cluster's HuggingFace token Secret a gated-model standup needs,
exactly as the upstream ``llm-d/helpers/hf-token.md`` recipe does:

    kubectl create secret generic <name> --from-literal=HF_TOKEN=$HF_TOKEN \
        --namespace <ns> --dry-run=client -o yaml | kubectl apply -f -

WHY A SCRIPT (and not a raw allowlisted ``kubectl create secret``): the token MUST stay
out of every argv. ``ToolContext._emit_command`` (app/tools/context.py) emits the FULL
argv of every executed command into a ``command`` event that reaches the browser, the
log, and the persisted command trail. A ``--from-literal=HF_TOKEN=<token>`` argument
would leak the secret into all of those. So the agent's allowlist/argv NEVER carries the
token: this script reads ``HF_TOKEN`` from the (already-scrubbed) child environment — the
runner injects it from ``settings.extra_subprocess_env`` (app/config.py) exactly as
``capacity_check.py`` reads ``os.environ["HF_TOKEN"]`` — and feeds it to ``kubectl`` over
its OWN subprocess (the token is passed on the inner ``kubectl create`` argv, which this
process spawns directly and which is never surfaced anywhere). The agent only ever sees
``["provision_hf_secret.py", "--namespace", "<ns>", "--name", "<name>"]``.

This is a MUTATING step (it writes a Secret to the cluster). The allowlist marks it
``mode: mutating`` so it is approval-gated like any other cluster mutation: the agent
proposes it, the user clicks Approve, and only then does the runner execute it. WHEN to
provision (only on a Phase 62 GATED+UNAUTHORIZED-with-no-token capacity verdict; never for
a public model) is JUDGMENT and lives in ``knowledge/capacity.md`` — never here.

Contract (mechanism only — no judgment lives here):
  * ``--namespace <ns>`` (required) is the target Kubernetes namespace.
  * ``--name <name>`` (optional) is the Secret name; defaults to the upstream
    ``HF_TOKEN_NAME`` default ``llm-d-hf-token``.
  * ``HF_TOKEN`` is read from the environment (scrubbed child env). It is NEVER an
    argument and is NEVER echoed to stdout/stderr.
  * stdout/stderr surface only kubectl's own apply confirmation (e.g.
    ``secret/llm-d-hf-token created``) — never the token value.
  * exit code 0 on success; non-zero with a token-free error otherwise.

The allowlist pins the two flags' values (``--namespace`` to an RFC1123 label, ``--name``
to an RFC1123 object name) and screens every token for shell metacharacters, so there is
no arbitrary surface beyond this audited file. The script itself runs ``kubectl`` with
``shell=False`` (a real argv list, no shell string) so the pipeline in the upstream recipe
is replaced by an in-process pipe — there is no shell to inject into.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

# Upstream default (llm-d/helpers/hf-token.md: HF_TOKEN_NAME=${HF_TOKEN_NAME:-llm-d-hf-token}).
_DEFAULT_SECRET_NAME = "llm-d-hf-token"

# The env var the secret materializes (and the literal key inside the Secret). Backend-only.
_TOKEN_ENV = "HF_TOKEN"


def _fail(msg: str) -> int:
    """Print a token-free error to stderr and signal failure. The message must never
    interpolate the token value."""
    sys.stderr.write(msg.rstrip("\n") + "\n")
    return 1


def provision(namespace: str, name: str, token: str) -> int:
    """Create-or-update the HF token Secret via the upstream two-stage kubectl shape.

    ``kubectl create secret ... --dry-run=client -o yaml`` renders the Secret manifest
    WITHOUT touching the cluster; piping it into ``kubectl apply -f -`` creates it or
    updates it in place (idempotent — re-running is safe). The token is passed only on the
    inner ``create`` argv, which this process spawns directly; it is never returned to the
    agent's runner and so never appears in any command event/log.
    """
    create_argv = [
        "kubectl", "create", "secret", "generic", name,
        f"--from-literal={_TOKEN_ENV}={token}",
        "--namespace", namespace,
        "--dry-run=client", "-o", "yaml",
    ]
    apply_argv = ["kubectl", "apply", "--namespace", namespace, "-f", "-"]

    # Stage 1: render the manifest YAML (no cluster contact). shell=False — a real argv.
    try:
        rendered = subprocess.run(  # noqa: S603 — fixed argv, shell=False, no user shell
            create_argv,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return _fail("kubectl not found on PATH — cannot provision the HF secret")
    if rendered.returncode != 0:
        # kubectl's own error (e.g. invalid name) — token-free; it never echoes the literal.
        return _fail(
            "failed to render the HF secret manifest "
            f"(kubectl create --dry-run exit {rendered.returncode}): "
            f"{rendered.stderr.strip() or rendered.stdout.strip()}"
        )

    # Stage 2: apply the rendered manifest. The manifest carries the token base64-encoded;
    # it goes to kubectl over stdin, never onto an argv or into our stdout.
    try:
        applied = subprocess.run(  # noqa: S603 — fixed argv, shell=False, no user shell
            apply_argv,
            input=rendered.stdout,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return _fail("kubectl not found on PATH — cannot provision the HF secret")
    if applied.returncode != 0:
        return _fail(
            f"failed to apply the HF secret (kubectl apply exit {applied.returncode}): "
            f"{applied.stderr.strip() or applied.stdout.strip()}"
        )

    # Surface kubectl's own confirmation line (e.g. "secret/llm-d-hf-token created").
    sys.stdout.write(applied.stdout.strip() + "\n")
    sys.stdout.flush()
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="provision_hf_secret.py",
        description="Provision the cluster HuggingFace token Secret for a gated-model standup.",
    )
    parser.add_argument("--namespace", "-p", required=True, help="Target Kubernetes namespace")
    parser.add_argument(
        "--name",
        default=_DEFAULT_SECRET_NAME,
        help=f"Secret name (default: {_DEFAULT_SECRET_NAME})",
    )
    args = parser.parse_args(argv[1:])

    # The token comes from the (already-scrubbed) child env only — NEVER an argument.
    token = os.environ.get(_TOKEN_ENV)
    if not token or token == "REPLACE_TOKEN":
        return _fail(
            f"{_TOKEN_ENV} is not configured in the backend environment — cannot provision "
            "the HF secret. Set it in the backend .env (it stays backend-only) and retry."
        )

    name = args.name or _DEFAULT_SECRET_NAME
    return provision(args.namespace, name, token)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
