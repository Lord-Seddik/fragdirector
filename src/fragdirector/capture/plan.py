"""What to capture and which commands do it: pure functions, no game needed.

Everything here can be tested without CS2 or FFmpeg: turning a shot list
into capture jobs, naming clips, the console commands sent to the game, the
FFmpeg command lines, and the timing sums that decide where a clip is cut.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from fragdirector.models import Demo, Shot, ShotList, demo_file_name, shot_problems

CS2_APP_ID = "730"

# Console lines the capture waits for (see docs/capture.md).
DEMO_REQUESTED = r"Requesting playback of"
DEMO_LOADED = r'Signon traffic "DEMO"'
GAME_PAUSED = r"CGameRules - paused on tick"
UNPAUSED = r"CGameRules - unpaused on tick"
STEP_DONE = r"Demo paused at engine time \d+, demo tick (\d+)"
READY_WORD = "FRAGDIRECTOR_READY"

# Settings the game saves to disk. They are changed for the capture and put
# back afterwards; one that cannot be read is left alone.
SAVED_SETTINGS = {
    "engine_no_focus_sleep": "0",  # full frame rate when the window is not in front
    "r_show_build_info": "0",  # the build line bottom-left
}
# Also changed and put back when the sound is recorded (T20).
SOUND_SETTINGS = {
    "snd_mute_losefocus": "0",  # the game goes silent when its window is not in front
    "volume": "0.5",  # the level T20 measured: loud fights peak at about 0.77 of full scale
}
# Also changed and put back, always (T21). Not saved by the game, but put back anyway.
AFTER_DEATH_SETTINGS = {
    "spec_freeze_time": "10",  # the camera stays on a dead player this long (the game's default is 3)
}
# The console line CS2 prints when the camera moves to another player (T21): between
# the POV player's death and the shot's end it means the shot no longer shows them.
CAMERA_LEFT = r"Scanned \d+ entities, activated prediction"

# Ticks played before the shot at least, so the two-step pause check
# (two single ticks) cannot run past the shot's first tick.
MIN_LEAD_TICKS = 8
# The final pause may land this many ticks off and still count.
END_TICK_SLACK = 2

_UNSAFE_TEXT = re.compile(r'["\n\r;\x00-\x1f]')
_FILE_SAFE = re.compile(r"[^A-Za-z0-9_-]+")


class CaptureError(RuntimeError):
    """Capturing cannot go on. The message is written for the user."""


class ClipFailed(CaptureError):
    """One clip could not be captured; the others may still work."""


@dataclass(frozen=True)
class CaptureOptions:
    fps: int = 60
    preroll_seconds: float = 2.0  # played before the shot so the scene settles
    settle_seconds: float = 2.0  # wait after seeking, lets textures load
    picture_delay_seconds: float = 0.06  # the picture trails the tick by this much (T4)
    attempts: int = 3  # seek + record tries per clip
    demo_ui: bool = False  # keep the playback bar (shows the tick; for checking)
    sound: bool = True  # record the game's sound into each clip
    sound_delay_seconds: float = 0.12  # the sound cut is moved this much later (T20)

    def __post_init__(self) -> None:
        if not 1 <= self.fps <= 240:
            raise CaptureError(f"fps must be between 1 and 240, got {self.fps}")
        for name in ("preroll_seconds", "settle_seconds", "picture_delay_seconds", "sound_delay_seconds"):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise CaptureError(f"{name} must be 0 or more, got {value}")
        if self.attempts < 1:
            raise CaptureError(f"attempts must be 1 or more, got {self.attempts}")

    def settings(self) -> dict[str, str]:
        """The saved game settings to change for this capture."""
        return {**SAVED_SETTINGS, **AFTER_DEATH_SETTINGS, **(SOUND_SETTINGS if self.sound else {})}


@dataclass(frozen=True)
class Job:
    """One clip to record: a span of one demo through one player's eyes.

    Shots that differ only in speed or label share a clip (slow motion is
    applied when the video is put together), so ``shots`` can hold several
    positions in the shot list.
    """

    shots: tuple[int, ...]  # positions in the shot list, counting from 0
    demo_path: Path
    tick_rate: int
    start_tick: int
    end_tick: int
    pov_player: str
    player_name: str
    seek_tick: int
    clip: Path
    death_tick: int | None = None  # when the POV player dies in this round; None if they survive

    @property
    def ticks(self) -> int:
        return self.end_tick - self.start_tick

    @property
    def ends_after_death(self) -> bool:
        """True if the shot runs on past the POV player's death (death camera)."""
        return self.death_tick is not None and self.end_tick > self.death_tick

    def frames(self, fps: int) -> int:
        return round(self.ticks / self.tick_rate * fps)

    def describe(self) -> str:
        numbers = ", ".join(str(i + 1) for i in self.shots)
        seconds = self.ticks / self.tick_rate
        text = (f"shot {numbers}: {self.player_name}, ticks {self.start_tick}-{self.end_tick} "
                f"({seconds:.1f} s)")
        if self.ends_after_death:
            text += f", {(self.end_tick - self.death_tick) / self.tick_rate:.1f} s after the death"
        return text


