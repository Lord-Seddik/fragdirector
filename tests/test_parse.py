"""Tests for the demo parser.

Most tests feed ``build_demo`` hand-made raw events, so they need no demo
file. One test at the end parses a real demo from ``demos/`` (skipped when
there is none) and checks it against the scoreboard stored inside the demo.
"""

from pathlib import Path

import pytest

from fragdirector.models import Demo
from fragdirector.parse import (
    DemoParseError,
    RawDemo,
    build_demo,
    is_team_kill,
    parse_demo,
    read_raw,
    scoreboard,
)

P1, P2, P3 = "76561198000000001", "76561198000000002", "76561198000000003"
T_TEAM, CT_TEAM = 2, 3


def death(tick, victim, attacker=None, weapon="ak47", **extra):
    row = {"tick": tick, "user_steamid": victim, "user_name": f"n{victim[-1]}", "weapon": weapon}
    if attacker is not None:
        row.update(attacker_steamid=attacker, attacker_name=f"n{attacker[-1]}")
    row.update(extra)
    return row


def roster(*entries):
    return [{"steamid": int(who) if who.isdigit() else who, "name": f"n{who[-1]}", "team_num": team}
            for who, team in entries]


def raw_demo(**changes) -> RawDemo:
    """Two rounds. Round 1: ticks 100-1000, round 2: ticks 1000-2001.

    Round 1: P1 (CT) and P2 (T) and a bot (T). Sides swap in round 2.
    """
    raw = RawDemo(
        path="demos/test.dem", map_name="de_mirage", last_tick=2000,
        round_starts=[(1, 100), (2, 1000)],
        freeze_ends=[200, 1100],
        round_ends=[
            {"tick": 900, "winner": "CT", "reason": "t_killed"},
            {"tick": 1900, "winner": "CT", "reason": "bomb_defused"},
        ],
        rosters={
            200: roster((P1, CT_TEAM), (P2, T_TEAM), ("0", T_TEAM)),
            1100: roster((P1, T_TEAM), (P2, CT_TEAM), ("0", CT_TEAM)),
        },
    )
    for name, value in changes.items():
        setattr(raw, name, value)
    return raw


# -- rounds -------------------------------------------------------------------


def test_rounds_follow_the_events():
    demo = build_demo(raw_demo())
    assert demo.map_name == "de_mirage" and demo.tick_rate == 64
    first, second = demo.rounds
    assert (first.number, first.start_tick, first.freeze_end_tick, first.end_tick) == (1, 100, 200, 1000)
    assert (first.decided_tick, first.winner, first.reason) == (900, "CT", "t_killed")
    # the last round runs to the end of the demo
    assert (second.start_tick, second.end_tick) == (1000, 2001)
    assert second.reason == "bomb_defused"


def test_a_round_the_demo_ended_in_has_no_winner():
    raw = raw_demo(round_ends=[{"tick": 900, "winner": "CT", "reason": "t_killed"}])
    second = build_demo(raw).rounds[1]
    assert second.decided_tick is None and second.winner is None


def test_a_draw_has_no_winner():
    raw = raw_demo(round_ends=[{"tick": 900, "winner": "None", "reason": "draw"}])
    assert build_demo(raw).rounds[0].winner is None


def test_round_numbers_starting_over_keep_only_the_last_run():
    raw = raw_demo(round_starts=[(1, 10), (2, 50), (1, 100), (2, 1000)])
    demo = build_demo(raw)
    assert [(r.number, r.start_tick) for r in demo.rounds] == [(1, 100), (2, 1000)]


def test_a_demo_without_rounds_is_refused():
    with pytest.raises(DemoParseError, match="No rounds"):
        build_demo(raw_demo(round_starts=[]))


# -- who is alive -------------------------------------------------------------


def test_lives_have_sides_and_end_at_death():
    raw = raw_demo(deaths=[death(500, P2, P1), death(1500, P1, P2)])
    demo = build_demo(raw)
    first, second = demo.rounds
    assert (first.life_of(P1).side, first.life_of(P1).until_tick) == ("CT", 1000)
    assert (first.life_of(P2).side, first.life_of(P2).until_tick) == ("T", 500)
    assert first.life_of(P1).from_tick == 100
    # sides swap after half-time with no special handling
    assert (second.life_of(P1).side, second.life_of(P1).until_tick) == ("T", 1500)
    assert second.life_of(P2).side == "CT"
    assert demo.is_alive(P2, 499) and not demo.is_alive(P2, 500)


