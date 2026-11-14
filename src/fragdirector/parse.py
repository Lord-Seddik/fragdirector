"""Read a CS2 demo (``.dem``) into a ``models.Demo``.

The work is split in two so that most of it can be tested without a demo file:

* ``read_raw`` asks ``demoparser2`` for the events we need and returns them
  as plain Python values (a ``RawDemo``). It is the only part that touches
  the parser library.
* ``build_demo`` turns a ``RawDemo`` into a ``Demo``: it works out the rounds,
  who was alive when, and cleans up the kills. It is plain code on plain data.

Choices made while turning raw events into a ``Demo``:

* A round runs from its ``round_start`` event to the next round's
  ``round_start`` (the last round runs to the end of the demo). Everything
  that happens in between, including the seconds after the winner is known,
  belongs to it.
* A round's players are the ones on a team when freeze time ends. Their side
  comes from that moment, so half-time swaps need no special handling.
* A death before freeze time ends (CS2 respawns players who use ``kill``
  before the match starts) is dropped: the player is alive again afterwards.
* A death by the world (fall, bomb) or by oneself has ``attacker = None``.
* Bots have no SteamID, so their id is ``bot:<name>``.
* If round numbers start over (a restart before the real match), only the last
  run of rounds is kept.
* ``tick_rate`` is 64: CS2 demos do not store it and the game runs at 64.
* A player who disconnects in the middle of a round is treated as alive until
  the round ends.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Any

from fragdirector.models import (
    DEFAULT_TICK_RATE,
    BombEvent,
    Demo,
    Kill,
    Life,
    ModelError,
    Player,
    Round,
)

# The ``team_num`` values CS2 uses for the two playing sides.
TEAM_SIDES = {2: "T", 3: "CT"}
# Weapons that mean "nobody killed this player".
NO_ATTACKER_WEAPONS = ("world", "planted_c4")
BOMB_EVENTS = {"bomb_planted": "planted", "bomb_defused": "defused", "bomb_exploded": "exploded"}


class DemoParseError(ValueError):
    """The file could not be read as a CS2 demo."""


# --------------------------------------------------------------------------
# Raw events (plain data, as the parser library gives them)
# --------------------------------------------------------------------------


@dataclass
class RawDemo:
    """What ``read_raw`` collects; the input of ``build_demo``.

    ``deaths`` and ``bombs`` hold one dict per event with the parser's own
    column names (``tick``, ``user_steamid``, ``attacker_steamid``,
    ``weapon``, ``headshot``, ``thrusmoke``, ...); missing keys mean "no".
    ``rosters`` maps a freeze-end tick to the players on a team then, each a
    dict with ``steamid``, ``name`` and ``team_num``.
    """

    path: str
    map_name: str
    last_tick: int
    round_starts: list[tuple[int, int]] = field(default_factory=list)  # (number, tick)
    freeze_ends: list[int] = field(default_factory=list)
    round_ends: list[dict[str, Any]] = field(default_factory=list)  # tick, winner, reason
    deaths: list[dict[str, Any]] = field(default_factory=list)
    bombs: list[dict[str, Any]] = field(default_factory=list)  # kind, tick, user_steamid, ...
    rosters: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    tick_rate: int = DEFAULT_TICK_RATE


def _clean(value: Any) -> Any:
    """Turn the parser's missing values (NaN, None) into ``None``."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def _records(frame: Any) -> list[dict[str, Any]]:
    """Rows of a parser result as dicts. Unknown or empty results give ``[]``."""
    if frame is None or isinstance(frame, list) or len(frame) == 0:
        return []
    return [{key: _clean(value) for key, value in row.items()} for row in frame.to_dict("records")]


def read_raw(path: str | os.PathLike) -> RawDemo:
    """Read the events we need from a demo file."""
    path = os.fspath(path)
    if not os.path.isfile(path):
        raise DemoParseError(f"Demo file not found: {path}")
    try:
        from demoparser2 import DemoParser

        parser = DemoParser(path)
        header = parser.parse_header()
        raw = RawDemo(path=path, map_name=str(header.get("map_name") or ""), last_tick=0)
        for row in _records(parser.parse_event("round_start")):
            raw.round_starts.append((int(row["round"]), int(row["tick"])))
        raw.freeze_ends = [int(row["tick"]) for row in _records(parser.parse_event("round_freeze_end"))]
        raw.round_ends = _records(parser.parse_event("round_end"))
        raw.deaths = _records(parser.parse_event("player_death"))
        for event, kind in BOMB_EVENTS.items():
            for row in _records(parser.parse_event(event)):
                raw.bombs.append({**row, "kind": kind})

        if raw.freeze_ends:
            frame = parser.parse_ticks(["team_num", "name"], ticks=sorted(set(raw.freeze_ends)))
            for row in _records(frame):
                raw.rosters.setdefault(int(row["tick"]), []).append(row)
        ticks = parser.parse_ticks(["tick"])
        raw.last_tick = int(ticks["tick"].max()) if len(ticks) else 0
    except DemoParseError:
        raise
    except Exception as error:  # demoparser2 raises its own exception types
        raise DemoParseError(f"Could not read {path} as a CS2 demo: {error}") from error

    event_ticks = [t for _, t in raw.round_starts] + [int(d["tick"]) for d in raw.deaths]
    raw.last_tick = max([raw.last_tick, *event_ticks], default=0)
    return raw


