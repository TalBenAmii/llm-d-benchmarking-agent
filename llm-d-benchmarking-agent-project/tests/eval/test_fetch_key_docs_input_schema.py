"""The fetch_key_docs input schema bounds — the request-time skill-fetch contract.

max_bytes_each is clamped to [1, 80_000]; task is optional. Guards the tool-arg
validation the agent's skill-grounding call goes through. Hermetic, instant.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.tools.schemas.docs import FetchKeyDocsInput


def test_defaults_are_sane():
    """No args -> no task filter, default byte cap."""
    m = FetchKeyDocsInput()
    assert m.task is None
    assert m.max_bytes_each == 20_000


def test_task_is_optional_free_text():
    """A skill task name is accepted as the filter."""
    assert FetchKeyDocsInput(task="deploy_skill").task == "deploy_skill"


@pytest.mark.parametrize("bad", [0, -1, 80_001])
def test_max_bytes_each_out_of_bounds_rejected(bad):
    """max_bytes_each below 1 or above 80_000 is rejected."""
    with pytest.raises(ValidationError):
        FetchKeyDocsInput(max_bytes_each=bad)


@pytest.mark.parametrize("ok", [1, 20_000, 80_000])
def test_max_bytes_each_within_bounds_accepted(ok):
    """max_bytes_each at the boundaries and midrange is accepted."""
    assert FetchKeyDocsInput(max_bytes_each=ok).max_bytes_each == ok
