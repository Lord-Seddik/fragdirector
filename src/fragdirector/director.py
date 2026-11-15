"""The director: turn chosen moments into the first draft of a shot list.

Plain functions over the model: moments + demo + preset (+ a budget in
seconds) give a ``Plan``, which is a ``ShotList`` plus notes for the user.
Nothing here reads the demo file or talks to the game; the only files read
are presets. The same input always gives the same output.

Presets
    A preset is a TOML file of numbers (``presets/*.toml`` next to this
    module; ``killer.toml`` explains every setting). ``[shots]`` says which
    shots a moment gets, in this order:

    * killer  - the killer's view in real time. Kills further apart than
      ``split_gap_seconds`` become separate shots, so a long quiet gap is
      not shown. Pre-roll and post-roll grow with the average gap between
      the kills of the shot: a fast spray gets a short lead-in, a slow
      multi-kill a longer one.
    * victims - each victim's view of the seconds before their death, then
      ``after_death_seconds`` more (default 1.5) on the camera the game puts
      by the body. The extra time is cut at the limit the validity rule
      allows (5 s) and at the end of the round; 0 ends the shot on the
      death tick. The minimum shot length is measured on the part before
      the death.
    * replay  - the moment's last kill again from the killer's view, slowed
      down.

    Every shot is clamped to the POV player's life (and so to the round). A
    shot that this leaves shorter than ``min_shot_seconds`` is lengthened on
    the side that has room, and left out, with a note, if it cannot reach
    the minimum.

Length
    The length of a shot is always its length in the finished video:
    ``ticks / tick_rate / speed``. Two seconds of demo at half speed count
    as four. The minimum shot length and the budget both use this measure.

Overlap rule
    Two shots that show the same player's view at the same speed never share
    a tick. Where the draft would make two such shots that overlap or touch
    (two chosen moments that share kills, for example) they are merged into
    one shot, which keeps the place of the earlier one. Showing the same
    ticks again through another player, or through the same player at
    another speed (the slow-motion replay), is not an overlap problem: it is
    how a play is shown twice. ``wrong_overlaps`` checks this rule.

Budget
    ``fit_budget`` is a step applied to shots a preset made, not another way
    of making shots; it works with every preset (the ``budget`` preset is
    ``killer`` with a 30 second budget). Every shot has a shortest length
    (its kills with ``keep_before_seconds`` / ``keep_after_seconds`` around
    them, at least ``min_shot_seconds``) and a longest one (``max_stretch``
    times its drafted length, limited by the player's life and by the next
    shot of the same view).

    1. While the shots at their shortest do not fit, the lowest-score shot
       is left out (of equal scores the earlier one, so the end of a play
       outlasts its build-up). The last shot is never left out.
    2. The time that is left is split between the remaining shots in
       proportion to their score; what a shot cannot take because it is at
       its longest goes to the others.
    3. Each shot is shortened or lengthened around its kills, keeping the
       drafted proportion of pre-roll to post-roll.

    So a budget both shortens a long draft and lengthens a short one. The
    result is within ``BUDGET_TOLERANCE`` (5%) of the budget except in two
    cases, which are reported in ``Plan.notes`` with ``Plan.budget_met``
    false: the budget is smaller than the last remaining shot at its
    shortest (that shot is returned as short as it goes), or larger than all
    remaining shots at their longest (they are returned as long as they go).

    The score of a shot is its moment's score, shared out by how many of the
    moment's kills the shot shows, times ``victim_weight`` or
    ``replay_weight`` for those shots. If no moment has a score, all moments
    count the same.
"""

from __future__ import annotations

import math
import os
import tomllib
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Iterable, Sequence

from fragdirector import edits
from fragdirector.models import AFTER_DEATH_SECONDS, Demo, Moment, Shot, ShotList, seconds_to_ticks

PRESETS_DIR = Path(__file__).with_name("presets")
DEFAULT_PRESET = "killer"
BUDGET_TOLERANCE = 0.05


class DirectorError(ValueError):
    """The director was asked for something it cannot do. The message says what."""


class PresetError(DirectorError):
    """A preset is missing, is not valid TOML, or holds a bad setting."""