# --------------------------------------------------------------------------
# Clip names
# --------------------------------------------------------------------------


def clip_key(shot: Shot) -> tuple[str, int, int, str]:
    """What makes two shots need the same clip."""
    return (demo_file_name(shot.demo), shot.start_tick, shot.end_tick, shot.pov_player)


SOUND_SUFFIX = "_sound"


def clip_name(shot: Shot, sound: bool = True) -> str:
    """File name of a shot's clip. Depends only on the shot (and whether the
    clip has sound), so the video assembler finds the clip from the shot list
    alone. A clip with sound ends in ``_sound.mp4``; a clip without has the
    name v1 used, so old silent clips are never taken for clips with sound."""
    demo, start, end, player = clip_key(shot)
    digest = hashlib.sha1(f"{demo}|{start}|{end}|{player}".encode("utf-8")).hexdigest()[:8]
    readable = _FILE_SAFE.sub("_", player)[:24].strip("_") or "player"
    return f"{start}-{end}_{readable}_{digest}{SOUND_SUFFIX if sound else ''}.mp4"


def clip_path(shot: Shot, clips_dir: str | Path, sound: bool = True) -> Path:
    return Path(clips_dir) / clip_name(shot, sound)


def partial_path(clip: Path) -> Path:
    """Where a clip is written before it is checked and renamed into place."""
    return clip.with_name(clip.stem + ".part.mp4")


# --------------------------------------------------------------------------
# Shot list -> jobs
# --------------------------------------------------------------------------


def unsafe_text_problem(what: str, text: str) -> str | None:
    """A console command argument is sent in double quotes; these break it."""
    if _UNSAFE_TEXT.search(text):
        return (f"{what} {text!r} holds a double quote, semicolon or control character, "
                f"which cannot be sent to the CS2 console.")
    return None


def spectate_name_problem(demo: Demo, player_id: str) -> str | None:
    """Why ``spec_player`` cannot be trusted to find this player, if it cannot.

    CS2 gives no error for a name it does not know (T4 trap 5), so the name
    must be in the demo, unique and safe to quote.
    """
    player = demo.player(player_id)
    if player is None:
        return f"Player '{player_id}' is not in this demo."
    if not player.name.strip():
        return f"Player '{player_id}' has no name in the demo, so the game cannot be told to follow them."
    problem = unsafe_text_problem("The player name", player.name)
    if problem:
        return problem
    same = [p for p in demo.players if p.name.casefold() == player.name.casefold()]
    if len(same) > 1:
        return f"Two players are called {player.name!r}; the game cannot be told which one to follow."
    return None


def seek_tick_for(shot: Shot, demo: Demo, options: CaptureOptions) -> int:
    """Where to jump before the shot: ``preroll`` earlier, but not before the
    player spawned, unless that would leave fewer than ``MIN_LEAD_TICKS``."""
    preroll = round(options.preroll_seconds * demo.tick_rate)
    want = shot.start_tick - max(preroll, MIN_LEAD_TICKS)
    rnd = demo.round_at(shot.start_tick)
    life = rnd.life_of(shot.pov_player) if rnd is not None else None
    floor = life.from_tick if life is not None else 0
    seek = max(want, floor)
    if shot.start_tick - seek < MIN_LEAD_TICKS:
        seek = shot.start_tick - MIN_LEAD_TICKS
    return max(1, seek)


DemoSource = tuple[Demo, Path]  # a parsed demo and the file to play


