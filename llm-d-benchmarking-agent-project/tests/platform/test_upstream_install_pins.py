"""Fresh-clone revision selection must never reset an existing upstream checkout."""
import shlex
import subprocess
from pathlib import Path

import pytest

HELPERS = Path(__file__).resolve().parents[2] / 'scripts' / '_env.sh'


@pytest.mark.parametrize('present', [False, True])
def test_clone_pin_only_applies_to_new_checkout(tmp_path, present):
    destination = tmp_path / 'repo with spaces'
    if present:
        destination.mkdir()
        (destination / 'local-work').write_text('preserve me')
    script = f'''
set -e
source {shlex.quote(str(HELPERS))}
log() {{ :; }}
die() {{ exit 1; }}
git() {{ printf '%s\\n' "$*"; }}
clone_if_missing llm-d {shlex.quote(str(destination))} llm-d release-ref
'''
    result = subprocess.run(['bash', '-c', script], capture_output=True, text=True, check=True)
    if present:
        assert result.stdout == ''
        assert (destination / 'local-work').read_text() == 'preserve me'
    else:
        lines = result.stdout.splitlines()
        assert len(lines) == 3
        assert lines[0].startswith('clone --depth 1 https://github.com/llm-d/llm-d ')
        assert lines[1].endswith('fetch --depth 1 origin release-ref')
        assert lines[2].endswith('checkout --detach FETCH_HEAD')
