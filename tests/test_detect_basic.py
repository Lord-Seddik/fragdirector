"""Tests for the basic detectors, using hand-built kill lists.

Every test demo has one round (ticks 1000-20000, freeze ends at 1640) with
five CT players (c1..c5) against five T players (t1..t5), 64 ticks per second.
"""

import pytest

from fragdirector.detect.basic import (
    detect_basic,
    detect_clutches,
    detect_deaths,
    detect_multikills,
    detect_style_kills,
    detect_weapon_kills,
    kill_style_flags,
)
from fragdirector.models import Demo, Kill, Life, Player, Round

CT = [f"c{i}" for i in range(1, 6)]
T = [f"t{i}" for i in range(1, 6)]
START, FREEZE, END = 1000, 1640, 20000


def make_demo(kills, winner="CT", rounds=1):
    """One 5v5 round (plus empty extra rounds); lives are derived from the kills."""
    died = {}
    for kill in kills:
        died.setdefault(kill.victim, kill.tick)
    lives = tuple(
        Life(p, side, START, died.get(p, END))
        for side, group in (("CT", CT), ("T", T)) for p in group
    )
    rnd = Round(number=1, start_tick=START, freeze_end_tick=FREEZE, end_tick=END,
                lives=lives, decided_tick=max([k.tick for k in kills], default=END), winner=winner,
                reason="test")
    return Demo(
        path="demos/test.dem", tick_rate=64,
        players=tuple(Player(p, p) for p in CT + T),
        rounds=(rnd,), kills=tuple(sorted(kills, key=lambda k: k.tick)),
    )


def kill(tick, attacker, victim, **extra):
    return Kill(tick=tick, round=1, attacker=attacker, victim=victim, weapon="ak47", **extra)


# -- multi-kills ---------------------------------------------------------


def test_double_and_triple_kill_are_found_with_tags():
    demo = make_demo([kill(3000, "c1", "t1"), kill(3100, "c1", "t2"), kill(3200, "c1", "t3")])
    [moment] = detect_multikills(demo)
    assert (moment.kind, moment.player, moment.round) == ("multikill", "c1", 1)
    assert (moment.start_tick, moment.end_tick) == (3000, 3200)
    assert moment.tags == ("3k",)
    assert len(moment.kills) == 3
    assert moment.score == 0.0


def test_ace_is_five_kills_tagged_ace():
    demo = make_demo([kill(3000 + 100 * i, "c1", T[i]) for i in range(5)])
    [moment] = detect_multikills(demo)
    assert moment.tags == ("5k", "ace")
    assert moment.details["kills"] == 5


def test_ace_with_kills_spread_over_the_round_is_still_an_ace():
    # First kill is far outside the 10 s window of the other four.
    demo = make_demo([kill(2000, "c1", "t1")] + [kill(5000 + 100 * i, "c1", T[i + 1]) for i in range(4)])
    [moment] = detect_multikills(demo)
    assert moment.tags == ("5k", "ace") and len(moment.kills) == 5
    assert (moment.start_tick, moment.end_tick) == (2000, 5300)


def test_kills_outside_the_window_do_not_merge():
    # 12 s apart (768 ticks) with the default 10 s window: two single kills.
    demo = make_demo([kill(3000, "c1", "t1"), kill(3000 + 768, "c1", "t2")])
    assert detect_multikills(demo) == []
    # A wider window merges them.
    [moment] = detect_multikills(demo, window_seconds=15)
    assert moment.tags == ("2k",)


def test_window_splits_a_long_run_into_two_multikills():
    demo = make_demo([
        kill(3000, "c1", "t1"), kill(3100, "c1", "t2"),
        kill(9000, "c1", "t3"), kill(9100, "c1", "t4"),
    ])
    moments = detect_multikills(demo)
    assert [m.tags for m in moments] == [("2k",), ("2k",)]


def test_team_kills_do_not_count_toward_a_multikill():
    demo = make_demo([kill(3000, "c1", "t1"), kill(3100, "c1", "c2"), kill(3200, "c1", "t2")])
    [moment] = detect_multikills(demo)
    assert moment.tags == ("2k",)
    assert all(k.victim != "c2" for k in moment.kills)
    # A single enemy kill next to a team kill is no multi-kill at all.
    demo = make_demo([kill(3000, "c1", "t1"), kill(3100, "c1", "c2")])
    assert detect_multikills(demo) == []


