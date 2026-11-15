"""T21 after-death spike: record a victim's view through their death and on after it.

    python spikes/capture_after_death.py demos/x.dem \
        --death emlight:117243 --death OutubroAK42:118495 --after 6

For every `name:tick` (the tick of the player's death), records `--before`
seconds before the death and `--after` seconds after it, from that player's
view, all in one game session. Next to each clip it writes:

- `<clip>.console.txt`: every console line during playback, stamped with the
  seconds from the death (negative = before), to see what CS2 does on its own;
- `<clip>.sheet.png`: one frame every 0.5 s from 0.5 s before the death, to see
  who the camera is on and what the HUD shows.

`--hold repeat` sends `spec_mode` / `spec_player` again every 0.25 s from the
death on, to test whether that holds the camera on the dead player.
`--set name=value` sets a console variable for the session (put back after).

Same needs as `capture_one.py` (T4). How and why: docs/capture.md, section
"After death (spike T21)".
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from capture_one import (  # noqa: E402  (spike: reuse the T4 pieces)
    CS2_APP_ID,
    DEMO_LOADED,
    DEMO_REQUESTED,
    GAME_PAUSED,
    STEP_DONE,
    TICK_RATE,
    CaptureError,
    Console,
    Recorder,
    cs2_running,
    find_steam,
    log,
    need_tool,
    probe,
    step_one_tick,
)

_paused_at: int | None = None  # where the demo is known to be paused


def seek_and_hold(console: Console, seek_tick: int, player: str, settle: float) -> int:
    """T12's seek (traps 1, 9, 10): resume before a forward seek, repeat an unanswered one."""
    global _paused_at
    console.send("demo_resume" if _paused_at is None or seek_tick > _paused_at else "demo_pause")
    console.sleep(0.5)
    _paused_at = None
    start = console.mark()
    for _ in range(3):
        console.send(f"demo_gototick {seek_tick}")
        try:
            console.wait_for(r"Demo Skipping", 5, start)
            break
        except CaptureError:
            log(f"no answer to the seek to {seek_tick}; sending it again")
    console.wait_for(rf"Demo Skipping finished at tick {seek_tick}\b", 60, start)
    try:
        console.wait_for(GAME_PAUSED, 3, start)
    except CaptureError:
        pass
    console.sleep(1.0)
    console.send("spec_mode 1")
    console.send(f'spec_player "{player}"')
    for _ in range(4):
        first = step_one_tick(console)
        console.send(f'spec_player "{player}"')
        console.sleep(settle)
        second = step_one_tick(console)
        if first is not None and second == first + 1:
            _paused_at = second
            return second
        console.send("demo_pause")
        console.sleep(1.0)
    raise CaptureError("the demo would not stay paused after seeking")

ATTEMPTS = 3
PICTURE_DELAY = 0.060  # T4/T12: the picture trails the tick by about 60 ms

# Spectator settings worth knowing the value of (unknown ones are reported as such).
SPEC_CVARS = (
    "spec_autodirector", "spec_autodirector_cameraman", "spec_autodirector_pausetime",
    "spec_freeze_time", "spec_freeze_time_lock", "spec_freeze_deathanim_time",
    "spec_freeze_traveltime", "spec_freeze_panel_extended_time", "spec_freeze_cinematiclight_scale",
    "spec_replay_enable", "spec_hide_players", "spec_show_xray", "spec_allow_roaming",
    "cl_spec_follow_grenade_key", "cl_deathcam_audio_mix_phase1_duration",
    "cl_obs_interp_enable", "mp_forcecamera",
)
SPAM = ("TrueView", "Prediction", "UserCommands", "TrueSpectate")


def read_cvar(console: Console, name: str) -> str:
    start = console.mark()
    console.send(name)
    deadline = time.time() + 1.5
    while time.time() < deadline:
        console.pump()
        for _, text in console.lines[start:]:
            if text.startswith(f"{name} = ") or text.startswith(f"{name} ="):
                return text.split("=", 1)[1].strip()
            if "Unknown command" in text and name in text:
                return "(unknown)"
    return "(no reply)"


