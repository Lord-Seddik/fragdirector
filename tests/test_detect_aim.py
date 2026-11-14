"""Tests for the flick detector, with made-up view angles (no demo file)."""

import pytest

from fragdirector.detect.aim import angle_between, detect_flicks, flick_before, measure_flicks
from fragdirector.models import Demo, Kill, Life, Player, Round


def test_angle_between_simple_and_wraparound():
    assert angle_between(0, 10, 0, 40) == pytest.approx(30)
    assert angle_between(0, 179, 0, -179) == pytest.approx(2)  # across +-180
    assert angle_between(10, 0, -10, 0) == pytest.approx(20)  # pitch only
    assert angle_between(5, 33, 5, 33) == pytest.approx(0)


def test_angle_between_combines_pitch_and_yaw():
    # 30 of yaw and 40 of pitch: cos(angle) = cos(40) * cos(30)
    assert angle_between(0, 0, 40, 30) == pytest.approx(48.44, abs=0.01)
    # Near straight up, a big yaw change is a small turn.
    assert angle_between(89, 0, 89, 90) < 2


def test_flick_picks_furthest_view_in_window():
    samples = [(100 + i, 0.0, 0.0 + 4 * i) for i in range(10)]  # yaw 0..36 over 9 ticks
    flick = flick_before(samples, kill_tick=109, tick_rate=64)
    assert flick.degrees == pytest.approx(36)
    assert (flick.from_tick, flick.to_tick) == (100, 109)
    assert flick.ms == pytest.approx(9 * 1000 / 64)


def test_flick_ignores_samples_outside_the_window():
    samples = [(50, 0.0, 0.0), (100, 0.0, 90.0), (109, 0.0, 95.0), (120, 0.0, 170.0)]
    flick = flick_before(samples, kill_tick=109, tick_rate=64)  # window is ticks 96..109
    assert flick.degrees == pytest.approx(5)
    assert flick_before([(10, 0.0, 0.0)], kill_tick=109) is None
    assert flick_before([], kill_tick=109) is None


def test_flick_across_yaw_wraparound():
    flick = flick_before([(100, 0.0, 170.0), (108, 0.0, -170.0)], kill_tick=108)
    assert flick.degrees == pytest.approx(20)


def make_demo():
    lives = (Life("111", "CT", 1000, 5000), Life("222", "T", 1000, 4000), Life("333", "T", 1000, 5000),
             Life("444", "CT", 1000, 5000))
    rnd = Round(number=1, start_tick=1000, freeze_end_tick=1100, end_tick=6000, lives=lives)
    kills = (
        Kill(tick=3000, round=1, attacker="111", victim="222", weapon="awp"),
        Kill(tick=4000, round=1, attacker="111", victim="333", weapon="ak47"),
        Kill(tick=4500, round=1, attacker="444", victim="111", weapon="ak47"),  # team kill
    )
    return Demo(path="demos/test.dem", tick_rate=64,
                players=tuple(Player(p, p) for p in ("111", "222", "333", "444")),
                rounds=(rnd,), kills=kills)


def fake_reader(path, wanted):
    assert path == "demos/test.dem"
    assert set(wanted) == {"111"}  # team kill's attacker is not read
    assert 2987 in wanted["111"] and 3000 in wanted["111"] and 2986 not in wanted["111"]
    flick = [(3000 - 9 + i, 0.0, 4.0 * i) for i in range(10)]  # 36 degrees
    still = [(4000 - 9 + i, 0.0, 50.0) for i in range(10)]
    return {"111": flick + still}


def test_measure_flicks_reports_every_kill():
    results = measure_flicks(make_demo(), read_angles=fake_reader)
    assert [(k.tick, round(f.degrees)) for k, f in results] == [(3000, 36), (4000, 0)]


def test_detect_flicks_uses_the_threshold():
    [moment] = detect_flicks(make_demo(), read_angles=fake_reader)
    assert (moment.kind, moment.player, moment.round) == ("flick", "111", 1)
    assert (moment.start_tick, moment.end_tick) == (2991, 3000)
    assert moment.details["degrees"] == 36.0
    assert moment.details["weapon"] == "awp"
    assert detect_flicks(make_demo(), min_degrees=40, read_angles=fake_reader) == []
    assert len(detect_flicks(make_demo(), min_degrees=0, read_angles=fake_reader)) == 2


def test_bot_kills_are_not_read():
    demo = make_demo()
    bots = Demo(path=demo.path, tick_rate=64, players=(Player("bot:a", "a"), Player("222", "b")),
                rounds=(Round(number=1, start_tick=1000, freeze_end_tick=1100, end_tick=6000,
                              lives=(Life("bot:a", "CT", 1000, 6000), Life("222", "T", 1000, 3000))),),
                kills=(Kill(tick=3000, round=1, attacker="bot:a", victim="222"),))

    def never(path, wanted):
        raise AssertionError("should not be called")

    assert measure_flicks(bots, read_angles=never) == []
