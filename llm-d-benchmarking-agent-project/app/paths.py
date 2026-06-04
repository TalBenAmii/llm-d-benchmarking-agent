"""Shared filesystem path helpers.

Deliberately imports nothing from ``app`` (only the stdlib) so every layer — security,
orchestrator, tools — can import it without risking a circular import.
"""

from pathlib import Path


def is_within(child: Path, parent: Path) -> bool:
    """True if ``child`` is contained in ``parent`` after resolving symlinks and ``..`` on both.

    This is the path-traversal guard. A purely lexical ``Path.relative_to`` treats
    ``/a/../etc`` as living under ``/a`` (it never collapses ``..``), so we ``resolve()``
    first — ``Path.resolve()`` does not require the path to exist (it resolves as far as it
    can and appends the remainder lexically), so this is safe for not-yet-created paths.
    """
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False
