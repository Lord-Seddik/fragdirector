"""Tests for the director, using hand-built demos.

Unless a test says otherwise the demo is 5 CT (c1..c5) against 5 T (t1..t5)
at 64 ticks per second. Round 1 is ticks 1000-20000 (freeze ends at 1640),
round 2 is ticks 20000-39000, and so on.
"""

import random
from dataclasses import replace
from pathlib import Path

import pytest

from fragdirector import cli
from fragdirector.detect.basic import detect_basic, detect_deaths, detect_multikills
from fragdirector.director import (
    BUDGET_TOLERANCE,
    DirectorError,
    Plan,
    PresetError,
    direct,
    fit_budget,
    load_preset,
    merge_overlaps,
    moment_shots,
    preset_names,
    wrong_overlaps,
)
from fragdirector.models import Demo, Kill, Life, Moment, Player, Round, Shot, ShotList, shotlist_problems
from fragdirector.score import rank_moments

CT = [f"c{i}" for i in range(1, 6)]
T = [f"t{i}" for i in range(1, 6)]
START, FREEZE, LENGTH = 1000, 640, 19000
END = START + LENGTH
PATH = "demos/test.dem"


def make_demo(kills, rounds=1, born=None):
    """Lives are derived from the kills. ``born`` gives a later first tick
    alive to some players of round 1: ``{"t1": 3900}``."""
    made = []
    for number in range(1, rounds + 1):
        start = START + (number - 1) * LENGTH
        died = {k.victim: k.tick for k in reversed(kills) if k.round == number}
        lives = tuple(
            Life(p, side, (born or {}).get(p, start) if number == 1 else start,
                 died.get(p, start + LENGTH))
            for side, group in (("CT", CT), ("T", T)) for p in group
        )
        made.append(Round(number=number, start_tick=start, freeze_end_tick=start + FREEZE,
                          end_tick=start + LENGTH, lives=lives, winner="CT", reason="test"))
    return Demo(path=PATH, tick_rate=64, players=tuple(Player(p, p) for p in CT + T),
                rounds=tuple(made), kills=tuple(sorted(kills, key=lambda k: k.tick)))


def kill(tick, attacker, victim, round=1):
    return Kill(tick=tick, round=round, attacker=attacker, victim=victim, weapon="ak47")


def spread_ace():
    """c1 kills t1 at 4000, then the other four 31 s later, 2 to 6 s apart."""
    ticks = [4000, 6000, 6128, 6384, 6768]
    demo = make_demo([kill(tick, "c1", victim) for tick, victim in zip(ticks, T)])
    [moment] = rank_moments(detect_multikills(demo))
    return demo, moment


def spans(shots):
    return [(s.start_tick, s.end_tick, s.pov_player) for s in shots]


def check(plan, demo):
    """What must hold for every shot list the director makes."""
    assert shotlist_problems(plan.shotlist, demo) == []
    assert wrong_overlaps(plan.shotlist, demo) == []
    for shot in plan.shotlist.shots:
        rnd = demo.round_at(shot.start_tick)
        assert rnd is not None and shot.end_tick <= rnd.end_tick
        assert shot.label


# -- presets -------------------------------------------------------------


def test_the_four_presets_exist_and_make_the_shots_the_plan_says():
    assert preset_names() == ["budget", "killer", "major-replay", "victims"]
    made = {name: load_preset(name) for name in preset_names()}
    kinds = {name: (p.shots_killer, p.shots_victims, p.shots_replay) for name, p in made.items()}
    assert kinds == {
        "killer": (True, False, False),
        "victims": (False, True, False),
        "major-replay": (True, True, True),
        "budget": (True, False, False),
    }
    assert made["budget"].budget_seconds == 30
    assert all(p.budget_seconds == 0 for name, p in made.items() if name != "budget")
    assert load_preset().name == "killer"


def write(tmp_path, text):
    path = tmp_path / "mine.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_user_preset_changes_only_what_it_lists(tmp_path):
    killer = load_preset("killer")
    mine = load_preset(write(tmp_path, "[victims]\nseconds = 7\n"))
    assert mine.name == "mine"
    assert mine.victims_seconds == 7.0
    assert replace(mine, name="killer", victims_seconds=killer.victims_seconds) == killer


