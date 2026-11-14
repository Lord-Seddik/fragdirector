"""Scoring: turn detected moments into scores and rank them.

A moment's score is the sum of three parts, all read from a weights file:
the base value of its kind, a bonus for each of its tags, and a bonus for
every kill in it for each style flag (noscope, wallbang, ...) and weapon.
A kind, tag or weapon the weights file does not mention is worth 0.

The defaults are in ``weights.default.toml`` next to this module. A user file
only lists what it changes; everything else keeps the default value.
"""

from __future__ import annotations

import math
import os
import tomllib
from dataclasses import replace
from pathlib import Path
from typing import Iterable

from fragdirector.detect.basic import kill_style_flags
from fragdirector.models import Kill, Moment

DEFAULT_WEIGHTS_PATH = Path(__file__).with_name("weights.default.toml")
SECTIONS = ("kind", "tag", "style", "weapon")

Weights = dict[str, dict[str, float]]


class ScoreError(ValueError):
    """A weights file is missing, is not valid TOML, or holds a bad weight."""


def _read(path: str | os.PathLike) -> Weights:
    path = Path(path)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ScoreError(f"Weights file not found: {path}") from None
    except (OSError, UnicodeDecodeError) as error:
        raise ScoreError(f"Cannot read weights file {path}: {error}") from error
    except tomllib.TOMLDecodeError as error:
        raise ScoreError(f"{path} is not a valid TOML file: {error}") from error

    weights: Weights = {}
    for section, table in data.items():
        if section not in SECTIONS:
            raise ScoreError(
                f"{path}: unknown section [{section}] (use {', '.join(f'[{s}]' for s in SECTIONS)})"
            )
        if not isinstance(table, dict):
            raise ScoreError(f"{path}: {section!r} must be a section such as [{section}]")
        weights[section] = {}
        for name, value in table.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ScoreError(
                    f"{path}: weight {name!r} in [{section}] must be a number, got {value!r}"
                )
            weights[section][name] = float(value)
    return weights


def load_weights(user_path: str | os.PathLike | None = None) -> Weights:
    """The default weights, overlaid by the weights file at ``user_path``."""
    weights = _read(DEFAULT_WEIGHTS_PATH)
    if user_path is not None:
        for section, table in _read(user_path).items():
            weights[section].update(table)
    return weights


def _weapon_name(weapon: str) -> str:
    name = weapon.casefold().removeprefix("weapon_")
    if name.startswith("knife") or "bayonet" in name or "karambit" in name:
        return "knife"
    return "taser" if name == "zeus" else name


def _kill_bonus(kill: Kill, weights: Weights) -> float:
    bonus = sum(weights["style"].get(flag, 0.0) for flag in kill_style_flags(kill))
    if kill.flash_assist:
        bonus += weights["style"].get("flash_assist", 0.0)
    return bonus + weights["weapon"].get(_weapon_name(kill.weapon), 0.0)


def score_moment(moment: Moment, weights: Weights) -> Moment:
    """A copy of ``moment`` with its score set."""
    score = weights["kind"].get(moment.kind, 0.0)
    score += sum(weights["tag"].get(tag, 0.0) for tag in moment.tags)
    score += sum(_kill_bonus(kill, weights) for kill in moment.kills)
    return replace(moment, score=score)


def rank_moments(moments: Iterable[Moment], weights: Weights | None = None) -> list[Moment]:
    """Score every moment and return them best first. Equal scores are ordered
    by round, start tick, player and kind, so the output is always the same."""
    if weights is None:
        weights = load_weights()
    scored = [score_moment(moment, weights) for moment in moments]
    return sorted(
        scored,
        key=lambda m: (-m.score, m.round, m.start_tick, m.player, m.kind),
    )
