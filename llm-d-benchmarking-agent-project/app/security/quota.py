"""Usage-quota counter — pure MECHANISM (Phase 13).

The CAPS are DATA (``security/allowlist.yaml`` → a command's ``quota`` block); this file
only counts and compares. It holds no per-command knowledge: it increments a count against
whatever opaque ``key`` the policy resolved (the executable[+subcommand]) and answers
"would running this once more exceed the cap the policy supplied?".

Two windows are tracked:
  * per-session — counts for the lifetime of this counter instance (one per session).
  * per-day     — counts bucketed by calendar day (UTC), so the window rolls at midnight.

A clock function is injectable so tests can advance days deterministically with no real
sleeps (and so the per-day window is exercised hermetically).
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime


def _utc_today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


@dataclass
class QuotaExceeded(Exception):
    """Raised (pre-execution) when running a command once more would break its cap."""

    key: str
    window: str  # "per_session" | "per_day"
    cap: int
    used: int

    def __str__(self) -> str:  # pragma: no cover - trivial formatting
        return (
            f"usage quota exceeded for {self.key!r}: {self.window} cap is {self.cap}, "
            f"already used {self.used} this {self.window.removeprefix('per_')}"
        )


class QuotaCounter:
    """Per-session usage counter. Mechanism only; caps are passed in per check."""

    def __init__(self, *, now: Callable[[], str] = _utc_today):
        self._now = now
        self._session_counts: dict[str, int] = defaultdict(int)
        # day -> key -> count
        self._day_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    def session_used(self, key: str) -> int:
        return self._session_counts.get(key, 0)

    def day_used(self, key: str) -> int:
        return self._day_counts.get(self._now(), {}).get(key, 0)

    def check(self, key: str, *, per_session: int | None, per_day: int | None) -> None:
        """Refuse (raise :class:`QuotaExceeded`) if the NEXT use would exceed a cap.
        Called BEFORE execution / before the approval prompt. A ``None`` cap is unlimited.
        This does NOT mutate state — call :meth:`record` only once the command actually runs."""
        if per_session is not None and self.session_used(key) >= per_session:
            raise QuotaExceeded(key=key, window="per_session", cap=per_session,
                                used=self.session_used(key))
        if per_day is not None and self.day_used(key) >= per_day:
            raise QuotaExceeded(key=key, window="per_day", cap=per_day, used=self.day_used(key))

    def record(self, key: str) -> None:
        """Tally one successful (permitted) use against both windows."""
        self._session_counts[key] += 1
        self._day_counts[self._now()][key] += 1