def test_user_preset_can_build_on_another_preset(tmp_path):
    mine = load_preset(write(tmp_path, 'base = "major-replay"\n[replay]\nspeed = 0.25\n'))
    assert (mine.shots_killer, mine.shots_victims, mine.shots_replay) == (True, True, True)
    assert mine.replay_speed == 0.25


@pytest.mark.parametrize("text, message", [
    ("[killer\npre_roll_seconds = \n", "not a valid TOML"),
    ("[killers]\npre_roll_seconds = 1\n", "unknown section"),
    ("[killer]\npre_roll = 1\n", "unknown setting"),
    ('[killer]\npre_roll_seconds = "long"\n', "must be a number"),
    ("[killer]\npre_roll_seconds = true\n", "must be a number"),
    ("[killer]\npre_roll_seconds = nan\n", "must be a number"),
    ("[killer]\npre_roll_seconds = -1\n", "must be 0 or more"),
    ("[replay]\nspeed = 0\n", "must be more than 0"),
    ("[limits]\nmin_shot_seconds = 0\n", "must be more than 0"),
    ("[budget]\nmax_stretch = 0.5\n", "must be 1 or more"),
    ("[shots]\nkiller = 1\n", "must be true or false"),
    ("[shots]\nkiller = false\n", "at least one"),
    ('base = "nope"\n', "base must be one of"),
    ("killer = 3\n", "must be a section"),
])
def test_bad_preset_file_is_a_clear_error(tmp_path, text, message):
    with pytest.raises(PresetError, match=message):
        load_preset(write(tmp_path, text))


def test_unknown_preset_is_a_clear_error(tmp_path):
    with pytest.raises(PresetError, match="no preset named"):
        load_preset("cinematic")
    with pytest.raises(PresetError, match="no preset named"):
        load_preset(tmp_path / "nope.toml")
    assert issubclass(PresetError, ValueError) and issubclass(DirectorError, ValueError)


# -- killer --------------------------------------------------------------


def test_fast_multikill_is_one_shot_from_the_killers_view():
    # Kills 1 s apart: pre-roll 2 + 0.5 * 1 = 2.5 s, post-roll 1 + 0.25 * 1 = 1.25 s.
    demo = make_demo([kill(3000, "c1", "t1"), kill(3064, "c1", "t2"), kill(3128, "c1", "t3")])
    [moment] = detect_multikills(demo)
    [shot] = moment_shots(moment, demo, load_preset("killer"))
    assert (shot.start_tick, shot.end_tick) == (3000 - 160, 3128 + 80)
    assert (shot.pov_player, shot.speed, shot.label, shot.demo) == ("c1", 1.0, "c1 3k", PATH)


def test_rolls_grow_with_the_gap_between_kills_up_to_their_maximum():
    preset = load_preset("killer")

    def rolls(gap_ticks):
        demo = make_demo([kill(5000, "c1", "t1"), kill(5000 + gap_ticks, "c1", "t2")])
        [shot] = moment_shots(detect_multikills(demo)[0], demo, preset)
        return 5000 - shot.start_tick, shot.end_tick - (5000 + gap_ticks)

    assert rolls(0) == (128, 64)          # the base: 2 s and 1 s
    assert rolls(128) == (192, 96)        # 2 s gap: 3 s and 1.5 s
    assert rolls(320) == (256, 128)       # 5 s gap: capped at 4 s and 2 s
    assert rolls(64)[0] < rolls(128)[0] < rolls(320)[0]


def test_long_quiet_gap_splits_the_killers_view():
    demo, moment = spread_ace()
    first, second = moment_shots(moment, demo, load_preset("killer"))
    assert spans([first, second]) == [(3872, 4064, "c1"), (5744, 6896, "c1")]
    assert (first.label, second.label) == ("c1 5k (1/2)", "c1 5k (2/2)")


def test_gap_setting_decides_where_the_view_splits(tmp_path):
    demo, moment = spread_ace()
    wide = load_preset(write(tmp_path, "[killer]\nsplit_gap_seconds = 60\n"))
    [shot] = moment_shots(moment, demo, wide)
    assert shot.start_tick < 4000 and shot.end_tick > 6768 and shot.label == "c1 5k"
    narrow = load_preset(write(tmp_path, "[killer]\nsplit_gap_seconds = 3\n"))
    assert len(moment_shots(moment, demo, narrow)) == 4