# --------------------------------------------------------------------------
# Presets
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Preset:
    """The settings of one preset. A field ``killer_pre_roll_seconds`` is the
    setting ``pre_roll_seconds`` in the section ``[killer]`` of the file."""

    name: str
    description: str
    shots_killer: bool
    shots_victims: bool
    shots_replay: bool
    killer_split_gap_seconds: float
    killer_pre_roll_seconds: float
    killer_pre_roll_per_gap_second: float
    killer_pre_roll_max_seconds: float
    killer_post_roll_seconds: float
    killer_post_roll_per_gap_second: float
    killer_post_roll_max_seconds: float
    victims_seconds: float
    victims_after_death_seconds: float
    replay_speed: float
    replay_before_seconds: float
    replay_after_seconds: float
    limits_min_shot_seconds: float
    budget_seconds: float
    budget_max_stretch: float
    budget_keep_before_seconds: float
    budget_keep_after_seconds: float
    budget_victim_weight: float
    budget_replay_weight: float


# Setting -> "bool" or "float", read off the dataclass.
_SETTINGS = {f.name: f.type for f in fields(Preset) if f.name not in ("name", "description")}
_SECTIONS = tuple(dict.fromkeys(name.split("_", 1)[0] for name in _SETTINGS))
_MORE_THAN_ZERO = ("victims_seconds", "replay_speed", "limits_min_shot_seconds")


def _where(setting: str) -> str:
    section, name = setting.split("_", 1)
    return f"{name!r} in [{section}]"


def preset_names() -> list[str]:
    """Names of the built-in presets."""
    return sorted(path.stem for path in PRESETS_DIR.glob("*.toml"))