def start_session(args: argparse.Namespace, demo: Path) -> Console:
    password = __import__("secrets").token_hex(8)
    log("starting CS2 (-insecure, demo playback only)")
    subprocess.Popen([args.steam or find_steam(), "-applaunch", CS2_APP_ID, "-insecure", "-novid",
                      "-netconport", str(args.port), "-netconpassword", password])
    console = Console(args.port, password, timeout=180)
    console.ask("echo FRAGDIRECTOR_READY", r"FRAGDIRECTOR_READY", timeout=60)
    log("loading demo")
    if not args.demo_ui:
        console.send("demo_ui_mode 0")
        console.send("cl_trueview_show_status 0")
    start = console.mark()
    deadline = time.time() + 180
    while True:
        console.send(f'playdemo "{demo}"')
        try:
            console.wait_for(DEMO_REQUESTED, 5, start)
            break
        except CaptureError:
            if time.time() > deadline:
                raise CaptureError("CS2 never accepted the playdemo command") from None
    console.wait_for(DEMO_LOADED, 240, start)
    console.sleep(1)
    return console


def record_one(console: Console, args: argparse.Namespace, ffmpeg: str, ffprobe: str,
               name: str, death: int, out_path: Path, work: Path) -> None:
    global _paused_at
    start_tick = death - round(args.before * TICK_RATE)
    end_tick = death + round(args.after * TICK_RATE)
    seek_tick = start_tick - round(args.preroll * TICK_RATE)
    frames_wanted = round((end_tick - start_tick) / TICK_RATE * args.fps)
    raw_path = work / f"{out_path.stem}.raw.mkv"

    for attempt in range(1, ATTEMPTS + 1):
        log(f"{name}: seeking to tick {seek_tick}")
        current = seek_and_hold(console, seek_tick, name, args.settle)
        if current >= start_tick:
            log(f"attempt {attempt}: demo ran on to tick {current}; seeking again")
            continue
        recorder = Recorder(ffmpeg, raw_path, args.fps)
        try:
            recorder.wait_until_recording()
            start = console.mark()
            console.send(f"demo_step_tick {end_tick - current}")
            t_sent = time.time()
            t_death_guess = t_sent + (death - current) / TICK_RATE
            if args.hold == "repeat":
                while time.time() < t_death_guess:
                    console.pump()
                while time.time() < t_sent + (end_tick - current) / TICK_RATE - 0.1:
                    console.send("spec_mode 1")
                    console.send(f'spec_player "{name}"')
                    console.sleep(0.25)
            play_seconds = (end_tick - current) / TICK_RATE
            t_paused, match = console.wait_for(STEP_DONE, play_seconds * 3 + 30, start)
            final = int(match.group(1))
            _paused_at = final
            console.sleep(0.5)
        finally:
            recorder.stop()
        if abs(final - end_tick) <= 2:
            break
        log(f"attempt {attempt}: playback ended at tick {final}, expected {end_tick}; again")
    else:
        raise CaptureError(f"no clean capture of {name} in {ATTEMPTS} attempts")

    # Same timing as T4/T12: anchor on the final pause, assume real time.
    t_death = t_paused - (final - death) / TICK_RATE
    shot_start = t_paused - (final - start_tick) / TICK_RATE + PICTURE_DELAY
    offset = shot_start - recorder.base - float(probe(ffprobe, raw_path, "format=start_time")["start_time"])
    drift = (t_paused - t_sent) - (final - current) / TICK_RATE
    log(f"{name}: played {current}..{final}, drift {drift * 1000:+.0f} ms, cut at {offset:.3f}s")
    if offset < 0:
        raise CaptureError("recording started after the shot began")

    lines = [f"{t - t_death:+8.3f}  {text}" for t, text in console.lines[start:]
             if not any(word in text for word in SPAM)]
    out_path.with_suffix(".console.txt").write_text(
        f"# {name} dies at tick {death}; clip ticks {start_tick}..{end_tick}; hold={args.hold}\n"
        + "\n".join(lines) + "\n", encoding="utf-8")

    cut = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(raw_path),
         "-ss", f"{offset:.4f}", "-vf", f"fps={args.fps}", "-frames:v", str(frames_wanted),
         "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
         "-movflags", "+faststart", "-an", str(out_path)],
        capture_output=True, text=True)
    if cut.returncode != 0:
        raise CaptureError("FFmpeg cut failed: " + cut.stderr[-600:])
    frames = int(probe(ffprobe, out_path, "stream=nb_read_frames", count_frames=True).get("nb_read_frames", "0"))
    log(f"wrote {out_path.name}: {frames} frames (wanted {frames_wanted})")

    # Contact sheet: 4 frames a row, one every 0.5 s, ending at the clip's end;
    # the sheet's first frame is at `sheet_from` (logged) seconds from the death.
    cols = 4
    rows = -(-int((args.after + 2.0) / 0.5) // cols)
    sheet_from = max(0.0, args.before + args.after - rows * cols * 0.5)
    log(f"{name}: sheet frames start {sheet_from - args.before:+.1f}s from the death, 0.5 s apart")
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{sheet_from:.3f}", "-i", str(out_path),
         "-vf", f"fps=2,scale=480:-1,tile={cols}x{rows}",
         "-frames:v", "1", str(out_path.with_suffix(".sheet.png"))],
        capture_output=True, text=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Record victims through their death (T21 spike).")
    parser.add_argument("demo")
    parser.add_argument("--death", action="append", required=True, metavar="NAME:TICK",
                        help="player name exactly as in the demo and the tick of their death; repeatable")
    parser.add_argument("--before", type=float, default=3.0, help="seconds before the death (default 3)")
    parser.add_argument("--after", type=float, default=6.0, help="seconds after the death (default 6)")
    parser.add_argument("--hold", choices=("none", "repeat"), default="none")
    parser.add_argument("--set", action="append", default=[], metavar="CVAR=VALUE",
                        help="console variable to set for the session (put back afterwards)")
    parser.add_argument("--tag", default="", help="added to the clip names")
    parser.add_argument("--out-dir", default="output")
    parser.add_argument("--fps", type=int, default=60)
    parser.add_argument("--preroll", type=float, default=2.0)
    parser.add_argument("--settle", type=float, default=2.0)
    parser.add_argument("--port", type=int, default=2121)
    parser.add_argument("--steam")
    parser.add_argument("--demo-ui", action="store_true", help="keep the playback bar (shows the tick)")
    args = parser.parse_args()
    if sys.platform != "win32":
        print("Windows only", file=sys.stderr)
        return 2

    deaths = []
    for item in args.death:
        name, _, tick = item.rpartition(":")
        if not name or not tick.isdigit() or '"' in name or ";" in name:
            parser.error(f"bad --death {item!r}")
        deaths.append((name, int(tick)))
    demo = Path(args.demo).resolve()
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg, ffprobe = need_tool("ffmpeg"), need_tool("ffprobe")
    if cs2_running():
        print("CS2 is already running; close it first.", file=sys.stderr)
        return 1

    work = Path(tempfile.mkdtemp(prefix="fragdirector_t21_"))
    console: Console | None = None
    restore: list[tuple[str, str]] = []
    failed = 0
    try:
        console = start_session(args, demo)
        values = {name: read_cvar(console, name) for name in SPEC_CVARS}
        (out_dir / f"t21_cvars{args.tag}.txt").write_text(
            "\n".join(f"{k} = {v}" for k, v in values.items()) + "\n", encoding="utf-8")
        log("spectator settings: " + ", ".join(f"{k}={v}" for k, v in values.items()))
        found = console.mark()
        for word in ("spec_", "deathcam", "freezecam", "replay"):
            console.send(f"find {word}")
        console.sleep(3)
        (out_dir / f"t21_find{args.tag}.txt").write_text(
            "\n".join(text for _, text in console.lines[found:]) + "\n", encoding="utf-8")
        for name in ("engine_no_focus_sleep",):
            restore.append((name, read_cvar(console, name)))
            console.send(f"{name} 0")
        for item in args.set:
            name, _, value = item.partition("=")
            restore.append((name, read_cvar(console, name)))
            console.send(f"{name} {value}")
            log(f"set {name} {value}; now {read_cvar(console, name)}")
        for name, death in deaths:
            safe = "".join(c if c.isalnum() else "_" for c in name)
            out_path = out_dir / f"t21_{safe}_{death}{args.tag}.mp4"
            try:
                record_one(console, args, ffmpeg, ffprobe, name, death, out_path, work)
            except CaptureError as exc:
                failed += 1
                print(f"[capture] {name}: FAILED: {exc}", file=sys.stderr)
                console.dump_tail()
    except CaptureError as exc:
        print(f"[capture] FAILED: {exc}", file=sys.stderr)
        if console is not None:
            console.dump_tail()
        return 1
    finally:
        if console is not None:
            try:
                for name, value in restore:
                    if value and not value.startswith("("):
                        console.send(f"{name} {value.split()[0]}")
                console.send("quit")
                console.sleep(1)
            except (CaptureError, OSError):
                pass
            console.close()
        shutil.rmtree(work, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
