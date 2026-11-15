import json
from dataclasses import replace

import pytest

from conftest import DEMO_PATH, P1, P2, P3, P4
from fragdirector.models import (
    AFTER_DEATH_SECONDS,
    Demo,
    Kill,
    Life,
    ModelError,
    Moment,
    Player,
    Round,
    Shot,
    ShotList,
    seconds_to_ticks,
    shot_problems,
    shotlist_problems,
)


def shot(start=2000, end=3200, pov=P1, **extra) -> Shot:
    return Shot(demo=DEMO_PATH, start_tick=start, end_tick=end, pov_player=pov, **extra)


# -- save / load ------------------------------------------------------------


def test_shotlist_round_trip(tmp_path):
    shots = ShotList(shots=(
        shot(label="4k — first kill ☆"),
        shot(2500, 3000, pov=P3, speed=0.25, label="victim"),
    ))
    path = tmp_path / "shots.json"
    shots.save(path)
    assert ShotList.load(path) == shots
    assert not list(tmp_path.glob("*.tmp"))


def test_shotlist_file_has_the_documented_fields(tmp_path):
    path = tmp_path / "shots.json"
    ShotList(shots=(shot(),)).save(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data) == {"version", "tick_rate", "shots"}
    assert set(data["shots"][0]) == {
        "demo", "start_tick", "end_tick", "pov_player", "speed", "label"
    }


def test_empty_shotlist_round_trip(tmp_path):
    path = tmp_path / "shots.json"
    ShotList().save(path)
    assert ShotList.load(path) == ShotList()


def test_demo_round_trip(tmp_path, demo):
    path = tmp_path / "demo.json"
    demo.save(path)
    assert Demo.load(path) == demo


def test_moment_round_trip(demo):
    moment = Moment(
        kind="multikill", round=1, player=P1, start_tick=3000, end_tick=4000,
        kills=demo.kills[:2], tags=("2k", "awp", "noscope"), score=7.5,
        details={"gap_seconds": 15.6, "count": 2, "weapon": "awp", "ace": False},
    )
    assert Moment.from_dict(json.loads(json.dumps(moment.to_dict()))) == moment


def test_load_rejects_broken_json(tmp_path):
    path = tmp_path / "shots.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ModelError, match="not a valid JSON file"):
        ShotList.load(path)


@pytest.mark.parametrize("change, message", [
    (lambda d: d.update(version=2), "unsupported version"),
    (lambda d: d.pop("version"), "unsupported version"),
    (lambda d: d.update(extra=1), "unknown field"),
    (lambda d: d.update(shots={}), "must be a list"),
    (lambda d: d["shots"][0].pop("pov_player"), "missing field"),
    (lambda d: d["shots"][0].update(sped=0.5), "unknown field"),
    (lambda d: d["shots"][0].update(start_tick=2000.5), "whole number"),
    (lambda d: d["shots"][0].update(start_tick=True), "whole number"),
    (lambda d: d["shots"][0].update(start_tick=-1), "0 or more"),
    (lambda d: d["shots"][0].update(end_tick=2000), "must start before it ends"),
    (lambda d: d["shots"][0].update(speed=0), "more than 0"),
    (lambda d: d["shots"][0].update(speed="fast"), "must be a number"),
    (lambda d: d["shots"][0].update(pov_player=""), "must not be empty"),
    (lambda d: d["shots"][0].update(label=None), "must be text"),
])
def test_shotlist_from_dict_rejects_bad_data(change, message):
    data = ShotList(shots=(shot(),)).to_dict()
    change(data)
    with pytest.raises(ModelError, match=message):
        ShotList.from_dict(data)


def test_from_dict_rejects_non_objects():
    with pytest.raises(ModelError):
        ShotList.from_dict([])
    with pytest.raises(ModelError):
        Shot.from_dict("shot")


# -- objects check themselves ----------------------------------------------


def test_shot_rejects_bad_values():
    with pytest.raises(ModelError):
        shot(start=3000, end=3000)
    with pytest.raises(ModelError):
        shot(speed=float("nan"))
    with pytest.raises(ModelError):
        shot(speed=-1)
    with pytest.raises(ModelError):
        shot(start=1.5)


