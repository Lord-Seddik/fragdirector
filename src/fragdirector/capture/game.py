"""Talking to CS2 and FFmpeg: the parts that need the real game.

``Console`` is the TCP console (``-netconport``). ``GameSession`` drives one
running game through a whole list of clips: load a demo, seek, lock the
view, play an exact range. ``Recorder`` captures the game window. The
method and every trap it works around are in ``docs/capture.md``.
"""

from __future__ import annotations

import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Protocol

from fragdirector.capture.plan import (
    DEMO_LOADED,
    DEMO_REQUESTED,
    GAME_PAUSED,
    NVENC,
    READY_WORD,
    SAVED_SETTINGS,
    STEP_DONE,
    UNPAUSED,
    X264_FAST,
    CaptureError,
    CaptureOptions,
    Playback,
    before_demo_commands,
    playdemo_command,
    record_command,
    setting_value_pattern,
    spectate_commands,
)

Log = Callable[[str], None]
SEEK_SENDS = 3
SEEK_ANSWER_SECONDS = 5.0


class GameGone(CaptureError):
    """The game closed the console connection (it quit or crashed)."""


# --------------------------------------------------------------------------
# The console
# --------------------------------------------------------------------------


class ConsoleLike(Protocol):
    def send(self, command: str) -> None: ...
    def mark(self) -> int: ...
    def wait_for(self, pattern: str, timeout: float, start: int) -> tuple[float, re.Match]: ...
    def sleep(self, seconds: float) -> None: ...
    def ask(self, command: str, pattern: str, timeout: float = 10.0) -> re.Match: ...


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
                    raise CaptureError(f"The CS2 console did not open on port {port}: {exc}") from exc
                time.sleep(1)
        self.sock.settimeout(0.05)
        self.pending = b""
        self.lines: list[tuple[float, str]] = []
        self.send(f"PASS {password}")

    def send(self, command: str) -> None:
        try:
            self.sock.sendall(command.encode("utf-8") + b"\n")
        except OSError as exc:
            raise GameGone(f"CS2 closed the console connection: {exc}") from exc

    def pump(self) -> None:
        try:
            data = self.sock.recv(65536)
        except socket.timeout:
            return
        except OSError as exc:
            raise GameGone(f"CS2 closed the console connection: {exc}") from exc
        if not data:
            raise GameGone("CS2 closed the console connection")
        now = time.time()
        self.pending += data
        while b"\n" in self.pending:
            raw, self.pending = self.pending.split(b"\n", 1)
            self.lines.append((now, raw.decode("utf-8", "replace").rstrip("\r")))

    def mark(self) -> int:
        return len(self.lines)

    def wait_for(self, pattern: str, timeout: float, start: int) -> tuple[float, re.Match]:
        """Return (arrival time, match) of the first line from ``start`` on that matches."""
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
                raise CaptureError(f"CS2 did not print {pattern!r} within {timeout:.0f} s")
            self.pump()

    def sleep(self, seconds: float) -> None:
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.pump()

    def ask(self, command: str, pattern: str, timeout: float = 10.0) -> re.Match:
        start = self.mark()
        self.send(command)
        return self.wait_for(pattern, timeout, start)[1]

    def tail(self, count: int = 40) -> list[str]:
        """The last console lines, minus per-frame spam, to help diagnose a failure."""
        spam = ("TrueView", "Prediction", "UserCommands", "TrueSpectate")
        return [text[:200] for _, text in self.lines if not any(word in text for word in spam)][-count:]

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


# --------------------------------------------------------------------------
# One game session
# --------------------------------------------------------------------------


