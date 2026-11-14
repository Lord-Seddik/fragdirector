"""Tests for the timing detectors, using hand-built kill lists.

Every test demo has one round (ticks 1000-20000, freeze ends at 1640) with
five CT players (c1..c5) against five T players (t1..t5), 64 ticks per second.
"""

from fragdirector.detect.timing import (
    detect_late_defuses,
    detect_late_kills,
    detect_opening_kills,
    detect_quick_kills,
    detect_timing,
    detect_trade_kills,
)
from fragdirector.models import BombEvent, Demo, Kill, Life, Player, Round

CT = [f"c{i}" for i in range(1, 6)]
T = [f"t{i}" for i in range(1, 6)]
START, FREEZE, END = 1000, 1640, 20000


def make_demo(kills, bombs=()):
    died = {}
    for k in kills:
        died.setdefault(k.victim, k.tick)
    lives = tuple(
        Life(p, side, START, died.get(p, END))
        for side, group in (("CT", CT), ("T", T)) for p in group
    )
    rnd = Round(number=1, start_tick=START, freeze_end_tick=FREEZE, end_tick=END,
                lives=lives, decided_tick=END, winner="CT", reason="test")
    return Demo(path="demos/test.dem", tick_rate=64, players=tuple(Player(p, p) for p in CT + T),
                rounds=(rnd,), kills=tuple(sorted(kills, key=lambda k: k.tick)),
                bomb_events=tuple(bombs))


def kill(tick, attacker, victim, **extra):
    return Kill(tick=tick, round=1, attacker=attacker, victim=victim, weapon="ak47", **extra)


# -- quick kills ---------------------------------------------------------


def test_quick_kills_found_within_gap_only():
    demo = make_demo([kill(3000, "c1", "t1"), kill(3040, "c1", "t2"), kill(3200, "c1", "t3")])
    [moment] = detect_quick_kills(demo)  # 40 ticks = 625 ms; the next gap is 2.5 s
    assert moment.kind == "quick_kills" and moment.player == "c1"
    assert (moment.start_tick, moment.end_tick) == (3000, 3040)
    assert moment.details["gap_ms"] == 625.0
    assert len(detect_quick_kills(demo, max_gap_seconds=3)) == 2
    assert detect_quick_kills(demo, player="c2") == []


def test_quick_kills_ignore_other_players_and_team_kills():
    demo = make_demo([kill(3000, "c1", "t1"), kill(3010, "c2", "t2"), kill(3020, "c1", "c3")])
    assert detect_quick_kills(demo) == []


# -- opening kill --------------------------------------------------------


def test_opening_kill_is_the_first_kill_of_the_round():
    demo = make_demo([kill(3000, "c1", "t1"), kill(3100, "t2", "c2")])
    [moment] = detect_opening_kills(demo)
    assert (moment.kind, moment.player, moment.start_tick) == ("opening_kill", "c1", 3000)
    assert moment.details["seconds_into_round"] == round((3000 - FREEZE) / 64, 2)
    assert detect_opening_kills(demo, player="t2") == []


def test_no_opening_kill_if_first_death_is_a_team_kill_or_world():
    assert detect_opening_kills(make_demo([kill(3000, "c1", "c2"), kill(3100, "c1", "t1")])) == []
    assert detect_opening_kills(make_demo([Kill(tick=3000, round=1, victim="t1", weapon="world")])) == []
    assert detect_opening_kills(make_demo([])) == []


# -- trade kills ---------------------------------------------------------


def test_trade_kill_within_window():
    demo = make_demo([kill(3000, "t1", "c1"), kill(3100, "c2", "t1")])
    [moment] = detect_trade_kills(demo)
    assert (moment.kind, moment.player) == ("trade_kill", "c2")
    assert (moment.start_tick, moment.end_tick) == (3000, 3100)
    assert moment.details["traded_player"] == "c1"
    assert len(moment.kills) == 2


def test_trade_needs_window_teammate_and_same_round():
    late = make_demo([kill(3000, "t1", "c1"), kill(3000 + 6 * 64, "c2", "t1")])
    assert detect_trade_kills(late) == []
    assert len(detect_trade_kills(late, window_seconds=7)) == 1
    # t1 killed a T teammate by accident-free kill of an enemy t2? Killing t1 after t1 killed t2 is not a trade.
    other_side = make_demo([kill(3000, "t1", "c1"), kill(3100, "t2", "t1")])
    assert detect_trade_kills(other_side) == []  # t2 killing t1 is a team kill, not a trade
    assert detect_trade_kills(make_demo([kill(3000, "t1", "c1"), kill(3100, "c2", "t1")]), player="c3") == []


# -- late kills ----------------------------------------------------------


def test_late_kill_in_last_seconds():
    clock_end = FREEZE + 115 * 64
    demo = make_demo([kill(clock_end - 5 * 64, "c1", "t1"), kill(clock_end - 30 * 64, "c1", "t2")])
    [moment] = detect_late_kills(demo)
    assert (moment.kind, moment.start_tick) == ("late_kill", clock_end - 5 * 64)
    assert moment.details["time_left_seconds"] == 5.0
    assert len(detect_late_kills(demo, last_seconds=40)) == 2


def test_no_late_kill_after_bomb_plant():
    clock_end = FREEZE + 115 * 64
    demo = make_demo([kill(clock_end - 64, "c1", "t1")],
                     bombs=[BombEvent(tick=clock_end - 128, round=1, kind="planted", player="t2")])
    assert detect_late_kills(demo) == []


# -- last-second defuse --------------------------------------------------


def test_last_second_defuse():
    plant = BombEvent(tick=5000, round=1, kind="planted", player="t1")
    close = make_demo([], bombs=[plant, BombEvent(tick=5000 + 40 * 64 - 32, round=1, kind="defused", player="c1")])
    [moment] = detect_late_defuses(close)
    assert (moment.kind, moment.player) == ("late_defuse", "c1")
    assert moment.details["time_left_seconds"] == 0.5
    early = make_demo([], bombs=[plant, BombEvent(tick=5000 + 20 * 64, round=1, kind="defused", player="c1")])
    assert detect_late_defuses(early) == []
    assert len(detect_late_defuses(early, last_seconds=25)) == 1
    assert detect_late_defuses(make_demo([], bombs=[BombEvent(tick=6000, round=1, kind="defused", player="c1")])) == []


def test_detect_timing_combines_in_tick_order():
    demo = make_demo([kill(3000, "t1", "c1"), kill(3030, "c2", "t1")])
    kinds = [(m.start_tick, m.kind) for m in detect_timing(demo)]
    assert kinds == sorted(kinds)
    assert {"opening_kill", "trade_kill"} <= {k for _, k in kinds}