def test_players_not_on_a_team_have_no_life():
    raw = raw_demo()
    raw.rosters[200] = roster((P1, CT_TEAM), (P3, 1))  # P3 is a spectator
    assert [life.player for life in build_demo(raw).rounds[0].lives] == [P1]


# -- kills --------------------------------------------------------------------


def test_kill_flags_are_read():
    raw = raw_demo(deaths=[death(
        500, P2, P1, weapon="awp", headshot=True, noscope=True, thrusmoke=True, penetrated=2,
        attackerblind=True, attackerinair=True, assistedflash=True, distance=21.5,
        assister_steamid=P3, assister_name="n3",
    )])
    (kill,) = build_demo(raw).kills
    assert (kill.tick, kill.round, kill.victim, kill.attacker, kill.assister) == (500, 1, P2, P1, P3)
    assert kill.weapon == "awp" and kill.distance == 21.5
    assert kill.headshot and kill.noscope and kill.through_smoke and kill.wallbang
    assert kill.penetrated == 2 and kill.attacker_blind and kill.attacker_in_air and kill.flash_assist


def test_flags_default_to_false():
    (kill,) = build_demo(raw_demo(deaths=[death(500, P2, P1)])).kills
    assert not (kill.headshot or kill.noscope or kill.through_smoke or kill.wallbang)
    assert kill.penetrated == 0 and not kill.attacker_blind and kill.assister is None


def test_world_and_self_deaths_have_no_attacker():
    nan = float("nan")
    raw = raw_demo(deaths=[
        death(400, P2, weapon="world", attacker_steamid=nan, attacker_name=nan),
        death(500, P1, P1, weapon="world"),
        death(600, "0", P2, weapon="planted_c4"),
    ])
    assert [kill.attacker for kill in build_demo(raw).kills] == [None, None, None]


def test_bots_get_an_id_from_their_name():
    raw = raw_demo(deaths=[death(500, "0", P1, user_name="Vitaliy")])
    demo = build_demo(raw)
    assert demo.kills[0].victim == "bot:Vitaliy"
    assert demo.player("bot:Vitaliy").name == "Vitaliy"


def test_deaths_before_freeze_end_or_outside_rounds_are_dropped():
    raw = raw_demo(deaths=[
        death(50, P2, P1),     # before the first round
        death(150, P2, P1),    # freeze time: a player using "kill" before the match
        death(500, P2, P1),    # a real kill
    ])
    demo = build_demo(raw)
    assert [kill.tick for kill in demo.kills] == [500]
    assert demo.rounds[0].life_of(P2).until_tick == 500


def test_a_kill_after_the_round_was_decided_stays_in_that_round():
    (kill,) = build_demo(raw_demo(deaths=[death(950, P2, P1)])).kills
    assert kill.round == 1


def test_kills_are_sorted_by_tick():
    raw = raw_demo(deaths=[death(1500, P1, P2), death(500, P2, P1)])
    assert [kill.tick for kill in build_demo(raw).kills] == [500, 1500]


def test_names_are_kept_even_for_players_only_seen_in_kills():
    raw = raw_demo(deaths=[death(500, P2, P3, assister_steamid=P1, assister_name="n1")])
    demo = build_demo(raw)
    assert {player.id for player in demo.players} == {P1, P2, P3, "bot:n0"}
    assert demo.player_name(P3) == "n3"


# -- bomb ---------------------------------------------------------------------


def test_bomb_events_are_read_with_round_and_site():
    raw = raw_demo(bombs=[
        {"kind": "exploded", "tick": 1800, "user_steamid": P1, "user_name": "n1", "site": 165},
        {"kind": "planted", "tick": 400, "user_steamid": P2, "user_name": "n2", "site": 165},
        {"kind": "planted", "tick": 10, "user_steamid": P2, "user_name": "n2", "site": 165},
    ])
    bombs = build_demo(raw).bomb_events
    assert [(b.kind, b.tick, b.round, b.player, b.site) for b in bombs] == [
        ("planted", 400, 1, P2, "165"), ("exploded", 1800, 2, P1, "165")]