def test_multikills_are_per_player_and_per_filter():
    demo = make_demo([
        kill(3000, "c1", "t1"), kill(3050, "c2", "t2"),
        kill(3100, "c1", "t3"), kill(3150, "c2", "t4"),
    ])
    assert {m.player for m in detect_multikills(demo)} == {"c1", "c2"}
    assert [m.player for m in detect_multikills(demo, player="c2")] == ["c2"]


def test_no_kills_no_multikills():
    assert detect_multikills(make_demo([])) == []


# -- clutches --------------------------------------------------------------


def test_won_1v3_clutch_records_x_and_time_left():
    # c2..c5 die, then c1 is alone against t1..t3 from tick 4000 (after t4, t5 died).
    kills = [
        kill(2000, "t1", "c2"), kill(2100, "t2", "c3"), kill(2200, "t3", "c4"),
        kill(2300, "c1", "t4"), kill(2400, "c1", "t5"),
        kill(4000, "t1", "c5"),
        kill(5000, "c1", "t1"), kill(5100, "c1", "t2"), kill(5200, "c1", "t3"),
    ]
    demo = make_demo(kills)
    [clutch] = detect_clutches(demo)
    assert clutch.kind == "clutch" and clutch.player == "c1"
    assert clutch.start_tick == 4000 and clutch.end_tick == 5200
    assert clutch.tags == ("clutch", "1v3")
    assert clutch.details["versus"] == 3
    # 4000 - 1640 = 2360 ticks = 36.875 s in; 115 - 36.875 left.
    assert clutch.details["seconds_into_round"] == pytest.approx(36.88, abs=0.01)
    assert clutch.details["time_left_seconds"] == pytest.approx(78.12, abs=0.01)
    assert [k.victim for k in clutch.kills] == ["t1", "t2", "t3"]


def test_lost_1vx_is_not_a_clutch():
    kills = [kill(2000, "t1", "c2"), kill(2100, "t2", "c3"), kill(2200, "t3", "c4"),
             kill(2300, "t4", "c5"), kill(5000, "c1", "t5"), kill(6000, "t1", "c1")]
    demo = make_demo(kills, winner="T")
    assert detect_clutches(demo) == []


def test_lone_player_who_dies_before_the_round_is_won_is_not_a_clutch():
    # c1 is alone, dies, but the (T) bomb explodes: T wins, c1 did not clutch.
    kills = [kill(2000, "t1", "c2"), kill(2100, "t2", "c3"), kill(2200, "t3", "c4"),
             kill(2300, "t4", "c5"), kill(6000, "t1", "c1")]
    demo = make_demo(kills, winner="T")
    # T has five alive, so no T clutch; c1 lost.
    assert detect_clutches(demo) == []


def test_clutch_for_the_t_side():
    kills = [kill(2000, "c1", "t2"), kill(2100, "c2", "t3"), kill(2200, "c3", "t4"),
             kill(2300, "c4", "t5"), kill(5000, "t1", "c1"), kill(5100, "t1", "c2"),
             kill(5200, "t1", "c3"), kill(5300, "t1", "c4"), kill(5400, "t1", "c5")]
    demo = make_demo(kills, winner="T")
    [clutch] = detect_clutches(demo)
    assert clutch.player == "t1" and clutch.tags == ("clutch", "1v5")


def test_one_against_zero_is_not_a_clutch_and_no_winner_means_none():
    # Everyone on T dead at the moment CT drops to one: no enemy left.
    kills = [kill(2000, "c1", "t1"), kill(2000, "c1", "t2"), kill(2000, "c1", "t3"),
             kill(2000, "c1", "t4"), kill(2000, "c1", "t5")]
    assert detect_clutches(make_demo(kills)) == []
    demo = make_demo([kill(2000, "t1", "c2"), kill(2100, "t1", "c3"), kill(2200, "t1", "c4"),
                      kill(2300, "t1", "c5")], winner=None)
    assert detect_clutches(demo) == []


def test_clutch_player_filter():
    kills = [kill(2000, "t1", "c2"), kill(2100, "t2", "c3"), kill(2200, "t3", "c4"),
             kill(2300, "t4", "c5"), kill(5000, "c1", "t1"), kill(5100, "c1", "t2"),
             kill(5200, "c1", "t3"), kill(5300, "c1", "t4"), kill(5400, "c1", "t5")]
    demo = make_demo(kills)
    assert len(detect_clutches(demo, player="c1")) == 1
    assert detect_clutches(demo, player="c2") == []


