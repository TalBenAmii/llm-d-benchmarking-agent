"""Gating for the opt-in ``llm-d-inference-sim`` integration layer (Phase 26).

The integration tests run END TO END against a REAL ``llm-d-inference-sim`` only when BOTH:

  1. ``LLMD_SIM_INTEGRATION=1`` is set (explicit opt-in — never on by default), AND
  2. ``llm-d-inference-sim`` is actually locatable in this environment.

When either is false, every integration test SKIPS cleanly (so the default suite stays
fully hermetic and green, with no new required dependency). The gate is pure mechanism: WHAT
to run against the sim is the test; the locate-the-sim policy below is just discovery.

How the sim is located (most-direct first), all overridable so no path is hardcoded:
  * ``LLMD_SIM_BINARY`` — an explicit path to a ``llm-d-inference-sim`` executable, or its
    name on ``PATH`` (the standalone build / the image's ``/app/llm-d-inference-sim``); else
  * the ``llm-d-inference-sim`` name on ``PATH``; else
  * a container image (``LLMD_SIM_IMAGE``, default ``ghcr.io/llm-d/llm-d-inference-sim``)
    runnable via an available container engine (docker/podman) — image-present check only.

If none resolve, ``sim_available()`` is False and the integration tests skip — they NEVER
hang trying to reach a server that isn't there.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass

import pytest

INTEGRATION_ENV_FLAG = "LLMD_SIM_INTEGRATION"
SIM_BINARY_ENV = "LLMD_SIM_BINARY"
SIM_IMAGE_ENV = "LLMD_SIM_IMAGE"
DEFAULT_SIM_IMAGE = "ghcr.io/llm-d/llm-d-inference-sim"
DEFAULT_SIM_BINARY_NAME = "llm-d-inference-sim"


@dataclass(frozen=True)
class SimLocation:
    """Where the sim was found. ``kind`` is 'binary' or 'image' (or '' when absent)."""

    kind: str
    ref: str

    @property
    def available(self) -> bool:
        return bool(self.kind)


def integration_enabled() -> bool:
    """True iff the opt-in env flag explicitly enables the integration layer."""
    return os.environ.get(INTEGRATION_ENV_FLAG, "").strip().lower() in {"1", "true", "yes"}


def _container_engine() -> str | None:
    for engine in ("docker", "podman"):
        if shutil.which(engine):
            return engine
    return None


def _image_present(engine: str, image: str) -> bool:
    """True iff ``image`` is already pulled (we never pull/network from a test)."""
    try:
        # argv list, shell=False, no user-controlled input.
        proc = subprocess.run(
            [engine, "image", "inspect", image],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def locate_sim() -> SimLocation:
    """Locate ``llm-d-inference-sim`` without running it or touching the network."""
    explicit = os.environ.get(SIM_BINARY_ENV, "").strip()
    if explicit:
        # An explicit path that exists, or a name resolvable on PATH.
        if os.path.isfile(explicit) and os.access(explicit, os.X_OK):
            return SimLocation("binary", explicit)
        found = shutil.which(explicit)
        if found:
            return SimLocation("binary", found)

    on_path = shutil.which(DEFAULT_SIM_BINARY_NAME)
    if on_path:
        return SimLocation("binary", on_path)

    image = os.environ.get(SIM_IMAGE_ENV, "").strip() or DEFAULT_SIM_IMAGE
    engine = _container_engine()
    if engine and _image_present(engine, image):
        return SimLocation("image", image)

    return SimLocation("", "")


def sim_available() -> bool:
    return locate_sim().available


@pytest.fixture(scope="session")
def sim_location() -> SimLocation:
    return locate_sim()


# A module-level marker the integration test applies so the test is COLLECTED (it shows up in
# the run) but SKIPPED with a clear reason whenever the flag/sim aren't both present. This is
# what keeps the default suite hermetic: the test is visible and explicitly skipped.
def integration_skip_reason() -> str | None:
    if not integration_enabled():
        return f"opt-in: set {INTEGRATION_ENV_FLAG}=1 to run the llm-d-inference-sim integration"
    if not sim_available():
        return (
            "llm-d-inference-sim not found "
            f"(set {SIM_BINARY_ENV}=<path> or pull {DEFAULT_SIM_IMAGE})"
        )
    return None


requires_sim_integration = pytest.mark.skipif(
    integration_skip_reason() is not None,
    reason=integration_skip_reason() or "",
)