def test_shot_rejects_values_far_outside_anything_useful():
    """A shot list file can come from someone else; these would make a length
    that is not a finite number."""
    for speed in (1e-320, 0.001, 1000, 1e300):
        with pytest.raises(ModelError, match="between 0.01 and 100"):
            shot(speed=speed)
    assert shot(speed=0.01).speed == 0.01 and shot(speed=100).speed == 100.0
    with pytest.raises(ModelError, match="end_tick must be"):
        shot(end=10**400)
    with pytest.raises(ModelError, match="start_tick must be"):
        shot(start=2**31, end=2**31 + 5)


def test_a_number_too_long_to_read_is_a_model_error(tmp_path):
    path = tmp_path / "shots.json"
    path.write_text('{"version": 1, "tick_rate": ' + "1" * 5000 + ', "shots": []}', encoding="utf-8")
    with pytest.raises(ModelError, match="not a valid JSON file"):
        ShotList.load(path)


def test_shot_speed_is_stored_as_float():
    assert isinstance(shot(speed=1).speed, float)


def test_durations():
    assert shot(2000, 2640).ticks == 640
    assert shot(2000, 2640).duration_seconds(64) == 10.0
    assert shot(2000, 2640, speed=0.5).duration_seconds(64) == 20.0
    shots = ShotList(shots=(shot(2000, 2640), shot(2000, 2640, speed=0.5)))
    assert shots.duration_seconds() == 30.0
    assert len(shots) == 2
    assert seconds_to_ticks(0.5) == 32


def test_kill_wallbang_flag(demo):
    assert not demo.kills[0].wallbang
    assert demo.kills[1].wallbang


def test_demo_rejects_inconsistent_data(demo):
    with pytest.raises(ModelError, match="same id"):
        replace(demo, players=demo.players + (Player(P1, "again"),))
    with pytest.raises(ModelError, match="starts before"):
        replace(demo, rounds=(demo.rounds[0], replace(demo.rounds[1], start_tick=4999)))
    with pytest.raises(ModelError, match="order of round number"):
        replace(demo, rounds=demo.rounds[::-1], kills=(), bomb_events=())
    with pytest.raises(ModelError, match="not inside round"):
        replace(demo, kills=(Kill(tick=6000, round=1, victim=P1),))
    with pytest.raises(ModelError, match="unknown player"):
        replace(demo, kills=(Kill(tick=3000, round=1, victim="nobody"),))
    with pytest.raises(ModelError, match="order of tick"):
        replace(demo, kills=demo.kills[::-1])


def test_round_rejects_inconsistent_data():
    with pytest.raises(ModelError, match="outside the round"):
        Round(number=1, start_tick=100, freeze_end_tick=200, end_tick=900,
              lives=(Life(P1, "CT", 100, 901),))
    with pytest.raises(ModelError, match="more than one life"):
        Round(number=1, start_tick=100, freeze_end_tick=200, end_tick=900,
              lives=(Life(P1, "CT", 100, 500), Life(P1, "CT", 600, 900)))
    with pytest.raises(ModelError, match="winner"):
        Round(number=1, start_tick=100, freeze_end_tick=200, end_tick=900, winner="X")
    with pytest.raises(ModelError):
        Life(P1, "SPEC", 100, 200)


# -- lookups ----------------------------------------------------------------


def test_demo_lookups(demo):
    assert demo.round_at(999) is None
    assert demo.round_at(1000).number == 1
    assert demo.round_at(4999).number == 1
    assert demo.round_at(5000).number == 2
    assert demo.round_at(9000) is None
    assert demo.round_number(2).winner == "T"
    assert demo.player_name(P3) == "donk"
    assert demo.player_name("nobody") == "nobody"
    assert demo.seconds(640) == 10.0


def test_alive_lookups(demo):
    assert demo.is_alive(P3, 2999)
    assert not demo.is_alive(P3, 3000)  # the death tick itself is not alive
    assert demo.is_alive(P3, 5000)  # back next round
    assert demo.alive_span(P3, 2000) == (1000, 3000)
    assert demo.alive_span(P3, 3000) is None
    assert demo.alive_span(P3, 3500) is None
    assert demo.alive_span("nobody", 2000) is None
    assert demo.players_alive_during(2000, 3000) == (P1, P2, P3, P4)
    assert demo.players_alive_during(2000, 3001) == (P1, P2, P4)
    assert demo.players_alive_during(7600, 8000) == (P2, P4)
    assert demo.players_alive_during(100, 200) == ()


