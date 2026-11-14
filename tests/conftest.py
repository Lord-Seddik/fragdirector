"""A small hand-built demo shared by the tests.

Two rounds, 2 v 2, 64 ticks per second.

Round 1 (ticks 1000-5000): p1 kills p3 at 3000, then p4 at 4000. CT win.
Round 2 (ticks 5000-9000): p3 kills p1 at 7000, p2 kills p3 at 7500,
                           p4 kills p2 at 8000. T win.
"""

import pytest

from fragdirector.models import BombEvent, Demo, Kill, Life, Player, Round

DEMO_PATH = "demos/match.dem"
P1, P2, P3, P4 = "76561198000000001", "76561198000000002", "76561198000000003", "bot:Vitaliy"


def build_demo() -> Demo:
    return Demo(
        path=DEMO_PATH,
        tick_rate=64,
        map_name="de_mirage",
        players=(
            Player(P1, "ZywOo"),
            Player(P2, "Ünïcödé ☆"),
            Player(P3, "donk"),
            Player(P4, "Vitaliy"),
        ),
        rounds=(
            Round(
                number=1, start_tick=1000, freeze_end_tick=1640, end_tick=5000,
                decided_tick=4000, winner="CT", reason="ct_killed",
                lives=(
                    Life(P1, "CT", 1000, 5000),
                    Life(P2, "CT", 1000, 5000),
                    Life(P3, "T", 1000, 3000),
                    Life(P4, "T", 1000, 4000),
                ),
            ),
            Round(
                number=2, start_tick=5000, freeze_end_tick=5640, end_tick=9000,
                decided_tick=8000, winner="T", reason="t_killed",
                lives=(
                    Life(P1, "CT", 5000, 7000),
                    Life(P2, "CT", 5000, 8000),
                    Life(P3, "T", 5000, 7500),
                    Life(P4, "T", 5000, 9000),
                ),
            ),
        ),
        kills=(
            Kill(tick=3000, round=1, attacker=P1, victim=P3, weapon="awp", noscope=True),
            Kill(tick=4000, round=1, attacker=P1, victim=P4, weapon="awp",
                 headshot=True, penetrated=1, distance=31.5),
            Kill(tick=7000, round=2, attacker=P3, victim=P1, weapon="ak47", through_smoke=True),
            Kill(tick=7500, round=2, attacker=P2, victim=P3, weapon="m4a1", assister=None),
            Kill(tick=8000, round=2, attacker=P4, victim=P2, weapon="deagle", attacker_in_air=True),
        ),
        bomb_events=(BombEvent(tick=6500, round=2, kind="planted", player=P3, site="A"),),
    )


@pytest.fixture
def demo() -> Demo:
    return build_demo()
