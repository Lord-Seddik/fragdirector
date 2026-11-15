"""Capture a list of jobs in one game session, skipping clips already made.

A clip is written as ``<name>.part.mp4``, checked (frame count), and only
then renamed to ``<name>.mp4``. A clip file that exists is therefore
complete, and running the capture again picks up where it stopped.
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
    cut_command,
    cut_offset,
    launch_command,
    partial_path,
    playback_drift,
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


def capture_one(session: GameSession, job: Job, tools: Tools, options: CaptureOptions, work: Path) -> str:
    """Seek, record and cut one clip. Returns a short note; raises ``ClipFailed``."""
    log = session.log
    raw_path = work / "raw.mkv"
    playback = None
    for attempt in range(1, options.attempts + 1):
        log(f"seeking to tick {job.seek_tick}")
        held = session.seek_and_hold(job.seek_tick, job.player_name, options.settle_seconds)
        if held >= job.start_tick:
            log(f"attempt {attempt}: the demo ran on to tick {held}, past the shot start; seeking again")
            continue
        recorder = Recorder(tools, raw_path, options.fps)
        try:
            recorder.wait_until_recording()
            playback = session.play_to(job.end_tick, held, job.tick_rate)
            session.console.sleep(0.5)  # the picture trails the tick; keep recording a little
        finally:
            recorder.stop()
        if abs(playback.final_tick - job.end_tick) <= END_TICK_SLACK:
            break
        log(f"attempt {attempt}: playback stopped at tick {playback.final_tick}, "
            f"expected {job.end_tick}; trying again")
        playback = None
    if playback is None:
        raise ClipFailed(f"no clean recording in {options.attempts} attempts")

    raw_start = float(tools.probe(raw_path, "format=start_time").get("start_time", "nan"))
    offset = cut_offset(playback, job.start_tick, job.tick_rate, recorder.base, raw_start,
                        options.picture_delay_seconds)
    if not offset >= 0:  # also catches nan
        raise ClipFailed("the recording started after the shot began")
    drift = playback_drift(playback, job.tick_rate)

    frames = job.frames(options.fps)
    job.clip.parent.mkdir(parents=True, exist_ok=True)
    part = partial_path(job.clip)
    cut = subprocess.run(cut_command(tools.ffmpeg, raw_path, offset, options.fps, frames, part),
                         capture_output=True, text=True)
    if cut.returncode != 0:
        part.unlink(missing_ok=True)
        raise ClipFailed("FFmpeg could not cut the clip: " + cut.stderr[-600:])
    info = tools.probe(part, "stream=width,height,nb_read_frames", count_frames=True)
    got = int(info.get("nb_read_frames", "0") or 0)
    if got != frames:
        part.unlink(missing_ok=True)
        raise ClipFailed(f"expected {frames} frames, got {got}")
    os.replace(part, job.clip)
    note = f"{info.get('width')}x{info.get('height')}, {got} frames, playback drift {drift * 1000:+.0f} ms"
    if abs(drift) > 0.25:
        note += " (WARNING: playback was not real time; the clip may be off)"
    return note


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
        for number, job in enumerate(todo, start=1):
            if session.loaded_demo != job.demo_path:
                log(f"loading demo {job.demo_path.name}")
                session.load_demo(job.demo_path, options)
                session.change_settings()
            log(f"[{number}/{len(todo)}] {job.describe()}")
            try:
                note = capture_one(session, job, tools, options, work)
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
