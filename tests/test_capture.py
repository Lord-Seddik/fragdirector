"""Tests for capture: jobs, clip names, console and FFmpeg commands, timing,
and the game session driven by a scripted fake console. No game needed."""

import io
import re
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import DEMO_PATH, P1, P2, P3, P4, build_demo
from fragdirector.capture import CaptureError, CaptureOptions, clip_name, clip_path, demo_files, plan_jobs
from fragdirector.capture import game, run, sound
from fragdirector.capture.game import GameGone, GameSession
from fragdirector.capture.plan import (
    AFTER_DEATH_SETTINGS,
    MIN_LEAD_TICKS,
    SAVED_SETTINGS,
    Playback,
    before_demo_commands,
    camera_left_victim,
    cut_command,
    cut_offset,
    launch_command,
    mux_command,
    partial_path,
    playback_drift,
    playdemo_command,
    record_command,
    seconds_per_tick,
    seek_tick_for,
    shot_start_time,
    sound_offset,
    spectate_commands,
)
from fragdirector.cli import main
from fragdirector.models import Demo, Player, Shot, ShotList, demo_file_name


def shot(start=2000, end=2640, pov=P1, **extra) -> Shot:
    return Shot(demo=DEMO_PATH, start_tick=start, end_tick=end, pov_player=pov, **extra)


def sources(demo: Demo | None = None, path: str = DEMO_PATH) -> dict:
    demo = demo or build_demo()
    return {demo_file_name(demo.path): (demo, Path(path))}


def shots(*items: Shot) -> ShotList:
    return ShotList(shots=items, tick_rate=64)


# -- clip names --------------------------------------------------------------


def test_clip_name_depends_only_on_demo_span_and_player():
    base = shot()
    assert clip_name(base) == clip_name(replace(base, speed=0.5, label="slow"))
    assert clip_name(base) == clip_name(replace(base, demo="C:\\elsewhere\\MATCH.dem"))
    assert clip_name(base) != clip_name(replace(base, end_tick=2641))
    assert clip_name(base) != clip_name(replace(base, pov_player=P2))
    assert clip_name(base) != clip_name(replace(base, demo="demos/other.dem"))
    assert clip_name(base).startswith(f"2000-2640_{P1}_") and clip_name(base).endswith(".mp4")


def test_clip_name_is_file_safe_for_bot_ids():
    name = clip_name(shot(pov=P4))
    assert re.fullmatch(r"[A-Za-z0-9_.-]+", name)
    assert "bot_Vitaliy" in name


def test_partial_path_is_beside_the_clip(tmp_path):
    clip = clip_path(shot(), tmp_path)
    assert partial_path(clip).parent == clip.parent
    assert partial_path(clip).name.endswith(".part.mp4")
    assert partial_path(clip) != clip


# -- planning jobs -------------------------------------------------------------


def test_one_job_per_clip_in_demo_order(tmp_path):
    shotlist = shots(
        shot(3500, 3900, P1),
        shot(2000, 2640, P1, speed=1.0),
        shot(2000, 2640, P1, speed=0.5, label="replay"),  # same footage, slowed later
        shot(2000, 2640, P2),
    )
    jobs = plan_jobs(shotlist, sources(), tmp_path)
    assert [(j.start_tick, j.pov_player, j.shots) for j in jobs] == [
        (2000, P1, (1, 2)),
        (2000, P2, (3,)),
        (3500, P1, (0,)),
    ]
    first = jobs[0]
    assert first.player_name == "ZywOo"
    assert first.clip == tmp_path / clip_name(shotlist.shots[1])
    assert first.demo_path == Path(DEMO_PATH).resolve()
    assert first.frames(60) == 600  # 640 ticks = 10 s
    assert first.seek_tick == 2000 - 128  # 2 s of preroll


def test_only_limits_the_shots(tmp_path):
    shotlist = shots(shot(2000, 2640), shot(3500, 3900))
    jobs = plan_jobs(shotlist, sources(), tmp_path, only=[1])
    assert [j.shots for j in jobs] == [(1,)]
    with pytest.raises(CaptureError, match="There is no shot 3"):
        plan_jobs(shotlist, sources(), tmp_path, only=[2])


def test_every_problem_is_reported_before_anything_starts(tmp_path):
    shotlist = shots(
        shot(2000, 3400, P3),  # donk dies at 3000; 400 ticks after is too long
        shot(4900, 5100, P1),  # runs past the round
        replace(shot(), demo="demos/unknown.dem"),
    )
    with pytest.raises(CaptureError) as error:
        plan_jobs(shotlist, sources(), tmp_path)
    text = str(error.value)
    assert "Shot 1: donk dies at tick 3000" in text
    assert "Shot 2: The shot ends at tick 5100" in text
    assert "Shot 3: its demo 'demos/unknown.dem' was not found." in text


def test_empty_shot_list_is_refused(tmp_path):
    with pytest.raises(CaptureError, match="empty"):
        plan_jobs(shots(), sources(), tmp_path)


def test_tick_rate_mismatch_is_refused(tmp_path):
    with pytest.raises(CaptureError, match="128 ticks per second"):
        plan_jobs(ShotList(shots=(shot(),), tick_rate=128), sources(), tmp_path)


@pytest.mark.parametrize("name, fragment", [
    ('evil" ; quit', "double quote"),
    ("semi;colon", "semicolon"),
    ("line\nbreak", "control character"),
    ("", "has no name"),
])
def test_names_the_console_cannot_take_are_refused(tmp_path, name, fragment):
    demo = build_demo()
    players = tuple(Player(p.id, name) if p.id == P1 else p for p in demo.players)
    demo = replace(demo, players=players)
    with pytest.raises(CaptureError, match=fragment):
        plan_jobs(shots(shot()), sources(demo), tmp_path)