def test_killer_shot_stays_inside_the_round_and_the_killers_life():
    # A kill 1 s after the round starts and one 0.5 s before it ends.
    early = make_demo([kill(START + 64, "c1", "t1"), kill(START + 100, "c1", "t2")])
    [shot] = moment_shots(detect_multikills(early)[0], early, load_preset("killer"))
    assert shot.start_tick == START
    late = make_demo([kill(END - 100, "c1", "t1"), kill(END - 32, "c1", "t2")])
    [shot] = moment_shots(detect_multikills(late)[0], late, load_preset("killer"))
    assert shot.end_tick == END
    # The killer dies 10 ticks after the second kill: the shot stops there.
    traded = make_demo([kill(5000, "c1", "t1"), kill(5064, "c1", "t2"), kill(5074, "t3", "c1")])
    plan = direct(detect_multikills(traded), traded, load_preset("killer"))
    assert [s.end_tick for s in plan.shotlist.shots] == [5074]
    check(plan, traded)


def test_kill_that_lands_after_the_killer_died_is_left_out_with_a_note():
    demo = make_demo([kill(5000, "c1", "t1"), kill(5064, "c1", "t2"),
                      kill(5100, "t3", "c1"), kill(5600, "c1", "t4")])  # a grenade lands late
    moment = Moment(kind="multikill", round=1, player="c1", start_tick=5000, end_tick=5600,
                    kills=tuple(k for k in demo.kills if k.attacker == "c1"), tags=("3k",))
    plan = direct([moment], demo, load_preset("killer"))
    [shot] = plan.shotlist.shots
    assert shot.end_tick == 5100
    assert any("5600" in note for note in plan.notes)
    check(plan, demo)


def test_moment_without_kills_shows_its_player():
    demo = make_demo([kill(5000, "t1", "c2")])
    clutch = Moment(kind="clutch", round=1, player="c1", start_tick=6000, end_tick=6100,
                    tags=("clutch", "1v1"), details={"versus": 1})
    [shot] = moment_shots(clutch, demo, load_preset("killer"))
    assert shot.pov_player == "c1" and shot.start_tick < 6000 and shot.end_tick > 6100
    assert shot.label == "c1 1v1 clutch"


# -- victims -------------------------------------------------------------


def test_each_victims_view_runs_1_5_seconds_past_their_death_tick():
    demo, moment = spread_ace()
    shots = moment_shots(moment, demo, load_preset("victims"))
    assert spans(shots) == [(k.tick - 5 * 64, k.tick + 96, k.victim) for k in moment.kills]
    assert [s.label for s in shots] == [f"{t}'s view" for t in T]
    assert shotlist_problems(ShotList(tuple(shots)), demo) == []


def test_victims_view_is_clamped_to_the_round_and_to_their_life():
    preset = load_preset("victims")
    # The round started 3 s before the death.
    demo = make_demo([kill(START + 192, "c1", "t1"), kill(START + 200, "c1", "t2")])
    shots = moment_shots(detect_multikills(demo)[0], demo, preset)
    assert spans(shots) == [(START, START + 192 + 96, "t1"), (START, START + 200 + 96, "t2")]
    # t1 was only alive for the last 2.5 s (160 ticks) before dying.
    demo = make_demo([kill(4000, "c1", "t1"), kill(4100, "c1", "t2")], born={"t1": 3840})
    shots = moment_shots(detect_multikills(demo)[0], demo, preset)
    assert spans(shots)[0] == (3840, 4000 + 96, "t1")


def test_victim_alive_for_less_than_the_minimum_shot_is_left_out_with_a_note():
    demo = make_demo([kill(4000, "c1", "t1"), kill(4100, "c1", "t2")], born={"t1": 3950})
    plan = direct(detect_multikills(demo), demo, load_preset("victims"))
    assert [s.pov_player for s in plan.shotlist.shots] == ["t2"]
    assert any("t1" in note and "minimum" in note for note in plan.notes)
    check(plan, demo)


