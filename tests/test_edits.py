import random

import pytest

from conftest import DEMO_PATH, P1, P2, P3, P4
from fragdirector import edits
from fragdirector.edits import EditError
from fragdirector.models import Shot, ShotList, shotlist_problems


def shot(start=2000, end=3200, pov=P1, **extra) -> Shot:
    return Shot(demo=DEMO_PATH, start_tick=start, end_tick=end, pov_player=pov, **extra)


@pytest.fixture
def shots() -> ShotList:
    return ShotList(shots=(
        shot(2000, 3200, P1, label="a"),
        shot(2600, 3000, P3, label="b"),
        shot(6000, 7400, P2, label="c"),
    ))


def labels(shotlist: ShotList) -> str:
    return "".join(s.label for s in shotlist.shots)


# -- insert / delete / move / duplicate --------------------------------------


def test_insert(shots, demo):
    new = shot(7600, 8000, P4, label="x")
    assert labels(edits.insert(shots, demo, new)) == "abcx"
    assert labels(edits.insert(shots, demo, new, 0)) == "xabc"
    assert labels(edits.insert(shots, demo, new, 2)) == "abxc"
    assert labels(edits.insert(shots, demo, new, 3)) == "abcx"
    assert labels(shots) == "abc"  # the original is untouched


def test_insert_refuses_invalid_shots(shots, demo):
    with pytest.raises(EditError, match="no longer alive"):
        edits.insert(shots, demo, shot(7000, 7600, P3))  # dies at 7500
    with pytest.raises(EditError, match="after round 1 is over"):
        edits.insert(shots, demo, shot(4000, 5500, P1))
    with pytest.raises(EditError, match="no shot at position"):
        edits.insert(shots, demo, shot(), 4)
    with pytest.raises(EditError):
        edits.insert(shots, demo, {"start_tick": 1})


def test_delete(shots):
    assert labels(edits.delete(shots, 1)) == "ac"
    assert labels(edits.delete(edits.delete(edits.delete(shots, 0), 0), 0)) == ""
    with pytest.raises(EditError):
        edits.delete(shots, 3)
    with pytest.raises(EditError):
        edits.delete(shots, -1)
    with pytest.raises(EditError):
        edits.delete(ShotList(), 0)


def test_move(shots):
    assert labels(edits.move(shots, 0, 2)) == "bca"
    assert labels(edits.move(shots, 2, 0)) == "cab"
    assert labels(edits.move(shots, 0, 1)) == "bac"
    assert labels(edits.move(shots, 1, 1)) == "abc"
    with pytest.raises(EditError):
        edits.move(shots, 0, 3)
    with pytest.raises(EditError):
        edits.move(shots, 1.0, 0)


def test_duplicate(shots):
    result = edits.duplicate(shots, 1)
    assert labels(result) == "abbc"
    assert result.shots[1] == result.shots[2]


# -- trim ---------------------------------------------------------------------


def test_trim(shots, demo):
    result = edits.trim(shots, demo, 0, start_tick=1800)
    assert (result.shots[0].start_tick, result.shots[0].end_tick) == (1800, 3200)
    result = edits.trim(shots, demo, 0, end_tick=5000)  # up to the round's end
    assert result.shots[0].end_tick == 5000
    result = edits.trim(shots, demo, 1, start_tick=1000, end_tick=3000)  # whole life
    assert (result.shots[1].start_tick, result.shots[1].end_tick) == (1000, 3000)
    assert shots.shots[0].start_tick == 2000


def test_trim_by(shots, demo):
    result = edits.trim_by(shots, demo, 0, start_delta=-32, end_delta=32)
    assert (result.shots[0].start_tick, result.shots[0].end_tick) == (1968, 3232)
    result = edits.trim_by(shots, demo, 0, end_delta=-32)
    assert result.shots[0].end_tick == 3168