def parse_demo(path: str | os.PathLike) -> Demo:
    """Read a ``.dem`` file into a ``Demo`` (rounds, kills, bomb events, players)."""
    return build_demo(read_raw(path))


# --------------------------------------------------------------------------
# Raw events -> Demo
# --------------------------------------------------------------------------


def _player_id(steamid: Any, name: Any) -> str | None:
    """Our id for a player: the SteamID64 as text, or ``bot:<name>`` for a bot."""
    steamid = _clean(steamid)
    if steamid is None:
        return None
    text = str(int(steamid)) if isinstance(steamid, (int, float)) else str(steamid).strip()
    if text in ("", "0"):
        return f"bot:{name}" if _clean(name) else None
    return text


def _round_windows(raw: RawDemo) -> list[tuple[int, int, int]]:
    """``(number, start_tick, end_tick)`` for every round, in order."""
    starts = sorted(
        ((number, tick) for number, tick in raw.round_starts if number >= 1),
        key=lambda item: item[1],
    )
    # Round numbers that start over mean the match was restarted: keep the last run.
    first = 0
    for index in range(1, len(starts)):
        if starts[index][0] <= starts[index - 1][0]:
            first = index
    starts = starts[first:]

    windows = []
    for index, (number, start) in enumerate(starts):
        if index + 1 < len(starts):
            end = starts[index + 1][1]
        else:
            end = max(raw.last_tick + 1, start + 1)
        if end > start:
            windows.append((number, start, end))
    return windows


def _first_in(window: tuple[int, int, int], items: list[dict[str, Any]]) -> dict[str, Any] | None:
    for item in items:
        if window[1] <= int(item["tick"]) < window[2]:
            return item
    return None


