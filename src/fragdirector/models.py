"""Data model for fragdirector.

Two groups of types live here:

* What the parser reads out of a demo: ``Player``, ``Life``, ``Round``,
  ``Kill``, ``BombEvent``, collected in a ``Demo``.
* What the tool produces: ``Moment`` (a detected highlight), and ``Shot`` /
  ``ShotList`` (what ends up in the video). The shot list file format is
  described in ``docs/shotlist.md``.

Conventions used everywhere:

* All times are demo ticks (whole numbers). ``Demo.tick_rate`` converts to
  seconds.
* Every span is half-open: it includes its first tick and stops just before
  its last one. A shot ``start_tick=100, end_tick=164`` is 64 ticks long.
* Players are referred to by ``Player.id``, an opaque string (the SteamID64
  for humans). It is a string because 64-bit numbers lose precision in
  JavaScript.
* Objects are immutable and check themselves when created, so a wrong type
  or an impossible value fails at once with a ``ModelError``.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import MISSING, asdict, dataclass, fields
from pathlib import Path, PureWindowsPath
from typing import Any, Callable

SHOTLIST_VERSION = 1
DEMO_VERSION = 1
DEFAULT_TICK_RATE = 64
# Limits on what a shot list file or an editor request may hold. They are far
# outside anything useful and keep every length a finite number.
MAX_TICK = 2**31 - 1  # the game counts ticks in 32 bits
MIN_SPEED, MAX_SPEED = 0.01, 100.0
SIDES = ("CT", "T")
BOMB_EVENT_KINDS = ("planted", "defused", "exploded")


class ModelError(ValueError):
    """A model object, or the JSON it is loaded from, is not valid."""


# --------------------------------------------------------------------------
# Small checking helpers
# --------------------------------------------------------------------------


def _int(owner: str, name: str, value: Any, minimum: int | None = None) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ModelError(f"{owner}: {name} must be a whole number, got {value!r}")
    if minimum is not None and value < minimum:
        raise ModelError(f"{owner}: {name} must be {minimum} or more, got {value}")


def _opt_int(owner: str, name: str, value: Any, minimum: int | None = None) -> None:
    if value is not None:
        _int(owner, name, value, minimum)


def _number(owner: str, name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ModelError(f"{owner}: {name} must be a number, got {value!r}")
    if not math.isfinite(value):
        raise ModelError(f"{owner}: {name} must be a finite number, got {value!r}")
    return float(value)


def _str(owner: str, name: str, value: Any, allow_empty: bool = True) -> None:
    if not isinstance(value, str):
        raise ModelError(f"{owner}: {name} must be text, got {value!r}")
    if not allow_empty and not value:
        raise ModelError(f"{owner}: {name} must not be empty")


def _opt_str(owner: str, name: str, value: Any) -> None:
    if value is not None:
        _str(owner, name, value, allow_empty=False)


def _bool(owner: str, name: str, value: Any) -> None:
    if not isinstance(value, bool):
        raise ModelError(f"{owner}: {name} must be true or false, got {value!r}")


def _tuple_of(owner: str, name: str, value: Any, item_type: type) -> tuple:
    if not isinstance(value, (list, tuple)):
        raise ModelError(f"{owner}: {name} must be a list, got {value!r}")
    for item in value:
        if not isinstance(item, item_type):
            raise ModelError(
                f"{owner}: every item of {name} must be a {item_type.__name__}, got {item!r}"
            )
    return tuple(value)


def _from_dict(cls: type, data: Any, nested: dict[str, Callable[[Any], Any]] | None = None):
    """Build ``cls`` from a JSON object, refusing unknown and missing fields."""
    owner = cls.__name__
    if not isinstance(data, dict):
        raise ModelError(f"{owner}: expected a JSON object, got {data!r}")
    known = [f.name for f in fields(cls)]
    unknown = sorted(str(k) for k in data if k not in known)
    if unknown:
        raise ModelError(f"{owner}: unknown field(s): {', '.join(unknown)}")
    required = [
        f.name for f in fields(cls) if f.default is MISSING and f.default_factory is MISSING
    ]
    missing = [name for name in required if name not in data]
    if missing:
        raise ModelError(f"{owner}: missing field(s): {', '.join(missing)}")
    kwargs = dict(data)
    for name, convert in (nested or {}).items():
        if name in kwargs:
            if not isinstance(kwargs[name], list):
                raise ModelError(f"{owner}: {name} must be a list, got {kwargs[name]!r}")
            kwargs[name] = tuple(convert(item) for item in kwargs[name])
    return cls(**kwargs)


def _write_json(path: str | os.PathLike, data: dict) -> None:
    """Write via a temporary file so a crash never leaves a half-written file."""
    path = Path(path)
    text = json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _read_json(path: str | os.PathLike) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (ValueError, RecursionError) as error:  # bad UTF-8, bad JSON, a number too long to read
        raise ModelError(f"{path} is not a valid JSON file: {str(error)[:200]}") from error


def _check_version(owner: str, data: Any, expected: int) -> dict:
    if not isinstance(data, dict):
        raise ModelError(f"{owner}: expected a JSON object, got {data!r}")
    data = dict(data)
    version = data.pop("version", None)
    if version != expected:
        raise ModelError(
            f"{owner}: unsupported version {version!r} (this program reads version {expected})"
        )
    return data


def demo_file_name(path: str) -> str:
    """File name of a demo path, for comparing paths written in different ways."""
    return PureWindowsPath(path).name.casefold()


def seconds_to_ticks(seconds: float, tick_rate: int = DEFAULT_TICK_RATE) -> int:
    return round(seconds * tick_rate)


# --------------------------------------------------------------------------
# What is in a demo
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Player:
    id: str
    name: str

    def __post_init__(self) -> None:
        _str("Player", "id", self.id, allow_empty=False)
        _str("Player", "name", self.name)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> Player:
        return _from_dict(cls, data)


@dataclass(frozen=True)
class Life:
    """One player's time alive in one round.

    The player is alive on ticks ``from_tick <= tick < until_tick``.
    ``until_tick`` is the tick of their death, or the round's ``end_tick`` if
    they survived.
    """

    player: str
    side: str
    from_tick: int
    until_tick: int

    def __post_init__(self) -> None:
        _str("Life", "player", self.player, allow_empty=False)
        if self.side not in SIDES:
            raise ModelError(f"Life: side must be one of {SIDES}, got {self.side!r}")
        _int("Life", "from_tick", self.from_tick, 0)
        _int("Life", "until_tick", self.until_tick, 0)
        if self.from_tick > self.until_tick:
            raise ModelError(
                f"Life: from_tick ({self.from_tick}) is after until_tick ({self.until_tick})"
            )

    def covers(self, start_tick: int, end_tick: int) -> bool:
        """True if the player is alive for the whole span."""
        return self.from_tick <= start_tick and end_tick <= self.until_tick

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> Life:
        return _from_dict(cls, data)


@dataclass(frozen=True)
class Round:
    """One round.

    ``start_tick``       players spawn, freeze time begins
    ``freeze_end_tick``  players can move, the round clock starts
    ``decided_tick``     the winner is known (``None`` if the demo was cut)
    ``end_tick``         first tick that no longer belongs to this round

    The time between ``decided_tick`` and ``end_tick`` is part of the round,
    so a shot can keep running after the last kill.
    """

    number: int
    start_tick: int
    freeze_end_tick: int
    end_tick: int
    lives: tuple[Life, ...] = ()
    decided_tick: int | None = None
    winner: str | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        owner = f"Round {self.number!r}"
        _int("Round", "number", self.number, 1)
        _int(owner, "start_tick", self.start_tick, 0)
        _int(owner, "freeze_end_tick", self.freeze_end_tick, 0)
        _int(owner, "end_tick", self.end_tick, 0)
        _opt_int(owner, "decided_tick", self.decided_tick, 0)
        _str(owner, "reason", self.reason)
        if not self.start_tick < self.end_tick:
            raise ModelError(
                f"{owner}: start_tick ({self.start_tick}) must be before end_tick ({self.end_tick})"
            )
        if not self.start_tick <= self.freeze_end_tick <= self.end_tick:
            raise ModelError(f"{owner}: freeze_end_tick ({self.freeze_end_tick}) is outside the round")
        if self.decided_tick is not None and not (
            self.start_tick <= self.decided_tick <= self.end_tick
        ):
            raise ModelError(f"{owner}: decided_tick ({self.decided_tick}) is outside the round")
        if self.winner is not None and self.winner not in SIDES:
            raise ModelError(f"{owner}: winner must be one of {SIDES} or null, got {self.winner!r}")
        lives = _tuple_of(owner, "lives", self.lives, Life)
        object.__setattr__(self, "lives", lives)
        seen: set[str] = set()
        for life in lives:
            if life.player in seen:
                raise ModelError(f"{owner}: player {life.player!r} has more than one life")
            seen.add(life.player)
            if life.from_tick < self.start_tick or life.until_tick > self.end_tick:
                raise ModelError(f"{owner}: the life of player {life.player!r} is outside the round")

    def contains(self, tick: int) -> bool:
        return self.start_tick <= tick < self.end_tick

    def life_of(self, player: str) -> Life | None:
        for life in self.lives:
            if life.player == player:
                return life
        return None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> Round:
        return _from_dict(cls, data, {"lives": Life.from_dict})


@dataclass(frozen=True)
class Kill:
    """One death. ``attacker`` is ``None`` when nobody did it (fall, bomb)."""

    tick: int
    round: int
    victim: str
    attacker: str | None = None
    assister: str | None = None
    weapon: str = ""
    headshot: bool = False
    noscope: bool = False
    through_smoke: bool = False
    penetrated: int = 0  # number of surfaces the bullet went through
    attacker_blind: bool = False
    attacker_in_air: bool = False
    flash_assist: bool = False
    distance: float = 0.0

    def __post_init__(self) -> None:
        _int("Kill", "tick", self.tick, 0)
        _int("Kill", "round", self.round, 1)
        _str("Kill", "victim", self.victim, allow_empty=False)
        _opt_str("Kill", "attacker", self.attacker)
        _opt_str("Kill", "assister", self.assister)
        _str("Kill", "weapon", self.weapon)
        for name in ("headshot", "noscope", "through_smoke", "attacker_blind",
                     "attacker_in_air", "flash_assist"):
            _bool("Kill", name, getattr(self, name))
        _int("Kill", "penetrated", self.penetrated, 0)
        distance = _number("Kill", "distance", self.distance)
        if distance < 0:
            raise ModelError(f"Kill: distance must be 0 or more, got {distance}")
        object.__setattr__(self, "distance", distance)

    @property
    def wallbang(self) -> bool:
        return self.penetrated > 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> Kill:
        return _from_dict(cls, data)


@dataclass(frozen=True)
class BombEvent:
    tick: int
    round: int
    kind: str  # one of BOMB_EVENT_KINDS
    player: str | None = None
    site: str = ""

    def __post_init__(self) -> None:
        _int("BombEvent", "tick", self.tick, 0)
        _int("BombEvent", "round", self.round, 1)
        if self.kind not in BOMB_EVENT_KINDS:
            raise ModelError(f"BombEvent: kind must be one of {BOMB_EVENT_KINDS}, got {self.kind!r}")
        _opt_str("BombEvent", "player", self.player)
        _str("BombEvent", "site", self.site)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> BombEvent:
        return _from_dict(cls, data)


@dataclass(frozen=True)
class Demo:
    """Everything the tool knows about one parsed demo."""

    path: str
    tick_rate: int
    players: tuple[Player, ...]
    rounds: tuple[Round, ...]
    kills: tuple[Kill, ...] = ()
    bomb_events: tuple[BombEvent, ...] = ()
    map_name: str = ""

    def __post_init__(self) -> None:
        _str("Demo", "path", self.path, allow_empty=False)
        _int("Demo", "tick_rate", self.tick_rate, 1)
        _str("Demo", "map_name", self.map_name)
        for name, item_type in (("players", Player), ("rounds", Round),
                                ("kills", Kill), ("bomb_events", BombEvent)):
            object.__setattr__(self, name, _tuple_of("Demo", name, getattr(self, name), item_type))

        ids = [player.id for player in self.players]
        if len(set(ids)) != len(ids):
            raise ModelError("Demo: two players have the same id")
        known = set(ids)

        previous: Round | None = None
        for rnd in self.rounds:
            if previous is not None:
                if rnd.number <= previous.number:
                    raise ModelError("Demo: rounds must be in order of round number")
                if rnd.start_tick < previous.end_tick:
                    raise ModelError(
                        f"Demo: round {rnd.number} starts before round {previous.number} ends"
                    )
            for life in rnd.lives:
                if life.player not in known:
                    raise ModelError(
                        f"Demo: round {rnd.number} has a life for unknown player {life.player!r}"
                    )
            previous = rnd

        by_number = {rnd.number: rnd for rnd in self.rounds}
        for name, events in (("kill", self.kills), ("bomb event", self.bomb_events)):
            last_tick = -1
            for event in events:
                if event.tick < last_tick:
                    raise ModelError(f"Demo: {name}s must be in order of tick")
                last_tick = event.tick
                rnd = by_number.get(event.round)
                if rnd is None or not rnd.contains(event.tick):
                    raise ModelError(
                        f"Demo: {name} at tick {event.tick} is not inside round {event.round}"
                    )
        for kill in self.kills:
            for who in (kill.victim, kill.attacker, kill.assister):
                if who is not None and who not in known:
                    raise ModelError(f"Demo: kill at tick {kill.tick} names unknown player {who!r}")
        for event in self.bomb_events:
            if event.player is not None and event.player not in known:
                raise ModelError(
                    f"Demo: bomb event at tick {event.tick} names unknown player {event.player!r}"
                )

    # -- lookups -----------------------------------------------------------

    def player(self, player_id: str) -> Player | None:
        for player in self.players:
            if player.id == player_id:
                return player
        return None

    def player_name(self, player_id: str) -> str:
        player = self.player(player_id)
        return player.name if player is not None and player.name else player_id

    def round_number(self, number: int) -> Round | None:
        for rnd in self.rounds:
            if rnd.number == number:
                return rnd
        return None

    def round_at(self, tick: int) -> Round | None:
        for rnd in self.rounds:
            if rnd.contains(tick):
                return rnd
        return None

    def is_alive(self, player_id: str, tick: int) -> bool:
        return self.allowed_span(player_id, tick) is not None

    def allowed_span(self, player_id: str, tick: int) -> tuple[int, int] | None:
        """The widest ``(start_tick, end_tick)`` a shot of this player can
        have around ``tick``, or ``None`` if they are not alive then."""
        rnd = self.round_at(tick)
        life = rnd.life_of(player_id) if rnd is not None else None
        if life is None or not life.from_tick <= tick < life.until_tick:
            return None
        return (life.from_tick, life.until_tick)

    def players_alive_during(self, start_tick: int, end_tick: int) -> tuple[str, ...]:
        """Ids of the players a shot over this span could follow."""
        rnd = self.round_at(start_tick)
        if rnd is None:
            return ()
        return tuple(life.player for life in rnd.lives if life.covers(start_tick, end_tick))

    def seconds(self, ticks: int) -> float:
        return ticks / self.tick_rate

    # -- JSON --------------------------------------------------------------

    def to_dict(self) -> dict:
        return {"version": DEMO_VERSION, **asdict(self)}

    @classmethod
    def from_dict(cls, data: Any) -> Demo:
        data = _check_version("Demo", data, DEMO_VERSION)
        return _from_dict(cls, data, {
            "players": Player.from_dict,
            "rounds": Round.from_dict,
            "kills": Kill.from_dict,
            "bomb_events": BombEvent.from_dict,
        })

    def save(self, path: str | os.PathLike) -> None:
        _write_json(path, self.to_dict())

    @classmethod
    def load(cls, path: str | os.PathLike) -> Demo:
        return cls.from_dict(_read_json(path))


# --------------------------------------------------------------------------
# What the tool produces
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Moment:
    """A detected highlight: something worth showing, not yet a camera plan.

    ``start_tick``/``end_tick`` span the action itself (first to last kill,
    for example), without any lead-in; they are equal for a single event.
    ``details`` holds detector-specific numbers such as a flick angle.
    """

    kind: str
    round: int
    player: str
    start_tick: int
    end_tick: int
    kills: tuple[Kill, ...] = ()
    tags: tuple[str, ...] = ()
    score: float = 0.0
    details: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        _str("Moment", "kind", self.kind, allow_empty=False)
        _int("Moment", "round", self.round, 1)
        _str("Moment", "player", self.player, allow_empty=False)
        _int("Moment", "start_tick", self.start_tick, 0)
        _int("Moment", "end_tick", self.end_tick, 0)
        if self.start_tick > self.end_tick:
            raise ModelError(
                f"Moment: start_tick ({self.start_tick}) is after end_tick ({self.end_tick})"
            )
        object.__setattr__(self, "kills", _tuple_of("Moment", "kills", self.kills, Kill))
        object.__setattr__(self, "tags", _tuple_of("Moment", "tags", self.tags, str))
        object.__setattr__(self, "score", _number("Moment", "score", self.score))
        details = {} if self.details is None else self.details
        if not isinstance(details, dict):
            raise ModelError(f"Moment: details must be an object, got {details!r}")
        for key, value in details.items():
            _str("Moment", "details key", key, allow_empty=False)
            if isinstance(value, float) and not math.isfinite(value):
                raise ModelError(f"Moment: details[{key!r}] must be a finite number")
            if value is not None and not isinstance(value, (bool, int, float, str)):
                raise ModelError(
                    f"Moment: details[{key!r}] must be a number, text, true/false or null"
                )
        object.__setattr__(self, "details", dict(details))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> Moment:
        return _from_dict(cls, data, {"kills": Kill.from_dict, "tags": lambda tag: tag})


@dataclass(frozen=True)
class Shot:
    """One piece of the final video: a span of a demo seen through one player.

    Only what can be checked without the demo is checked here. Whether the
    shot fits its round and its player is alive is ``shot_problems``' job.
    """

    demo: str
    start_tick: int
    end_tick: int
    pov_player: str
    speed: float = 1.0
    label: str = ""

    def __post_init__(self) -> None:
        _str("Shot", "demo", self.demo, allow_empty=False)
        _int("Shot", "start_tick", self.start_tick, 0)
        _int("Shot", "end_tick", self.end_tick, 0)
        for name in ("start_tick", "end_tick"):
            if getattr(self, name) > MAX_TICK:
                raise ModelError(f"Shot: {name} must be {MAX_TICK} or less")
        _str("Shot", "pov_player", self.pov_player, allow_empty=False)
        _str("Shot", "label", self.label)
        speed = _number("Shot", "speed", self.speed)
        if speed <= 0:
            raise ModelError(f"Shot: speed must be more than 0, got {speed}")
        if not MIN_SPEED <= speed <= MAX_SPEED:
            raise ModelError(f"Shot: speed must be between {MIN_SPEED:g} and {MAX_SPEED:g}, got {speed:g}")
        object.__setattr__(self, "speed", speed)
        if self.start_tick >= self.end_tick:
            raise ModelError(
                f"Shot: a shot must start before it ends "
                f"(start tick {self.start_tick}, end tick {self.end_tick})"
            )

    @property
    def ticks(self) -> int:
        return self.end_tick - self.start_tick

    def duration_seconds(self, tick_rate: int = DEFAULT_TICK_RATE) -> float:
        """Length in the finished video, slow motion included."""
        return self.ticks / tick_rate / self.speed

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> Shot:
        return _from_dict(cls, data)


@dataclass(frozen=True)
class ShotList:
    """The shots of a video, in the order they are shown.

    Shots may overlap in demo time: the same seconds can be shown twice from
    two players' views.
    """

    shots: tuple[Shot, ...] = ()
    tick_rate: int = DEFAULT_TICK_RATE

    def __post_init__(self) -> None:
        object.__setattr__(self, "shots", _tuple_of("ShotList", "shots", self.shots, Shot))
        _int("ShotList", "tick_rate", self.tick_rate, 1)

    def __len__(self) -> int:
        return len(self.shots)

    def duration_seconds(self) -> float:
        return sum(shot.duration_seconds(self.tick_rate) for shot in self.shots)

    def to_dict(self) -> dict:
        return {
            "version": SHOTLIST_VERSION,
            "tick_rate": self.tick_rate,
            "shots": [shot.to_dict() for shot in self.shots],
        }

    @classmethod
    def from_dict(cls, data: Any) -> ShotList:
        data = _check_version("ShotList", data, SHOTLIST_VERSION)
        return _from_dict(cls, data, {"shots": Shot.from_dict})

    def save(self, path: str | os.PathLike) -> None:
        _write_json(path, self.to_dict())

    @classmethod
    def load(cls, path: str | os.PathLike) -> ShotList:
        return cls.from_dict(_read_json(path))


# --------------------------------------------------------------------------
# Checking shots against a demo
# --------------------------------------------------------------------------


def shot_problems(shot: Shot, demo: Demo) -> list[str]:
    """Reasons this shot cannot be captured from this demo; empty if it can.

    A valid shot lies inside one round and its POV player is alive for all of
    it. The messages are written to be shown to the user.
    """
    if demo_file_name(shot.demo) != demo_file_name(demo.path):
        return [f"The shot is for demo '{shot.demo}', not '{demo.path}'."]

    problems: list[str] = []
    known_player = demo.player(shot.pov_player) is not None
    if not known_player:
        problems.append(f"Player '{shot.pov_player}' is not in this demo.")

    rnd = demo.round_at(shot.start_tick)
    if rnd is None:
        problems.append(f"The shot starts at tick {shot.start_tick}, which is not inside any round.")
        return problems
    if shot.end_tick > rnd.end_tick:
        problems.append(
            f"The shot ends at tick {shot.end_tick}, "
            f"after round {rnd.number} is over (tick {rnd.end_tick})."
        )
    if not known_player:
        return problems

    name = demo.player_name(shot.pov_player)
    life = rnd.life_of(shot.pov_player)
    if life is None:
        problems.append(f"{name} did not play in round {rnd.number}.")
        return problems
    if shot.start_tick < life.from_tick:
        problems.append(
            f"{name} is not alive before tick {life.from_tick}; "
            f"the shot starts at tick {shot.start_tick}."
        )
    if shot.end_tick > life.until_tick and life.until_tick < rnd.end_tick:
        problems.append(
            f"{name} is no longer alive from tick {life.until_tick}; "
            f"the shot ends at tick {shot.end_tick}."
        )
    return problems


def shotlist_problems(shotlist: ShotList, demo: Demo) -> list[str]:
    """All problems in a shot list, each prefixed with the shot's position."""
    problems: list[str] = []
    if shotlist.tick_rate != demo.tick_rate:
        problems.append(
            f"The shot list is for {shotlist.tick_rate} ticks per second, "
            f"the demo has {demo.tick_rate}."
        )
    for position, shot in enumerate(shotlist.shots, start=1):
        problems.extend(f"Shot {position}: {text}" for text in shot_problems(shot, demo))
    return problems
