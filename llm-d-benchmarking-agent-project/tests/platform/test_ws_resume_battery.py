"""Resume battery 3a, over the wire: park an approval gate → drop the socket → reconnect
WITH an ``after_seq`` cursor → the pending card re-emits → approve → the SAME parked turn
completes to ``done`` and the gate clears.

The whole sequence (and its assertions: gate persisted across the drop, re-emitted on
reconnect, cleared + decision recorded after resolve) is ``Player.act_reconnect_midturn`` —
the hard-won BUG-G regression action from the self-play driver. Here it runs DETERMINISTICALLY
(a fixed-outcome Random pins the incremental after_seq reconnect + the approve decision, the
two paths the battery specifies) instead of seed-weighted.
"""
from __future__ import annotations

import random

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from tests.eval.app_driver import Player, install_isolated_state

pytestmark = pytest.mark.skipif(
    not get_settings().bench_repo.is_dir(), reason="bench repo not present"
)


class _FixedRandom(random.Random):
    """Pins act_reconnect_midturn's two choices: 0.4 < 0.5 → the after_seq (incremental)
    reconnect; 0.4 < 0.6 → approve the re-emitted gate."""

    def random(self) -> float:
        return 0.4


def test_parked_gate_survives_reconnect_with_after_seq(tmp_path):
    from app.main import app

    with TestClient(app) as client:
        primer = install_isolated_state(app, tmp_path)
        player = Player(app, client, primer, _FixedRandom())
        try:
            player.act_reconnect_midturn()
            player.check_all()
        finally:
            player.finish()