def test_victims_preset_works_for_a_death_moment():
    demo = make_demo([kill(4000, "t1", "c1")])
    [shot] = moment_shots(detect_deaths(demo, "c1")[0], demo, load_preset("victims"))
    assert (shot.start_tick, shot.end_tick, shot.pov_player) == (3680, 4000 + 96, "c1")


def test_after_death_seconds_sets_how_long_a_victims_view_runs_on(tmp_path):
    demo, moment = spread_ace()
    base = load_preset("victims")
    assert base.victims_after_death_seconds == 1.5
    for seconds, ticks in ((0, 0), (0.5, 32), (3, 192), (5, 320)):
        preset = replace(base, victims_after_death_seconds=seconds)
        shots = moment_shots(moment, demo, preset)
        assert spans(shots) == [(k.tick - 5 * 64, k.tick + ticks, k.victim) for k in moment.kills]
        assert shotlist_problems(ShotList(tuple(shots)), demo) == []
    # Set in a preset file; more than the 5 s the model allows is refused.
    assert load_preset(write(tmp_path, "[victims]\nafter_death_seconds = 0\n")).victims_after_death_seconds == 0
    with pytest.raises(PresetError, match="at most 5"):
        load_preset(write(tmp_path, "[victims]\nafter_death_seconds = 5.5\n"))
    with pytest.raises(PresetError, match="must be 0 or more"):
        load_preset(write(tmp_path, "[victims]\nafter_death_seconds = -1\n"))


def test_victims_view_after_death_is_cut_at_the_end_of_the_round():
    # The death is 0.5 s (32 ticks) before the round ends.
    demo = make_demo([kill(END - 32, "c1", "t1"), kill(END - 200, "c1", "t2")])
    shots = moment_shots(detect_multikills(demo)[0], demo, load_preset("victims"))
    assert spans(shots) == [(END - 200 - 320, END - 200 + 96, "t2"), (END - 32 - 320, END, "t1")]
    assert shotlist_problems(ShotList(tuple(shots)), demo) == []


def test_victim_shots_with_after_death_time_never_overlap_wrongly():
    # c2 kills c1, then c1's own kill of t1 comes earlier: c1's killer view and
    # victim view are the same view and are merged, not repeated.
    demo = make_demo([kill(4000, "c1", "t1"), kill(4100, "t2", "c1"), kill(4150, "c2", "t2")])
    plan = direct(detect_basic(demo), demo, load_preset("major-replay"))
    check(plan, demo)


# -- major-replay --------------------------------------------------------


def test_major_replay_is_killer_then_victims_then_slow_motion_last_kill():
    demo, moment = spread_ace()
    plan = direct([moment], demo, load_preset("major-replay"))
    shots = plan.shotlist.shots
    assert [s.pov_player for s in shots] == ["c1", "c1", *T, "c1"]
    assert [s.speed for s in shots] == [1.0] * 7 + [0.5]
    replay = shots[-1]
    assert (replay.start_tick, replay.end_tick, replay.label) == (6768 - 96, 6768 + 32, "replay 0.5x")
    # 2 s of demo at half speed are 4 s of video.
    assert replay.duration_seconds(64) == 4.0
    assert plan.shotlist.duration_seconds() == 3 + 18 + 5 * (4 + 1.5) + 4
    assert plan.notes == () and plan.budget_seconds is None
    check(plan, demo)


# -- the overlap rule ----------------------------------------------------


def shot(start, end, pov="c1", **extra):
    return Shot(demo=PATH, start_tick=start, end_tick=end, pov_player=pov, **extra)


def test_same_view_shots_that_overlap_or_touch_are_merged():
    merged = merge_overlaps([
        shot(3000, 3200, label="a"), shot(5000, 5200, label="b"),
        shot(3100, 3400, label="c"), shot(3400, 3500, label="a"),
    ])
    assert spans(merged) == [(3000, 3500, "c1"), (5000, 5200, "c1")]
    assert [s.label for s in merged] == ["a + c", "b"]


def test_another_player_or_another_speed_is_not_an_overlap():
    shots = [shot(3000, 3200), shot(3000, 3200, pov="t1"), shot(3100, 3150, speed=0.5)]
    assert merge_overlaps(shots) == shots
    assert wrong_overlaps(ShotList(tuple(shots))) == []


