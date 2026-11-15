"""T20 sound spike: one clip of one player's view from a CS2 demo, with the game's sound.

    python spikes/capture_sound.py demos/x.dem --player sedx --tick 23200

Same flow as `capture_one.py` (T4), plus a recorder for the sound of
`cs2.exe` alone: Windows' per-process loopback (Windows 10 2004 / build 19041
or newer), so other programs on the PC are not recorded. Every audio packet
carries the performance-counter time of its first sample; that time is turned
into wall-clock time so the sound can be cut with the same timing as the
picture.

After the capture the script measures the sync: it reads the player's
`weapon_fire` ticks from the demo and, for the first shot of each burst,
compares the first frame with a muzzle flash to the first loud millisecond
of sound. The flash finder can pick the wrong frame; check a few by eye.
`--test-tone` plays a 1 kHz tone from another process during the capture
and reports whether it reached the clip.

Needs what `capture_one.py` needs, plus `demoparser2` and `numpy` for the
measurement and `sounddevice` for `--test-tone`. How and why: docs/capture.md.
"""

from __future__ import annotations

import argparse
import ctypes
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from capture_one import (  # noqa: E402  (spike: reuse the T4 pieces)
    CS2_APP_ID,
    DEMO_LOADED,
    DEMO_REQUESTED,
    STEP_DONE,
    TICK_RATE,
    UNPAUSED,
    CaptureError,
    Console,
    Recorder,
    cs2_running,
    find_steam,
    log,
    need_tool,
    probe,
    seek_and_hold,
)

ATTEMPTS = 3
PICTURE_DELAY = 0.060  # T4/T12: the picture trails the tick by about 60 ms
RATE = 48000
CHANNELS = 2
SAMPLE_BYTES = 2  # 16-bit PCM
FRAME_BYTES = CHANNELS * SAMPLE_BYTES

# Game settings changed for the capture and put back afterwards.
SAVED_SETTINGS = {
    "engine_no_focus_sleep": "0",  # full frame rate when the window is not in front
    "snd_mute_losefocus": "0",  # keep playing sound when the window is not in front
}


# --------------------------------------------------------------------------
# Per-process loopback with WASAPI, through ctypes
# --------------------------------------------------------------------------


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def parse(cls, text: str) -> GUID:
        import uuid

        raw = uuid.UUID(text).bytes_le
        guid = cls()
        ctypes.memmove(ctypes.byref(guid), raw, 16)
        return guid

    def __eq__(self, other: object) -> bool:
        return isinstance(other, GUID) and bytes(self) == bytes(other)


IID_IUnknown = GUID.parse("00000000-0000-0000-C000-000000000046")
IID_IAgileObject = GUID.parse("94ea2b94-e9cc-49e0-c0ff-ee64ca8f5b90")
IID_IActivateAudioInterfaceCompletionHandler = GUID.parse("41D949AB-9862-444A-80F6-C261334DA5EB")
IID_IAudioClient = GUID.parse("1CB9AD4C-DBFA-4c32-B178-C2F568A703B2")
IID_IAudioCaptureClient = GUID.parse("C8ADBD64-E71E-48a0-A4DE-185C395CD317")

VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback"
AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0
VT_BLOB = 65
AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000
AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY = 0x1
AUDCLNT_BUFFERFLAGS_SILENT = 0x2
AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR = 0x4
S_OK = 0
E_NOINTERFACE = -2147467262  # 0x80004002


class AUDIOCLIENT_ACTIVATION_PARAMS(ctypes.Structure):
    _fields_ = [
        ("ActivationType", ctypes.c_int),
        ("TargetProcessId", wintypes.DWORD),
        ("ProcessLoopbackMode", ctypes.c_int),
    ]


class BLOB(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.ULONG), ("pBlobData", ctypes.c_void_p)]


