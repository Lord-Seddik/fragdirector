"""Tests for scoring and ranking, using hand-built moments."""

import pytest

from fragdirector.detect.basic import STYLE_FLAGS
from fragdirector.models import Kill, Moment
from fragdirector.score import (
    ScoreError,
    load_weights,
    rank_moments,
    score_moment,
)


def moment(kind="multikill", tags=(), kills=(), round=1, start=1000, player="p1"):
    return Moment(kind=kind, round=round, player=player, start_tick=start, end_tick=start,
                  kills=tuple(kills), tags=tuple(tags))


def kill(**extra):
    return Kill(tick=1000, round=1, attacker="p1", victim="p2", weapon="ak47", **extra)


def write(tmp_path, text):
    path = tmp_path / "weights.toml"
    path.write_text(text, encoding="utf-8")
    return path


# -- default weights -----------------------------------------------------


def test_default_weights_cover_every_kind_and_tag_the_detectors_emit():
    weights = load_weights()
    kinds = ["multikill", "clutch", "weapon_kill", "style_kill", "death",
             "flick", "quick_kills", "opening_kill", "trade_kill", "late_kill", "late_defuse"]
    tags = ["2k", "3k", "4k", "5k", "ace", "clutch", "1v1", "1v2", "1v3", "1v4", "1v5",
            "weapon", "death", "no_attacker", "team_kill", "flick", "quick_kills",
            "opening", "trade", "late_round", "defuse", "last_second", *STYLE_FLAGS]
    assert [k for k in kinds if k not in weights["kind"]] == []
    assert [t for t in tags if t not in weights["tag"]] == []
    assert [f for f in STYLE_FLAGS if f not in weights["style"]] == []


def test_a_bigger_multikill_scores_higher():
    weights = load_weights()
    scores = [score_moment(moment(tags=(f"{n}k",)), weights).score for n in (2, 3, 4, 5)]
    assert scores == sorted(scores) and len(set(scores)) == 4


# -- formula -------------------------------------------------------------


def test_score_is_kind_plus_tags_plus_kill_bonuses():
    weights = load_weights()
    weights["kind"]["multikill"] = 10
    weights["tag"].update({"3k": 30})
    weights["style"].update({"noscope": 15, "headshot": 2, "flash_assist": 2})
    weights["weapon"].update({"awp": 3})
    kills = [
        Kill(tick=1000, round=1, attacker="p1", victim="p2", weapon="awp",
             noscope=True, headshot=True),
        Kill(tick=1100, round=1, attacker="p1", victim="p3", weapon="ak47", flash_assist=True),
    ]
    result = score_moment(moment(tags=("3k",), kills=kills), weights)
    assert result.score == 10 + 30 + (15 + 2 + 3) + 2


def test_knife_variants_and_zeus_use_their_shared_weapon_weights():
    weights = load_weights()
    weights["kind"]["style_kill"] = 0
    weights["weapon"].update({"knife": 20, "taser": 30})
    for name, expected in (("knife_t", 20), ("weapon_knife", 20), ("bayonet", 20),
                           ("knife_karambit", 20), ("taser", 30), ("zeus", 30)):
        k = Kill(tick=1, round=1, attacker="p1", victim="p2", weapon=name)
        assert score_moment(moment("style_kill", kills=[k]), weights).score == expected, name


def test_unknown_kind_tag_and_weapon_score_zero():
    weights = load_weights()
    k = Kill(tick=1, round=1, attacker="p1", victim="p2", weapon="mystery_gun")
    assert score_moment(moment("brand_new", tags=("nope",), kills=[k]), weights).score == 0


def test_scoring_returns_a_new_moment_and_keeps_the_rest():
    original = moment(tags=("2k",))
    result = score_moment(original, load_weights())
    assert original.score == 0
    assert result is not original
    assert (result.kind, result.tags, result.start_tick) == (original.kind, original.tags, original.start_tick)


# -- ranking -------------------------------------------------------------


def test_ranking_is_highest_first():
    ranked = rank_moments([moment(tags=("2k",)), moment(tags=("5k", "ace")), moment(tags=("3k",))])
    assert [m.tags[0] for m in ranked] == ["5k", "3k", "2k"]
    assert ranked[0].score > ranked[1].score > ranked[2].score


def test_ties_are_broken_by_round_tick_and_player_whatever_the_input_order():
    a = moment("death", round=1, start=500, player="a")
    b = moment("death", round=1, start=500, player="b")
    c = moment("death", round=2, start=100, player="a")
    d = moment("death", round=1, start=900, player="a")
    expected = [a, b, d, c]
    for order in ([c, d, b, a], [a, b, c, d], [d, a, c, b]):
        assert [(m.round, m.start_tick, m.player) for m in rank_moments(order)] == \
               [(m.round, m.start_tick, m.player) for m in expected]


def test_changing_a_weight_changes_the_ranking(tmp_path):
    clutch = moment("clutch", tags=("clutch", "1v3"), start=1000)
    ace = moment(tags=("5k", "ace"), start=2000)
    default = rank_moments([clutch, ace], load_weights())
    assert [m.kind for m in default] == ["multikill", "clutch"]

    path = write(tmp_path, "[tag]\n1v3 = 500\n")
    tuned = rank_moments([clutch, ace], load_weights(path))
    assert [m.kind for m in tuned] == ["clutch", "multikill"]


# -- weights files -------------------------------------------------------


def test_user_file_overrides_only_what_it_lists(tmp_path):
    defaults = load_weights()
    user = load_weights(write(tmp_path, "[kind]\nflick = 99\n\n[weapon]\nm4a1 = 4.5\n"))
    assert user["kind"]["flick"] == 99
    assert user["weapon"]["m4a1"] == 4.5
    assert user["kind"]["clutch"] == defaults["kind"]["clutch"]
    assert user["tag"] == defaults["tag"]


def test_missing_file_is_a_clear_error(tmp_path):
    with pytest.raises(ScoreError, match="not found"):
        load_weights(tmp_path / "nope.toml")


def test_invalid_toml_is_a_clear_error(tmp_path):
    with pytest.raises(ScoreError, match="not a valid TOML"):
        load_weights(write(tmp_path, "[kind\nflick = \n"))


@pytest.mark.parametrize("text", [
    '[kind]\nflick = "high"\n',
    "[kind]\nflick = true\n",
    "[kind]\nflick = nan\n",
    "[kind]\nflick = [1, 2]\n",
])
def test_non_numeric_weight_is_a_clear_error(tmp_path, text):
    with pytest.raises(ScoreError, match="must be a number"):
        load_weights(write(tmp_path, text))


def test_unknown_section_is_a_clear_error(tmp_path):
    with pytest.raises(ScoreError, match="unknown section"):
        load_weights(write(tmp_path, "[kinds]\nflick = 1\n"))


def test_score_error_is_a_value_error():
    assert issubclass(ScoreError, ValueError)
