"""Capture a list of jobs in one game session, skipping clips already made.

A clip is written as ``<name>.part.mp4``, checked (frame count, and the
sound's length), and only then renamed to ``<name>.mp4``. A clip file that
exists is therefore complete, and running the capture again picks up where
it stopped.
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from fragdirector.capture.game import (
    Console,
    GameGone,
    GameSession,
    Log,
    Recorder,
    Tools,
    check_port_free,
    cs2_running,
    find_steam,
    free_port,
    start_game,
)
from fragdirector.capture.plan import (
    END_TICK_SLACK,
    CaptureError,
    CaptureOptions,
    ClipFailed,
    Job,
    Playback,
    camera_left_victim,
    cut_command,
    cut_offset,
    launch_command,
    mux_command,
    partial_path,
    playback_drift,
    sound_offset,
)
from fragdirector.capture.sound import (
    FRAME_BYTES,
    RATE,
    SoundRecorder,
    is_silent,
    lay_out,
    process_ids,
    sound_support_problem,
    write_wav,
)
from fragdirector.models import ShotList, demo_file_name


CONSOLE_WAIT_SECONDS = 180  # how long the game gets to open its console


def _is_network_path(text: str) -> bool:
    """True for a path on a network share (two leading slashes of either kind)."""
    return len(text) >= 2 and text[0] in "\\/" and text[1] in "\\/"


def demo_files(shotlist: ShotList, shots_file: Path, override: Path | None = None) -> dict[str, Path]:
    """The demo file for each demo the shot list names, keyed by ``demo_file_name``.

    A shot's ``demo`` is tried as written (from the current folder), then
    next to the shot list file. ``override`` replaces the demo whose file
    name it has. Raises ``CaptureError`` naming every demo not found.

    A shot list can come from someone else, so a ``demo`` that names a
    network share is never opened: Windows would send this user's login to
    that server. Only a file of that name next to the shot list is used; a
    demo that really is on a share can be given with ``override`` (``--demo``).
    """
    found: dict[str, Path] = {}
    missing: list[str] = []
    for shot in shotlist.shots:
        key = demo_file_name(shot.demo)
        if key in found or shot.demo in missing:
            continue
        if override is not None and demo_file_name(str(override)) == key:
            candidates = [override]
        elif _is_network_path(shot.demo):
            candidates = [shots_file.parent / PureWindowsPath(shot.demo).name]
        else:
            written = Path(shot.demo)
            candidates = [written] if written.is_absolute() else [written, shots_file.parent / written]
        for candidate in candidates:
            if candidate.is_file():
                found[key] = candidate
                break
        else:
            missing.append(shot.demo)
    if override is not None and demo_file_name(str(override)) not in found:
        raise CaptureError(f"No shot in the list is from {override.name}.")
    if missing:
        raise CaptureError("Demo file(s) not found: " + ", ".join(missing) + " (pass --demo PATH)")
    return found


@dataclass(frozen=True)
class JobResult:
    job: Job
    status: str  # "captured", "skipped" (clip already there) or "failed"
    message: str = ""


def capture_one(session: GameSession, job: Job, tools: Tools, options: CaptureOptions, work: Path,
                sound_pid: int | None = None) -> str:
    """Seek, record and cut one clip. Returns a short note; raises ``ClipFailed``.

    With ``sound_pid`` the sound of that process is recorded alongside the
    picture and added to the clip, cut with the same timing (T20).
    """
    log = session.log
    raw_path = work / "raw.mkv"
    playback = None
    sound: SoundRecorder | None = None
    why_not = ""  # the reason of the last attempt that was thrown away, for the final message
    for attempt in range(1, options.attempts + 1):
        why_not = ""
        log(f"seeking to tick {job.seek_tick}")
        held = session.seek_and_hold(job.seek_tick, job.player_name, options.settle_seconds)
        if held >= job.start_tick:
            log(f"attempt {attempt}: the demo ran on to tick {held}, past the shot start; seeking again")
            continue
        recorder: Recorder | None = None
        sound = None
        sound_problem: str | None = None
        try:
            if sound_pid is not None:  # started first: it must cover the whole picture
                sound = SoundRecorder(sound_pid)
            recorder = Recorder(tools, raw_path, options.fps)
            recorder.wait_until_recording()
            played_from = session.console.mark()
            playback = session.play_to(job.end_tick, held, job.tick_rate)
            session.console.sleep(0.5)  # the picture trails the tick; keep recording a little
        finally:
            if recorder is not None:
                recorder.stop()
            if sound is not None:
                try:
                    sound.stop()
                except CaptureError as error:
                    sound_problem = str(error)
        if sound_problem:
            raise ClipFailed(sound_problem)
        if abs(playback.final_tick - job.end_tick) > END_TICK_SLACK:
            log(f"attempt {attempt}: playback stopped at tick {playback.final_tick}, "
                f"expected {job.end_tick}; trying again")
            playback = None
            continue
        left = camera_left_victim(session.console.lines_from(played_from), playback, job)
        if left is not None:  # the game moved the camera off the dead player (T21)
            why_not = f"the camera left {job.player_name} {left:.1f} s after the death"
            log(f"attempt {attempt}: {why_not}; trying again")
            playback = None
            continue
        gaps = _gaps_in_shot(sound, playback, job, options) if sound is not None else []
        if not gaps or attempt == options.attempts:  # the last try is kept, with a warning
            break
        log(f"attempt {attempt}: the sound recording has {_describe_gaps(gaps)}; trying again")
        playback = None
    if playback is None:
        raise ClipFailed(f"no clean recording in {options.attempts} attempts"
                         + (f" ({why_not})" if why_not else ""))

    raw_start = float(tools.probe(raw_path, "format=start_time").get("start_time", "nan"))
    offset = cut_offset(playback, job.start_tick, job.tick_rate, recorder.base, raw_start,
                        options.picture_delay_seconds)
    if not offset >= 0:  # also catches nan
        raise ClipFailed("the recording started after the shot began")
    drift = playback_drift(playback, job.tick_rate)

    frames = job.frames(options.fps)
    seconds = frames / options.fps
    job.clip.parent.mkdir(parents=True, exist_ok=True)
    part = partial_path(job.clip)
    picture = work / "picture.mp4" if sound is not None else part
    try:
        cut = subprocess.run(cut_command(tools.ffmpeg, raw_path, offset, options.fps, frames, picture),
                             capture_output=True, text=True)
        if cut.returncode != 0:
            raise ClipFailed("FFmpeg could not cut the clip: " + cut.stderr[-600:])
        sound_note = ""
        if sound is not None:
            sound_note = _add_sound(sound, playback, job, tools, options, work, picture, part, seconds)
        info = tools.probe(part, "stream=width,height,nb_read_frames", count_frames=True)
        got = int(info.get("nb_read_frames", "0") or 0)
        if got != frames:
            raise ClipFailed(f"expected {frames} frames, got {got}")
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    os.replace(part, job.clip)
    note = f"{info.get('width')}x{info.get('height')}, {got} frames{sound_note}, playback drift {drift * 1000:+.0f} ms"
    if abs(drift) > 0.25:
        note += " (WARNING: playback was not real time; the clip may be off)"
    return note


def _add_sound(sound: SoundRecorder, playback: Playback, job: Job, tools: Tools, options: CaptureOptions,
               work: Path, picture: Path, part: Path, seconds: float) -> str:
    """Cut the recorded sound like the picture and put both into ``part``. Returns a note."""
    pcm, _ = lay_out(sound.packets)
    if not pcm:
        raise ClipFailed("no sound was recorded from the game")
    start = sound_offset(playback, job.start_tick, job.tick_rate, sound.packets[0][0],
                         options.sound_delay_seconds)
    if not start >= 0:  # also catches nan
        raise ClipFailed("the sound recording started after the shot began")
    frame = 1 / options.fps
    recorded = len(pcm) / FRAME_BYTES / RATE
    if recorded < start + seconds - frame:
        raise ClipFailed(f"the sound recording ends {start + seconds - recorded:.2f} s before the shot does")
    wav = work / "sound.wav"
    write_wav(wav, pcm)
    mux = subprocess.run(mux_command(tools.ffmpeg, picture, wav, start, seconds, part),
                         capture_output=True, text=True)
    if mux.returncode != 0:
        raise ClipFailed("FFmpeg could not add the sound: " + mux.stderr[-600:])
    try:
        length = float(tools.probe(part, "stream=duration", stream="a:0").get("duration", "nan"))
    except ValueError:
        length = float("nan")
    if not abs(length - seconds) <= frame:  # also catches nan
        raise ClipFailed(f"the clip's sound is {length:.3f} s long, its picture {seconds:.3f} s")
    note = f", {length:.3f} s of sound"
    shot_pcm = pcm[round(start * RATE) * FRAME_BYTES:round((start + seconds) * RATE) * FRAME_BYTES]
    if is_silent(shot_pcm):
        note += " (WARNING: the sound is silent; is CS2 muted in the Windows volume mixer?)"
    gaps = _gaps_in_shot(sound, playback, job, options)
    if gaps:
        note += f" (WARNING: the sound has {_describe_gaps(gaps)})"
    if sound.untimed:
        note += f" ({sound.untimed} sound packet(s) without a time from Windows)"
    return note


def _gaps_in_shot(sound: SoundRecorder, playback: Playback, job: Job,
                  options: CaptureOptions) -> list[tuple[float, float]]:
    """Jumps in the sound recording that fall inside the shot, as (seconds into
    the clip, size in seconds). Jumps before or after the shot do not matter."""
    if not sound.packets:
        return []
    _, jumps = lay_out(sound.packets)
    start = sound_offset(playback, job.start_tick, job.tick_rate, sound.packets[0][0],
                         options.sound_delay_seconds)
    seconds = job.frames(options.fps) / options.fps
    return [(at - start, size) for at, size in jumps if start <= at <= start + seconds]


def _describe_gaps(gaps: list[tuple[float, float]]) -> str:
    parts = [f"{'a gap' if size > 0 else 'an overlap'} of {abs(size) * 1000:.0f} ms at {at:.2f} s"
             for at, size in gaps]
    return ", ".join(parts)


def _game_process_id(log: Log) -> int:
    """The process id of the running game, whose sound is recorded."""
    pids = process_ids("cs2.exe")
    if not pids:
        raise CaptureError("CS2 answers on its console, but cs2.exe is not in the process list, "
                           "so its sound cannot be recorded. Try again, or capture with --no-sound.")
    if len(pids) > 1:
        log(f"several cs2.exe processes ({', '.join(map(str, pids))}); recording the sound of {pids[0]}")
    return pids[0]


def capture_jobs(
    jobs: list[Job],
    options: CaptureOptions = CaptureOptions(),
    *,
    log: Log = print,
    force: bool = False,
    steam: str | None = None,
    port: int | None = None,
) -> list[JobResult]:
    """Capture every job whose clip is missing (all of them with ``force``).

    Starts CS2, captures in one session, puts the changed settings back and
    quits the game. A clip that fails is reported and the rest go on; a
    problem with the game itself stops the run.
    """
    results: list[JobResult] = []
    todo: list[Job] = []
    for job in jobs:
        if job.clip.exists() and not force:
            results.append(JobResult(job, "skipped", f"{job.clip.name} already exists"))
        else:
            todo.append(job)
    if not todo:
        log("every clip already exists; nothing to capture (use --force to capture again)")
        return results

    if options.sound:
        problem = sound_support_problem()
        if problem:
            raise CaptureError(f"{problem} Capture the picture only with --no-sound.")
    tools = Tools()
    steam = steam or find_steam()
    if cs2_running():
        raise CaptureError("CS2 is already running. Close it first; fragdirector never closes a game it did not start.")
    port = port or free_port()
    check_port_free(port)
    password = secrets.token_hex(16)

    log(f"starting CS2 for {len(todo)} clip(s) (-insecure, demo playback only)")
    start_game(launch_command(steam, port, password))
    try:
        console = Console(port, password, timeout=CONSOLE_WAIT_SECONDS)
    except GameGone:
        raise
    except CaptureError as error:
        # Without a console there is no way to tell the game to quit.
        raise CaptureError(
            f"{error} CS2 was started and may still be running; close it yourself before trying again."
        ) from error
    session = GameSession(console, log)
    work = Path(tempfile.mkdtemp(prefix="fragdirector_capture_"))
    try:
        session.wait_ready()
        sound_pid = _game_process_id(log) if options.sound else None
        for number, job in enumerate(todo, start=1):
            if session.loaded_demo != job.demo_path:
                log(f"loading demo {job.demo_path.name}")
                session.load_demo(job.demo_path, options)
                session.change_settings(options.settings())
            log(f"[{number}/{len(todo)}] {job.describe()}")
            try:
                note = capture_one(session, job, tools, options, work, sound_pid)
            except GameGone:
                raise
            except CaptureError as error:  # this clip only
                log(f"  FAILED: {error}")
                for line in console.tail(25):
                    log("    | " + line)
                results.append(JobResult(job, "failed", str(error)))
                continue
            log(f"  wrote {job.clip.name}: {note}")
            results.append(JobResult(job, "captured", note))
    except CaptureError:
        tail = console.tail()
        if tail:
            log("last console lines:")
            for line in tail:
                log("    " + line)
        raise
    finally:
        try:
            session.restore_settings()
            session.quit()
        except (CaptureError, OSError):
            pass
        console.close()
        shutil.rmtree(work, ignore_errors=True)
    return results