class PROPVARIANT(ctypes.Structure):
    _fields_ = [
        ("vt", wintypes.USHORT),
        ("wReserved1", wintypes.WORD),
        ("wReserved2", wintypes.WORD),
        ("wReserved3", wintypes.WORD),
        ("blob", BLOB),
    ]


class WAVEFORMATEX(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("wFormatTag", wintypes.WORD),
        ("nChannels", wintypes.WORD),
        ("nSamplesPerSec", wintypes.DWORD),
        ("nAvgBytesPerSec", wintypes.DWORD),
        ("nBlockAlign", wintypes.WORD),
        ("wBitsPerSample", wintypes.WORD),
        ("cbSize", wintypes.WORD),
    ]


HRESULT = ctypes.c_long


def com_method(obj: ctypes.c_void_p, index: int, *argtypes):
    """The `index`-th method of a COM object, callable with the object bound."""
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    prototype = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, *argtypes)
    function = prototype(vtable[index])

    def call(*args):
        hr = function(obj, *args)
        if hr < 0:
            raise CaptureError(f"audio: COM method {index} failed with 0x{hr & 0xFFFFFFFF:08X}")
        return hr

    return call


def release(obj: ctypes.c_void_p) -> None:
    if obj:
        vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)(vtable[2])(obj)


class _CompletionHandler:
    """A minimal IActivateAudioInterfaceCompletionHandler (also IAgileObject)."""

    QI = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))
    REF = ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)
    DONE = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, ctypes.c_void_p)

    def __init__(self, event: wintypes.HANDLE) -> None:
        self.event = event
        self._fns = (self.QI(self._qi), self.REF(self._ref), self.REF(self._ref), self.DONE(self._done))
        self._vtable = (ctypes.c_void_p * 4)(*[ctypes.cast(f, ctypes.c_void_p) for f in self._fns])
        self._object = ctypes.c_void_p(ctypes.addressof(self._vtable))
        self.pointer = ctypes.c_void_p(ctypes.addressof(self._object))

    def _qi(self, this, riid, ppv):
        if riid[0] in (IID_IUnknown, IID_IAgileObject, IID_IActivateAudioInterfaceCompletionHandler):
            ppv[0] = this
            return S_OK
        ppv[0] = None
        return E_NOINTERFACE

    def _ref(self, this):
        return 1

    def _done(self, this, operation):
        ctypes.windll.kernel32.SetEvent(self.event)
        return S_OK


def precise_wall_clock() -> float:
    """Unix time from GetSystemTimePreciseAsFileTime (time.time() is coarse on 3.12)."""
    filetime = ctypes.c_ulonglong()
    ctypes.windll.kernel32.GetSystemTimePreciseAsFileTime(ctypes.byref(filetime))
    return (filetime.value - 116444736000000000) / 1e7


def qpc_to_wall_offset() -> float:
    """Seconds to add to a performance-counter time (in seconds) to get unix time."""
    samples = []
    for _ in range(20):
        before = time.perf_counter()
        wall = precise_wall_clock()
        after = time.perf_counter()
        samples.append((after - before, wall - (before + after) / 2))
    samples.sort()
    return samples[0][1]  # the tightest pair


def process_ids(image: str) -> list[int]:
    out = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {image}", "/FO", "CSV", "/NH"], capture_output=True, text=True
    ).stdout
    pids = []
    for line in out.splitlines():
        parts = [part.strip('"') for part in line.split('","')]
        if len(parts) > 1 and parts[0].lower() == image.lower():
            pids.append(int(parts[1]))
    return pids


