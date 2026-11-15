"""Recording the game's sound: Windows per-process loopback of ``cs2.exe``.

Found in spike T20 (``docs/capture.md``, "Sound (spike T20)"). Only the
sound of the game's process tree is recorded, so music or a voice chat on
the same PC never reaches a clip. Standard library only (``ctypes``); needs
Windows 10 2004 (build 19041) or newer.

Every packet is kept with the wall-clock time of its first sample, taken
from the packet's performance-counter stamp. ``lay_out`` places the packets
on that time line, so the sound can be cut with the same timing as the
picture.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
import wave
from pathlib import Path
from typing import Iterable

from fragdirector.capture.plan import CaptureError

RATE = 48000
CHANNELS = 2
SAMPLE_BYTES = 2  # 16-bit PCM
FRAME_BYTES = CHANNELS * SAMPLE_BYTES
MIN_WINDOWS_BUILD = 19041  # Windows 10 2004: first with process loopback
JUMP_SECONDS = 0.005  # a packet further than this from where it belongs is a gap


def sound_support_problem() -> str | None:
    """Why the game's sound cannot be recorded on this PC, if it cannot."""
    if sys.platform != "win32":
        return "Recording the game's sound only works on Windows."
    build = sys.getwindowsversion().build
    if build < MIN_WINDOWS_BUILD:
        return (f"Recording the game's sound needs Windows 10 version 2004 (build {MIN_WINDOWS_BUILD}) "
                f"or newer; this is build {build}.")
    return None


def process_ids(image: str) -> list[int]:
    """Process ids of every running program with this file name."""
    out = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {image}", "/FO", "CSV", "/NH"], capture_output=True, text=True
    ).stdout
    pids = []
    for line in out.splitlines():
        parts = [part.strip('"') for part in line.split('","')]
        if len(parts) > 1 and parts[0].lower() == image.lower() and parts[1].isdigit():
            pids.append(int(parts[1]))
    return pids


# --------------------------------------------------------------------------
# Laying the packets out (pure)
# --------------------------------------------------------------------------


def lay_out(packets: Iterable[tuple[float, bytes]], rate: int = RATE,
            frame_bytes: int = FRAME_BYTES) -> tuple[bytes, list[tuple[float, float]]]:
    """Join ``(wall-clock time of first sample, PCM bytes)`` packets on their time line.

    Time zero is the first packet. Small timing jitter keeps the sound
    continuous; a packet more than ``JUMP_SECONDS`` from where the samples so
    far end is a jump: silence fills a gap, an overlap is cut back. Returns
    the PCM bytes and the jumps, as (seconds into the sound where the jump
    is, its size in seconds: more than 0 for a gap, less for an overlap).
    """
    out = bytearray()
    jumps: list[tuple[float, float]] = []
    base: float | None = None
    for stamp, chunk in packets:
        if base is None:
            base = stamp
        at = round((stamp - base) * rate) * frame_bytes
        if abs(at - len(out)) > JUMP_SECONDS * rate * frame_bytes:
            jumps.append((len(out) / frame_bytes / rate, (at - len(out)) / frame_bytes / rate))
            if at > len(out):
                out.extend(bytes(at - len(out)))
            else:
                del out[max(0, at):]
        out.extend(chunk)
    return bytes(out), jumps


def is_silent(pcm: bytes) -> bool:
    return not pcm.strip(b"\x00")


def write_wav(path: Path, pcm: bytes) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(CHANNELS)
        wav.setsampwidth(SAMPLE_BYTES)
        wav.setframerate(RATE)
        wav.writeframes(pcm)


# --------------------------------------------------------------------------
# Per-process loopback with WASAPI, through ctypes (Windows only)
# --------------------------------------------------------------------------

if sys.platform == "win32":
    import ctypes
    import uuid
    from ctypes import wintypes

    HRESULT = ctypes.c_long

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

        @classmethod
        def parse(cls, text: str) -> "GUID":
            guid = cls()
            ctypes.memmove(ctypes.byref(guid), uuid.UUID(text).bytes_le, 16)
            return guid

        def __eq__(self, other: object) -> bool:
            return isinstance(other, GUID) and bytes(self) == bytes(other)

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
    AUDCLNT_BUFFERFLAGS_SILENT = 0x2
    AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR = 0x4
    S_OK = 0
    E_NOINTERFACE = -2147467262  # 0x80004002
    BUFFER_100NS = 2_000_000  # 200 ms of buffer in the audio engine

    def _com_method(obj: ctypes.c_void_p, index: int, *argtypes):
        """The ``index``-th method of a COM object, callable with the object bound."""
        vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        function = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, *argtypes)(vtable[index])

        def call(*args):
            hr = function(obj, *args)
            if hr < 0:
                raise CaptureError(f"Windows audio call {index} failed with 0x{hr & 0xFFFFFFFF:08X}")
            return hr

        return call

    def _release(obj: ctypes.c_void_p) -> None:
        if obj:
            vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
            ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)(vtable[2])(obj)

    class _CompletionHandler:
        """A minimal IActivateAudioInterfaceCompletionHandler (also IAgileObject)."""

        QI = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))
        REF = ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)
        DONE = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, ctypes.c_void_p)

        def __init__(self, event) -> None:
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
        """Unix time from GetSystemTimePreciseAsFileTime (``time.time()`` is coarse on 3.12)."""
        filetime = ctypes.c_ulonglong()
        ctypes.windll.kernel32.GetSystemTimePreciseAsFileTime(ctypes.byref(filetime))
        return (filetime.value - 116444736000000000) / 1e7

    def _qpc_to_wall_offset() -> float:
        """Seconds to add to a performance-counter time (in seconds) to get unix time."""
        samples = []
        for _ in range(20):
            before = time.perf_counter()
            wall = precise_wall_clock()
            after = time.perf_counter()
            samples.append((after - before, wall - (before + after) / 2))
        samples.sort()
        return samples[0][1]  # the tightest pair