def test_wrong_overlaps_names_the_shots():
    listed = ShotList((shot(3000, 3200), shot(4000, 4100, pov="t1"), shot(3199, 3300)))
    [problem] = wrong_overlaps(listed)
    assert "Shots 1 and 3" in problem and "3199" in problem
    touching = ShotList((shot(3000, 3200), shot(3200, 3300)))
    assert wrong_overlaps(touching) == []


def test_two_moments_that_share_kills_give_one_shot():
    demo = make_demo([kill(5000, "c1", "t1"), kill(5100, "c1", "t2"), kill(5200, "c1", "t3")])
    [triple] = detect_multikills(demo)
    clutch = Moment(kind="clutch", round=1, player="c1", start_tick=5050, end_tick=5200,
                    kills=triple.kills[1:], tags=("clutch", "1v2"), details={"versus": 2})
    for name in ("killer", "victims", "major-replay"):
        plan = direct([triple, clutch], demo, load_preset(name))
        check(plan, demo)
        views = [(s.pov_player, s.speed) for s in plan.shotlist.shots]
        assert len(views) == len(set(views)), name
    [only] = direct([triple, clutch], demo, load_preset("killer")).shotlist.shots
    assert only.label == "c1 3k + c1 1v2 clutch"
    assert only.start_tick <= 5000 - 128 and only.end_tick >= 5200 + 64


# -- budget --------------------------------------------------------------


def within(plan, budget):
    return abs(plan.shotlist.duration_seconds() - budget) <= BUDGET_TOLERANCE * budget


@pytest.mark.parametrize("name, budgets", [
    # Draft 21 s; from 15.5 s (kills kept) to 42 s (twice the draft) with both shots.
    ("killer", [14, 16, 21, 30, 40]),
    # Draft 25 s; five views of 2 s to 10 s each.
    ("victims", [4, 9, 10, 25, 40, 50]),
    # Draft 45 s; 28.5 s to 90 s with all eight shots, less by leaving shots out.
    ("major-replay", [15, 20, 30, 45, 60, 80]),
])
def test_total_length_is_within_5_percent_of_the_budget(name, budgets):
    """The normal cases. Not covered by the 5% rule, and tested further down:
    a budget below the best shot at its shortest, a budget above all shots at
    their longest, and no shots at all."""
    demo, moment = spread_ace()
    for budget in budgets:
        plan = direct([moment], demo, load_preset(name), budget)
        assert within(plan, budget), (name, budget, plan.shotlist.duration_seconds())
        assert plan.budget_met and plan.budget_seconds == budget
        check(plan, demo)


def test_budget_length_is_the_length_on_screen():
    demo, moment = spread_ace()
    plan = direct([moment], demo, load_preset("major-replay"), 30)
    replay = plan.shotlist.shots[-1]
    assert replay.speed == 0.5
    by_hand = sum((s.end_tick - s.start_tick) / 64 / s.speed for s in plan.shotlist.shots)
    assert by_hand == pytest.approx(30, abs=0.1)
    assert sum(s.ticks for s in plan.shotlist.shots) / 64 < 29  # demo time is less


def test_budget_never_cuts_a_kill_out_of_a_kept_shot():
    demo, moment = spread_ace()
    for budget in (14, 16, 20, 30):
        plan = direct([moment], demo, load_preset("major-replay"), budget)
        for s in plan.shotlist.shots:
            if s.label.startswith("c1 5k (2/2)"):
                assert s.start_tick <= 6000 - 64 and s.end_tick >= 6768 + 32
            if s.label.endswith("view"):
                # The death stays in the shot, with at most the drafted time after it.
                death = next(k.tick for k in moment.kills if f"{k.victim}'s view" == s.label)
                assert death <= s.end_tick <= death + 96
            assert s.duration_seconds(64) >= 2.0 - 1e-9


def three_doubles(scores=(30, 10, 20)):
    """Three 2ks by c1, c2 and c3, far apart, with the given scores."""
    kills = [kill(4000, "c1", "t1"), kill(4064, "c1", "t2"),
             kill(8000, "c2", "t3"), kill(8064, "c2", "t4"),
             kill(12000, "c3", "t5"), kill(12064, "c3", "c4")]
    demo = make_demo(kills)
    moments = [
        Moment(kind="multikill", round=1, player=p, start_tick=t, end_tick=t + 64, tags=("2k",),
               kills=tuple(k for k in demo.kills if k.attacker == p), score=score)
        for p, t, score in zip(("c1", "c2", "c3"), (4000, 8000, 12000), scores)
    ]
    return demo, moments