class SoundRecorder:
    """Records the sound of one process (and its children) to memory.

    Each packet is kept with the wall-clock time of its first sample;
    `write_wav` lays the packets out on that time line, so gaps (CS2 plays
    nothing while paused) become silence at the right place.
    """

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.packets: list[tuple[float, bytes]] = []
        self.flags_seen = 0
        self.qpc_missing = 0
        self.error: BaseException | None = None
        self._stop = threading.Event()
        self._started = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._started.wait(10):
            raise CaptureError("audio: the recorder did not start within 10 s")
        if self.error:
            raise CaptureError(f"audio: {self.error}")

    def _run(self) -> None:
        ole32 = ctypes.windll.ole32
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateEventW.restype = wintypes.HANDLE
        ole32.CoInitializeEx(None, 0)  # multithreaded apartment
        client = ctypes.c_void_p()
        capture = ctypes.c_void_p()
        try:
            activated = kernel32.CreateEventW(None, True, False, None)
            handler = _CompletionHandler(activated)
            params = AUDIOCLIENT_ACTIVATION_PARAMS(
                AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK,
                self.pid,
                PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE,
            )
            variant = PROPVARIANT()
            variant.vt = VT_BLOB
            variant.blob.cbSize = ctypes.sizeof(params)
            variant.blob.pBlobData = ctypes.cast(ctypes.pointer(params), ctypes.c_void_p)

            activate = ctypes.windll.mmdevapi.ActivateAudioInterfaceAsync
            activate.restype = HRESULT
            activate.argtypes = [
                wintypes.LPCWSTR, ctypes.POINTER(GUID), ctypes.POINTER(PROPVARIANT),
                ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
            ]
            operation = ctypes.c_void_p()
            hr = activate(
                VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK, ctypes.byref(IID_IAudioClient),
                ctypes.byref(variant), handler.pointer, ctypes.byref(operation),
            )
            if hr < 0:
                raise CaptureError(f"ActivateAudioInterfaceAsync failed with 0x{hr & 0xFFFFFFFF:08X}")
            if kernel32.WaitForSingleObject(wintypes.HANDLE(activated), 10000) != 0:
                raise CaptureError("process loopback activation never completed")
            result = HRESULT()
            com_method(operation, 3, ctypes.POINTER(HRESULT), ctypes.POINTER(ctypes.c_void_p))(
                ctypes.byref(result), ctypes.byref(client)
            )
            release(operation)
            if result.value < 0:
                raise CaptureError(f"process loopback activation failed with 0x{result.value & 0xFFFFFFFF:08X}")

            fmt = WAVEFORMATEX(1, CHANNELS, RATE, RATE * FRAME_BYTES, FRAME_BYTES, SAMPLE_BYTES * 8, 0)
            com_method(client, 3, ctypes.c_int, wintypes.DWORD, ctypes.c_longlong, ctypes.c_longlong,
                       ctypes.POINTER(WAVEFORMATEX), ctypes.c_void_p)(
                AUDCLNT_SHAREMODE_SHARED,
                AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK
                | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY,
                2_000_000, 0, ctypes.byref(fmt), None,
            )
            ready = kernel32.CreateEventW(None, False, False, None)
            com_method(client, 13, wintypes.HANDLE)(wintypes.HANDLE(ready))
            com_method(client, 14, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))(
                ctypes.byref(IID_IAudioCaptureClient), ctypes.byref(capture)
            )
            get_buffer = com_method(
                capture, 3, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT),
                ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(ctypes.c_ulonglong),
                ctypes.POINTER(ctypes.c_ulonglong),
            )
            release_buffer = com_method(capture, 4, wintypes.UINT)
            next_packet = com_method(capture, 5, ctypes.POINTER(wintypes.UINT))

            offset = qpc_to_wall_offset()
            com_method(client, 10)()  # Start
            self._started.set()

            data = ctypes.c_void_p()
            frames = wintypes.UINT()
            flags = wintypes.DWORD()
            device_pos = ctypes.c_ulonglong()
            qpc_pos = ctypes.c_ulonglong()
            size = wintypes.UINT()
            while not self._stop.is_set():
                kernel32.WaitForSingleObject(wintypes.HANDLE(ready), 100)
                while True:
                    next_packet(ctypes.byref(size))
                    if size.value == 0:
                        break
                    get_buffer(ctypes.byref(data), ctypes.byref(frames), ctypes.byref(flags),
                               ctypes.byref(device_pos), ctypes.byref(qpc_pos))
                    count = frames.value
                    self.flags_seen |= flags.value
                    if flags.value & AUDCLNT_BUFFERFLAGS_SILENT or not data.value:
                        chunk = bytes(count * FRAME_BYTES)
                    else:
                        chunk = ctypes.string_at(data.value, count * FRAME_BYTES)
                    release_buffer(count)
                    if qpc_pos.value and not flags.value & AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR:
                        stamp = qpc_pos.value / 1e7 + offset
                    else:  # no time from Windows: estimate from now, minus the packet length
                        self.qpc_missing += 1
                        stamp = precise_wall_clock() - count / RATE
                    self.packets.append((stamp, chunk))
            com_method(client, 11)()  # Stop
        except BaseException as exc:  # reported to the main thread
            self.error = exc
            self._started.set()
        finally:
            release(capture)
            release(client)
            ole32.CoUninitialize()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(5)
        if self.error:
            raise CaptureError(f"audio: {self.error}")

    @property
    def start_time(self) -> float:
        if not self.packets:
            raise CaptureError("audio: no sound packets were recorded")
        return self.packets[0][0]

    def write_wav(self, path: Path) -> tuple[float, int]:
        """Write the recording on its time line; returns (start time, packets placed by jumps)."""
        base = self.start_time
        out = bytearray()
        jumps = 0
        for stamp, chunk in self.packets:
            at = round((stamp - base) * RATE) * FRAME_BYTES
            # Small timing jitter: keep the sound continuous. A real gap: pad with silence.
            if abs(at - len(out)) > 0.005 * RATE * FRAME_BYTES:
                jumps += 1
                if at > len(out):
                    out.extend(bytes(at - len(out)))
                else:
                    del out[at:]
            out.extend(chunk)
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(CHANNELS)
            wav.setsampwidth(SAMPLE_BYTES)
            wav.setframerate(RATE)
            wav.writeframes(bytes(out))
        return base, jumps