def plan_jobs(
    shotlist: ShotList,
    demos: Mapping[str, DemoSource],
    clips_dir: str | Path,
    options: CaptureOptions = CaptureOptions(),
    only: Iterable[int] | None = None,
) -> list[Job]:
    """One job per distinct clip, in the order that plays each demo forwards.

    ``demos`` maps ``demo_file_name`` to the parsed demo and its file.
    ``only`` limits the jobs to these shot positions (from 0). Every problem
    is collected first; if there is any, nothing is planned.
    """
    positions = list(range(len(shotlist.shots))) if only is None else sorted(set(only))
    problems: list[str] = []
    for index in positions:
        if not 0 <= index < len(shotlist.shots):
            problems.append(f"There is no shot {index + 1} (the list has {len(shotlist.shots)}).")
    if problems:
        raise CaptureError("\n".join(problems))
    if not positions:
        raise CaptureError("The shot list is empty; there is nothing to capture.")

    clips_dir = Path(clips_dir)
    grouped: dict[tuple, list[int]] = {}
    for index in positions:
        shot = shotlist.shots[index]
        prefix = f"Shot {index + 1}: "
        source = demos.get(demo_file_name(shot.demo))
        if source is None:
            problems.append(prefix + f"its demo '{shot.demo}' was not found.")
            continue
        demo, path = source
        if shotlist.tick_rate != demo.tick_rate:
            problems.append(prefix + f"the shot list is for {shotlist.tick_rate} ticks per second, "
                                     f"the demo has {demo.tick_rate}.")
        found = shot_problems(shot, demo)
        problems.extend(prefix + text for text in found)
        if not found:
            name_problem = spectate_name_problem(demo, shot.pov_player)
            if name_problem:
                problems.append(prefix + name_problem)
        path_problem = unsafe_text_problem("The demo path", str(path))
        if path_problem:
            problems.append(prefix + path_problem + " Move or rename the demo.")
        grouped.setdefault(clip_key(shot), []).append(index)
    if problems:
        # the same demo problem would repeat for every shot
        raise CaptureError("\n".join(dict.fromkeys(problems)))

    jobs: list[Job] = []
    for key, indexes in grouped.items():
        shot = shotlist.shots[indexes[0]]
        demo, path = demos[key[0]]
        rnd = demo.round_at(shot.start_tick)
        life = rnd.life_of(shot.pov_player) if rnd is not None else None
        died = life is not None and life.until_tick < rnd.end_tick
        jobs.append(Job(
            shots=tuple(indexes),
            demo_path=Path(path).resolve(),
            tick_rate=demo.tick_rate,
            start_tick=shot.start_tick,
            end_tick=shot.end_tick,
            pov_player=shot.pov_player,
            player_name=demo.player_name(shot.pov_player),
            seek_tick=seek_tick_for(shot, demo, options),
            clip=clip_path(shot, clips_dir, options.sound),
            death_tick=life.until_tick if died else None,
        ))
    jobs.sort(key=lambda job: (str(job.demo_path).casefold(), job.start_tick, job.end_tick, job.pov_player))
    return jobs


# --------------------------------------------------------------------------
# Console commands
# --------------------------------------------------------------------------


def launch_command(steam: str, port: int, password: str) -> list[str]:
    """Start CS2 through Steam with the TCP console open. Never without a password:
    the console port listens on every network interface."""
    if not password:
        raise CaptureError("refusing to open the CS2 console without a password")
    return [steam, "-applaunch", CS2_APP_ID, "-insecure", "-novid",
            "-netconport", str(port), "-netconpassword", password]


def before_demo_commands(options: CaptureOptions) -> list[str]:
    """Sent before ``playdemo``; the playback bar can only be hidden then.
    The game does not save either setting."""
    if options.demo_ui:
        return []
    return ["demo_ui_mode 0", "cl_trueview_show_status 0"]


def playdemo_command(path: Path) -> str:
    problem = unsafe_text_problem("The demo path", str(path))
    if problem:
        raise CaptureError(problem)
    return f'playdemo "{path}"'


def spectate_commands(name: str) -> list[str]:
    problem = unsafe_text_problem("The player name", name)
    if problem:
        raise CaptureError(problem)
    return ["spec_mode 1", f'spec_player "{name}"']


def setting_value_pattern(name: str) -> str:
    return rf"{re.escape(name)} = (\S+)"


# --------------------------------------------------------------------------
# FFmpeg
# --------------------------------------------------------------------------


NVENC = ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "constqp", "-qp", "16"]
X264_FAST = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "14"]


def record_command(ffmpeg: str, raw_path: Path, fps: int, base: int, encoder: list[str]) -> list[str]:
    """Record the CS2 window; frame times are wall-clock seconds minus ``base``."""
    source = f"gfxcapture=window_exe=cs2.exe:capture_cursor=0:max_framerate={fps},hwdownload,format=bgra"
    return [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostats", "-y",
        "-use_wallclock_as_timestamps", "1", "-f", "lavfi", "-i", source,
        "-copyts", "-vf", f"setpts=PTS-{base}/TB",
        *encoder, "-pix_fmt", "yuv420p",
        "-progress", "pipe:1", str(raw_path),
    ]


