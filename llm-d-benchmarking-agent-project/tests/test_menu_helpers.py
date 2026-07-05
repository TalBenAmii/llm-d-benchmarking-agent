"""Exercise the arrow-key menu helpers in scripts/_env.sh (menu_select / confirm).

A real pseudo-terminal (stdlib `pty`) is allocated so the arrow-key path is genuinely
driven, not just the non-interactive fallback. The menu renders on /dev/tty while the
selected index goes to stdout, so the bash snippet redirects stdout to a temp file and we
read the index from there (both would otherwise land on the same pty).
"""

import os
import pty
import select
import signal
import subprocess
import tempfile
import time
from pathlib import Path

ENV_SH = Path(__file__).resolve().parent.parent / "scripts" / "_env.sh"


def _drive_pty(inner: str, keys: bytes, timeout: float = 3.0) -> tuple[str, int]:
    """Run `source _env.sh; <inner> >tmpfile` under a pty, feed `keys`, return (file_contents, exit_code).

    Fails (via kill + assert) rather than hanging if the child does not exit in `timeout`.
    """
    fd, out_path = tempfile.mkstemp(prefix="menusel_")
    os.close(fd)
    script = f'source "{ENV_SH}"; {inner} >"{out_path}"'
    pid, fd = pty.fork()
    if pid == 0:  # child — becomes the pty session leader, so /dev/tty resolves to it
        try:
            os.execvp("bash", ["bash", "--noprofile", "--norc", "-c", script])
        finally:
            os._exit(127)

    os.write(fd, keys)
    deadline = time.time() + timeout
    hung = False
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            hung = True
            break
        try:
            ready, _, _ = select.select([fd], [], [], remaining)
        except OSError:
            break
        if not ready:
            continue
        try:
            if not os.read(fd, 4096):  # EOF: child closed the pty
                break
        except OSError:  # EIO on Linux when the child exits
            break
    if hung:
        os.kill(pid, signal.SIGKILL)
    _, status = os.waitpid(pid, 0)
    os.close(fd)

    contents = Path(out_path).read_text().strip()
    os.unlink(out_path)  # before the assert, so a hung child doesn't leak the temp file
    assert not hung, "menu helper hung under a pty"
    return contents, os.waitstatus_to_exitcode(status)


def test_menu_select_arrow_navigation():
    # Down, Down, Enter over A/B/C starting at index 0 -> lands on C (index 2).
    idx, code = _drive_pty('menu_select "Pick" 0 A B C', b"\x1b[B\x1b[B\r")
    assert idx == "2"
    assert code == 0


def test_menu_select_enter_takes_default():
    # First key is Enter -> the default index (1) is returned unchanged.
    idx, code = _drive_pty('menu_select "Pick" 1 A B C', b"\r")
    assert idx == "1"
    assert code == 0


def test_confirm_yes_via_arrow():
    # Default N (No highlighted); Up to Yes then Enter -> confirm returns 0.
    rc, code = _drive_pty('confirm "Proceed" N; echo $?', b"\x1b[A\r")
    assert rc == "0"
    assert code == 0


def test_confirm_no_via_arrow():
    # Default Y (Yes highlighted); Down to No then Enter -> confirm returns 1.
    rc, code = _drive_pty('confirm "Proceed" Y; echo $?', b"\x1b[B\r")
    assert rc == "1"
    assert code == 0


def test_non_interactive_fallback_returns_default():
    # No pty and no controlling terminal (start_new_session) -> fallback prints the default
    # index to stdout and exits 0 without hanging.
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c",
         f'source "{ENV_SH}"; menu_select "Pick" 1 A B C </dev/null'],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=5,
        start_new_session=True,
    )
    assert result.stdout.strip() == "1"
    assert result.returncode == 0


def test_tty_interactive_true_under_pty():
    # Under a real pty the child is the terminal's FOREGROUND process group, so _tty_interactive
    # succeeds -> the arrow-key path is taken. This is what keeps the interactive menu working for
    # a human running `bash <(curl …)` / `curl | bash` at a real terminal.
    out, code = _drive_pty('_tty_interactive; echo "rc=$?"', b"")
    assert out == "rc=0"
    assert code == 0


def test_tty_interactive_false_without_controlling_terminal():
    # start_new_session detaches the controlling tty, so /dev/tty cannot be opened and
    # _tty_interactive reports non-interactive (rc=1) without blocking.
    # The other false case — /dev/tty OPENABLE but the process is not the terminal's foreground
    # process group (the real `bash <(curl …)` WSL-non-interactive-exec hang) — is not cleanly
    # reproducible under pytest; it's covered by the fresh-env curl validation + scripts probe.
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c",
         f'source "{ENV_SH}"; _tty_interactive; echo "rc=$?"'],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=5,
        start_new_session=True,
    )
    assert result.stdout.strip() == "rc=1"
    assert result.returncode == 0
