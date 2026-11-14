"""Aim detector: flicks (a big turn of the view just before a kill).

The maths works on plain ``(tick, pitch, yaw)`` samples so it can be tested
with made-up data. Only ``read_view_angles`` touches a ``.dem`` file.

Choices made:

* The angle between two views is the real angle between the two look
  directions, so yaw wrap-around at +-180 and the shrinking of yaw near
  straight up or down are handled.
* A flick is measured over the ``window_seconds`` (default 0.2 s) up to and
  including the kill tick: the angle between the view at the kill tick and
  the view that was furthest away from it inside the window. ``ms`` is the
  time from that earlier view to the kill.
* Angle alone also matches spray transfers and panic turns, so
  ``DEFAULT_MIN_FLICK_DEGREES`` is only a starting point: tune it on real
  demos. ``measure_flicks`` returns the number for every kill so a threshold
  can be picked.
* Kills by bots (ids ``bot:<name>``), kills with no attacker and team kills
  are not measured. Kills with no view samples in the window are skipped.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping, Sequence

from fragdirector.detect.basic import _enemy_kills
from fragdirector.models import Demo, Kill, Moment

DEFAULT_WINDOW_SECONDS = 0.2
DEFAULT_MIN_FLICK_DEGREES = 30.0

# (tick, pitch degrees, yaw degrees)
AngleSample = tuple[int, float, float]
# Reads samples: (demo path, {player id: ticks wanted}) -> {player id: samples}
AngleReader = Callable[[str, Mapping[str, Sequence[int]]], Mapping[str, Sequence[AngleSample]]]


@dataclass(frozen=True)
class Flick:
    degrees: float
    ms: float
    from_tick: int
    to_tick: int


# --------------------------------------------------------------------------
# Pure maths
# --------------------------------------------------------------------------


def _direction(pitch: float, yaw: float) -> tuple[float, float, float]:
    p, y = math.radians(pitch), math.radians(yaw)
    return (math.cos(p) * math.cos(y), math.cos(p) * math.sin(y), -math.sin(p))


def angle_between(pitch1: float, yaw1: float, pitch2: float, yaw2: float) -> float:
    """Angle in degrees (0 to 180) between two views."""
    a, b = _direction(pitch1, yaw1), _direction(pitch2, yaw2)
    dot = sum(x * y for x, y in zip(a, b))
    cross = math.sqrt(sum(c * c for c in (
        a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0],
    )))
    return math.degrees(math.atan2(cross, dot))


def flick_before(
    samples: Iterable[AngleSample],
    kill_tick: int,
    tick_rate: int = 64,
    window_seconds: float = DEFAULT_WINDOW_SECONDS,
) -> Flick | None:
    """The turn of the view in the window ending at ``kill_tick``, or ``None``
    if there is no sample at or before the kill inside the window."""
    first = kill_tick - round(window_seconds * tick_rate)
    window = sorted(s for s in samples if first <= s[0] <= kill_tick)
    if not window:
        return None
    end = window[-1]
    best = max(window, key=lambda s: angle_between(s[1], s[2], end[1], end[2]))
    degrees = angle_between(best[1], best[2], end[1], end[2])
    return Flick(degrees=degrees, ms=(end[0] - best[0]) * 1000 / tick_rate,
                 from_tick=best[0], to_tick=end[0])


# --------------------------------------------------------------------------
# Reading angles from a demo
# --------------------------------------------------------------------------


def read_view_angles(
    path: str | os.PathLike, wanted: Mapping[str, Sequence[int]],
) -> dict[str, list[AngleSample]]:
    """View angles of the given players (SteamID64 strings) at the given ticks."""
    from demoparser2 import DemoParser

    ticks = sorted({t for values in wanted.values() for t in values})
    players = [int(p) for p in wanted if p.isdigit()]
    if not ticks or not players:
        return {}
    frame = DemoParser(os.fspath(path)).parse_ticks(["pitch", "yaw"], ticks=ticks, players=players)
    result: dict[str, list[AngleSample]] = {}
    for tick, pitch, yaw, steamid in zip(frame["tick"], frame["pitch"], frame["yaw"], frame["steamid"]):
        result.setdefault(str(int(steamid)), []).append((int(tick), float(pitch), float(yaw)))
    return result


# --------------------------------------------------------------------------
# Detector
# --------------------------------------------------------------------------


def measure_flicks(
    demo: Demo,
    window_seconds: float = DEFAULT_WINDOW_SECONDS,
    player: str | None = None,
    kills: Iterable[Kill] | None = None,
    read_angles: AngleReader = read_view_angles,
) -> list[tuple[Kill, Flick]]:
    """The flick measured for every kill (no threshold), in order of tick."""
    candidates = [
        k for k in (_enemy_kills(demo, player) if kills is None else kills)
        if k.attacker is not None and k.attacker.isdigit()
    ]
    if not candidates:
        return []
    reach = round(window_seconds * demo.tick_rate)
    wanted: dict[str, set[int]] = {}
    for k in candidates:
        wanted.setdefault(k.attacker, set()).update(range(max(0, k.tick - reach), k.tick + 1))
    samples = read_angles(demo.path, {p: sorted(t) for p, t in wanted.items()})
    found = []
    for k in candidates:
        flick = flick_before(samples.get(k.attacker, ()), k.tick, demo.tick_rate, window_seconds)
        if flick is not None:
            found.append((k, flick))
    return sorted(found, key=lambda item: (item[0].tick, item[0].attacker))


def detect_flicks(
    demo: Demo,
    min_degrees: float = DEFAULT_MIN_FLICK_DEGREES,
    window_seconds: float = DEFAULT_WINDOW_SECONDS,
    player: str | None = None,
    read_angles: AngleReader = read_view_angles,
) -> list[Moment]:
    """Kills where the view turned at least ``min_degrees`` in the window."""
    moments = []
    for k, flick in measure_flicks(demo, window_seconds, player, read_angles=read_angles):
        if flick.degrees >= min_degrees:
            moments.append(Moment(
                kind="flick", round=k.round, player=k.attacker,
                start_tick=flick.from_tick, end_tick=k.tick, kills=(k,),
                tags=("flick",),
                details={"degrees": round(flick.degrees, 1), "ms": round(flick.ms, 1),
                         "weapon": k.weapon},
            ))
    return moments
