import json
from dataclasses import replace

import pytest

from conftest import DEMO_PATH, P1, P2, P3, P4
from fragdirector.models import (
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
    assert demo.allowed_span(P3, 2000) == (1000, 3000)
    assert demo.allowed_span(P3, 3500) is None
    assert demo.allowed_span("nobody", 2000) is None
    assert demo.players_alive_during(2000, 3000) == (P1, P2, P3, P4)
    assert demo.players_alive_during(2000, 3001) == (P1, P2, P4)
    assert demo.players_alive_during(7600, 8000) == (P2, P4)
    assert demo.players_alive_during(100, 200) == ()


# -- shots against a demo ---------------------------------------------------


def test_valid_shots_have_no_problems(demo):
    assert shot_problems(shot(1000, 5000, P1), demo) == []  # the whole round
    assert shot_problems(shot(2000, 3000, P3), demo) == []  # right up to the death
    assert shot_problems(shot(4000, 4900, P1), demo) == []  # after the round is decided
    assert shotlist_problems(ShotList(shots=(shot(), shot(2000, 3000, P3))), demo) == []


def test_demo_path_may_be_written_differently(demo):
    other = replace(shot(), demo=r"C:\Users\me\Demos\MATCH.dem")
    assert shot_problems(other, demo) == []


@pytest.mark.parametrize("bad, message", [
    (shot(500, 1500), "not inside any round"),
    (shot(9000, 9500), "not inside any round"),
    (shot(4500, 5001), "after round 1 is over"),
    (shot(4000, 6000), "after round 1 is over"),
    (shot(2000, 3001, P3), "donk is no longer alive from tick 3000"),
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
    shots = ShotList(shots=(shot(), shot(2000, 3001, P3)), tick_rate=128)
    problems = shotlist_problems(shots, demo)
    assert len(problems) == 2
    assert "128 ticks per second" in problems[0]
    assert problems[1].startswith("Shot 2: ")
