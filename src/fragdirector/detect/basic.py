"""Basic detectors: multi-kills, clutches, weapon kills, style kills, deaths.

Every detector is a plain function over a parsed ``Demo`` and returns a list
of ``Moment`` objects in order of tick. Nothing here reads files or talks to
the game. Scoring is not done here: ``Moment.score`` stays at its default.

Choices made:

* Team kills (attacker and victim on the same side) never count as kills.
  Kills with no attacker are not kills by anyone.
* A multi-kill is a run of kills by one player in one round where each kill
  comes at most ``window_seconds`` after the one before it. An ace (5 kills
  by one player in one round, all enemies) is reported whole as a ``5k``
  tagged ``ace`` however far apart the kills are; it replaces the shorter
  runs that make it up.
* A clutch starts when a player's side is down to that one living player and
  at least one enemy is alive. It counts only if that player is still alive
  when the round ends and their side won. Deaths are read from
  ``Round.lives``.
* ``Round`` does not store the round time limit, so "time left" in a clutch
  is ``round_seconds`` (default 115, the competitive limit) minus the time
  since freeze time ended. It ignores bomb timers.
"""

from __future__ import annotations

from typing import Iterable

from fragdirector.models import Demo, Kill, Moment, Round
from fragdirector.parse import is_team_kill

DEFAULT_WINDOW_SECONDS = 10.0
DEFAULT_ROUND_SECONDS = 115.0

STYLE_FLAGS = ("noscope", "wallbang", "smoke", "blind", "air", "headshot")

_WEAPON_ALIASES = {"zeus": "taser"}


def _enemy_kills(demo: Demo, player: str | None = None) -> list[Kill]:
    """Kills with an attacker on the other side, optionally by one player."""
    return [
        kill for kill in demo.kills
        if kill.attacker is not None
        and (player is None or kill.attacker == player)
        and not is_team_kill(demo, kill)
    ]


# --------------------------------------------------------------------------
# Multi-kills
# --------------------------------------------------------------------------


def detect_multikills(
    demo: Demo,
    window_seconds: float = DEFAULT_WINDOW_SECONDS,
    min_kills: int = 2,
    player: str | None = None,
) -> list[Moment]:
    """2k to 5k by one player in one round (an ace is tagged ``ace``)."""
    window = window_seconds * demo.tick_rate
    runs: dict[tuple[int, str], list[list[Kill]]] = {}
    for kill in _enemy_kills(demo, player):
        groups = runs.setdefault((kill.round, kill.attacker), [])
        if groups and kill.tick - groups[-1][-1].tick <= window:
            groups[-1].append(kill)
        else:
            groups.append([kill])

    moments = []
    for (number, attacker), groups in runs.items():
        total = sum(len(group) for group in groups)
        if total >= 5 and max(2, min_kills) <= 5:
            groups = [[kill for group in groups for kill in group]]
        for group in groups:
            count = len(group)
            if count < max(2, min_kills):
                continue
            tags = [f"{count}k"]
            if count >= 5:
                tags.append("ace")
            moments.append(Moment(
                kind="multikill", round=number, player=attacker,
                start_tick=group[0].tick, end_tick=group[-1].tick,
                kills=tuple(group), tags=tuple(tags), details={"kills": count},
            ))
    return sorted(moments, key=lambda m: (m.start_tick, m.player))


# --------------------------------------------------------------------------
# Clutches
# --------------------------------------------------------------------------


def _clutches_in_round(demo: Demo, rnd: Round, round_seconds: float) -> list[Moment]:
    if rnd.winner is None:
        return []
    deaths = sorted(
        (life.until_tick, life.player, life.side)
        for life in rnd.lives if life.until_tick < rnd.end_tick
    )
    alive = {side: {life.player for life in rnd.lives if life.side == side} for side in ("CT", "T")}
    started: set[str] = set()
    found: list[tuple[int, str, str, int]] = []  # tick, player, side, enemies

    def check(tick: int) -> None:
        for side, other in (("CT", "T"), ("T", "CT")):
            if side not in started and len(alive[side]) == 1 and alive[other]:
                started.add(side)
                found.append((tick, next(iter(alive[side])), side, len(alive[other])))

    check(rnd.start_tick)
    index = 0
    while index < len(deaths):
        tick = deaths[index][0]
        while index < len(deaths) and deaths[index][0] == tick:
            alive[deaths[index][2]].discard(deaths[index][1])
            index += 1
        check(tick)

    moments = []
    for tick, player, side, enemies in found:
        life = rnd.life_of(player)
        if side != rnd.winner or life is None or life.until_tick < rnd.end_tick:
            continue
        elapsed = max(0, tick - rnd.freeze_end_tick) / demo.tick_rate
        end = rnd.decided_tick if rnd.decided_tick is not None and rnd.decided_tick >= tick else tick
        kills = tuple(
            kill for kill in _enemy_kills(demo, player)
            if kill.round == rnd.number and kill.tick >= tick
        )
        moments.append(Moment(
            kind="clutch", round=rnd.number, player=player,
            start_tick=tick, end_tick=end, kills=kills,
            tags=("clutch", f"1v{enemies}"),
            details={
                "versus": enemies,
                "seconds_into_round": round(elapsed, 2),
                "time_left_seconds": round(max(0.0, round_seconds - elapsed), 2),
            },
        ))
    return moments