def test_trim_refuses_invalid_results(shots, demo):
    with pytest.raises(EditError, match="after round 1 is over"):
        edits.trim(shots, demo, 0, end_tick=5001)
    with pytest.raises(EditError, match="not inside any round"):
        edits.trim(shots, demo, 0, start_tick=999)
    with pytest.raises(EditError, match="no longer alive from tick 3000"):
        edits.trim_by(shots, demo, 1, end_delta=1)  # P3 dies at 3000
    with pytest.raises(EditError, match="must start before it ends"):
        edits.trim(shots, demo, 0, start_tick=3200)
    with pytest.raises(EditError, match="must start before it ends"):
        edits.trim_by(shots, demo, 0, end_delta=-1200)
    with pytest.raises(EditError, match="whole number"):
        edits.trim(shots, demo, 0, start_tick=2000.5)
    with pytest.raises(EditError, match="whole number"):
        edits.trim_by(shots, demo, 0, start_delta=0.5)
    with pytest.raises(EditError, match="0 or more"):
        edits.trim_by(shots, demo, 0, start_delta=-5000)


# -- POV, speed, label --------------------------------------------------------


def test_change_pov(shots, demo):
    assert edits.change_pov(shots, demo, 1, P4).shots[1].pov_player == P4
    with pytest.raises(EditError, match="no longer alive"):
        edits.change_pov(shots, demo, 0, P3)  # shot runs to 3200, P3 dies at 3000
    with pytest.raises(EditError, match="not in this demo"):
        edits.change_pov(shots, demo, 0, "nobody")
    with pytest.raises(EditError):
        edits.change_pov(shots, demo, 0, "")


def test_set_speed_and_label(shots):
    assert edits.set_speed(shots, 0, 0.5).shots[0].speed == 0.5
    assert edits.set_label(shots, 0, "ace").shots[0].label == "ace"
    with pytest.raises(EditError, match="more than 0"):
        edits.set_speed(shots, 0, 0)
    with pytest.raises(EditError):
        edits.set_speed(shots, 0, "slow")
    with pytest.raises(EditError):
        edits.set_label(shots, 0, None)


# -- every edit keeps the list valid -----------------------------------------


def random_edit(rng: random.Random, shots: ShotList, demo) -> ShotList:
    position = lambda: rng.randrange(-1, len(shots) + 2)
    tick = lambda: rng.randrange(800, 9300)
    player = lambda: rng.choice([P1, P2, P3, P4, "nobody"])
    choice = rng.randrange(9)
    if choice == 0:
        start = tick()
        new = Shot(DEMO_PATH, start, start + rng.randrange(1, 1500), player())
        return edits.insert(shots, demo, new, rng.choice([None, position()]))
    if choice == 1:
        return edits.delete(shots, position())
    if choice == 2:
        return edits.move(shots, position(), position())
    if choice == 3:
        return edits.duplicate(shots, position())
    if choice == 4:
        return edits.trim(shots, demo, position(), rng.choice([None, tick()]),
                          rng.choice([None, tick()]))
    if choice == 5:
        return edits.trim_by(shots, demo, position(), rng.randrange(-700, 700),
                             rng.randrange(-700, 700))
    if choice == 6:
        return edits.change_pov(shots, demo, position(), player())
    if choice == 7:
        return edits.set_speed(shots, position(), rng.choice([0.25, 0.5, 1, 2, 0, -1]))
    return edits.set_label(shots, position(), rng.choice(["", "clutch", "ace ☆"]))


@pytest.mark.parametrize("seed", range(5))
def test_random_edits_never_break_the_list(demo, seed):
    rng = random.Random(seed)
    shots = ShotList()
    accepted = refused = 0
    for _ in range(3000):
        before = shots
        try:
            shots = random_edit(rng, shots, demo)
            accepted += 1
        except EditError as error:
            assert str(error)  # a refusal always says why
            assert shots is before
            refused += 1
            continue
        assert shotlist_problems(shots, demo) == []
        if len(shots) > 12:  # keep the list small so deletes and moves stay likely
            shots = edits.delete(shots, 0)
    assert accepted > 300 and refused > 300