# -- weapon filter -----------------------------------------------------------


def weapon_demo():
    return make_demo([
        Kill(tick=3000, round=1, attacker="c1", victim="t1", weapon="awp"),
        Kill(tick=3100, round=1, attacker="c2", victim="t2", weapon="deagle"),
        Kill(tick=3200, round=1, attacker="c1", victim="t3", weapon="knife_t"),
        Kill(tick=3300, round=1, attacker="c1", victim="t4", weapon="taser"),
        Kill(tick=3400, round=1, attacker="c2", victim="c3", weapon="awp"),  # team kill
    ])


def test_weapon_filter_by_name():
    demo = weapon_demo()
    assert [m.start_tick for m in detect_weapon_kills(demo, "awp")] == [3000]  # not the team kill
    assert [m.start_tick for m in detect_weapon_kills(demo, "deagle")] == [3100]
    assert [m.start_tick for m in detect_weapon_kills(demo, "knife")] == [3200]
    assert [m.start_tick for m in detect_weapon_kills(demo, "zeus")] == [3300]
    assert [m.start_tick for m in detect_weapon_kills(demo, ["awp", "deagle"])] == [3000, 3100]
    assert [m.player for m in detect_weapon_kills(demo, ["awp", "deagle"], player="c2")] == ["c2"]
    assert detect_weapon_kills(demo, "scout") == []


# -- style flags -------------------------------------------------------------


def test_style_flags_are_tagged():
    demo = make_demo([
        kill(3000, "c1", "t1", noscope=True, headshot=True),
        kill(3100, "c1", "t2", penetrated=2),
        kill(3200, "c1", "t3", through_smoke=True, attacker_blind=True),
        kill(3300, "c1", "t4", attacker_in_air=True),
        kill(3400, "c1", "t5"),  # plain
    ])
    moments = detect_style_kills(demo)
    assert [m.tags for m in moments] == [
        ("noscope", "headshot"), ("wallbang",), ("smoke", "blind"), ("air",),
    ]
    assert [m.start_tick for m in detect_style_kills(demo, flags=["wallbang"])] == [3100]
    assert [m.start_tick for m in detect_style_kills(demo, flags=["headshot", "air"])] == [3000, 3300]
    assert kill_style_flags(kill(1, "c1", "t1")) == ()


def test_style_kills_ignore_team_kills_and_reject_unknown_flags():
    demo = make_demo([kill(3000, "c1", "c2", noscope=True)])
    assert detect_style_kills(demo) == []
    with pytest.raises(ValueError):
        detect_style_kills(demo, flags=["backflip"])


# -- deaths --------------------------------------------------------------------


def test_player_deaths_lists_every_death_of_that_player():
    demo = make_demo([
        kill(3000, "c1", "t1"),
        Kill(tick=3100, round=1, attacker=None, victim="t2", weapon="world"),
        kill(3200, "t3", "c1"),
    ])
    [first, second] = detect_deaths(demo, "t1"), detect_deaths(demo, "t2")
    assert first[0].tags == ("death",) and first[0].kills[0].attacker == "c1"
    assert second[0].tags == ("death", "no_attacker")
    [death] = detect_deaths(demo, "c1")
    assert death.player == "c1" and death.start_tick == death.end_tick == 3200


def test_player_with_no_deaths_gives_empty_list():
    demo = make_demo([kill(3000, "c1", "t1")])
    assert detect_deaths(demo, "c2") == []
    assert detect_deaths(make_demo([]), "c1") == []


# -- everything together ---------------------------------------------------------


def test_detect_basic_combines_multikills_and_clutches_in_tick_order():
    kills = [kill(2000, "t1", "c2"), kill(2100, "t2", "c3"), kill(2200, "t3", "c4"),
             kill(2300, "t4", "c5"), kill(5000, "c1", "t1"), kill(5100, "c1", "t2"),
             kill(5200, "c1", "t3"), kill(5300, "c1", "t4"), kill(5400, "c1", "t5")]
    moments = detect_basic(make_demo(kills))
    assert [(m.kind, m.start_tick) for m in moments] == [("clutch", 2300), ("multikill", 5000)]