# --------------------------------------------------------------------------
# The capture
# --------------------------------------------------------------------------


def start_test_tone(seconds: float) -> subprocess.Popen:
    """Play a 1 kHz tone from a separate process (stands in for music or Discord)."""
    code = (
        "import numpy as np, sounddevice as sd;"
        f"t=np.arange(int(48000*{seconds}))/48000;"
        "sd.play((0.2*np.sin(2*np.pi*1000*t)).astype('float32'), 48000, blocking=True)"
    )
    return subprocess.Popen([sys.executable, "-c", code])


def capture(args: argparse.Namespace, work: Path) -> dict:
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
    wav_path = work / "raw.wav"

    start_tick = args.tick
    end_tick = start_tick + round(args.seconds * TICK_RATE)
    seek_tick = max(1, start_tick - round(args.preroll * TICK_RATE))
    frames_wanted = round(args.seconds * args.fps)

    import secrets

    password = secrets.token_hex(16)
    log("starting CS2 (-insecure, demo playback only)")
    subprocess.Popen([steam, "-applaunch", CS2_APP_ID, "-insecure", "-novid",
                      "-netconport", str(args.port), "-netconpassword", password])
    console = Console(args.port, password, timeout=180)
    recorder: Recorder | None = None
    sound: SoundRecorder | None = None
    tone: subprocess.Popen | None = None
    old_settings: dict[str, str] = {}
    try:
        console.ask("echo FRAGDIRECTOR_READY", r"FRAGDIRECTOR_READY", timeout=60)
        pids = process_ids("cs2.exe")
        if not pids:
            raise CaptureError("cs2.exe not found in the process list")
        log(f"cs2.exe process id {pids[0]}")

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

        for name, value in SAVED_SETTINGS.items():
            try:
                old_settings[name] = console.ask(name, rf"{name} = (\S+)").group(1)
            except CaptureError:
                log(f"could not read {name}; leaving it alone")
                continue
            console.send(f"{name} {value}")
            log(f"{name}: {old_settings[name]} -> {value}")
        for name in ("volume", "snd_musicvolume", "snd_toolvolume"):
            try:
                log(f"{name} = {console.ask(name, rf'{name} = (\S+)').group(1)}")
            except CaptureError:
                log(f"{name}: no answer")

        for attempt in range(1, ATTEMPTS + 1):
            log(f"seeking to tick {seek_tick} ({args.preroll:g}s before the shot)")
            current_tick = seek_and_hold(console, seek_tick, args.player, args.settle)
            if current_tick >= start_tick:
                log(f"attempt {attempt}: demo ran on to tick {current_tick}, past the shot start; seeking again")
                continue

            log(f"holding at tick {current_tick}; recording window and sound")
            sound = SoundRecorder(pids[0])
            recorder = Recorder(ffmpeg, raw_path, args.fps)
            recorder.wait_until_recording()
            if args.test_tone:
                tone = start_test_tone((end_tick - current_tick) / TICK_RATE + 4)

            start = console.mark()
            console.send(f"demo_step_tick {end_tick - current_tick}")
            t_sent = time.time()
            play_seconds = (end_tick - current_tick) / TICK_RATE
            t_paused, match = console.wait_for(STEP_DONE, play_seconds * 3 + 30, start)
            final_tick = int(match.group(1))
            try:
                t_unpaused: float | None = console.wait_for(UNPAUSED, 0, start)[0]
            except CaptureError:
                t_unpaused = None
            console.sleep(0.5)
            recorder.stop()
            sound.stop()
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
        if sound is not None:
            try:
                sound.stop()
            except CaptureError:
                pass
        if tone is not None:
            tone.kill()
        try:
            for name, value in old_settings.items():
                console.send(f"{name} {value}")
            console.send("quit")
            console.sleep(1)
        except (CaptureError, OSError):
            pass
        console.close()

    played = final_tick - current_tick
    if t_unpaused is not None:
        seconds_per_tick = (t_paused - t_unpaused) / played
        drift = (t_paused - t_unpaused) - played / TICK_RATE
    else:
        seconds_per_tick = 1 / TICK_RATE
        drift = (t_paused - t_sent) - played / TICK_RATE
    shot_start = t_paused - (final_tick - start_tick) * seconds_per_tick
    raw_info = probe(ffprobe, raw_path, "format=start_time")
    video_offset = shot_start + PICTURE_DELAY - recorder.base - float(raw_info["start_time"])
    if video_offset < 0:
        raise CaptureError("recording started after the shot began")

    audio_start, jumps = sound.write_wav(wav_path)
    audio_offset = shot_start + args.audio_delay - audio_start
    if audio_offset < 0:
        raise CaptureError("sound recording started after the shot began")
    log(f"played ticks {current_tick}..{final_tick}, playback drift {drift * 1000:+.0f} ms")
    log(f"sound: {len(sound.packets)} packets, {jumps} gaps or overlaps, "
        f"{sound.qpc_missing} without a timestamp, flags seen 0x{sound.flags_seen:x}")
    log(f"cutting picture at {video_offset:.3f}s, sound at {audio_offset:.3f}s")

    # Two steps: an output -ss would also move the sound, so the picture is cut
    # on its own exactly as in T4/T12, then the sound is cut and added.
    seconds = frames_wanted / args.fps
    video_only = work / "video.mp4"
    cut = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(raw_path),
         "-ss", f"{video_offset:.4f}", "-vf", f"fps={args.fps}", "-frames:v", str(frames_wanted),
         "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
         "-an", str(video_only)],
        capture_output=True, text=True,
    )
    if cut.returncode != 0:
        raise CaptureError("FFmpeg cut failed: " + cut.stderr[-600:])
    mux = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(video_only),
         "-ss", f"{audio_offset:.4f}", "-i", str(wav_path),  # input seek on a WAV is sample-exact
         "-map", "0:v", "-map", "1:a", "-c:v", "copy",
         "-c:a", "aac", "-b:a", "192k", "-af", "apad", "-t", f"{seconds:.4f}",
         "-movflags", "+faststart", str(out_path)],
        capture_output=True, text=True,
    )
    if mux.returncode != 0:
        raise CaptureError("FFmpeg mux failed: " + mux.stderr[-600:])
    return {
        "demo": demo, "start_tick": start_tick, "end_tick": end_tick, "seconds": seconds,
        "drift": drift, "jumps": jumps, "frames_wanted": frames_wanted,
    }