def test_what_a_shot_may_do(demo):
    # p3 dies at 3000; a shot may stay on them for 5 s (320 ticks) more.
    assert AFTER_DEATH_SECONDS == 5.0
    assert demo.after_death_ticks == 320
    assert replace(demo, tick_rate=128).after_death_ticks == 640
    assert demo.allowed_span(P3, 2000) == (1000, 3320)
    assert demo.allowed_span(P3, 3000) == (1000, 3320)  # the death can be shown
    assert demo.allowed_span(P3, 3319) == (1000, 3320)
    assert demo.allowed_span(P3, 3320) is None
    assert demo.allowed_span(P1, 2000) == (1000, 5000)  # a survivor: the round
    assert demo.allowed_span(P2, 8300) == (5000, 8320)  # dead since 8000
    assert demo.allowed_span(P2, 8500) is None
    assert demo.allowed_span("nobody", 2000) is None
    assert demo.allowed_span(P1, 999) is None
    assert demo.allowed_players(2000, 3000) == (P1, P2, P3, P4)
    assert demo.allowed_players(2000, 3320) == (P1, P2, P3, P4)
    assert demo.allowed_players(2000, 3321) == (P1, P2, P4)
    assert demo.allowed_players(3000, 3100) == (P1, P2, P4)  # p3 is dead at the start
    assert demo.allowed_players(7600, 8000) == (P2, P4)
    assert demo.allowed_players(100, 200) == ()
    rnd = demo.rounds[0]
    assert demo.latest_shot_end(rnd, rnd.life_of(P1)) == 5000
    assert demo.latest_shot_end(rnd, rnd.life_of(P3)) == 3320


def test_a_player_who_was_never_alive_cannot_be_followed(demo):
    lives = (Life(P1, "CT", 1000, 5000), Life(P3, "T", 2000, 2000))
    demo = replace(demo, rounds=(replace(demo.rounds[0], lives=lives), demo.rounds[1]),
                   kills=(), bomb_events=())
    assert demo.allowed_span(P3, 2000) is None
    assert demo.allowed_players(2000, 2100) == (P1,)
    assert shot_problems(shot(2000, 2100, P3), demo) != []


# -- shots against a demo ---------------------------------------------------


def test_valid_shots_have_no_problems(demo):
    assert shot_problems(shot(1000, 5000, P1), demo) == []  # the whole round
    assert shot_problems(shot(2000, 3000, P3), demo) == []  # right up to the death
    assert shot_problems(shot(4000, 4900, P1), demo) == []  # after the round is decided
    assert shotlist_problems(ShotList(shots=(shot(), shot(2000, 3000, P3))), demo) == []


def test_a_shot_may_run_on_after_its_players_death(demo):
    assert shot_problems(shot(2000, 3001, P3), demo) == []
    assert shot_problems(shot(2000, 3320, P3), demo) == []  # death + 5 s exactly
    assert shot_problems(shot(2999, 3320, P3), demo) == []  # alive for its first tick only
    assert shot_problems(shot(1000, 3320, P3), demo) == []  # the widest there is

    (problem,) = shot_problems(shot(2000, 3321, P3), demo)  # one tick more
    assert problem == ("donk dies at tick 3000. A shot can stay on them for 5 s after that, "
                       "to tick 3320; this one ends at tick 3321.")


def test_a_shot_must_start_while_its_player_is_alive(demo):
    (problem,) = shot_problems(shot(3000, 3100, P3), demo)  # starts on the death tick
    assert problem == ("donk is no longer alive from tick 3000; the shot starts at tick 3000. "
                       "A shot has to start while its player is alive.")
    # Too late at both ends: only the start is reported.
    (problem,) = shot_problems(shot(3500, 3800, P3), demo)
    assert "the shot starts at tick 3500" in problem


def test_time_after_death_stops_at_the_end_of_the_round(demo):
    # p4 now dies 200 ticks before round 1 is over (tick 5000).
    lives = tuple(replace(life, until_tick=4800) if life.player == P4 else life
                  for life in demo.rounds[0].lives)
    demo = replace(demo, rounds=(replace(demo.rounds[0], lives=lives), demo.rounds[1]),
                   kills=(), bomb_events=())
    assert demo.allowed_span(P4, 4000) == (1000, 5000)
    assert shot_problems(shot(4700, 5000, P4), demo) == []
    (problem,) = shot_problems(shot(4700, 5001, P4), demo)
    assert "after round 1 is over" in problem


