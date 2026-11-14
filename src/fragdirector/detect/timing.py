"""Timing detectors: quick kill gaps, opening kills, trades, late-round kills,
last-second defuses.

Everything works from the parsed ``Demo`` alone and returns a list of
``Moment`` objects in order of tick.

Choices made:

* Team kills and kills with no attacker never count (as in ``basic``).
* Quick kills: two kills by one player in one round at most ``max_gap_seconds``
  apart (default 1.0 s). A run of fast kills gives one moment per pair.
* Opening kill: the first kill of a round, if it is an enemy kill.
* Trade kill: a kill on the player who killed a teammate of the trader at most
  ``window_seconds`` (default 5 s) earlier, in the same round.
* Late kill: a kill in the last ``last_seconds`` (default 10 s) of the round
  clock (``round_seconds`` default 115 minus time since freeze time ended).
  Once the bomb is planted the clock no longer decides the round, so kills
  after a plant are not late kills.
* Last-second defuse: a defuse at most ``last_seconds`` (default 1.0 s)
  before the bomb would have exploded, ``bomb_seconds`` (default 40) after
  the plant. Needs the plant and the defuse in the demo's bomb events.
"""

from __future__ import annotations

from fragdirector.detect.basic import DEFAULT_ROUND_SECONDS, _enemy_kills
from fragdirector.models import Demo, Kill, Moment
from fragdirector.parse import side_in_round

DEFAULT_MAX_GAP_SECONDS = 1.0
DEFAULT_TRADE_SECONDS = 5.0
DEFAULT_LATE_SECONDS = 10.0
DEFAULT_BOMB_SECONDS = 40.0
DEFAULT_LAST_SECOND_DEFUSE = 1.0


def detect_quick_kills(
    demo: Demo,
    max_gap_seconds: float = DEFAULT_MAX_GAP_SECONDS,
    player: str | None = None,
) -> list[Moment]:
    """Pairs of consecutive kills by one player in one round, close together."""
    limit = max_gap_seconds * demo.tick_rate
    last: dict[tuple[int, str], Kill] = {}
    moments = []
    for kill in _enemy_kills(demo, player):
        key = (kill.round, kill.attacker)
        before = last.get(key)
        if before is not None and kill.tick - before.tick <= limit:
            gap = kill.tick - before.tick
            moments.append(Moment(
                kind="quick_kills", round=kill.round, player=kill.attacker,
                start_tick=before.tick, end_tick=kill.tick, kills=(before, kill),
                tags=("quick_kills",),
                details={"gap_ms": round(gap * 1000 / demo.tick_rate, 1)},
            ))
        last[key] = kill
    return sorted(moments, key=lambda m: (m.start_tick, m.player))


def detect_opening_kills(demo: Demo, player: str | None = None) -> list[Moment]:
    """The first kill of each round."""
    moments = []
    for rnd in demo.rounds:
        first = next((k for k in demo.kills if k.round == rnd.number), None)
        if first is None or first.attacker is None or first not in _enemy_kills(demo, first.attacker):
            continue
        if player is not None and first.attacker != player:
            continue
        seconds = max(0, first.tick - rnd.freeze_end_tick) / demo.tick_rate
        moments.append(Moment(
            kind="opening_kill", round=rnd.number, player=first.attacker,
            start_tick=first.tick, end_tick=first.tick, kills=(first,),
            tags=("opening",), details={"seconds_into_round": round(seconds, 2)},
        ))
    return moments


def detect_trade_kills(
    demo: Demo,
    window_seconds: float = DEFAULT_TRADE_SECONDS,
    player: str | None = None,
) -> list[Moment]:
    """Kills on the player who just killed one of the killer's teammates."""
    limit = window_seconds * demo.tick_rate
    enemy = _enemy_kills(demo)
    moments = []
    for kill in enemy:
        if player is not None and kill.attacker != player:
            continue
        side = side_in_round(demo, kill.attacker, kill.round)
        for earlier in enemy:
            if earlier.tick >= kill.tick:
                break
            if (earlier.round == kill.round and earlier.attacker == kill.victim
                    and kill.tick - earlier.tick <= limit
                    and side_in_round(demo, earlier.victim, kill.round) == side):
                moments.append(Moment(
                    kind="trade_kill", round=kill.round, player=kill.attacker,
                    start_tick=earlier.tick, end_tick=kill.tick, kills=(earlier, kill),
                    tags=("trade",),
                    details={"traded_player": earlier.victim,
                             "seconds": round((kill.tick - earlier.tick) / demo.tick_rate, 2)},
                ))
                break
    return sorted(moments, key=lambda m: (m.end_tick, m.player))


def detect_late_kills(
    demo: Demo,
    last_seconds: float = DEFAULT_LATE_SECONDS,
    round_seconds: float = DEFAULT_ROUND_SECONDS,
    player: str | None = None,
) -> list[Moment]:
    """Kills in the last ``last_seconds`` of the round clock, before any plant."""
    moments = []
    for kill in _enemy_kills(demo, player):
        rnd = demo.round_number(kill.round)
        if rnd is None:
            continue
        if any(b.round == rnd.number and b.kind == "planted" and b.tick <= kill.tick
               for b in demo.bomb_events):
            continue
        left = round_seconds - max(0, kill.tick - rnd.freeze_end_tick) / demo.tick_rate
        if left <= last_seconds:
            moments.append(Moment(
                kind="late_kill", round=kill.round, player=kill.attacker,
                start_tick=kill.tick, end_tick=kill.tick, kills=(kill,),
                tags=("late_round",), details={"time_left_seconds": round(max(0.0, left), 2)},
            ))
    return moments


def detect_late_defuses(
    demo: Demo,
    last_seconds: float = DEFAULT_LAST_SECOND_DEFUSE,
    bomb_seconds: float = DEFAULT_BOMB_SECONDS,
    player: str | None = None,
) -> list[Moment]:
    """Defuses that finish within ``last_seconds`` of the bomb exploding."""
    moments = []
    for event in demo.bomb_events:
        if event.kind != "defused" or event.player is None:
            continue
        if player is not None and event.player != player:
            continue
        plant = next((b for b in demo.bomb_events
                      if b.round == event.round and b.kind == "planted" and b.tick <= event.tick), None)
        if plant is None:
            continue
        left = bomb_seconds - (event.tick - plant.tick) / demo.tick_rate
        if left <= last_seconds:
            moments.append(Moment(
                kind="late_defuse", round=event.round, player=event.player,
                start_tick=event.tick, end_tick=event.tick,
                tags=("defuse", "last_second"),
                details={"time_left_seconds": round(max(0.0, left), 2)},
            ))
    return moments


def detect_timing(demo: Demo) -> list[Moment]:
    """All timing detectors with default settings, in order of tick."""
    moments = (detect_quick_kills(demo) + detect_opening_kills(demo) + detect_trade_kills(demo)
               + detect_late_kills(demo) + detect_late_defuses(demo))
    return sorted(moments, key=lambda m: (m.start_tick, m.kind, m.player))