# --------------------------------------------------------------------------
# Checking the result
# --------------------------------------------------------------------------


def clip_audio(ffmpeg: str, path: Path):
    import numpy as np

    raw = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(path), "-map", "0:a",
         "-ac", "1", "-ar", str(RATE), "-f", "f32le", "-"],
        capture_output=True,
    ).stdout
    return np.frombuffer(raw, dtype=np.float32)


def clip_frames(ffmpeg: str, path: Path):
    """Brightness of the area around the muzzle (lower middle-right), per frame."""
    import numpy as np

    width, height = 480, 270
    raw = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(path),
         "-vf", f"scale={width}:{height},format=gray", "-f", "rawvideo", "-"],
        capture_output=True,
    ).stdout
    frames = np.frombuffer(raw, np.uint8).reshape(-1, height, width).astype(float)
    return frames[:, int(height * 0.40):int(height * 0.72), int(width * 0.45):int(width * 0.70)].mean(axis=(1, 2))


def measure_sync(ffmpeg: str, path: Path, info: dict, player: str, fps: int) -> None:
    """For the first shot of each burst: first muzzle-flash frame vs first sound of the shot.

    Only burst starts are used: inside a spray the next shot's flash and sound
    would be mixed up with this one's. The player's own gun must be the
    loudest thing then; a check by eye of a few frames is still advised.
    """
    import numpy as np
    from demoparser2 import DemoParser

    fires = DemoParser(str(info["demo"])).parse_event("weapon_fire", player=["name"])
    fires = fires[fires["user_name"] == player]
    ticks = sorted(t for t in fires["tick"].tolist() if info["start_tick"] <= t < info["end_tick"])
    starts = [t for i, t in enumerate(ticks) if i == 0 or t - ticks[i - 1] > 0.3 * TICK_RATE]
    samples = clip_audio(ffmpeg, path)
    log(f"check: clip sound {len(samples) / RATE:.3f}s, peak {np.abs(samples).max() if len(samples) else 0:.3f}; "
        f"{len(ticks)} shots by {player} in the clip, {len(starts)} burst starts")
    if not starts or not len(samples):
        return
    hop = RATE // 1000
    count = len(samples) // hop
    envelope = np.sqrt((samples[: count * hop].reshape(count, hop) ** 2).mean(axis=1))
    bright = clip_frames(ffmpeg, path)
    differences = []
    for tick in starts:
        at = (tick - info["start_tick"]) / TICK_RATE
        # Picture: first frame within -0.2 s..+0.15 s whose muzzle area is more than
        # halfway from the brightness before to the brightest frame in that window.
        first, last = max(1, int((at - 0.2) * fps)), min(len(bright) - 1, int((at + 0.15) * fps))
        base = np.median(bright[max(0, first - 10):first])
        peak = bright[first:last].max()
        flash = next((i for i in range(first, last) if bright[i] - base > max(6, 0.5 * (peak - base))), None)
        # Sound: first millisecond within -0.15 s..+0.25 s louder than half the
        # loudest millisecond there (the gunshot) and 4x the quiet before.
        lo, hi = max(0, int((at - 0.15) * 1000)), min(len(envelope), int((at + 0.25) * 1000))
        quiet = np.median(envelope[max(0, lo - 100):lo]) if lo else envelope[0]
        loud = envelope[lo:hi].max()
        onset = next((i for i in range(lo, hi) if envelope[i] > max(4 * quiet, 0.5 * loud, 0.02)), None)
        if flash is None or onset is None:
            log(f"  burst at {at:.3f}s: flash {flash}, sound {onset}; not measurable")
            continue
        difference = onset / 1000 - flash / fps
        differences.append(difference)
        log(f"  burst at {at:.3f}s (fire tick {tick}): first flash frame {flash} ({flash / fps:.3f}s), "
            f"sound {onset / 1000:.3f}s, sound - picture {difference * 1000:+.0f} ms")
    if differences:
        log(f"check: sound - picture median {np.median(differences) * 1000:+.0f} ms over {len(differences)} bursts "
            f"(one frame is {1000 / fps:.1f} ms; a flash can only be seen to the frame)")


