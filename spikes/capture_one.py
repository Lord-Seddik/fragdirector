"""T4 capture spike: one clip of one player's view from a CS2 demo, hands-free.

    python spikes/capture_one.py demos/x.dem --player sedx --tick 117100

Starts CS2, plays the demo, jumps to the tick, locks the camera to the player,
records the game window with FFmpeg and writes a trimmed .mp4 (video only).
Needs: Windows, Steam running, CS2 closed, ffmpeg + ffprobe 8.0 or newer on
PATH. Standard library only. How and why it works: docs/capture.md.
"""

from __future__ import annotations

import argparse
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

CS2_APP_ID = "730"
TICK_RATE = 64
ATTEMPTS = 3  # seek + record tries inside one game session

# Console lines the script waits for.
DEMO_REQUESTED = r"Requesting playback of"
DEMO_LOADED = r'Signon traffic "DEMO"'
GAME_PAUSED = r"CGameRules - paused on tick"
UNPAUSED = r"CGameRules - unpaused on tick"
STEP_DONE = r"Demo paused at engine time \d+, demo tick (\d+)"


class CaptureError(RuntimeError):
    pass


def log(message: str) -> None:
    print(f"[capture] {message}", flush=True)


# --------------------------------------------------------------------------
# CS2 console over TCP (-netconport)
# --------------------------------------------------------------------------


class Console:
    """Line-based console connection. Remembers every line and when it came."""

    def __init__(self, port: int, password: str, timeout: float) -> None:
        deadline = time.time() + timeout
        while True:
            try:
                self.sock = socket.create_connection(("127.0.0.1", port), timeout=2)
                break
            except OSError as exc:
                if time.time() > deadline:
                    raise CaptureError(f"CS2 console did not open on port {port}: {exc}") from exc
                time.sleep(1)
        self.sock.settimeout(0.05)
        self.pending = b""
        self.lines: list[tuple[float, str]] = []
        self.send(f"PASS {password}")

    def send(self, command: str) -> None:
        self.sock.sendall(command.encode("utf-8") + b"\n")

    def pump(self) -> None:
        try:
            data = self.sock.recv(65536)
        except socket.timeout:
            return
        if not data:
            raise CaptureError("CS2 closed the console connection")
        now = time.time()
        self.pending += data
        while b"\n" in self.pending:
            raw, self.pending = self.pending.split(b"\n", 1)
            self.lines.append((now, raw.decode("utf-8", "replace").rstrip("\r")))

    def mark(self) -> int:
        return len(self.lines)

    def wait_for(self, pattern: str, timeout: float, start: int) -> tuple[float, re.Match]:
        """Return (arrival time, match) of the first line from `start` on that matches."""
        rx = re.compile(pattern)
        index = start
        deadline = time.time() + timeout
        while True:
            while index < len(self.lines):
                match = rx.search(self.lines[index][1])
                if match:
                    return self.lines[index][0], match
                index += 1
            if time.time() > deadline:
                raise CaptureError(f"CS2 did not print {pattern!r} within {timeout:.0f}s")
            self.pump()

    def sleep(self, seconds: float) -> None:
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.pump()

    def ask(self, command: str, pattern: str, timeout: float = 10.0) -> re.Match:
        start = self.mark()
        self.send(command)
        return self.wait_for(pattern, timeout, start)[1]

    def dump_tail(self, count: int = 40) -> None:
        """Print the last console lines (minus per-frame spam) to help diagnose a failure."""
        spam = ("TrueView", "Prediction", "UserCommands", "TrueSpectate")
        tail = [text for _, text in self.lines if not any(word in text for word in spam)][-count:]
        print("[capture] last console lines:", file=sys.stderr)
        for text in tail:
            print("    " + text[:200].encode("ascii", "replace").decode(), file=sys.stderr)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


# --------------------------------------------------------------------------
# Finding and starting things
# --------------------------------------------------------------------------