def detect_clutches(
    demo: Demo,
    round_seconds: float = DEFAULT_ROUND_SECONDS,
    player: str | None = None,
) -> list[Moment]:
    """1vX situations the lone player's side won, with X and time left."""
    moments = []
    for rnd in demo.rounds:
        moments.extend(_clutches_in_round(demo, rnd, round_seconds))
    if player is not None:
        moments = [m for m in moments if m.player == player]
    return sorted(moments, key=lambda m: (m.start_tick, m.player))


# --------------------------------------------------------------------------
# Weapon filter
# --------------------------------------------------------------------------


def _weapon_matches(weapon: str, wanted: str) -> bool:
    weapon = weapon.casefold().removeprefix("weapon_")
    wanted = wanted.casefold().removeprefix("weapon_")
    wanted = _WEAPON_ALIASES.get(wanted, wanted)
    if wanted == "knife":  # knife, knife_t, knife_karambit, bayonet, ...
        return weapon.startswith("knife") or "bayonet" in weapon or "karambit" in weapon
    return weapon == wanted


def detect_weapon_kills(
    demo: Demo,
    weapons: str | Iterable[str],
    player: str | None = None,
) -> list[Moment]:
    """Kills with the given weapon name(s): ``awp``, ``deagle``, ``knife``,
    ``zeus`` (or ``taser``) or any weapon name the demo uses."""
    wanted = [weapons] if isinstance(weapons, str) else list(weapons)
    moments = []
    for kill in _enemy_kills(demo, player):
        if any(_weapon_matches(kill.weapon, name) for name in wanted):
            moments.append(Moment(
                kind="weapon_kill", round=kill.round, player=kill.attacker,
                start_tick=kill.tick, end_tick=kill.tick, kills=(kill,),
                tags=("weapon", kill.weapon or "unknown"),
                details={"weapon": kill.weapon},
            ))
    return moments


# --------------------------------------------------------------------------
# Style flags
# --------------------------------------------------------------------------


def kill_style_flags(kill: Kill) -> tuple[str, ...]:
    """The style flags (see ``STYLE_FLAGS``) that this kill carries."""
    present = {
        "noscope": kill.noscope,
        "wallbang": kill.wallbang,
        "smoke": kill.through_smoke,
        "blind": kill.attacker_blind,
        "air": kill.attacker_in_air,
        "headshot": kill.headshot,
    }
    return tuple(name for name in STYLE_FLAGS if present[name])


def detect_style_kills(
    demo: Demo,
    flags: Iterable[str] = STYLE_FLAGS,
    player: str | None = None,
) -> list[Moment]:
    """Kills with at least one of the wanted style flags, tagged with all
    the flags they carry."""
    wanted = set(flags)
    unknown = wanted - set(STYLE_FLAGS)
    if unknown:
        raise ValueError(f"Unknown style flag(s): {', '.join(sorted(unknown))}")
    moments = []
    for kill in _enemy_kills(demo, player):
        carried = kill_style_flags(kill)
        if wanted.intersection(carried):
            moments.append(Moment(
                kind="style_kill", round=kill.round, player=kill.attacker,
                start_tick=kill.tick, end_tick=kill.tick, kills=(kill,),
                tags=carried, details={"weapon": kill.weapon},
            ))
    return moments


# --------------------------------------------------------------------------
# Player deaths
# --------------------------------------------------------------------------


def detect_deaths(demo: Demo, player: str) -> list[Moment]:
    """Every death of ``player``, including falls, the bomb and team kills."""
    moments = []
    for kill in demo.kills:
        if kill.victim != player:
            continue
        tags = ["death"]
        if kill.attacker is None:
            tags.append("no_attacker")
        elif is_team_kill(demo, kill):
            tags.append("team_kill")
        moments.append(Moment(
            kind="death", round=kill.round, player=player,
            start_tick=kill.tick, end_tick=kill.tick, kills=(kill,),
            tags=tuple(tags), details={"weapon": kill.weapon},
        ))
    return moments


def detect_basic(demo: Demo) -> list[Moment]:
    """The detectors that need no choice of player or weapon: multi-kills and
    clutches, in order of tick."""
    moments = detect_multikills(demo) + detect_clutches(demo)
    return sorted(moments, key=lambda m: (m.start_tick, m.kind, m.player))