def test_lowest_score_shots_are_dropped_first():
    # Each shot is 4.75 s drafted and 2.5 s at its shortest.
    demo, moments = three_doubles()
    killer = load_preset("killer")
    assert [s.pov_player for s in direct(moments, demo, killer, 8).shotlist.shots] == ["c1", "c2", "c3"]
    plan = direct(moments, demo, killer, 6)
    assert [s.pov_player for s in plan.shotlist.shots] == ["c1", "c3"]
    assert within(plan, 6) and any("c2 2k" in note for note in plan.notes)
    plan = direct(moments, demo, killer, 4)
    assert [s.pov_player for s in plan.shotlist.shots] == ["c1"]
    assert within(plan, 4)
    check(plan, demo)


def test_equal_scores_drop_the_earlier_shot():
    demo, moments = three_doubles(scores=(5, 5, 5))
    plan = direct(moments, demo, load_preset("killer"), 6)
    assert [s.pov_player for s in plan.shotlist.shots] == ["c2", "c3"]


def test_spare_time_is_split_by_score():
    demo, moments = three_doubles(scores=(30, 10, 20))
    plan = direct(moments, demo, load_preset("killer"), 7.5 + 3)  # 3 s above the shortest
    extra = [s.duration_seconds(64) - 2.5 for s in plan.shotlist.shots]
    assert extra == pytest.approx([1.5, 0.5, 1.0], abs=0.02)


def test_unscored_moments_count_the_same():
    demo, moments = three_doubles(scores=(0, 0, 0))
    plan = direct(moments, demo, load_preset("killer"), 9)
    assert [s.duration_seconds(64) for s in plan.shotlist.shots] == pytest.approx([3, 3, 3], abs=0.02)


def test_budget_lengthens_a_short_draft_but_not_past_its_neighbour():
    demo, moment = spread_ace()
    draft = direct([moment], demo, load_preset("killer"))
    plan = direct([moment], demo, load_preset("killer"), 40)
    assert draft.shotlist.duration_seconds() == 21 and within(plan, 40)
    first, second = plan.shotlist.shots
    assert first.start_tick < 3872 and second.end_tick > 6896
    assert first.end_tick <= second.start_tick
    check(plan, demo)


def test_budget_smaller_than_any_shot_keeps_the_best_shot_and_says_so():
    demo, moment = spread_ace()
    plan = direct([moment], demo, load_preset("major-replay"), 1)
    [only] = plan.shotlist.shots
    assert only.label == "c1 5k (2/2)"                 # the four kills, 13.5 s at its shortest
    assert plan.shotlist.duration_seconds() == 13.5
    assert not plan.budget_met and not within(plan, 1)
    assert any("cannot be met" in note for note in plan.notes)
    check(plan, demo)


def test_budget_larger_than_everything_gives_the_longest_and_says_so():
    demo, moment = spread_ace()
    draft = direct([moment], demo, load_preset("killer"))
    plan = direct([moment], demo, load_preset("killer"), 600)
    assert plan.shotlist.duration_seconds() == 2 * draft.shotlist.duration_seconds()
    assert not plan.budget_met
    assert any("cannot be met" in note and "longest" in note for note in plan.notes)
    check(plan, demo)


def test_budget_that_falls_between_two_sets_of_shots_is_reported(tmp_path):
    # Without stretching, three 2.5-4.75 s shots give 2.5-4.75, 5-9.5 or 7.5-14.25 s:
    # 4.9 s is between the first two ranges, so one shot at its longest is the answer.
    demo, moments = three_doubles()
    rigid = load_preset(write(tmp_path, "[budget]\nmax_stretch = 1\n"))
    plan = direct(moments, demo, rigid, 4.9)
    assert plan.shotlist.duration_seconds() == 4.75 and plan.budget_met
    plan = direct(moments, demo, rigid, 30)
    assert plan.shotlist.duration_seconds() == 14.25 and not plan.budget_met and plan.notes