def _read(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PresetError(f"Preset file not found: {path}") from None
    except (OSError, UnicodeDecodeError) as error:
        raise PresetError(f"Cannot read preset file {path}: {error}") from error
    except tomllib.TOMLDecodeError as error:
        raise PresetError(f"{path} is not a valid TOML file: {error}") from error


def _settings(path: Path, data: dict) -> dict[str, bool | float]:
    """The settings a preset file lists, with their types checked."""
    found: dict[str, bool | float] = {}
    for section, table in data.items():
        if section in ("base", "description"):
            if not isinstance(table, str):
                raise PresetError(f"{path}: {section!r} must be text, got {table!r}")
            continue
        if section not in _SECTIONS:
            raise PresetError(
                f"{path}: unknown section [{section}] (use {', '.join(f'[{s}]' for s in _SECTIONS)})"
            )
        if not isinstance(table, dict):
            raise PresetError(f"{path}: {section!r} must be a section such as [{section}]")
        for name, value in table.items():
            setting = f"{section}_{name}"
            kind = _SETTINGS.get(setting)
            if kind is None:
                raise PresetError(f"{path}: unknown setting {name!r} in [{section}]")
            if kind == "bool":
                if not isinstance(value, bool):
                    raise PresetError(f"{path}: {_where(setting)} must be true or false, got {value!r}")
                found[setting] = value
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise PresetError(f"{path}: {_where(setting)} must be a number, got {value!r}")
            if value < 0 or (value == 0 and setting in _MORE_THAN_ZERO):
                least = "more than 0" if setting in _MORE_THAN_ZERO else "0 or more"
                raise PresetError(f"{path}: {_where(setting)} must be {least}, got {value!r}")
            found[setting] = float(value)
    return found


def load_preset(name_or_path: str | os.PathLike = DEFAULT_PRESET) -> Preset:
    """A built-in preset by name, or the preset in a TOML file.

    A user file lists only what it changes. The rest comes from the built-in
    preset it names with ``base = "..."`` (``killer`` if it names none).
    """
    text = os.fspath(name_or_path)
    names = preset_names()
    builtin = text in names
    path = PRESETS_DIR / f"{text}.toml" if builtin else Path(text)
    if not builtin and not path.is_file():
        raise PresetError(
            f"There is no preset named {text!r} (the presets are {', '.join(names)}) "
            f"and no preset file at that path."
        )
    data = _read(path)
    values = _settings(path, data)
    description = data.get("description", "")
    if builtin:
        if "base" in data:
            raise PresetError(f"{path}: a built-in preset cannot have a base")
    else:
        base_name = data.get("base", DEFAULT_PRESET)
        if base_name not in names:
            raise PresetError(
                f"{path}: base must be one of {', '.join(names)}, got {base_name!r}"
            )
        base = load_preset(base_name)
        values = {**{name: getattr(base, name) for name in _SETTINGS}, **values}
        description = description or base.description
    missing = [name for name in _SETTINGS if name not in values]
    if missing:
        raise PresetError(f"{path}: missing setting(s): {', '.join(_where(m) for m in missing)}")

    if not (values["shots_killer"] or values["shots_victims"] or values["shots_replay"]):
        raise PresetError(f"{path}: [shots] must switch on at least one kind of shot")
    if values["victims_after_death_seconds"] > AFTER_DEATH_SECONDS:
        raise PresetError(
            f"{path}: {_where('victims_after_death_seconds')} must be at most "
            f"{AFTER_DEATH_SECONDS:g} (the longest a shot may follow a player after their "
            f"death), got {values['victims_after_death_seconds']!r}"
        )
    if values["budget_max_stretch"] < 1:
        raise PresetError(
            f"{path}: {_where('budget_max_stretch')} must be 1 or more, "
            f"got {values['budget_max_stretch']!r}"
        )
    if values["shots_replay"] and values["replay_before_seconds"] + values["replay_after_seconds"] <= 0:
        raise PresetError(f"{path}: the replay needs before_seconds or after_seconds above 0")
    return Preset(name=text if builtin else path.stem, description=description, **values)


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _life_around(demo: Demo, player: str, tick: int) -> tuple[int, int] | None:
    """``(from_tick, until_tick)`` of the life ``player`` is in at ``tick``.

    Unlike ``Demo.alive_span`` the tick of the death itself counts: a shot
    about a death needs the life that ends there.
    """
    rnd = demo.round_at(tick)
    life = rnd.life_of(player) if rnd is not None else None
    if life is None or not life.from_tick <= tick <= life.until_tick:
        return None
    if life.from_tick == life.until_tick:
        return None
    return (life.from_tick, life.until_tick)


def _min_ticks(tick_rate: int, preset: Preset, speed: float) -> int:
    """Demo ticks a shot at ``speed`` needs to last the minimum on screen."""
    return max(1, math.ceil(preset.limits_min_shot_seconds * tick_rate * speed - 1e-9))


def _fit(start: int, end: int, room: tuple[int, int], min_ticks: int) -> tuple[int, int] | None:
    """Clamp a span to ``room``. If that leaves it shorter than ``min_ticks``
    lengthen it, first at the start, then at the end; ``None`` if there is
    not enough room."""
    low, high = room
    start, end = max(start, low), min(end, high)
    if end - start < min_ticks:
        start = max(low, end - min_ticks)
        end = min(high, start + min_ticks)
    if end - start < min_ticks or start >= end:
        return None
    return (start, end)


def _same_view(a: Shot, b: Shot) -> bool:
    return a.demo == b.demo and a.pov_player == b.pov_player and a.speed == b.speed


def _join_labels(first: str, second: str) -> str:
    if not second or second == first or second in first.split(" + "):
        return first
    return f"{first} + {second}" if first else second


def moment_title(moment: Moment, demo: Demo) -> str:
    """A few words for a moment: ``sedx 5k``, ``donk 1v3 clutch``."""
    name = demo.player_name(moment.player)
    if moment.kind == "multikill":
        return f"{name} {len(moment.kills)}k"
    if moment.kind == "clutch":
        versus = moment.details.get("versus")
        return f"{name} 1v{versus} clutch" if versus else f"{name} clutch"
    if moment.kind == "death":
        return f"{name}'s death"
    return f"{name} {moment.kind.replace('_', ' ')}"


# --------------------------------------------------------------------------
# One moment -> shots
# --------------------------------------------------------------------------

_Draft = tuple[Shot, float]  # a shot and its score


def _events(moment: Moment, demo: Demo, notes: list[str]) -> list[tuple[int, str]]:
    """``(tick, player)`` pairs the killer's view has to show: the kills, or
    the start and end of a moment that has no kill with an attacker."""
    events = []
    for kill in moment.kills:
        if kill.attacker is None:
            continue
        if _life_around(demo, kill.attacker, kill.tick) is None:
            notes.append(
                f"{demo.player_name(kill.attacker)} was not alive when their kill at tick "
                f"{kill.tick} landed; the killer's view leaves that kill out."
            )
            continue
        events.append((kill.tick, kill.attacker))
    if not any(kill.attacker is not None for kill in moment.kills):
        for tick in dict.fromkeys((moment.start_tick, moment.end_tick)):
            if _life_around(demo, moment.player, tick) is not None:
                events.append((tick, moment.player))
    return events


def _killer_drafts(
    moment: Moment, demo: Demo, preset: Preset, score: float, notes: list[str]
) -> list[_Draft]:
    events = _events(moment, demo, notes)
    split = seconds_to_ticks(preset.killer_split_gap_seconds, demo.tick_rate)
    min_ticks = _min_ticks(demo.tick_rate, preset, 1.0)
    title = moment_title(moment, demo)

    spans: list[list] = []  # [player, start, end, number of events]
    for pov in dict.fromkeys(player for _, player in events):
        ticks = sorted(tick for tick, player in events if player == pov)
        groups = [[ticks[0]]]
        for tick in ticks[1:]:
            if tick - groups[-1][-1] > split:
                groups.append([tick])
            else:
                groups[-1].append(tick)
        for group in groups:
            gap = 0.0
            if len(group) > 1:
                gap = (group[-1] - group[0]) / (len(group) - 1) / demo.tick_rate
            pre = min(
                preset.killer_pre_roll_seconds + preset.killer_pre_roll_per_gap_second * gap,
                preset.killer_pre_roll_max_seconds,
            )
            post = min(
                preset.killer_post_roll_seconds + preset.killer_post_roll_per_gap_second * gap,
                preset.killer_post_roll_max_seconds,
            )
            fitted = _fit(
                group[0] - seconds_to_ticks(pre, demo.tick_rate),
                group[-1] + seconds_to_ticks(post, demo.tick_rate),
                _life_around(demo, pov, group[0]),
                min_ticks,
            )
            if fitted is None:
                notes.append(
                    f"{title}: {demo.player_name(pov)}'s view around tick {group[0]} would be "
                    f"shorter than the minimum shot length; left out."
                )
            elif spans and spans[-1][0] == pov and fitted[0] <= spans[-1][2]:
                spans[-1][2] = max(spans[-1][2], fitted[1])
                spans[-1][3] += len(group)
            else:
                spans.append([pov, fitted[0], fitted[1], len(group)])

    drafts = []
    for number, (pov, start, end, count) in enumerate(spans, start=1):
        label = title if pov == moment.player else f"{title}, {demo.player_name(pov)}'s view"
        if len(spans) > 1:
            label += f" ({number}/{len(spans)})"
        shot = Shot(demo=demo.path, start_tick=start, end_tick=end, pov_player=pov, label=label)
        drafts.append((shot, score * count / len(events)))
    return drafts


def _victim_drafts(
    moment: Moment, demo: Demo, preset: Preset, score: float, notes: list[str]
) -> list[_Draft]:
    before = seconds_to_ticks(preset.victims_seconds, demo.tick_rate)
    after = seconds_to_ticks(preset.victims_after_death_seconds, demo.tick_rate)
    min_ticks = _min_ticks(demo.tick_rate, preset, 1.0)
    drafts = []
    for kill in moment.kills:
        name = demo.player_name(kill.victim)
        life = _life_around(demo, kill.victim, kill.tick)
        fitted = None
        if life is not None:
            # The minimum length is measured on the time before the death, so a
            # player who lived only a moment is left out, not shown as a death cam.
            fitted = _fit(kill.tick - before, kill.tick, (life[0], kill.tick), min_ticks)
        if fitted is not None and after > 0:
            # Then run on past the death, as far as the validity rule allows.
            rnd = demo.round_at(kill.tick)
            latest = demo.latest_shot_end(rnd, rnd.life_of(kill.victim))
            fitted = (fitted[0], min(kill.tick + after, latest))
        if fitted is None:
            notes.append(
                f"{name} was not alive long enough before their death at tick {kill.tick} "
                f"for a shot of the minimum length; their view is left out."
            )
            continue
        shot = Shot(demo=demo.path, start_tick=fitted[0], end_tick=fitted[1],
                    pov_player=kill.victim, label=f"{name}'s view")
        drafts.append((shot, score * preset.budget_victim_weight / len(moment.kills)))
    return drafts


def _replay_drafts(
    moment: Moment, demo: Demo, preset: Preset, score: float, notes: list[str]
) -> list[_Draft]:
    speed = preset.replay_speed
    for kill in reversed(moment.kills):
        if kill.attacker is None:
            continue
        life = _life_around(demo, kill.attacker, kill.tick)
        if life is None:
            continue
        fitted = _fit(
            kill.tick - seconds_to_ticks(preset.replay_before_seconds, demo.tick_rate),
            kill.tick + seconds_to_ticks(preset.replay_after_seconds, demo.tick_rate),
            life,
            _min_ticks(demo.tick_rate, preset, speed),
        )
        if fitted is None:
            notes.append(
                f"{moment_title(moment, demo)}: the replay of the kill at tick {kill.tick} "
                f"would be shorter than the minimum shot length; left out."
            )
            return []
        shot = Shot(demo=demo.path, start_tick=fitted[0], end_tick=fitted[1],
                    pov_player=kill.attacker, speed=speed, label=f"replay {speed:g}x")
        return [(shot, score * preset.budget_replay_weight / len(moment.kills))]
    return []


def _moment_drafts(
    moment: Moment, demo: Demo, preset: Preset, score: float, notes: list[str]
) -> list[_Draft]:
    drafts: list[_Draft] = []
    if preset.shots_killer:
        drafts += _killer_drafts(moment, demo, preset, score, notes)
    if preset.shots_victims:
        drafts += _victim_drafts(moment, demo, preset, score, notes)
    if preset.shots_replay:
        drafts += _replay_drafts(moment, demo, preset, score, notes)
    if not drafts:
        notes.append(
            f"The preset '{preset.name}' has no shot to make for "
            f"{moment_title(moment, demo)} in round {moment.round}."
        )
    return _merge(drafts)


def _merge(drafts: Iterable[_Draft]) -> list[_Draft]:
    """Apply the overlap rule: shots of the same view that overlap or touch
    become one shot, at the place of the earliest of them."""
    merged: list[_Draft] = []
    for shot, score in drafts:
        place = None
        while True:
            index = next(
                (i for i, (other, _) in enumerate(merged)
                 if _same_view(other, shot)
                 and other.start_tick <= shot.end_tick and shot.start_tick <= other.end_tick),
                None,
            )
            if index is None:
                break
            other, other_score = merged.pop(index)
            shot = replace(
                other,
                start_tick=min(other.start_tick, shot.start_tick),
                end_tick=max(other.end_tick, shot.end_tick),
                label=_join_labels(other.label, shot.label),
            )
            score = max(score, other_score)
            place = index if place is None else min(place, index)
        merged.insert(len(merged) if place is None else place, (shot, score))
    return merged


def moment_shots(moment: Moment, demo: Demo, preset: Preset) -> list[Shot]:
    """The shots ``preset`` makes for one moment, in the order they are shown."""
    return [shot for shot, _ in _moment_drafts(moment, demo, preset, 1.0, [])]


def merge_overlaps(shots: Iterable[Shot]) -> list[Shot]:
    """Merge shots that break the overlap rule (see the module docstring)."""
    return [shot for shot, _ in _merge((shot, 0.0) for shot in shots)]


def wrong_overlaps(shotlist: ShotList, demo: Demo | None = None) -> list[str]:
    """Pairs of shots that break the overlap rule; empty if none do."""
    problems = []
    for first, a in enumerate(shotlist.shots):
        for second in range(first + 1, len(shotlist.shots)):
            b = shotlist.shots[second]
            if _same_view(a, b) and a.start_tick < b.end_tick and b.start_tick < a.end_tick:
                name = demo.player_name(a.pov_player) if demo is not None else a.pov_player
                problems.append(
                    f"Shots {first + 1} and {second + 1} both show {name}'s view of ticks "
                    f"{max(a.start_tick, b.start_tick)} to {min(a.end_tick, b.end_tick)} "
                    f"at the same speed."
                )
    return problems


# --------------------------------------------------------------------------
# Whole shot list, and the budget
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Plan:
    """What the director made.

    ``notes`` are sentences for the user: shots that were left out and why,
    a budget that could not be met. ``budget_seconds`` is the budget that was
    applied, if any; ``budget_met`` is false when the result is more than
    ``BUDGET_TOLERANCE`` away from it.
    """

    shotlist: ShotList
    notes: tuple[str, ...] = ()
    budget_seconds: float | None = None
    budget_met: bool = True


def direct(
    moments: Iterable[Moment],
    demo: Demo,
    preset: Preset,
    budget_seconds: float | None = None,
) -> Plan:
    """The first-draft shot list for ``moments``, shown in the order given.

    ``budget_seconds`` fits the result to that length; without it the
    preset's own ``[budget] seconds`` is used, if it is above 0.
    """
    moments = list(moments)
    scored = any(moment.score > 0 for moment in moments)
    notes: list[str] = []
    drafts: list[_Draft] = []
    for moment in moments:
        drafts += _moment_drafts(moment, demo, preset, moment.score if scored else 1.0, notes)
    drafts = _merge(drafts)

    shotlist = ShotList(tick_rate=demo.tick_rate)
    for shot, _ in drafts:
        shotlist = edits.insert(shotlist, demo, shot)  # also checks the shot is valid

    if budget_seconds is None and preset.budget_seconds > 0:
        budget_seconds = preset.budget_seconds
    if budget_seconds is None:
        return Plan(shotlist, tuple(notes))
    fitted = fit_budget(shotlist, demo, budget_seconds, preset, [score for _, score in drafts])
    return replace(fitted, notes=tuple(notes) + fitted.notes)


def _core(shot: Shot, demo: Demo, preset: Preset) -> tuple[int, int]:
    """The part of a shot the budget never cuts: the kills its player makes
    and their own death, with a little room around them. A shot with neither
    has an empty core in its middle."""
    pov, start, end = shot.pov_player, shot.start_tick, shot.end_tick
    ticks = [
        kill.tick for kill in demo.kills
        if (kill.attacker == pov and start <= kill.tick < end)
        or (kill.victim == pov and start < kill.tick <= end)
    ]
    if not ticks:
        middle = (start + end) // 2
        return (middle, middle)
    before = seconds_to_ticks(preset.budget_keep_before_seconds, demo.tick_rate)
    after = seconds_to_ticks(preset.budget_keep_after_seconds, demo.tick_rate)
    return (max(start, min(ticks) - before), min(end, max(ticks) + after))


def _bounds(shots: Sequence[Shot], kept: Sequence[int], index: int, demo: Demo) -> tuple[int, int]:
    """How far shot ``index`` may reach: its player's life, and half of the
    gap to the nearest kept shot of the same view on either side."""
    shot = shots[index]
    life = demo.alive_span(shot.pov_player, shot.start_tick)
    if life is None:
        return (shot.start_tick, shot.end_tick)
    low, high = min(life[0], shot.start_tick), max(life[1], shot.end_tick)
    for other_index in kept:
        other = shots[other_index]
        if other_index == index or not _same_view(other, shot):
            continue
        if other.end_tick <= shot.start_tick:
            low = max(low, (other.end_tick + shot.start_tick + 1) // 2)
        elif other.start_tick >= shot.end_tick:
            high = min(high, (shot.end_tick + other.start_tick) // 2)
    return (low, high)


def _resize(shot: Shot, core: tuple[int, int], bounds: tuple[int, int], ticks: int) -> tuple[int, int]:
    """New start and end for a shot ``ticks`` long: the core stays, the time
    around it keeps its proportion of pre-roll to post-roll where there is room."""
    core_start, core_end = core
    low, high = bounds
    extra = ticks - (core_end - core_start)
    pre, post = core_start - shot.start_tick, shot.end_tick - core_end
    share = pre / (pre + post) if pre + post else 0.5
    new_pre = min(round(extra * share), core_start - low)
    new_pre = max(new_pre, extra - (high - core_end))
    return (core_start - new_pre, core_end + extra - new_pre)


def fit_budget(
    shotlist: ShotList,
    demo: Demo,
    budget_seconds: float,
    preset: Preset,
    scores: Sequence[float] | None = None,
) -> Plan:
    """Shorten, lengthen and leave out shots so the video lasts ``budget_seconds``.

    ``scores`` gives one score per shot; without it all shots count the same.
    The rule is in the module docstring. The shot list must be valid for
    ``demo``.
    """
    if (isinstance(budget_seconds, bool) or not isinstance(budget_seconds, (int, float))
            or not math.isfinite(budget_seconds) or budget_seconds <= 0):
        raise DirectorError(f"The budget must be more than 0 seconds, got {budget_seconds!r}.")
    shots = shotlist.shots
    if scores is None:
        scores = [1.0] * len(shots)
    if len(scores) != len(shots):
        raise DirectorError(f"Got {len(scores)} scores for {len(shots)} shots.")
    if not shots:
        return Plan(shotlist, ("There are no shots to fit to the budget.",), budget_seconds, False)
    rate = shotlist.tick_rate

    def on_screen(index: int, ticks: float) -> float:
        return ticks / rate / shots[index].speed

    cores = [_core(shot, demo, preset) for shot in shots]
    shortest = [
        min(shot.ticks, max(core[1] - core[0], _min_ticks(rate, preset, shot.speed)))
        for shot, core in zip(shots, cores)
    ]

    # 1. Leave out the lowest-score shots while the rest cannot fit.
    kept = list(range(len(shots)))
    dropped: list[int] = []
    while len(kept) > 1 and sum(on_screen(i, shortest[i]) for i in kept) > budget_seconds:
        worst = min(kept, key=lambda i: (scores[i], i))
        kept.remove(worst)
        dropped.append(worst)

    bounds = {i: _bounds(shots, kept, i, demo) for i in kept}
    longest = {
        i: max(shots[i].ticks,
               min(int(shots[i].ticks * preset.budget_max_stretch), bounds[i][1] - bounds[i][0]))
        for i in kept
    }

    # 2. Split what is left by score; a shot at its longest passes its share on.
    seconds = {i: on_screen(i, shortest[i]) for i in kept}
    weights = {i: max(float(scores[i]), 0.0) for i in kept}
    spare = budget_seconds - sum(seconds.values())
    free = list(kept)
    while spare > 1e-9 and free:
        total = sum(weights[i] for i in free)
        share = {i: (weights[i] / total if total > 0 else 1 / len(free)) for i in free}
        full = [i for i in free if seconds[i] + spare * share[i] >= on_screen(i, longest[i])]
        if not full:
            for i in free:
                seconds[i] += spare * share[i]
            break
        for i in full:
            spare -= on_screen(i, longest[i]) - seconds[i]
            seconds[i] = on_screen(i, longest[i])
            free.remove(i)

    # 3. Resize the kept shots around their kills, then take the others out.
    result = shotlist
    for i in kept:
        ticks = max(shortest[i], min(longest[i], round(seconds[i] * rate * shots[i].speed)))
        start, end = _resize(shots[i], cores[i], bounds[i], ticks)
        if (start, end) != (shots[i].start_tick, shots[i].end_tick):
            result = edits.trim(result, demo, i, start, end)
    for i in sorted(dropped, reverse=True):
        result = edits.delete(result, i)

    notes = []
    if dropped:
        names = ", ".join(f"'{shots[i].label}'" for i in sorted(dropped))
        notes.append(f"Left out to fit {budget_seconds:g} s ({len(dropped)} shot(s)): {names}.")
    total_seconds = result.duration_seconds()
    met = abs(total_seconds - budget_seconds) <= BUDGET_TOLERANCE * budget_seconds
    if not met and total_seconds > budget_seconds:
        notes.append(
            f"The budget of {budget_seconds:g} s cannot be met: the one shot that is left "
            f"cannot be shorter than {total_seconds:.1f} s without cutting into its kills "
            f"or going under the minimum shot length."
        )
    elif not met:
        notes.append(
            f"The budget of {budget_seconds:g} s cannot be met: at their longest the shots "
            f"add up to {total_seconds:.1f} s (a shot is stretched to at most "
            f"{preset.budget_max_stretch:g} times its drafted length and stops where the "
            f"player's life or the round ends)."
        )
    return Plan(result, tuple(notes), budget_seconds, met)
