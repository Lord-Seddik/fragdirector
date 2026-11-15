"""Edit operations on a shot list.

Every function takes a ``ShotList`` and returns a new one; the one passed in
is never changed, so keeping the old value is all undo needs.

An edit that would leave a shot invalid (outside its round, starting when
its POV player is not alive, running on too long after their death, start
not before end) is refused with an ``EditError`` whose message can be shown
to the user. The rule itself is ``shot_problems`` in ``models.py``. Only the shot being edited is checked, so a list
that already has a problem elsewhere can still be worked on.

Positions are 0-based. Ticks are demo ticks.
"""

from __future__ import annotations

from dataclasses import replace

from .models import Demo, ModelError, Shot, ShotList, shot_problems


class EditError(ValueError):
    """The edit was refused. The message says why."""


def _position(shotlist: ShotList, index: int, upto: int | None = None) -> int:
    limit = len(shotlist) if upto is None else upto
    if isinstance(index, bool) or not isinstance(index, int):
        raise EditError(f"A shot position must be a whole number, got {index!r}.")
    if not 0 <= index < limit:
        raise EditError(f"There is no shot at position {index} (the list has {len(shotlist)}).")
    return index


def _checked(shot: Shot, demo: Demo) -> Shot:
    problems = shot_problems(shot, demo)
    if problems:
        raise EditError(" ".join(problems))
    return shot


def _with_shots(shotlist: ShotList, shots: list[Shot]) -> ShotList:
    return replace(shotlist, shots=tuple(shots))


def _change(shotlist: ShotList, demo: Demo | None, index: int, **changes) -> ShotList:
    index = _position(shotlist, index)
    try:
        shot = replace(shotlist.shots[index], **changes)
    except ModelError as error:
        raise EditError(str(error)) from error
    if demo is not None:
        _checked(shot, demo)
    shots = list(shotlist.shots)
    shots[index] = shot
    return _with_shots(shotlist, shots)


def insert(shotlist: ShotList, demo: Demo, shot: Shot, index: int | None = None) -> ShotList:
    """Add ``shot`` so it ends up at ``index``; at the end if no index is given."""
    if not isinstance(shot, Shot):
        raise EditError(f"Expected a shot, got {shot!r}.")
    if index is None:
        index = len(shotlist)
    index = _position(shotlist, index, upto=len(shotlist) + 1)
    shots = list(shotlist.shots)
    shots.insert(index, _checked(shot, demo))
    return _with_shots(shotlist, shots)


def delete(shotlist: ShotList, index: int) -> ShotList:
    index = _position(shotlist, index)
    shots = list(shotlist.shots)
    del shots[index]
    return _with_shots(shotlist, shots)


def move(shotlist: ShotList, from_index: int, to_index: int) -> ShotList:
    """Reorder: the shot at ``from_index`` ends up at ``to_index``."""
    from_index = _position(shotlist, from_index)
    to_index = _position(shotlist, to_index)
    shots = list(shotlist.shots)
    shots.insert(to_index, shots.pop(from_index))
    return _with_shots(shotlist, shots)


def duplicate(shotlist: ShotList, index: int) -> ShotList:
    """Put a copy of the shot right after it."""
    index = _position(shotlist, index)
    shots = list(shotlist.shots)
    shots.insert(index + 1, shots[index])
    return _with_shots(shotlist, shots)


def trim(
    shotlist: ShotList,
    demo: Demo,
    index: int,
    start_tick: int | None = None,
    end_tick: int | None = None,
) -> ShotList:
    """Set a new start and/or end tick for a shot (lengthen or shorten it)."""
    index = _position(shotlist, index)
    shot = shotlist.shots[index]
    return _change(
        shotlist,
        demo,
        index,
        start_tick=shot.start_tick if start_tick is None else start_tick,
        end_tick=shot.end_tick if end_tick is None else end_tick,
    )


def trim_by(
    shotlist: ShotList,
    demo: Demo,
    index: int,
    start_delta: int = 0,
    end_delta: int = 0,
) -> ShotList:
    """Shift a shot's start and/or end by a number of ticks.

    ``start_delta=-32`` starts the shot half a second earlier at 64 ticks per
    second; ``end_delta=32`` ends it half a second later.
    """
    index = _position(shotlist, index)
    for name, delta in (("start_delta", start_delta), ("end_delta", end_delta)):
        if isinstance(delta, bool) or not isinstance(delta, int):
            raise EditError(f"{name} must be a whole number of ticks, got {delta!r}.")
    shot = shotlist.shots[index]
    return trim(shotlist, demo, index, shot.start_tick + start_delta, shot.end_tick + end_delta)


def change_pov(shotlist: ShotList, demo: Demo, index: int, pov_player: str) -> ShotList:
    """Show the same span through another player.

    They must be alive when the shot starts; they may die during it if the
    shot ends within the time allowed after a death
    (``Demo.allowed_players`` lists who qualifies).
    """
    return _change(shotlist, demo, index, pov_player=pov_player)


def set_speed(shotlist: ShotList, index: int, speed: float) -> ShotList:
    """1.0 is real time, 0.5 is half speed."""
    return _change(shotlist, None, index, speed=speed)


def set_label(shotlist: ShotList, index: int, label: str) -> ShotList:
    return _change(shotlist, None, index, label=label)