def test_budget_preset_uses_its_own_seconds_unless_told_otherwise():
    demo, moment = spread_ace()
    budget = load_preset("budget")
    plan = direct([moment], demo, budget)
    assert plan.budget_seconds == 30 and within(plan, 30)
    assert within(direct([moment], demo, budget, 20), 20)
    assert direct([moment], demo, load_preset("killer")).budget_seconds is None


def test_fit_budget_works_on_any_valid_shot_list():
    demo, moment = spread_ace()
    listed = ShotList((shot(3800, 4100, label="a"), shot(5700, 6000, "t2", label="b")))
    plan = fit_budget(listed, demo, 5, load_preset("killer"))
    assert within(plan, 5) and plan.budget_met
    check(plan, demo)
    assert fit_budget(ShotList(), demo, 5, load_preset("killer")).budget_met is False


@pytest.mark.parametrize("bad", [0, -5, float("nan"), float("inf"), "30", True])
def test_bad_budget_is_refused(bad):
    demo, moment = spread_ace()
    with pytest.raises(DirectorError, match="budget"):
        direct([moment], demo, load_preset("killer"), bad)


# -- everything together, on many demos ----------------------------------


def random_demo(seed):
    """Two rounds of random kills; attackers are alive, victims die once."""
    rng = random.Random(seed)
    kills = []
    for number in (1, 2):
        start = START + (number - 1) * LENGTH
        alive = {"CT": list(CT), "T": list(T)}
        tick = start + FREEZE
        while True:
            tick += rng.choice([1, 20, 60, 150, 400, 900, 2500])
            if tick >= start + LENGTH or not alive["CT"] or not alive["T"]:
                break
            side = rng.choice(["CT", "T"])
            other = "T" if side == "CT" else "CT"
            attacker = rng.choice(alive[side][:2])  # few attackers, so multi-kills happen
            victim = rng.choice(alive[other])
            alive[other].remove(victim)
            kills.append(kill(tick, attacker, victim, round=number))
    return make_demo(kills, rounds=2)


@pytest.mark.parametrize("seed", range(25))
def test_every_preset_and_budget_gives_a_valid_shot_list(seed):
    demo = random_demo(seed)
    moments = rank_moments(detect_basic(demo))
    moments = sorted(moments[:6], key=lambda m: (m.round, m.start_tick, m.player, m.kind))
    for name in preset_names():
        preset = load_preset(name)
        for budget in (None, 3, 10, 30, 60, 500):
            plan = direct(moments, demo, preset, budget)
            check(plan, demo)
            assert plan == direct(moments, demo, preset, budget)  # same input, same output
            for moment_shot in plan.shotlist.shots:
                assert moment_shot.duration_seconds(64) >= preset.limits_min_shot_seconds - 1e-9
            if plan.budget_seconds is not None and plan.shotlist.shots:
                assert plan.budget_met == within(plan, plan.budget_seconds)
                if not plan.budget_met:
                    assert any("cannot be met" in note for note in plan.notes)
    for moment in moments:
        rnd = demo.round_number(moment.round)
        for name in ("killer", "victims", "major-replay"):
            for s in moment_shots(moment, demo, load_preset(name)):
                assert rnd.start_tick <= s.start_tick < s.end_tick <= rnd.end_tick


# -- fragdirector plan ---------------------------------------------------


@pytest.fixture
def parsed(monkeypatch):
    demo, _ = spread_ace()
    monkeypatch.setattr("fragdirector.parse.parse_demo", lambda path: demo)
    return demo


def test_plan_writes_a_valid_shot_list(parsed, tmp_path, capsys):
    out = tmp_path / "shots.json"
    code = cli.main(["plan", "x.dem", "--preset", "major-replay", "--player", "C1",
                     "--round", "1", "--top", "1", "--budget", "30", "--out", str(out)])
    assert code == 0
    listed = ShotList.load(out)
    assert shotlist_problems(listed, parsed) == []
    assert listed.duration_seconds() == pytest.approx(30, rel=BUDGET_TOLERANCE)
    assert "c1 5k (1/2)" in capsys.readouterr().out