def test_two_players_with_the_same_name_are_refused(tmp_path):
    demo = build_demo()
    players = tuple(Player(p.id, "zywoo") if p.id == P2 else p for p in demo.players)
    demo = replace(demo, players=players)
    with pytest.raises(CaptureError, match="Two players are called"):
        plan_jobs(shots(shot()), sources(demo), tmp_path)


def test_unicode_names_are_fine(tmp_path):
    jobs = plan_jobs(shots(shot(pov=P2)), sources(), tmp_path)
    assert jobs[0].player_name == "Ünïcödé ☆"


def test_demo_path_with_a_quote_is_refused(tmp_path):
    with pytest.raises(CaptureError, match="demo path"):
        plan_jobs(shots(shot()), sources(path='demos/ma"tch.dem'), tmp_path)


# -- where to seek -------------------------------------------------------------


def test_seek_is_preroll_before_the_shot():
    assert seek_tick_for(shot(2000, 2640), build_demo(), CaptureOptions(preroll_seconds=1)) == 2000 - 64


def test_seek_does_not_go_before_the_player_spawned():
    # round 2 starts (and P1 spawns) at 5000; 2 s of preroll would land in round 1
    assert seek_tick_for(shot(5050, 5300), build_demo(), CaptureOptions()) == 5000


def test_seek_keeps_a_minimum_lead_even_at_the_spawn():
    assert seek_tick_for(shot(5002, 5300), build_demo(), CaptureOptions()) == 5002 - MIN_LEAD_TICKS
    assert seek_tick_for(shot(5002, 5300), build_demo(), CaptureOptions(preroll_seconds=0)) == 5002 - MIN_LEAD_TICKS


# -- commands --------------------------------------------------------------------


def test_launch_command_always_has_a_password():
    command = launch_command("C:/Steam/steam.exe", 2121, "secret")
    assert command[:3] == ["C:/Steam/steam.exe", "-applaunch", "730"]
    assert command[command.index("-netconport") + 1] == "2121"
    assert command[command.index("-netconpassword") + 1] == "secret"
    assert "-insecure" in command  # demo playback only, never a VAC-secured game
    with pytest.raises(CaptureError):
        launch_command("steam.exe", 2121, "")


def test_console_commands_quote_their_arguments():
    path = Path("C:/Users/Some User/demos/x.dem")
    assert playdemo_command(path) == f'playdemo "{path}"'
    assert spectate_commands("Ünïcödé ☆") == ["spec_mode 1", 'spec_player "Ünïcödé ☆"']
    with pytest.raises(CaptureError):
        spectate_commands('a"; quit')
    with pytest.raises(CaptureError):
        playdemo_command(Path("x;quit.dem"))


def test_playback_bar_is_hidden_unless_asked_for():
    assert "demo_ui_mode 0" in before_demo_commands(CaptureOptions())
    assert before_demo_commands(CaptureOptions(demo_ui=True)) == []


def test_record_command_captures_the_game_window_with_wallclock_times():
    command = record_command("ffmpeg", Path("raw.mkv"), 60, 1700000000, ["-c:v", "libx264"])
    source = command[command.index("-i") + 1]
    assert source.startswith("gfxcapture=window_exe=cs2.exe:") and "max_framerate=60" in source
    assert command[command.index("-use_wallclock_as_timestamps") + 1] == "1"
    assert "setpts=PTS-1700000000/TB" in command
    assert command[-1] == "raw.mkv"


def test_cut_command_takes_an_exact_number_of_frames_without_sound():
    command = cut_command("ffmpeg", Path("raw.mkv"), 3.25, 60, 600, Path("out.mp4"))
    assert command[command.index("-ss") + 1] == "3.2500"
    assert command[command.index("-frames:v") + 1] == "600"
    assert command[command.index("-vf") + 1] == "fps=60"
    assert "-an" in command and command[-1] == "out.mp4"


def test_options_are_checked():
    with pytest.raises(CaptureError):
        CaptureOptions(fps=0)
    with pytest.raises(CaptureError):
        CaptureOptions(preroll_seconds=-1)
    with pytest.raises(CaptureError):
        CaptureOptions(attempts=0)


# -- timing ----------------------------------------------------------------------


def test_cut_offset_counts_back_from_the_final_pause():
    # paused at tick 2640 at t=1010.0; the shot starts at 2000, 640 ticks = 10 s earlier
    playback = Playback(from_tick=1990, final_tick=2640, t_sent=999.8, t_paused=1010.0, t_unpaused=None)
    offset = cut_offset(playback, 2000, 64, record_base=990, raw_start=1.5, picture_delay=0.06)
    assert offset == pytest.approx(1010.0 - 10.0 + 0.06 - 990 - 1.5)


def test_measured_speed_is_used_when_both_ends_are_known():
    playback = Playback(1990, 2640, 999.0, 1011.0, t_unpaused=1000.0)  # 650 ticks in 11 s
    assert seconds_per_tick(playback, 64) == pytest.approx(11 / 650)
    assert playback_drift(playback, 64) == pytest.approx(11 - 650 / 64)
    without = replace(playback, t_unpaused=None)
    assert seconds_per_tick(without, 64) == pytest.approx(1 / 64)
    assert playback_drift(without, 64) == pytest.approx(12 - 650 / 64)


# -- the game session, with a fake console -----------------------------------