def cut_command(ffmpeg: str, raw_path: Path, offset: float, fps: int, frames: int, out_path: Path) -> list[str]:
    """Cut the shot out of the raw recording at a constant frame rate, no sound.

    The sound is added afterwards by ``mux_command``: an output ``-ss`` moves
    every stream, so one run cannot cut picture and sound at their own
    points (T20 trap 1)."""
    return [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(raw_path),
        "-ss", f"{offset:.4f}", "-vf", f"fps={fps}", "-frames:v", str(frames),
        "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", "-an", str(out_path),
    ]


def mux_command(ffmpeg: str, video_path: Path, wav_path: Path, offset: float, seconds: float,
                out_path: Path) -> list[str]:
    """Add the sound to a cut picture: the WAV from ``offset`` on, exactly
    ``seconds`` long (padded with silence if a few samples short)."""
    return [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(video_path),
        "-ss", f"{offset:.4f}", "-i", str(wav_path),  # input seek on a WAV is sample-exact
        "-map", "0:v", "-map", "1:a", "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k", "-af", "apad", "-t", f"{seconds:.4f}",
        "-movflags", "+faststart", str(out_path),
    ]


# --------------------------------------------------------------------------
# Timing
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Playback:
    """What the console reported while a range played."""

    from_tick: int  # where the demo was paused before playing
    final_tick: int  # where it paused afterwards
    t_sent: float  # wall clock when the play command went out
    t_paused: float  # wall clock of the final "Demo paused" line
    t_unpaused: float | None  # of "unpaused", which CS2 does not always print

    @property
    def played(self) -> int:
        return self.final_tick - self.from_tick


def seconds_per_tick(playback: Playback, tick_rate: int) -> float:
    """Measured playback speed when both ends are known, else real time."""
    if playback.t_unpaused is not None and playback.played > 0:
        measured = (playback.t_paused - playback.t_unpaused) / playback.played
        if measured > 0:
            return measured
    return 1 / tick_rate


def playback_drift(playback: Playback, tick_rate: int) -> float:
    """Seconds the playback took beyond real time (estimate if ``unpaused`` is missing)."""
    began = playback.t_unpaused if playback.t_unpaused is not None else playback.t_sent
    return (playback.t_paused - began) - playback.played / tick_rate


def cut_offset(
    playback: Playback,
    start_tick: int,
    tick_rate: int,
    record_base: float,
    raw_start: float,
    picture_delay: float,
) -> float:
    """Seconds into the raw recording where the shot's first tick is on screen.

    The time of the start tick is counted back from the final pause; the
    picture shows a tick ``picture_delay`` after the game reaches it.
    """
    return shot_start_time(playback, start_tick, tick_rate) + picture_delay - record_base - raw_start


def shot_start_time(playback: Playback, start_tick: int, tick_rate: int) -> float:
    """Wall clock when the game reached the shot's first tick."""
    return playback.t_paused - (playback.final_tick - start_tick) * seconds_per_tick(playback, tick_rate)


def camera_left_victim(lines: Iterable[tuple[float, str]], playback: Playback, job: Job) -> float | None:
    """Seconds after the POV player's death at which CS2 moved the camera to
    another player, or ``None`` if it did not (or the shot ends before the death).

    ``lines`` are the console lines (arrival time, text) from just before the
    shot's playback. Only a ``CAMERA_LEFT`` line between the death and the end
    of the playback counts; the time of the death is counted back from the
    final pause, like the shot's start (T21 matched the picture within 0.05 s).
    """
    if not job.ends_after_death:
        return None
    died = shot_start_time(playback, job.death_tick, job.tick_rate)
    rx = re.compile(CAMERA_LEFT)
    for when, text in lines:
        if died < when <= playback.t_paused and rx.search(text):
            return when - died
    return None


def sound_offset(playback: Playback, start_tick: int, tick_rate: int, sound_start: float,
                 sound_delay: float) -> float:
    """Seconds into the sound recording (which starts at wall clock
    ``sound_start``) where the shot's sound begins. The sound reaches the
    recorder later than the picture; T20 measured ``sound_delay`` = 0.12 s."""
    return shot_start_time(playback, start_tick, tick_rate) + sound_delay - sound_start