@pytest.mark.parametrize("options, message", [
    (["--preset", "cinematic"], "no preset named"),
    (["--player", "nobody"], "No player named"),
    (["--round", "9"], "no round 9"),
    (["--player", "t1"], "No moments match"),
    (["--budget", "0"], "budget"),
])
def test_plan_refuses_bad_options_and_writes_nothing(parsed, tmp_path, capsys, options, message):
    out = tmp_path / "shots.json"
    assert cli.main(["plan", "x.dem", "--out", str(out), *options]) == 1
    assert message in capsys.readouterr().err
    assert not out.exists()


def test_plan_refuses_to_write_a_shot_list_with_problems(parsed, tmp_path, capsys, monkeypatch):
    dead = ShotList((shot(4000, 4200, "t1", label="t1 after dying"),))
    monkeypatch.setattr("fragdirector.director.direct", lambda *args: Plan(dead))
    out = tmp_path / "shots.json"
    assert cli.main(["plan", "x.dem", "--out", str(out)]) == 1
    assert "t1 is no longer alive" in capsys.readouterr().err
    assert not out.exists()


# -- control characters in printed text (T17) -----------------------------

HOSTILE = "c1\x1b[2J\x1b]0;owned\x07\n\tx"
HOSTILE_PRINTED = "c1?[2J?]0;owned???x"


@pytest.fixture
def hostile(monkeypatch):
    """The ace again, but c1's name holds an escape sequence, a BEL, a line
    break and a tab."""
    demo, _ = spread_ace()
    players = tuple(replace(p, name=HOSTILE) if p.id == "c1" else p for p in demo.players)
    demo = replace(demo, players=players)
    monkeypatch.setattr("fragdirector.parse.parse_demo", lambda path: demo)
    return demo


def test_analyze_prints_control_characters_as_question_marks(hostile, capsys):
    assert cli.main(["analyze", "x.dem", "--dump-kills"]) == 0
    out = capsys.readouterr().out
    assert HOSTILE_PRINTED in out
    assert not any(ch in out for ch in "\x1b\x07\t")
    assert hostile.player_name("c1") == HOSTILE  # the model keeps the real name


def test_plan_prints_control_characters_as_question_marks(hostile, tmp_path, capsys):
    out_file = tmp_path / "shots.json"
    assert cli.main(["plan", "x.dem", "--top", "1", "--out", str(out_file)]) == 0
    out = capsys.readouterr().out
    assert HOSTILE_PRINTED in out
    assert not any(ch in out for ch in "\x1b\x07\t")
    assert ShotList.load(out_file).shots[0].label.startswith(HOSTILE)  # the file keeps the real label


# -- format characters in printed text (T19) -----------------------------

FORMAT_NAME = "c1‮yek​"  # right-to-left override and zero-width space (category Cf)
FORMAT_PRINTED = "c1?yek?"


def test_analyze_prints_format_characters_as_question_marks(monkeypatch, capsys):
    demo, _ = spread_ace()
    players = tuple(replace(p, name=FORMAT_NAME) if p.id == "c1" else p for p in demo.players)
    demo = replace(demo, players=players)
    monkeypatch.setattr("fragdirector.parse.parse_demo", lambda path: demo)
    assert cli.main(["analyze", "x.dem", "--dump-kills"]) == 0
    out = capsys.readouterr().out
    assert FORMAT_PRINTED in out
    assert "‮" not in out and "​" not in out
    assert demo.player_name("c1") == FORMAT_NAME  # the model keeps the real name


# -- a real demo ---------------------------------------------------------

SAMPLE_DEMOS = sorted((Path(__file__).resolve().parent.parent / "demos").glob("*.dem"))


def test_real_demo_multikills_give_valid_shot_lists():
    if not SAMPLE_DEMOS:
        pytest.skip("no .dem file in demos/")
    from fragdirector.parse import parse_demo

    demo = parse_demo(str(SAMPLE_DEMOS[0]))
    moments = rank_moments(detect_basic(demo))
    for name in preset_names():
        for chosen in (moments[:1], sorted(moments, key=lambda m: m.start_tick)):
            for budget in (None, 30):
                plan = direct(chosen, demo, load_preset(name), budget)
                assert plan.shotlist.shots
                check(plan, demo)
                if plan.budget_seconds is not None:
                    assert plan.budget_met and within(plan, plan.budget_seconds)