def cs2_running() -> bool:
    out = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq cs2.exe", "/NH"], capture_output=True, text=True
    ).stdout
    return "cs2.exe" in out.lower()


def find_steam() -> str:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            path = winreg.QueryValueEx(key, "SteamExe")[0]
    except OSError as exc:
        raise CaptureError("Steam not found in the registry; pass --steam") from exc
    if not Path(path).is_file():
        raise CaptureError(f"Steam not found at {path}; pass --steam")
    return path


def need_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise CaptureError(f"{name} is not on PATH")
    return path


def pick_raw_encoder(ffmpeg: str) -> list[str]:
    """GPU encoder if it works (keeps the CPU free for the game), else x264."""
    probe = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
         "color=black:s=256x256:r=30", "-frames:v", "5", "-c:v", "h264_nvenc", "-f", "null", "-"],
        capture_output=True,
    )
    if probe.returncode == 0:
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "constqp", "-qp", "16"]
    return ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "14"]


class Recorder:
    """FFmpeg capturing the CS2 window; frame times are wall clock minus `base`."""

    def __init__(self, ffmpeg: str, raw_path: Path, fps: int) -> None:
        self.base = int(time.time())
        self.frames = 0
        source = f"gfxcapture=window_exe=cs2.exe:capture_cursor=0:max_framerate={fps},hwdownload,format=bgra"
        command = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-nostats", "-y",
            "-use_wallclock_as_timestamps", "1", "-f", "lavfi", "-i", source,
            "-copyts", "-vf", f"setpts=PTS-{self.base}/TB",
            *pick_raw_encoder(ffmpeg), "-pix_fmt", "yuv420p",
            "-progress", "pipe:1", str(raw_path),
        ]
        self.proc = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        threading.Thread(target=self._read_progress, daemon=True).start()

    def _read_progress(self) -> None:
        for raw in self.proc.stdout:
            line = raw.decode("ascii", "replace").strip()
            if line.startswith("frame="):
                try:
                    self.frames = int(line.split("=", 1)[1])
                except ValueError:
                    pass

    def wait_until_recording(self, timeout: float = 20.0) -> None:
        deadline = time.time() + timeout
        while self.frames < 15:
            if self.proc.poll() is not None:
                raise CaptureError("FFmpeg capture stopped: " + self.proc.stderr.read().decode("utf-8", "replace")[-600:])
            if time.time() > deadline:
                raise CaptureError("FFmpeg capture produced no frames")
            time.sleep(0.05)

    def stop(self) -> None:
        if self.proc.poll() is None:
            try:
                self.proc.stdin.write(b"q")
                self.proc.stdin.flush()
            except OSError:
                pass
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def probe(ffprobe: str, path: Path, entries: str, count_frames: bool = False) -> dict[str, str]:
    command = [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", entries, "-of", "default=nw=1"]
    if count_frames:
        command.insert(1, "-count_frames")
    out = subprocess.run(command + [str(path)], capture_output=True, text=True).stdout
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


# --------------------------------------------------------------------------
# The capture
# --------------------------------------------------------------------------


def step_one_tick(console: Console) -> int | None:
    """Play one tick, then pause. Returns the demo tick, or None if CS2 did not confirm."""
    start = console.mark()
    console.send("demo_step_tick 1")
    try:
        return int(console.wait_for(STEP_DONE, 8, start)[1].group(1))
    except CaptureError:
        return None


def seek_and_hold(console: Console, seek_tick: int, player: str, settle: float) -> int:
    """Jump to a tick, lock the camera, and return the tick once the demo is provably paused."""
    console.send("demo_pause")
    console.sleep(0.5)
    start = console.mark()
    console.send(f"demo_gototick {seek_tick}")
    console.wait_for(rf"Demo Skipping finished at tick {seek_tick}\b", 180, start)
    # After "finished" CS2 still pauses, unpauses and pauses again on its own. A
    # command sent during that is lost and can leave the demo playing, so wait it out.
    try:
        console.wait_for(GAME_PAUSED, 3, start)
    except CaptureError:
        pass
    console.sleep(1.0)
    console.send("spec_mode 1")
    console.send(f'spec_player "{player}"')

    # CS2 saying "paused" is not proof. Two single-tick steps `settle` seconds
    # apart must land exactly one tick apart; the wait also lets textures load.
    for _ in range(4):
        first = step_one_tick(console)
        console.send(f'spec_player "{player}"')
        console.sleep(settle)
        second = step_one_tick(console)
        if first is not None and second == first + 1:
            return second
        console.send("demo_pause")
        console.sleep(1.0)
    raise CaptureError("the demo would not stay paused after seeking")


def capture(args: argparse.Namespace, work: Path) -> int:
    demo = Path(args.demo).resolve()
    if not demo.is_file():
        raise CaptureError(f"demo not found: {demo}")
    if '"' in args.player or ";" in args.player:
        raise CaptureError("player name may not contain a double quote or a semicolon")
    ffmpeg, ffprobe = need_tool("ffmpeg"), need_tool("ffprobe")
    steam = args.steam or find_steam()
    if cs2_running():
        raise CaptureError("CS2 is already running. Close it first; this script never closes a game it did not start.")

    out_path = Path(args.out).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path = work / "raw.mkv"

    start_tick = args.tick
    end_tick = start_tick + round(args.seconds * TICK_RATE)
    seek_tick = max(1, start_tick - round(args.preroll * TICK_RATE))
    frames_wanted = round(args.seconds * args.fps)

    password = secrets.token_hex(8)
    log("starting CS2 (-insecure, demo playback only)")
    subprocess.Popen([steam, "-applaunch", CS2_APP_ID, "-insecure", "-novid",
                      "-netconport", str(args.port), "-netconpassword", password])
    console = Console(args.port, password, timeout=180)
    recorder: Recorder | None = None
    focus_sleep: str | None = None
    try:
        console.ask("echo FRAGDIRECTOR_READY", r"FRAGDIRECTOR_READY", timeout=60)

        # The game ignores playdemo until the main menu is up, so repeat until it answers.
        log("loading demo")
        if not args.demo_ui:
            # No playback bar (only works if set before the demo loads) and no
            # "TrueView" status label. CS2 does not save either setting.
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

        # Full frame rate even when the game window is not in front; restored at the end.
        focus_sleep = console.ask("engine_no_focus_sleep", r"engine_no_focus_sleep = (\S+)").group(1)
        console.send("engine_no_focus_sleep 0")

        for attempt in range(1, ATTEMPTS + 1):
            log(f"seeking to tick {seek_tick} ({args.preroll:g}s before the shot)")
            current_tick = seek_and_hold(console, seek_tick, args.player, args.settle)
            if current_tick >= start_tick:
                log(f"attempt {attempt}: demo ran on to tick {current_tick}, past the shot start; seeking again")
                continue

            log(f"holding at tick {current_tick}; recording window")
            recorder = Recorder(ffmpeg, raw_path, args.fps)
            recorder.wait_until_recording()

            start = console.mark()
            console.send(f"demo_step_tick {end_tick - current_tick}")
            t_sent = time.time()
            play_seconds = (end_tick - current_tick) / TICK_RATE
            t_paused, match = console.wait_for(STEP_DONE, play_seconds * 3 + 30, start)
            final_tick = int(match.group(1))
            try:  # CS2 does not always print this line, so it is only used when present
                t_unpaused: float | None = console.wait_for(UNPAUSED, 0, start)[0]
            except CaptureError:
                t_unpaused = None
            console.sleep(0.5)
            recorder.stop()
            if abs(final_tick - end_tick) <= 2:
                break
            log(f"attempt {attempt}: playback ended at tick {final_tick}, expected {end_tick}; trying again")
        else:
            raise CaptureError(f"no clean capture in {ATTEMPTS} attempts")
    except CaptureError:
        console.dump_tail()
        raise
    finally:
        if recorder is not None:
            recorder.stop()
        try:
            if focus_sleep is not None:
                console.send(f"engine_no_focus_sleep {focus_sleep}")
            console.send("quit")
            console.sleep(1)
        except (CaptureError, OSError):
            pass
        console.close()

    # Find the wall-clock time of the shot's first tick, counting back from the
    # moment CS2 reported the final pause, then cut the recording there.
    played = final_tick - current_tick
    if t_unpaused is not None:  # both ends known: measure the real playback speed
        seconds_per_tick = (t_paused - t_unpaused) / played
        drift = (t_paused - t_unpaused) - played / TICK_RATE
    else:  # only the end known: assume real time, estimate drift from the send time
        seconds_per_tick = 1 / TICK_RATE
        drift = (t_paused - t_sent) - played / TICK_RATE
    shot_start = t_paused - (final_tick - start_tick) * seconds_per_tick
    raw_info = probe(ffprobe, raw_path, "format=start_time")
    offset = shot_start - recorder.base - float(raw_info["start_time"])
    if offset < 0:
        raise CaptureError("recording started after the shot began")
    log(f"played ticks {current_tick}..{final_tick}, playback drift {drift * 1000:+.0f} ms; cutting at {offset:.3f}s")

    cut = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(raw_path),
         "-ss", f"{offset:.4f}", "-vf", f"fps={args.fps}", "-frames:v", str(frames_wanted),
         "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
         "-movflags", "+faststart", "-an", str(out_path)],
        capture_output=True, text=True,
    )
    if cut.returncode != 0:
        raise CaptureError("FFmpeg cut failed: " + cut.stderr[-600:])

    info = probe(ffprobe, out_path, "stream=width,height,nb_read_frames:format=duration", count_frames=True)
    frames = int(info.get("nb_read_frames", "0"))
    log(f"wrote {out_path}: {info.get('width')}x{info.get('height')}, {frames} frames, {float(info.get('duration', 0)):.2f}s")
    if abs(drift) > 0.25:
        log("WARNING: demo playback was not real time during capture; the clip may be off")
    if frames != frames_wanted:
        raise CaptureError(f"expected {frames_wanted} frames, got {frames}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture one POV clip from a CS2 demo (T4 spike).")
    parser.add_argument("demo", help="path to the .dem file")
    parser.add_argument("--player", required=True, help="player name exactly as in the demo")
    parser.add_argument("--tick", type=int, required=True, help="demo tick where the clip starts")
    parser.add_argument("--seconds", type=float, default=10.0, help="clip length (default 10)")
    parser.add_argument("--out", default="capture_one.mp4", help="output file (default capture_one.mp4)")
    parser.add_argument("--fps", type=int, default=60)
    parser.add_argument("--preroll", type=float, default=2.0, help="seconds played before the shot so the scene settles")
    parser.add_argument("--settle", type=float, default=2.0, help="seconds to wait after seeking for textures to load")
    parser.add_argument("--port", type=int, default=2121, help="local TCP port for the CS2 console")
    parser.add_argument("--steam", help="path to steam.exe (default: from the registry)")
    parser.add_argument("--keep-raw", action="store_true", help="keep the untrimmed recording")
    parser.add_argument("--demo-ui", action="store_true", help="keep the playback bar visible (shows the tick, for checking)")
    args = parser.parse_args()
    if sys.platform != "win32":
        print("capture_one.py only runs on Windows", file=sys.stderr)
        return 2
    work = Path(tempfile.mkdtemp(prefix="fragdirector_capture_"))  # holds the untrimmed recording
    try:
        return capture(args, work)
    except CaptureError as exc:
        print(f"[capture] FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        if args.keep_raw:
            log(f"untrimmed recording kept in {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