class GameSession:
    """Drives a running CS2 through the console. Knows nothing about files."""

    def __init__(self, console: ConsoleLike, log: Log) -> None:
        self.console = console
        self.log = log
        self.saved: dict[str, str] = {}  # settings changed, with their old values
        self.loaded_demo: Path | None = None
        self.paused_at: int | None = None  # demo tick while known to be paused

    def wait_ready(self, timeout: float = 60) -> None:
        self.console.ask(f"echo {READY_WORD}", READY_WORD, timeout=timeout)

    def load_demo(self, path: Path, options: CaptureOptions, timeout: float = 240) -> None:
        console = self.console
        for command in before_demo_commands(options):
            console.send(command)
        command = playdemo_command(path)
        start = console.mark()
        # The game ignores playdemo until the main menu is up (trap 3), so repeat it.
        deadline = time.time() + 180
        while True:
            console.send(command)
            try:
                console.wait_for(DEMO_REQUESTED, 5, start)
                break
            except GameGone:
                raise
            except CaptureError:
                if time.time() > deadline:
                    raise CaptureError("CS2 never accepted the playdemo command") from None
        console.wait_for(DEMO_LOADED, timeout, start)
        console.sleep(1)
        self.loaded_demo = path
        self.paused_at = None  # a freshly loaded demo plays

    def change_settings(self) -> None:
        """Apply ``SAVED_SETTINGS``, remembering the old values (trap 6)."""
        for name, value in SAVED_SETTINGS.items():
            if name in self.saved:
                continue
            try:
                old = self.console.ask(name, setting_value_pattern(name)).group(1)
            except GameGone:
                raise
            except CaptureError:
                self.log(f"could not read {name}; leaving it as it is")
                continue
            self.saved[name] = old
            self.console.send(f"{name} {value}")

    def restore_settings(self) -> None:
        for name, old in self.saved.items():
            self.console.send(f"{name} {old}")
        self.saved.clear()

    def quit(self) -> None:
        self.console.send("quit")
        self.console.sleep(1)

    # -- seeking and playing -----------------------------------------------

    def step_one_tick(self) -> int | None:
        """Play one tick, then pause. The demo tick, or None if CS2 did not confirm."""
        start = self.console.mark()
        self.console.send("demo_step_tick 1")
        try:
            return int(self.console.wait_for(STEP_DONE, 8, start)[1].group(1))
        except GameGone:
            raise
        except CaptureError:
            return None

    def seek_and_hold(self, seek_tick: int, player_name: str, settle: float) -> int:
        """Jump to a tick, lock the camera, and return the tick once the demo is
        provably paused (trap 1)."""
        console = self.console
        spectate = spectate_commands(player_name)
        if self.paused_at is None or seek_tick > self.paused_at:
            # A forward skip only advances while the demo plays; from a pause it
            # prints "skipping forward" and stays put. (Backward skips reload
            # from an earlier point and work while paused.) The pause check
            # below stops the demo again.
            console.send("demo_resume")
        else:
            console.send("demo_pause")
        console.sleep(0.5)
        self.paused_at = None
        start = console.mark()
        # Right after a demo loads, a seek can be dropped without a word; CS2
        # answers every seek it takes with a "Demo Skipping" line, so repeat
        # until it does.
        for _ in range(SEEK_SENDS):
            console.send(f"demo_gototick {seek_tick}")
            try:
                console.wait_for(r"Demo Skipping", SEEK_ANSWER_SECONDS, start)
                break
            except GameGone:
                raise
            except CaptureError:
                self.log(f"CS2 did not answer the seek to tick {seek_tick}; sending it again")
        console.wait_for(rf"Demo Skipping finished at tick {seek_tick}\b", 60, start)
        # After "finished" CS2 still pauses, unpauses and pauses again on its own;
        # a command sent during that is lost and can leave the demo playing.
        try:
            console.wait_for(GAME_PAUSED, 3, start)
        except GameGone:
            raise
        except CaptureError:
            pass
        console.sleep(1.0)
        for command in spectate:
            console.send(command)

        # "Paused" is not proof: two single-tick steps ``settle`` seconds apart
        # must land exactly one tick apart. The wait also lets textures load.
        for _ in range(4):
            first = self.step_one_tick()
            console.send(spectate[-1])
            console.sleep(settle)
            second = self.step_one_tick()
            if first is not None and second == first + 1:
                self.paused_at = second
                return second
            console.send("demo_pause")
            console.sleep(1.0)
        raise CaptureError("the demo would not stay paused after seeking")

    def play_to(self, end_tick: int, from_tick: int, tick_rate: int) -> Playback:
        """Play from the paused ``from_tick`` until ``end_tick`` and pause there."""
        console = self.console
        start = console.mark()
        console.send(f"demo_step_tick {end_tick - from_tick}")
        t_sent = time.time()
        seconds = (end_tick - from_tick) / tick_rate
        t_paused, match = console.wait_for(STEP_DONE, seconds * 3 + 30, start)
        try:  # trap 2: not always printed, so only used when present
            t_unpaused: float | None = console.wait_for(UNPAUSED, 0, start)[0]
        except GameGone:
            raise
        except CaptureError:
            t_unpaused = None
        final_tick = int(match.group(1))
        self.paused_at = final_tick
        return Playback(from_tick, final_tick, t_sent, t_paused, t_unpaused)