# -- scoreboard ---------------------------------------------------------------


def test_scoreboard_counts_kills_deaths_and_assists():
    raw = raw_demo(deaths=[
        death(500, P2, P1),                                           # P1 kills P2 (CT v T)
        death(600, P3, weapon="world"),                               # P3 falls
        death(1500, P1, P2, assister_steamid="0", assister_name="Bot"),
    ])
    raw.rosters[200] = roster((P1, CT_TEAM), (P2, T_TEAM), (P3, T_TEAM))
    raw.rosters[1100] = roster((P1, T_TEAM), (P2, CT_TEAM), (P3, CT_TEAM))
    board = {line.player: line for line in scoreboard(build_demo(raw))}
    assert (board[P1].kills, board[P1].deaths) == (1, 1)
    assert (board[P2].kills, board[P2].deaths) == (1, 1)
    assert (board[P3].kills, board[P3].deaths) == (0, 1)
    assert board["bot:Bot"].assists == 1


def test_team_kills_and_team_assists_are_not_counted():
    raw = raw_demo(deaths=[
        death(500, P3, P2, assister_steamid=P1, assister_name="n1"),
    ])
    raw.rosters[200] = roster((P1, T_TEAM), (P2, T_TEAM), (P3, T_TEAM))
    demo = build_demo(raw)
    assert is_team_kill(demo, demo.kills[0])
    board = {line.player: line for line in scoreboard(demo)}
    assert board[P2].kills == 0 and board[P1].assists == 0 and board[P3].deaths == 1


def test_world_deaths_are_not_team_kills():
    demo = build_demo(raw_demo(deaths=[death(500, P2, weapon="world")]))
    assert not is_team_kill(demo, demo.kills[0])


# -- the result is a valid, saveable Demo ---------------------------------------


def test_the_result_survives_save_and_load(tmp_path):
    raw = raw_demo(deaths=[death(500, P2, P1, headshot=True), death(1500, P1, P2)])
    demo = build_demo(raw)
    demo.save(tmp_path / "demo.json")
    assert Demo.load(tmp_path / "demo.json") == demo


# -- reading files --------------------------------------------------------------


def test_a_missing_file_gives_a_clear_error(tmp_path):
    with pytest.raises(DemoParseError, match="not found"):
        read_raw(tmp_path / "missing.dem")


def test_a_file_that_is_not_a_demo_gives_a_clear_error(tmp_path):
    junk = tmp_path / "junk.dem"
    junk.write_bytes(b"this is not a demo" * 50)
    with pytest.raises(DemoParseError, match="junk.dem"):
        parse_demo(junk)


# -- a real demo ----------------------------------------------------------------

SAMPLE_DEMOS = sorted((Path(__file__).resolve().parent.parent / "demos").glob("*.dem"))


@pytest.fixture(scope="module")
def sample():
    if not SAMPLE_DEMOS:
        pytest.skip("no .dem file in demos/")
    return str(SAMPLE_DEMOS[0])


def test_real_demo_matches_the_ingame_scoreboard(sample):
    """Kills and assists must equal the counters the game keeps for each player."""
    from demoparser2 import DemoParser

    demo = parse_demo(sample)
    assert demo.rounds and demo.kills and demo.map_name

    parser = DemoParser(sample)
    last_tick = max(demo.rounds[-1].end_tick - 1, 0)
    stats = parser.parse_ticks(["kills_total", "assists_total"], ticks=[last_tick])
    expected = {str(int(row.steamid)): (int(row.kills_total), int(row.assists_total))
                for row in stats.itertuples()}
    got = {line.player: (line.kills, line.assists) for line in scoreboard(demo)}
    assert got == expected

    # every round has players, and nobody is alive after their death
    for rnd in demo.rounds:
        assert rnd.lives
    for kill in demo.kills:
        assert not demo.is_alive(kill.victim, kill.tick)
        assert demo.is_alive(kill.victim, kill.tick - 1)