class FakeConsole:
    """Answers commands the way CS2 does; ``steps`` scripts what each
    ``demo_step_tick`` reports (None: no answer, like a lost command)."""

    def __init__(self, steps=None, settings=None):
        self.sent: list[str] = []
        self.lines: list[tuple[float, str]] = []
        self.clock = 1000.0
        self.tick = 0
        self.steps = list(steps or [])
        self.settings = dict(settings if settings is not None else {"engine_no_focus_sleep": "20",
                                                                   "r_show_build_info": "true"})
        self.closed = False
        self.playing = True  # a freshly loaded demo plays
        self.drop_seeks = 0  # seeks to ignore without a word, as just after loading

    def _say(self, text):
        self.lines.append((self.clock, text))

    def send(self, command):
        if self.closed:
            raise GameGone("CS2 closed the console connection")
        self.sent.append(command)
        word, _, rest = command.partition(" ")
        if word == "echo":
            self._say(rest)
        elif word == "playdemo":
            self._say("Requesting playback of demo")
            self._say('Signon traffic "DEMO": incoming 1')
        elif word == "demo_pause":
            self.playing = False
        elif word == "demo_resume":
            self.playing = True
        elif word == "demo_gototick":
            if self.drop_seeks:
                self.drop_seeks -= 1
                return
            target = int(rest)
            if target > self.tick and not self.playing:
                # what CS2 does: a forward skip from a pause never moves
                self._say(f"Demo Skipping: skipping forward to demo tick {target} from current {self.tick}")
                return
            self.tick = target
            self._say(f"Demo Skipping finished at tick {self.tick}")
            self._say(f"CGameRules - paused on tick {self.tick + 2321}")
        elif word == "demo_step_tick":
            self.playing = False
            count = int(rest)
            if count == 1 and self.steps:
                answer = self.steps.pop(0)
                if answer is None:
                    return
                self.tick = answer
            else:
                self._say("CGameRules - unpaused on tick 1")
                self.clock += count / 64
                self.tick += count
            self._say(f"Demo paused at engine time 123, demo tick {self.tick}")
        elif word in self.settings and not rest:
            self._say(f"{word} = {self.settings[word]}")
        elif word in self.settings:
            self.settings[word] = rest

    def mark(self):
        return len(self.lines)

    def wait_for(self, pattern, timeout, start):
        rx = re.compile(pattern)
        for when, text in self.lines[start:]:
            match = rx.search(text)
            if match:
                return when, match
        raise CaptureError(f"CS2 did not print {pattern!r}")

    def sleep(self, seconds):
        self.clock += seconds

    def ask(self, command, pattern, timeout=10.0):
        start = self.mark()
        self.send(command)
        return self.wait_for(pattern, timeout, start)[1]


def session_with(console):
    lines = []
    return GameSession(console, lines.append), lines


def test_load_demo_sends_the_hidden_ui_settings_first():
    console = FakeConsole()
    session, _ = session_with(console)
    session.wait_ready()
    session.load_demo(Path("C:/d/x.dem"), CaptureOptions())
    assert console.sent[1:4] == ["demo_ui_mode 0", "cl_trueview_show_status 0", f'playdemo "{Path("C:/d/x.dem")}"']
    assert session.loaded_demo == Path("C:/d/x.dem")


def test_saved_settings_are_changed_and_put_back():
    console = FakeConsole()
    session, _ = session_with(console)
    session.change_settings()
    assert console.settings == SAVED_SETTINGS
    session.restore_settings()
    assert console.settings == {"engine_no_focus_sleep": "20", "r_show_build_info": "true"}


def test_a_setting_that_cannot_be_read_is_left_alone():
    console = FakeConsole(settings={"engine_no_focus_sleep": "20"})
    session, lines = session_with(console)
    session.change_settings()
    assert "r_show_build_info 0" not in console.sent
    assert any("r_show_build_info" in line for line in lines)
    session.restore_settings()
    assert console.settings == {"engine_no_focus_sleep": "20"}


def test_seek_locks_the_view_and_proves_the_pause():
    console = FakeConsole(steps=[1873, 1874])
    session, _ = session_with(console)
    held = session.seek_and_hold(1872, "ZywOo", settle=2.0)
    assert held == 1874
    assert "demo_gototick 1872" in console.sent
    assert console.sent.index("spec_mode 1") < console.sent.index("demo_step_tick 1")
    assert console.sent.count('spec_player "ZywOo"') >= 2  # again after the first step


def test_seek_retries_when_the_demo_kept_playing():
    # first pair: 1873 then 1990 (the demo ran on); second pair is clean
    console = FakeConsole(steps=[1873, 1990, None, 1991, 1992, 1993])
    session, _ = session_with(console)
    assert session.seek_and_hold(1872, "ZywOo", settle=2.0) == 1993
    assert console.sent.count("demo_pause") >= 2


def test_forward_seek_from_a_pause_resumes_the_demo_first():
    console = FakeConsole(steps=[1873, 1874, 1001, 1002, 2001, 2002])
    session, _ = session_with(console)
    assert session.seek_and_hold(1872, "ZywOo", settle=0) == 1874
    assert session.paused_at == 1874
    # backward: works while paused, so the demo stays paused
    assert session.seek_and_hold(1000, "ZywOo", settle=0) == 1002
    assert console.sent[console.sent.index("demo_gototick 1000") - 1] == "demo_pause"
    # forward again: without a resume CS2 would sit at 1002
    assert session.seek_and_hold(2000, "ZywOo", settle=0) == 2002
    assert console.sent[console.sent.index("demo_gototick 2000") - 1] == "demo_resume"


def test_play_to_remembers_where_the_demo_paused():
    console = FakeConsole()
    console.tick = 1874
    session, _ = session_with(console)
    session.play_to(2640, 1874, 64)
    assert session.paused_at == 2640


def test_a_dropped_seek_is_sent_again(monkeypatch):
    monkeypatch.setattr("fragdirector.capture.game.SEEK_ANSWER_SECONDS", 0)
    console = FakeConsole(steps=[1873, 1874])
    console.drop_seeks = 1
    session, lines = session_with(console)
    assert session.seek_and_hold(1872, "ZywOo", settle=0) == 1874
    assert console.sent.count("demo_gototick 1872") == 2
    assert any("sending it again" in line for line in lines)


def test_seek_gives_up_after_four_tries():
    console = FakeConsole(steps=[100, 300] * 4)
    session, _ = session_with(console)
    with pytest.raises(CaptureError, match="would not stay paused"):
        session.seek_and_hold(99, "ZywOo", settle=0)