def tone_level(ffmpeg: str, path: Path) -> None:
    import numpy as np

    samples = clip_audio(ffmpeg, path)
    if not len(samples):
        return
    spectrum = np.abs(np.fft.rfft(samples * np.hanning(len(samples))))
    freqs = np.fft.rfftfreq(len(samples), 1 / RATE)
    at = spectrum[(freqs > 995) & (freqs < 1005)].max()
    around = np.median(spectrum[(freqs > 900) & (freqs < 1100)])
    log(f"check: 1 kHz test tone level {at / around:.1f}x its surroundings "
        f"({'PRESENT' if at / around > 20 else 'not present'} in the clip)")


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture one POV clip with sound from a CS2 demo (T20 spike).")
    parser.add_argument("demo", help="path to the .dem file")
    parser.add_argument("--player", required=True, help="player name exactly as in the demo")
    parser.add_argument("--tick", type=int, required=True, help="demo tick where the clip starts")
    parser.add_argument("--seconds", type=float, default=10.0, help="clip length (default 10)")
    parser.add_argument("--out", default="capture_sound.mp4", help="output file (default capture_sound.mp4)")
    parser.add_argument("--fps", type=int, default=60)
    parser.add_argument("--preroll", type=float, default=2.0)
    parser.add_argument("--settle", type=float, default=2.0)
    parser.add_argument("--audio-delay", type=float, default=0.12,
                        help="seconds the sound cut is moved later, so the sound plays earlier in the clip "
                             "(default 0.12, measured in T20: the sound reaches the recorder about 60 ms "
                             "after the picture, and the picture cut is itself moved 60 ms)")
    parser.add_argument("--port", type=int, default=2121)
    parser.add_argument("--steam", help="path to steam.exe (default: from the registry)")
    parser.add_argument("--keep-raw", action="store_true")
    parser.add_argument("--demo-ui", action="store_true")
    parser.add_argument("--test-tone", action="store_true",
                        help="play a 1 kHz tone from another process during the capture")
    args = parser.parse_args()
    if sys.platform != "win32":
        print("capture_sound.py only runs on Windows", file=sys.stderr)
        return 2
    work = Path(tempfile.mkdtemp(prefix="fragdirector_sound_"))
    try:
        info = capture(args, work)
        ffprobe = need_tool("ffprobe")
        out_path = Path(args.out).resolve()
        video = probe(ffprobe, out_path, "stream=width,height,nb_read_frames:format=duration", count_frames=True)
        audio = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries",
             "stream=codec_name,sample_rate,channels,duration", "-of", "default=nw=1", str(out_path)],
            capture_output=True, text=True,
        ).stdout.split()
        frames = int(video.get("nb_read_frames", "0"))
        log(f"wrote {out_path}: {video.get('width')}x{video.get('height')}, {frames} frames; audio {' '.join(audio)}")
        if frames != info["frames_wanted"]:
            raise CaptureError(f"expected {info['frames_wanted']} frames, got {frames}")
        ffmpeg = need_tool("ffmpeg")
        measure_sync(ffmpeg, out_path, info, args.player, args.fps)
        if args.test_tone:
            tone_level(ffmpeg, out_path)
        return 0
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