def test_the_time_after_death_follows_the_tick_rate(demo):
    demo = replace(demo, tick_rate=128)  # 5 s is now 640 ticks
    assert shot_problems(shot(2000, 3640, P3), demo) == []
    (problem,) = shot_problems(shot(2000, 3641, P3), demo)
    assert "to tick 3640" in problem


TICKS = sorted(set(range(900, 9200, 100)) | {
    2999, 3001, 3319, 3320, 3321, 3999, 4319, 4320, 4321, 4999, 5001,
    6999, 7001, 7319, 7320, 7321, 7499, 7820, 7821, 7999, 8320, 8321, 8999,
})
SPANS = [(start, end) for start in TICKS for end in TICKS if start < end]


def test_shots_valid_under_the_old_rule_are_still_valid(demo):
    """Before after-death shots, the POV player had to be alive throughout."""
    old = 0
    for start, end in SPANS:
        rnd = demo.round_at(start)
        if rnd is None or end > rnd.end_tick:
            continue
        for life in rnd.lives:
            if life.covers(start, end):
                old += 1
                assert shot_problems(shot(start, end, life.player), demo) == []
    assert old > 1000


def test_the_helpers_agree_with_the_check(demo):
    players = (P1, P2, P3, P4)
    for start, end in SPANS:
        valid = tuple(p for p in players if not shot_problems(shot(start, end, p), demo))
        assert demo.allowed_players(start, end) == valid, (start, end)
    for player in players:
        for tick in TICKS:
            span = demo.allowed_span(player, tick)
            if span is None:
                continue
            assert span[0] <= tick < span[1]
            assert shot_problems(shot(*span, player), demo) == []
            assert shot_problems(shot(span[0], span[1] + 1, player), demo) != []
            if span[0] > 0:
                assert shot_problems(shot(span[0] - 1, span[1], player), demo) != []


def test_old_shot_list_files_still_load_and_stay_valid(demo, tmp_path):
    """A file written before after-death shots: same format, same version."""
    path = tmp_path / "shots.json"
    path.write_text(json.dumps({
        "version": 1,
        "tick_rate": 64,
        "shots": [
            {"demo": DEMO_PATH, "start_tick": 2000, "end_tick": 3200,
             "pov_player": P1, "speed": 1.0, "label": "killer"},
            {"demo": DEMO_PATH, "start_tick": 2680, "end_tick": 3000,
             "pov_player": P3, "speed": 0.5, "label": "donk's view"},
        ],
    }), encoding="utf-8")
    shots = ShotList.load(path)
    assert shotlist_problems(shots, demo) == []
    shots.save(path)
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1


def test_demo_path_may_be_written_differently(demo):
    other = replace(shot(), demo=r"C:\Users\me\Demos\MATCH.dem")
    assert shot_problems(other, demo) == []


@pytest.mark.parametrize("bad, message", [
    (shot(500, 1500), "not inside any round"),
    (shot(9000, 9500), "not inside any round"),
    (shot(4500, 5001), "after round 1 is over"),
    (shot(4000, 6000), "after round 1 is over"),
    (shot(2000, 3321, P3), "donk dies at tick 3000"),
    (shot(3500, 3800, P3), "donk is no longer alive from tick 3000"),
    (shot(pov="nobody"), "not in this demo"),
    (replace(shot(), demo="demos/other.dem"), "not 'demos/match.dem'"),
])
def test_invalid_shots_are_reported(demo, bad, message):
    problems = shot_problems(bad, demo)
    assert any(message in text for text in problems), problems


def test_late_spawn_and_absent_player(demo):
    late = replace(demo.rounds[0], lives=(Life(P1, "CT", 1500, 5000),))
    demo = replace(demo, rounds=(late, demo.rounds[1]), kills=(), bomb_events=())
    assert "not alive before tick 1500" in shot_problems(shot(1200, 2000, P1), demo)[0]
    assert "did not play in round 1" in shot_problems(shot(1200, 2000, P2), demo)[0]


def test_shotlist_problems_name_the_shot(demo):
    shots = ShotList(shots=(shot(), shot(2000, 3321, P3)), tick_rate=128)
    problems = shotlist_problems(shots, demo)
    assert len(problems) == 2
    assert "128 ticks per second" in problems[0]
    assert problems[1].startswith("Shot 2: ")