class SoundRecorder:
    """Records the sound of one process (and its children) to memory.

    Started per clip, like the picture recorder. ``packets`` holds
    ``(wall-clock time of the first sample, PCM bytes)``.
    """

    def __init__(self, pid: int) -> None:
        if sys.platform != "win32":
            raise CaptureError("Recording the game's sound only works on Windows.")
        self.pid = pid
        self.packets: list[tuple[float, bytes]] = []
        self.untimed = 0  # packets Windows gave no time for
        self.error: BaseException | None = None
        self._stop = threading.Event()
        self._started = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._started.wait(10):
            self._stop.set()
            raise CaptureError("The sound recorder did not start within 10 s.")
        if self.error:
            raise CaptureError(f"The game's sound cannot be recorded: {self.error}")

    def _run(self) -> None:
        ole32 = ctypes.windll.ole32
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateEventW.restype = wintypes.HANDLE
        ole32.CoInitializeEx(None, 0)  # multithreaded apartment
        client = ctypes.c_void_p()
        capture = ctypes.c_void_p()
        events: list[int] = []
        try:
            activated = kernel32.CreateEventW(None, True, False, None)
            events.append(activated)
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
            _com_method(operation, 3, ctypes.POINTER(HRESULT), ctypes.POINTER(ctypes.c_void_p))(
                ctypes.byref(result), ctypes.byref(client)
            )
            _release(operation)
            if result.value < 0:
                raise CaptureError(f"process loopback activation failed with 0x{result.value & 0xFFFFFFFF:08X}")

            # Process loopback has no mix format: ask for one, Windows converts.
            fmt = WAVEFORMATEX(1, CHANNELS, RATE, RATE * FRAME_BYTES, FRAME_BYTES, SAMPLE_BYTES * 8, 0)
            _com_method(client, 3, ctypes.c_int, wintypes.DWORD, ctypes.c_longlong, ctypes.c_longlong,
                        ctypes.POINTER(WAVEFORMATEX), ctypes.c_void_p)(
                AUDCLNT_SHAREMODE_SHARED,
                AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK
                | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY,
                BUFFER_100NS, 0, ctypes.byref(fmt), None,
            )
            ready = kernel32.CreateEventW(None, False, False, None)
            events.append(ready)
            _com_method(client, 13, wintypes.HANDLE)(wintypes.HANDLE(ready))  # SetEventHandle
            _com_method(client, 14, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))(  # GetService
                ctypes.byref(IID_IAudioCaptureClient), ctypes.byref(capture)
            )
            get_buffer = _com_method(
                capture, 3, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT),
                ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(ctypes.c_ulonglong),
                ctypes.POINTER(ctypes.c_ulonglong),
            )
            release_buffer = _com_method(capture, 4, wintypes.UINT)
            next_packet = _com_method(capture, 5, ctypes.POINTER(wintypes.UINT))

            offset = _qpc_to_wall_offset()
            _com_method(client, 10)()  # Start
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
                    if flags.value & AUDCLNT_BUFFERFLAGS_SILENT or not data.value:
                        chunk = bytes(count * FRAME_BYTES)
                    else:
                        chunk = ctypes.string_at(data.value, count * FRAME_BYTES)
                    release_buffer(count)
                    if qpc_pos.value and not flags.value & AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR:
                        stamp = qpc_pos.value / 1e7 + offset  # 100 ns units
                    else:  # no time from Windows: estimate from now, minus the packet length
                        self.untimed += 1
                        stamp = precise_wall_clock() - count / RATE
                    self.packets.append((stamp, chunk))
            _com_method(client, 11)()  # Stop
        except BaseException as exc:  # reported to the main thread
            self.error = exc
            self._started.set()
        finally:
            _release(capture)
            _release(client)
            for event in events:
                kernel32.CloseHandle(wintypes.HANDLE(event))
            ole32.CoUninitialize()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(5)
        if self._thread.is_alive():
            raise CaptureError("The sound recorder did not stop.")
        if self.error:
            raise CaptureError(f"The game's sound recording failed: {self.error}")