def test_play_to_reports_the_final_tick_and_times():
    console = FakeConsole()
    console.tick = 1874
    session, _ = session_with(console)
    playback = session.play_to(2640, 1874, 64)
    assert "demo_step_tick 766" in console.sent
    assert playback.final_tick == 2640 and playback.from_tick == 1874
    assert playback.t_paused - playback.t_unpaused == pytest.approx(766 / 64)


def test_a_closed_console_stops_everything():
    console = FakeConsole()
    console.closed = True
    session, _ = session_with(console)
    with pytest.raises(GameGone):
        session.seek_and_hold(100, "ZywOo", settle=0)


# -- finding demo files ------------------------------------------------------


def test_demo_files_are_found_as_written_or_beside_the_shot_list(tmp_path, monkeypatch):
    # Run from tmp_path so the result does not depend on the folder pytest was started in.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "demos").mkdir()
    (tmp_path / "demos" / "match.dem").write_bytes(b"")
    shots_file = tmp_path / "shots.json"
    shotlist = shots(shot())
    found = demo_files(shotlist, shots_file)
    # "demos/match.dem" is found as written, so the path is relative to tmp_path.
    assert found["match.dem"].resolve() == (tmp_path / "demos" / "match.dem").resolve()
    # From another folder the written path is not there, so the file beside the shot list is used.
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.chdir(tmp_path / "elsewhere")
    assert demo_files(shotlist, shots_file) == {"match.dem": tmp_path / "demos" / "match.dem"}
    monkeypatch.chdir(tmp_path)

    other = tmp_path / "copy" / "match.dem"
    other.parent.mkdir()
    other.write_bytes(b"")
    assert demo_files(shotlist, shots_file, other) == {"match.dem": other}
    with pytest.raises(CaptureError, match="No shot in the list is from"):
        demo_files(shotlist, shots_file, tmp_path / "copy" / "else.dem")
    with pytest.raises(CaptureError, match="not found"):
        demo_files(shots(replace(shot(), demo="nowhere/gone.dem")), shots_file)


@pytest.mark.parametrize("written", [
    "\\\\server\\share\\match.dem", "//server/share/match.dem", "\\/server/share/match.dem",
    "\\\\?\\UNC\\server\\share\\match.dem",
])
def test_a_network_path_in_a_shot_list_is_never_opened(tmp_path, monkeypatch, written):
    """A shot list from someone else must not make Windows log on to their server."""
    opened = []
    real_is_file = Path.is_file

    def is_file(self):
        opened.append(str(self))
        return real_is_file(self)

    monkeypatch.setattr(Path, "is_file", is_file)
    monkeypatch.chdir(tmp_path)
    shots_file = tmp_path / "shots.json"
    shotlist = shots(replace(shot(), demo=written))
    with pytest.raises(CaptureError, match="not found"):
        demo_files(shotlist, shots_file)
    assert opened == [str(tmp_path / "match.dem")]

    # a file of that name next to the shot list is used; --demo still takes any path
    (tmp_path / "match.dem").write_bytes(b"")
    assert demo_files(shotlist, shots_file) == {"match.dem": tmp_path / "match.dem"}


# -- the CLI -------------------------------------------------------------------