# --------------------------------------------------------------------------
# Programs outside Python
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
        raise CaptureError("Steam was not found in the registry; pass --steam") from exc
    if not Path(path).is_file():
        raise CaptureError(f"Steam was not found at {path}; pass --steam")
    return path


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def check_port_free(port: int) -> None:
    """Refuse a port another program listens on: the password would go to it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            raise CaptureError(f"Port {port} is already in use; pick another with --port.")


class Tools:
    """FFmpeg and FFprobe, checked once per run."""

    def __init__(self) -> None:
        self.ffmpeg = shutil.which("ffmpeg")
        self.ffprobe = shutil.which("ffprobe")
        missing = [name for name, path in (("ffmpeg", self.ffmpeg), ("ffprobe", self.ffprobe)) if not path]
        if missing:
            raise CaptureError(f"{' and '.join(missing)} not found on PATH (FFmpeg 8.0 or newer is needed).")
        filters = subprocess.run([self.ffmpeg, "-hide_banner", "-filters"], capture_output=True, text=True).stdout
        if not re.search(r"\bgfxcapture\b", filters):
            raise CaptureError("This FFmpeg has no gfxcapture (window capture); FFmpeg 8.0 or newer is needed.")
        self.encoder = self._pick_encoder()

    def _pick_encoder(self) -> list[str]:
        """GPU encoder if it works (keeps the CPU free for the game), else x264."""
        probe = subprocess.run(
            [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
             "color=black:s=256x256:r=30", "-frames:v", "5", "-c:v", "h264_nvenc", "-f", "null", "-"],
            capture_output=True,
        )
        return list(NVENC if probe.returncode == 0 else X264_FAST)

    def probe(self, path: Path, entries: str, count_frames: bool = False) -> dict[str, str]:
        command = [self.ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", entries,
                   "-of", "default=nw=1"]
        if count_frames:
            command.insert(1, "-count_frames")
        out = subprocess.run(command + [str(path)], capture_output=True, text=True).stdout
        return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


class Recorder:
    """FFmpeg capturing the CS2 window; frame times are wall clock minus ``base``."""

    def __init__(self, tools: Tools, raw_path: Path, fps: int) -> None:
        self.base = int(time.time())
        self.frames = 0
        command = record_command(tools.ffmpeg, raw_path, fps, self.base, tools.encoder)
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
                error = self.proc.stderr.read().decode("utf-8", "replace")[-600:]
                raise CaptureError("FFmpeg window capture stopped: " + error)
            if time.time() > deadline:
                raise CaptureError("FFmpeg window capture produced no frames (is the game window minimised?)")
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


def start_game(command: list[str]) -> None:
    if sys.platform != "win32":
        raise CaptureError("Capturing only works on Windows.")
    subprocess.Popen(command)