def build_demo(raw: RawDemo) -> Demo:
    """Turn raw events into a ``Demo``. Raises ``DemoParseError`` if no rounds are found."""
    windows = _round_windows(raw)
    if not windows:
        raise DemoParseError(f"No rounds found in {raw.path}: is this a CS2 match demo?")

    freeze_ends = sorted(raw.freeze_ends)
    round_ends = sorted(raw.round_ends, key=lambda item: int(item["tick"]))
    freeze_of: dict[int, int] = {}
    for window in windows:
        found = [t for t in freeze_ends if window[1] <= t < window[2]]
        freeze_of[window[0]] = found[0] if found else window[1]

    # -- players ---------------------------------------------------------
    names: dict[str, str] = {}

    def remember(player: str | None, name: Any) -> None:
        if player is None:
            return
        name = _clean(name)
        if name:
            names[player] = str(name)  # the latest name wins
        else:
            names.setdefault(player, "")

    # -- kills -----------------------------------------------------------
    kills: list[Kill] = []
    for death in sorted(raw.deaths, key=lambda item: int(item["tick"])):
        tick = int(death["tick"])
        window = next((w for w in windows if w[1] <= tick < w[2]), None)
        if window is None or tick < freeze_of[window[0]]:
            continue
        victim = _player_id(death.get("user_steamid"), death.get("user_name"))
        if victim is None:
            continue
        attacker = _player_id(death.get("attacker_steamid"), death.get("attacker_name"))
        assister = _player_id(death.get("assister_steamid"), death.get("assister_name"))
        weapon = str(_clean(death.get("weapon")) or "")
        if attacker == victim or weapon in NO_ATTACKER_WEAPONS:
            attacker = None
        if assister in (victim, attacker):
            assister = None
        remember(victim, death.get("user_name"))
        remember(attacker, death.get("attacker_name"))
        remember(assister, death.get("assister_name"))
        kills.append(Kill(
            tick=tick,
            round=window[0],
            victim=victim,
            attacker=attacker,
            assister=assister,
            weapon=weapon,
            headshot=bool(_clean(death.get("headshot"))),
            noscope=bool(_clean(death.get("noscope"))),
            through_smoke=bool(_clean(death.get("thrusmoke"))),
            penetrated=int(_clean(death.get("penetrated")) or 0),
            attacker_blind=bool(_clean(death.get("attackerblind"))),
            attacker_in_air=bool(_clean(death.get("attackerinair"))),
            flash_assist=bool(_clean(death.get("assistedflash"))),
            distance=max(0.0, float(_clean(death.get("distance")) or 0.0)),
        ))

    # -- rounds, with who was alive ----------------------------------------
    first_death: dict[tuple[int, str], int] = {}
    for kill in kills:
        first_death.setdefault((kill.round, kill.victim), kill.tick)

    rounds: list[Round] = []
    for window in windows:
        number, start, end = window
        freeze_end = freeze_of[number]
        lives = []
        seen: set[str] = set()
        for row in raw.rosters.get(freeze_end, []):
            player = _player_id(row.get("steamid"), row.get("name"))
            side = TEAM_SIDES.get(int(_clean(row.get("team_num")) or 0))
            if player is None or side is None or player in seen:
                continue
            seen.add(player)
            remember(player, row.get("name"))
            died = first_death.get((number, player))
            lives.append(Life(player, side, start, min(end, died) if died is not None else end))

        round_end = _first_in(window, round_ends)
        winner = None
        reason = ""
        decided = None
        if round_end is not None:
            decided = int(round_end["tick"])
            reason = str(_clean(round_end.get("reason")) or "")
            text = str(_clean(round_end.get("winner")) or "").upper()
            winner = text if text in ("CT", "T") else None
        rounds.append(Round(
            number=number, start_tick=start, freeze_end_tick=freeze_end, end_tick=end,
            lives=tuple(lives), decided_tick=decided, winner=winner, reason=reason,
        ))

    # -- bomb events -------------------------------------------------------
    bombs: list[BombEvent] = []
    for event in sorted(raw.bombs, key=lambda item: int(item["tick"])):
        tick = int(event["tick"])
        window = next((w for w in windows if w[1] <= tick < w[2]), None)
        if window is None:
            continue
        player = _player_id(event.get("user_steamid"), event.get("user_name"))
        remember(player, event.get("user_name"))
        site = _clean(event.get("site"))
        bombs.append(BombEvent(
            tick=tick, round=window[0], kind=event["kind"], player=player,
            site="" if site is None else str(int(site)),
        ))

    players = tuple(Player(player_id, name) for player_id, name in names.items())
    try:
        return Demo(
            path=raw.path, tick_rate=raw.tick_rate, players=players, rounds=tuple(rounds),
            kills=tuple(kills), bomb_events=tuple(bombs), map_name=raw.map_name,
        )
    except ModelError as error:
        raise DemoParseError(f"{raw.path} has events that do not fit together: {error}") from error


# --------------------------------------------------------------------------
# Scoreboard
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ScoreLine:
    player: str
    kills: int
    deaths: int
    assists: int


def side_in_round(demo: Demo, player: str | None, round_number: int) -> str | None:
    rnd = demo.round_number(round_number)
    life = rnd.life_of(player) if rnd is not None and player is not None else None
    return life.side if life is not None else None


def is_team_kill(demo: Demo, kill: Kill) -> bool:
    """True if the killer and the victim were on the same side."""
    if kill.attacker is None:
        return False
    side = side_in_round(demo, kill.attacker, kill.round)
    return side is not None and side == side_in_round(demo, kill.victim, kill.round)


def scoreboard(demo: Demo) -> list[ScoreLine]:
    """Kills, deaths and assists per player, as the in-game scoreboard counts them.

    A death with no attacker still counts as a death. Kills and assists
    against a player's own side (team damage) are not counted.

    Deaths can come out one lower than the game shows: a player who uses
    ``kill`` before the match starts gets a death on the scoreboard, but that
    death is not in the ``Demo`` (see the module notes).
    """
    kills: dict[str, int] = {player.id: 0 for player in demo.players}
    deaths = dict(kills)
    assists = dict(kills)
    for kill in demo.kills:
        deaths[kill.victim] += 1
        if kill.attacker is not None and not is_team_kill(demo, kill):
            kills[kill.attacker] += 1
        if kill.assister is not None:
            assister_side = side_in_round(demo, kill.assister, kill.round)
            if assister_side is None or assister_side != side_in_round(demo, kill.victim, kill.round):
                assists[kill.assister] += 1
    lines = [ScoreLine(p.id, kills[p.id], deaths[p.id], assists[p.id]) for p in demo.players]
    return sorted(lines, key=lambda line: (-line.kills, line.deaths, line.player))
