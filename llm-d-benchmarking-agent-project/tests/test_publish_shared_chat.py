"""The publish-a-public-link script (``scripts/publish_shared_chat.sh``).

It renders a shared conversation to a self-contained .html (``app.packaging.shared_chat``) and
uploads it as a SECRET GitHub gist, so a chat gets a public link with the agent never exposed.
These tests exercise everything UP TO the gist upload via ``--dry-run`` — no ``gh``, no network:
the snapshot renders, the right ``gh`` command is composed, and the token/exit-code guards hold.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.storage.share import ShareStore

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "scripts" / "publish_shared_chat.sh"

pytestmark = pytest.mark.skipif(not _SCRIPT.exists(), reason="publish script missing")


def _run(args, workspace):
    """Run the script with the venv python + a tmp workspace; capture (rc, out, err)."""
    env = {**os.environ, "PYTHON": sys.executable, "PYTHONPATH": str(_ROOT),
           "WORKSPACE_DIR": str(workspace)}
    p = subprocess.run(["bash", str(_SCRIPT), *args], capture_output=True, text=True, env=env)
    return p.returncode, p.stdout, p.stderr


def _seed(workspace) -> str:
    return ShareStore(workspace).create(
        items=[{"role": "user", "text": "hi"}, {"role": "assistant", "text": "hello"}],
        title="demo chat", created_at=1.0, source_session_id="sess",
    )


def test_script_is_valid_bash():
    assert subprocess.run(["bash", "-n", str(_SCRIPT)]).returncode == 0


def test_help_exits_zero_and_prints_usage(tmp_path):
    rc, out, _ = _run(["--help"], tmp_path)
    assert rc == 0
    assert "Usage:" in out and "publish_shared_chat.sh" in out


def test_invalid_token_is_rejected_cleanly(tmp_path):
    rc, _, err = _run(["not-a-hex-token"], tmp_path)
    assert rc == 2
    assert "not a valid share token" in err
    assert "chars) 2" not in err   # regression: the exit code must not leak into the message


def test_dry_run_renders_but_uploads_nothing(tmp_path):
    token = _seed(tmp_path)
    rc, out, _ = _run(["--dry-run", token], tmp_path)
    assert rc == 0
    assert "gh gist create" in out and token in out
    assert "dry run" in out and "nothing uploaded" in out


def test_dry_run_accepts_a_pasted_share_link(tmp_path):
    """A user can paste the copied link (https://host/share/<token>), not just the bare token."""
    token = _seed(tmp_path)
    rc, out, _ = _run(["--dry-run", f"https://example.com/share/{token}"], tmp_path)
    assert rc == 0 and token in out and "gh gist create" in out


def test_dry_run_unknown_token_fails_to_render(tmp_path):
    _seed(tmp_path)                       # a DIFFERENT token exists
    rc, _, err = _run(["--dry-run", "a" * 32], tmp_path)
    assert rc == 1
    assert "could not render" in err


def test_revoke_without_a_recorded_gist_exits_one(tmp_path):
    token = _seed(tmp_path)
    rc, _, err = _run(["--revoke", token], tmp_path)
    assert rc == 1
    assert "nothing to revoke" in err


def test_dry_run_revoke_shows_delete_for_a_recorded_gist(tmp_path):
    token = _seed(tmp_path)
    (tmp_path / "shares").mkdir(exist_ok=True)
    (tmp_path / "shares" / f"{token}.gist").write_text("gistid123\n", "utf-8")
    rc, out, _ = _run(["--dry-run", "--revoke", token], tmp_path)
    assert rc == 0
    assert "gh gist delete gistid123" in out
