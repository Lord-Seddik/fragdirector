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
from fragdirector.capture import game, run
from fragdirector.capture.game import GameGone, GameSession
from fragdirector.capture.plan import (
    MIN_LEAD_TICKS,
    SAVED_SETTINGS,
    Playback,
    before_demo_commands,
    cut_command,
    cut_offset,
    launch_command,
    partial_path,
    playback_drift,
    playdemo_command,
    record_command,
    seconds_per_tick,
    seek_tick_for,
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
        shot(2000, 3100, P3),  # donk dies at 3000
        shot(4900, 5100, P1),  # runs past the round
        replace(shot(), demo="demos/unknown.dem"),
    )
    with pytest.raises(CaptureError) as error:
        plan_jobs(shotlist, sources(), tmp_path)
    text = str(error.value)
    assert "Shot 1: donk is no longer alive from tick 3000" in text
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
    shots(shot(2000, 3100, P3)).save(shots_file)
    (tmp_path / "demos").mkdir()
    (tmp_path / "demos" / "match.dem").write_bytes(b"")
    monkeypatch.setattr("fragdirector.parse.parse_demo", lambda path: build_demo())
    started = []
    monkeypatch.setattr("fragdirector.capture.capture_jobs", lambda *a, **k: started.append(1))
    assert main(["capture", str(shots_file)]) == 1
    assert "donk is no longer alive" in capsys.readouterr().err
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
    monkeypatch.setattr(run, "Console", never_opens)
    job = SimpleNamespace(clip=tmp_path / "clip.mp4")
    with pytest.raises(CaptureError, match=r"did not open on port 4000.*may still be running.*close it yourself"):
        run.capture_jobs([job], log=lambda text: None, port=4000)