def test_cli_capture_dry_run_lists_the_clips(tmp_path, monkeypatch, capsys):
    shots_file = tmp_path / "shots.json"
    shots(shot(2000, 2640), shot(2000, 2640, speed=0.5), shot(3500, 3900, P2)).save(shots_file)
    (tmp_path / "demos").mkdir()
    (tmp_path / "demos" / "match.dem").write_bytes(b"")
    monkeypatch.setattr("fragdirector.parse.parse_demo", lambda path: build_demo())
    assert main(["capture", str(shots_file), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "2 clip(s) for 3 shot(s)" in out
    assert "shot 1, 2: ZywOo" in out and "shot 3: Ünïcödé ☆" in out
    assert out.count("[to capture]") == 2

    clip_path(shot(2000, 2640), tmp_path / "clips").parent.mkdir()
    clip_path(shot(2000, 2640), tmp_path / "clips").write_bytes(b"done")
    assert main(["capture", str(shots_file), "--dry-run", "--only", "3,1"]) == 0
    out = capsys.readouterr().out
    assert "[exists]" in out and "[to capture]" in out


def test_cli_capture_reports_problems_without_starting_anything(tmp_path, monkeypatch, capsys):
    shots_file = tmp_path / "shots.json"
    shots(shot(2000, 3400, P3)).save(shots_file)
    (tmp_path / "demos").mkdir()
    (tmp_path / "demos" / "match.dem").write_bytes(b"")
    monkeypatch.setattr("fragdirector.parse.parse_demo", lambda path: build_demo())
    started = []
    monkeypatch.setattr("fragdirector.capture.capture_jobs", lambda *a, **k: started.append(1))
    assert main(["capture", str(shots_file)]) == 1
    assert "donk dies at tick 3000" in capsys.readouterr().err
    assert not started


@pytest.mark.parametrize("only", ["0", "x", "1,,2"])
def test_cli_capture_rejects_bad_shot_numbers(tmp_path, capsys, only):
    shots_file = tmp_path / "shots.json"
    shots(shot()).save(shots_file)
    assert main(["capture", str(shots_file), "--only", only]) == 1
    assert "--only" in capsys.readouterr().err


def test_cli_capture_reports_a_missing_shot_list(tmp_path, capsys):
    assert main(["capture", str(tmp_path / "none.json")]) == 1
    assert "does not exist" in capsys.readouterr().err


# -- the recorder and the game start -------------------------------------------


class FakeProc:
    """Stands in for the FFmpeg process: reports 20 frames, or has already exited."""

    def __init__(self, exit_code=None):
        self.stdin = io.BytesIO()
        # a process that already exited never got to report frames
        self.stdout = io.BytesIO(b"" if exit_code else b"frame=20\nprogress=continue\n")
        self.exit_code = exit_code

    def poll(self):
        return self.exit_code

    def wait(self, timeout=None):
        return self.exit_code or 0

    def kill(self):
        pass


def fake_tools():
    return SimpleNamespace(ffmpeg="ffmpeg", encoder=["-c:v", "libx264"])


def test_recorder_sends_ffmpeg_errors_to_a_file_not_a_pipe(tmp_path, monkeypatch):
    seen = {}

    def fake_popen(command, **kwargs):
        seen.update(kwargs)
        kwargs["stderr"].write(b"x" * 100_000 + b"the last error")  # more than a pipe holds
        return FakeProc(exit_code=1)

    monkeypatch.setattr(game.subprocess, "Popen", fake_popen)
    recorder = game.Recorder(fake_tools(), tmp_path / "raw.mkv", 60)
    assert seen["stderr"] not in (subprocess.PIPE, subprocess.STDOUT)  # a file, not a pipe
    assert recorder.error_text().endswith("the last error")
    assert len(recorder.error_text()) <= 600
    with pytest.raises(CaptureError, match="the last error"):
        recorder.wait_until_recording(timeout=1)


def test_recorder_error_text_is_empty_when_there_is_no_log(tmp_path, monkeypatch):
    monkeypatch.setattr(game.subprocess, "Popen", lambda command, **kwargs: FakeProc())
    recorder = game.Recorder(fake_tools(), tmp_path / "raw.mkv", 60)
    recorder.error_path.unlink()
    assert recorder.error_text() == ""


def test_a_console_that_never_opens_says_the_game_is_still_running(tmp_path, monkeypatch):
    def never_opens(port, password, timeout):
        raise CaptureError(f"The CS2 console did not open on port {port}: refused")

    monkeypatch.setattr(run, "Tools", lambda: object())
    monkeypatch.setattr(run, "find_steam", lambda: "steam.exe")
    monkeypatch.setattr(run, "cs2_running", lambda: False)
    monkeypatch.setattr(run, "check_port_free", lambda port: None)
    monkeypatch.setattr(run, "start_game", lambda command: None)
    monkeypatch.setattr(run, "sound_support_problem", lambda: None)
    monkeypatch.setattr(run, "Console", never_opens)
    job = SimpleNamespace(clip=tmp_path / "clip.mp4")
    with pytest.raises(CaptureError, match=r"did not open on port 4000.*may still be running.*close it yourself"):
        run.capture_jobs([job], log=lambda text: None, port=4000)


# -- sound (T25) ----------------------------------------------------------------


def test_clips_with_sound_have_their_own_names(tmp_path):
    base = shot()
    silent, with_sound = clip_name(base, sound=False), clip_name(base)
    assert with_sound != silent
    assert with_sound.endswith("_sound.mp4") and with_sound.removesuffix("_sound.mp4") == silent.removesuffix(".mp4")
    assert re.fullmatch(r"2000-2640_[0-9]+_[0-9a-f]{8}\.mp4", silent)  # the name v1 gave every clip
    assert partial_path(clip_path(base, tmp_path)).name.endswith("_sound.part.mp4")
    jobs = plan_jobs(shots(base), sources(), tmp_path)
    assert jobs[0].clip.name == with_sound
    jobs = plan_jobs(shots(base), sources(), tmp_path, CaptureOptions(sound=False))
    assert jobs[0].clip.name == silent


def test_sound_settings_are_changed_only_when_recording_sound_and_put_back():
    assert set(CaptureOptions(sound=False).settings()) == set(SAVED_SETTINGS) | set(AFTER_DEATH_SETTINGS)
    settings = CaptureOptions().settings()
    assert settings["snd_mute_losefocus"] == "0" and settings["volume"] == "0.5"
    before = {"engine_no_focus_sleep": "20", "r_show_build_info": "true", "spec_freeze_time": "2",
              "snd_mute_losefocus": "true", "volume": "1.000000"}
    console = FakeConsole(settings=before)
    session, lines = session_with(console)
    session.change_settings(settings)
    assert console.settings == settings
    assert any("volume: 1.000000 -> 0.5" in line for line in lines)
    session.change_settings(settings)  # a second demo: the first old values are kept
    session.restore_settings()
    assert console.settings == before


def test_sound_options_are_checked():
    with pytest.raises(CaptureError):
        CaptureOptions(sound_delay_seconds=-0.1)
    with pytest.raises(CaptureError):
        CaptureOptions(sound_delay_seconds=float("nan"))


def test_mux_command_seeks_the_sound_alone_and_keeps_the_picture():
    command = mux_command("ffmpeg", Path("picture.mp4"), Path("sound.wav"), 1.125, 10.0, Path("out.mp4"))
    # the seek is an input option of the WAV, so it moves the sound only (T20 trap 1)
    assert command[command.index("-ss") + 1:command.index("-ss") + 4] == ["1.1250", "-i", "sound.wav"]
    assert command.index("picture.mp4") < command.index("-ss")
    assert command[command.index("-c:v") + 1] == "copy"
    assert command[command.index("-t") + 1] == "10.0000"
    assert "apad" in command and command[-1] == "out.mp4"


def test_sound_offset_uses_the_same_shot_start_as_the_picture():
    playback = Playback(from_tick=1990, final_tick=2640, t_sent=999.8, t_paused=1012.0, t_unpaused=None)
    assert shot_start_time(playback, 2000, 64) == pytest.approx(1002.0)
    assert sound_offset(playback, 2000, 64, sound_start=1001.0, sound_delay=0.12) == pytest.approx(1.12)
    picture = cut_offset(playback, 2000, 64, record_base=1000, raw_start=0.5, picture_delay=0.06)
    assert picture == pytest.approx(1.56)


def packet(stamp, frames=480, value=1):
    return (stamp, bytes([value]) * frames * sound.FRAME_BYTES)


def test_packets_are_laid_out_on_their_time_line():
    # 10 ms packets with a little jitter join without a jump
    pcm, jumps = sound.lay_out([packet(5.0), packet(5.0102), packet(5.0199)])
    assert jumps == [] and len(pcm) == 3 * 480 * sound.FRAME_BYTES
    # a 50 ms hole after the first 10 ms becomes silence where it was
    pcm, jumps = sound.lay_out([packet(5.0), packet(5.06, value=2)])
    assert jumps == [(pytest.approx(0.01), pytest.approx(0.05))]
    hole = pcm[480 * sound.FRAME_BYTES:2880 * sound.FRAME_BYTES]
    assert len(pcm) == (2880 + 480) * sound.FRAME_BYTES and sound.is_silent(hole)
    assert pcm[-1] == 2
    # a packet that starts well inside the last one replaces the overlap
    pcm, jumps = sound.lay_out([packet(5.0), packet(5.0, value=2)])
    assert jumps == [(pytest.approx(0.01), pytest.approx(-0.01))]
    assert len(pcm) == 480 * sound.FRAME_BYTES and pcm[0] == 2
    assert sound.lay_out([]) == (b"", [])


def test_sound_needs_a_new_enough_windows(monkeypatch):
    monkeypatch.setattr(sound.sys, "platform", "win32")
    monkeypatch.setattr(sound.sys, "getwindowsversion", lambda: SimpleNamespace(build=18363), raising=False)
    assert "build 19041" in sound.sound_support_problem()
    monkeypatch.setattr(sound.sys, "getwindowsversion", lambda: SimpleNamespace(build=19041), raising=False)
    assert sound.sound_support_problem() is None
    monkeypatch.setattr(sound.sys, "platform", "linux")
    assert "only works on Windows" in sound.sound_support_problem()


def test_an_old_windows_stops_the_capture_before_the_game_starts(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(run, "sound_support_problem", lambda: "Too old.")
    monkeypatch.setattr(run, "start_game", lambda command: started.append(command))
    job = SimpleNamespace(clip=tmp_path / "clip.mp4")
    with pytest.raises(CaptureError, match="Too old. .*--no-sound"):
        run.capture_jobs([job], log=lambda text: None)
    assert not started


def test_process_ids_reads_the_task_list(monkeypatch):
    out = '"cs2.exe","4242","Console","1","2,000,000 K"\n"cs2.exe","77","Console","1","1 K"\n'
    monkeypatch.setattr(sound.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=out))
    assert sound.process_ids("cs2.exe") == [4242, 77]
    no_match = "INFO: No tasks are running which match the specified criteria.\n"
    monkeypatch.setattr(sound.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=no_match))
    assert sound.process_ids("cs2.exe") == []


# -- one clip, with fake recorders ---------------------------------------------


class FakeSession:
    """Seeks to just before the shot and plays it in real time: the game reaches
    the shot's first tick (2000) at wall clock 1002.0."""

    def __init__(self, console_lines=()):
        """``console_lines``: the console lines of each attempt, as (wall clock, text)."""
        self.lines = []
        self.log = self.lines.append
        self.attempt_lines = list(console_lines) or [[]]
        self.attempts = 0
        self.console = SimpleNamespace(sleep=lambda seconds: None, mark=lambda: 0, lines_from=self._console_lines)

    def _console_lines(self, start):
        return self.attempt_lines[min(self.attempts, len(self.attempt_lines)) - 1]

    def seek_and_hold(self, seek_tick, player_name, settle):
        return 1990

    def play_to(self, end_tick, from_tick, tick_rate):
        self.attempts += 1
        return Playback(from_tick, end_tick, 1001.8, 1002.0 + (end_tick - 2000) / 64, None)


class FakeRecorder:
    def __init__(self, tools, raw_path, fps):
        self.base = 1000
        self.stopped = False

    def wait_until_recording(self):
        pass

    def stop(self):
        self.stopped = True


class FakeSound:
    """Packets of 10 ms from wall clock ``start`` to ``end``."""

    def __init__(self, start=1001.0, end=1013.0, value=1, stop_error=None):
        count = round((end - start) * 100)
        self.packets = [packet(start + i / 100, value=value) for i in range(count)]
        self.untimed = 0
        self.stop_error = stop_error
        self.stopped = False

    def stop(self):
        self.stopped = True
        if self.stop_error:
            raise CaptureError(self.stop_error)


def one_clip(tmp_path, monkeypatch, fake_sound=None, audio_seconds="10.000000", frames="600", log=None,
             the_shot=None, console_lines=()):
    """Run ``capture_one`` with everything outside Python faked; returns
    (job, FFmpeg command lines, result or the ClipFailed raised).

    ``fake_sound`` is the sound recorder every attempt gets, or a list with
    one per attempt. ``log`` collects what the capture logs. ``the_shot`` is
    the shot to capture (default 2000-2640, 10 s) and ``console_lines`` the
    console lines of each attempt."""
    commands = []
    recorders = list(fake_sound) if isinstance(fake_sound, list) else None

    def fake_run(command, **kwargs):
        commands.append(command)
        Path(command[-1]).write_bytes(b"clip")
        return SimpleNamespace(returncode=0, stderr="")

    def probe(path, entries, count_frames=False, stream="v:0"):
        if entries == "format=start_time":
            return {"start_time": "0.500000"}
        if stream == "a:0":
            return {"duration": audio_seconds}
        return {"width": "1920", "height": "1080", "nb_read_frames": frames}

    monkeypatch.setattr(run.subprocess, "run", fake_run)
    monkeypatch.setattr(run, "Recorder", FakeRecorder)
    monkeypatch.setattr(run, "SoundRecorder", lambda pid: recorders.pop(0) if recorders is not None else fake_sound)
    tools = SimpleNamespace(ffmpeg="ffmpeg", probe=probe)
    job = plan_jobs(shots(the_shot or shot()), sources(), tmp_path / "clips",
                    CaptureOptions(sound=fake_sound is not None))[0]
    work = tmp_path / "work"
    work.mkdir(parents=True, exist_ok=True)
    pid = 4242 if fake_sound is not None else None
    session = FakeSession(console_lines)
    try:
        result = run.capture_one(session, job, tools, CaptureOptions(), work, pid)
    except CaptureError as error:
        result = error
    if log is not None:
        log.extend(session.lines)
    return job, commands, result


def test_a_clip_gets_the_sound_cut_with_the_pictures_timing(tmp_path, monkeypatch):
    fake = FakeSound()
    job, commands, note = one_clip(tmp_path, monkeypatch, fake)
    assert fake.stopped
    cut, mux = commands
    assert cut[cut.index("-ss") + 1] == "1.5600" and "-an" in cut  # picture alone first
    picture = cut[-1]
    assert mux[mux.index("-i") + 1] == picture
    assert mux[mux.index("-ss") + 1] == "1.1200"  # 1002.0 + 0.12 - 1001.0
    assert mux[mux.index("-t") + 1] == "10.0000" and mux[-1] == str(partial_path(job.clip))
    assert job.clip.is_file() and not partial_path(job.clip).exists()
    assert "1920x1080, 600 frames, 10.000 s of sound" in note and "WARNING" not in note


def test_a_clip_without_sound_is_cut_straight_into_place(tmp_path, monkeypatch):
    job, commands, note = one_clip(tmp_path, monkeypatch, fake_sound=None)
    assert len(commands) == 1 and commands[0][-1] == str(partial_path(job.clip))
    assert not job.clip.name.endswith("_sound.mp4") and job.clip.is_file()
    assert "sound" not in note


def test_sound_that_is_not_as_long_as_the_picture_fails_the_clip(tmp_path, monkeypatch):
    # one frame at 60 fps is 16.7 ms: 10.016 s passes, 10.020 s does not
    job, _, note = one_clip(tmp_path, monkeypatch, FakeSound(), audio_seconds="10.016000")
    assert isinstance(note, str)
    job.clip.unlink()
    job, _, error = one_clip(tmp_path, monkeypatch, FakeSound(), audio_seconds="10.020000")
    assert isinstance(error, run.ClipFailed) and "sound is 10.020 s long, its picture 10.000 s" in str(error)
    assert not job.clip.exists() and not partial_path(job.clip).exists()
    _, _, error = one_clip(tmp_path, monkeypatch, FakeSound(), audio_seconds="N/A")
    assert isinstance(error, run.ClipFailed)


def test_a_sound_recording_that_does_not_cover_the_shot_fails_the_clip(tmp_path, monkeypatch):
    # the shot's sound runs from 1002.12 to 1012.12 in wall clock
    job, _, error = one_clip(tmp_path, monkeypatch, FakeSound(start=1001.0, end=1011.5))
    assert isinstance(error, run.ClipFailed) and "ends 0.62 s before the shot does" in str(error)
    _, _, error = one_clip(tmp_path, monkeypatch, FakeSound(start=1002.5, end=1013.0))
    assert isinstance(error, run.ClipFailed) and "started after the shot began" in str(error)
    _, _, error = one_clip(tmp_path, monkeypatch, FakeSound(start=1001.0, end=1001.0))
    assert isinstance(error, run.ClipFailed) and "no sound was recorded" in str(error)
    assert not job.clip.exists() and not partial_path(job.clip).exists()


def test_a_sound_recorder_that_fails_fails_the_clip(tmp_path, monkeypatch):
    fake = FakeSound(stop_error="The game's sound recording failed: device gone")
    job, commands, error = one_clip(tmp_path, monkeypatch, fake)
    assert isinstance(error, run.ClipFailed) and "device gone" in str(error)
    assert commands == [] and not job.clip.exists()


def test_silent_sound_is_reported(tmp_path, monkeypatch):
    _, _, note = one_clip(tmp_path, monkeypatch, FakeSound(value=0))
    assert "WARNING: the sound is silent" in note


def gapped(first, last):
    """A recording whose packets ``first`` to ``last - 1`` (10 ms each, from wall
    clock 1001.0) are missing. The shot's sound is packets 112 to 1112."""
    fake = FakeSound()
    del fake.packets[first:last]
    return fake


def test_a_gap_in_the_shots_sound_is_recorded_again(tmp_path, monkeypatch):
    log = []
    job, commands, note = one_clip(tmp_path, monkeypatch, [gapped(500, 520), FakeSound()], log=log)
    assert any("attempt 1: the sound recording has a gap of 200 ms at 3.88 s; trying again" in line
               for line in log)
    assert "WARNING" not in note and job.clip.is_file()
    assert len(commands) == 2  # only the good take was cut


def test_a_gap_on_every_try_is_kept_with_a_warning(tmp_path, monkeypatch):
    log = []
    _, _, note = one_clip(tmp_path, monkeypatch, [gapped(500, 520) for _ in range(3)], log=log)
    assert sum("trying again" in line for line in log) == 2
    assert "WARNING: the sound has a gap of 200 ms at 3.88 s" in note


def test_a_gap_outside_the_shot_does_not_matter(tmp_path, monkeypatch):
    log = []
    _, _, note = one_clip(tmp_path, monkeypatch, [gapped(50, 60), FakeSound()], log=log)
    assert "WARNING" not in note and not any("trying again" in line for line in log)


def test_cli_no_sound_lists_the_silent_clip_names(tmp_path, monkeypatch, capsys):
    shots_file = tmp_path / "shots.json"
    shots(shot(2000, 2640)).save(shots_file)
    (tmp_path / "demos").mkdir()
    (tmp_path / "demos" / "match.dem").write_bytes(b"")
    monkeypatch.setattr("fragdirector.parse.parse_demo", lambda path: build_demo())
    assert main(["capture", str(shots_file), "--dry-run"]) == 0
    assert clip_name(shot(2000, 2640)) in capsys.readouterr().out
    assert main(["capture", str(shots_file), "--dry-run", "--no-sound"]) == 0
    assert clip_name(shot(2000, 2640), sound=False) in capsys.readouterr().out


# -- shots that run past the POV player's death (T26) ----------------------------


def after_death_shot(end=3072, pov=P3):
    """donk (P3) dies at tick 3000; the default end is 1.125 s after, 1072 ticks = 16.75 s."""
    return shot(2000, end, pov)


def test_a_job_knows_when_its_player_dies(tmp_path):
    jobs = plan_jobs(shots(after_death_shot(), shot(2000, 2640, P3), shot(2000, 2640, P1)),
                     sources(), tmp_path)
    by_end = {(j.pov_player, j.end_tick): j for j in jobs}
    after = by_end[(P3, 3072)]
    assert after.death_tick == 3000 and after.ends_after_death
    assert "1.1 s after the death" in after.describe()
    before = by_end[(P3, 2640)]
    assert before.death_tick == 3000 and not before.ends_after_death
    assert "after the death" not in before.describe()
    survivor = by_end[(P1, 2640)]
    assert survivor.death_tick is None and not survivor.ends_after_death


def test_spec_freeze_time_is_held_for_the_session_and_put_back():
    for options in (CaptureOptions(), CaptureOptions(sound=False)):
        assert options.settings()["spec_freeze_time"] == "10"
    before = {"engine_no_focus_sleep": "20", "r_show_build_info": "true", "spec_freeze_time": "2"}
    console = FakeConsole(settings=before)
    session, lines = session_with(console)
    session.change_settings(CaptureOptions(sound=False).settings())
    assert console.settings["spec_freeze_time"] == "10"
    assert any("spec_freeze_time: 2 -> 10" in line for line in lines)
    session.restore_settings()
    assert console.settings == before


def test_settings_are_sent_again_after_a_second_demo_loads():
    # loading a demo puts its own replicated spec_freeze_time in force (T21)
    before = {"engine_no_focus_sleep": "20", "r_show_build_info": "true", "spec_freeze_time": "2"}
    console = FakeConsole(settings=before)
    session, _ = session_with(console)
    settings = CaptureOptions(sound=False).settings()
    session.change_settings(settings)
    console.settings["spec_freeze_time"] = "3"  # the second demo's server value
    session.change_settings(settings)
    assert console.settings["spec_freeze_time"] == "10"
    session.restore_settings()
    assert console.settings == before  # the value read first is the one put back


def test_spec_player_is_not_sent_again_during_the_shot():
    # the game ignores it for a dead player (T21 trap 1); only the seek sends it
    console = FakeConsole(steps=[None, 2000, None, 2001])
    session, _ = session_with(console)
    session.seek_and_hold(1990, "donk", settle=0)
    sent_before = len(console.sent)
    session.play_to(3072, 2001, 64)
    assert console.sent[sent_before:] == ["demo_step_tick 1071"]


SWITCH = "Scanned 64 entities, activated prediction for donk"


def test_camera_left_victim_looks_only_between_the_death_and_the_end():
    job = plan_jobs(shots(after_death_shot()), sources(), Path("clips"))[0]
    playback = Playback(1990, 3072, 1001.8, 1019.0, None)  # tick 3000 was at 1017.875
    assert shot_start_time(playback, 3000, 64) == pytest.approx(1017.875)
    other = (1018.0, "CGameRules - paused on tick 3050")
    assert camera_left_victim([(1010.0, SWITCH), other], playback, job) is None  # before the death
    assert camera_left_victim([(1019.5, SWITCH)], playback, job) is None  # after the shot
    assert camera_left_victim([other], playback, job) is None
    assert camera_left_victim([other, (1018.125, SWITCH), (1018.5, SWITCH)], playback, job) == pytest.approx(0.25)
    # a shot that ends before the death is not asked at all
    early = plan_jobs(shots(shot(2000, 2640, P3)), sources(), Path("clips"))[0]
    assert camera_left_victim([(1010.0, SWITCH)], Playback(1990, 2640, 1001.8, 1012.0, None), early) is None


def test_a_shot_that_holds_on_the_victim_is_kept(tmp_path, monkeypatch):
    log = []
    job, commands, note = one_clip(tmp_path, monkeypatch, frames="1005", the_shot=after_death_shot(), log=log,
                                   console_lines=[[(1002.5, SWITCH)]])  # before the death at 1017.6
    assert isinstance(note, str) and "1005 frames" in note and job.clip.is_file()
    assert not any("camera left" in line for line in log)


def test_a_camera_that_leaves_the_victim_is_recorded_again(tmp_path, monkeypatch):
    log = []
    job, commands, note = one_clip(tmp_path, monkeypatch, frames="1005", the_shot=after_death_shot(), log=log,
                                   console_lines=[[(1018.5, SWITCH)], []])
    assert any("attempt 1: the camera left donk 0.9 s after the death; trying again" in line for line in log)
    assert isinstance(note, str) and job.clip.is_file()
    assert len(commands) == 1  # only the good take was cut


def test_a_camera_that_leaves_every_time_fails_the_clip(tmp_path, monkeypatch):
    job, commands, error = one_clip(tmp_path, monkeypatch, frames="1005", the_shot=after_death_shot(),
                                    console_lines=[[(1018.5, SWITCH)]])
    assert isinstance(error, run.ClipFailed)
    assert "no clean recording in 3 attempts (the camera left donk 0.9 s after the death)" in str(error)
    assert commands == [] and not job.clip.exists() and not partial_path(job.clip).exists()
