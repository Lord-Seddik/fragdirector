"""Put the captured clips together into one video (T13).

Input is the shot list and the folder of clips ``fragdirector capture`` wrote
(each shot's clip is found with ``capture.clip_path``, so nothing else is
needed). One FFmpeg run cuts every clip to its length, applies the shot's
speed (slow motion repeats frames; fast motion drops them), optionally writes
the shot's label on the picture, joins the shots with a hard cut or a
crossfade, and encodes the result. The sound of each clip goes the same way
(T27): cut to the shot's length, slowed with ``atempo`` (same pitch), joined
with ``concat`` or ``acrossfade``; a clip without sound (an old v1 clip, or one
captured with ``--no-sound``) gets silence. ``RenderOptions.sound = False``
writes a video with no sound track.

Everything up to ``render_video`` is pure and testable without FFmpeg: the
timing sums, the filter graph and the command line. FFmpeg and FFprobe are
only run from ``render_video``. Arguments are passed as a list (never
through a shell), and label text is written to files, so a shot label cannot
change the FFmpeg command.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Callable

from fragdirector.capture.plan import clip_path, partial_path
from fragdirector.models import Shot, ShotList

Log = Callable[[str], None]

MAX_CROSSFADE_SECONDS = 5.0
MAX_FILTER_CHARS = 30000  # Windows refuses command lines near 32 000 characters
AUDIO_RATE = 48000
AUDIO_BITRATE = "192k"
AUDIO_TOLERANCE_SECONDS = 0.1  # how far the finished sound track may differ from the picture
FONT_SIZE_DIVISOR = 24  # label height is the picture height / 24
FONT_CANDIDATES = (
    "{SYSTEMROOT}/Fonts/arial.ttf",
    "{SYSTEMROOT}/Fonts/segoeui.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
)
_PROGRESS_KEYS = (
    "frame", "fps", "stream_", "bitrate", "total_size", "out_time", "dup_frames",
    "drop_frames", "speed", "progress",
)


class RenderError(RuntimeError):
    """The video cannot be made. The message is written for the user."""


@dataclass(frozen=True)
class RenderOptions:
    crossfade_seconds: float = 0.0  # 0 = hard cuts
    labels: bool = False  # write each shot's label on its first seconds
    label_seconds: float = 3.0
    crf: int = 18  # x264 quality; lower is better and bigger
    fps: int | None = None  # None = the frame rate of the first clip
    sound: bool = True  # carry the clips' sound (silence for a clip without); False = no sound track

    def __post_init__(self) -> None:
        if not math.isfinite(self.crossfade_seconds) or not 0 <= self.crossfade_seconds <= MAX_CROSSFADE_SECONDS:
            raise RenderError(
                f"The crossfade must be between 0 and {MAX_CROSSFADE_SECONDS:g} seconds, "
                f"got {self.crossfade_seconds}."
            )
        if not math.isfinite(self.label_seconds) or self.label_seconds <= 0:
            raise RenderError(f"label_seconds must be more than 0, got {self.label_seconds}.")
        if isinstance(self.crf, bool) or not isinstance(self.crf, int) or not 0 <= self.crf <= 51:
            raise RenderError(f"crf must be a whole number from 0 to 51, got {self.crf!r}.")
        if self.fps is not None and (
            isinstance(self.fps, bool) or not isinstance(self.fps, int) or not 1 <= self.fps <= 240
        ):
            raise RenderError(f"fps must be a whole number from 1 to 240, got {self.fps!r}.")


@dataclass(frozen=True)
class ClipInfo:
    """What FFprobe says about one clip file."""

    width: int
    height: int
    fps: Fraction
    seconds: float
    has_audio: bool = False


@dataclass(frozen=True)
class Segment:
    """One shot as it goes into the video."""

    position: int  # in the shot list, from 0
    clip: Path
    speed: float
    frames: int  # length in the video, at the output frame rate
    label: str
    width: int
    height: int
    source_seconds: float = 0.0  # how much of the clip the shot uses (before the speed)
    has_audio: bool = False  # the clip has a sound track


@dataclass(frozen=True)
class RenderResult:
    path: Path
    shots: int
    fps: int
    frames: int  # counted in the finished file
    seconds: float  # frames / fps
    expected_seconds: float
    audio_seconds: float | None = None  # length of the sound track; None = the video has none


# --------------------------------------------------------------------------
# Timing (pure)
# --------------------------------------------------------------------------


def shot_frames(shot: Shot, tick_rate: int, fps: int) -> int:
    """Frames the shot takes in the video: its on-screen seconds times ``fps``."""
    return max(1, round(shot.ticks / tick_rate / shot.speed * fps))


def crossfade_frames(options: RenderOptions, fps: int) -> int:
    if options.crossfade_seconds <= 0:
        return 0
    return max(1, round(options.crossfade_seconds * fps))


def video_frames(frames: list[int], fade: int) -> int:
    """Length of the whole video; each crossfade makes two shots overlap by ``fade`` frames."""
    if not frames:
        return 0
    return sum(frames) - fade * (len(frames) - 1)


def expected_seconds(shotlist: ShotList, options: RenderOptions) -> float:
    """What the video should last: the shot list's length less the crossfade overlaps.

    Without a crossfade this is exactly ``shotlist.duration_seconds()``."""
    overlaps = max(0, len(shotlist.shots) - 1) * options.crossfade_seconds
    return shotlist.duration_seconds() - overlaps


def check_crossfade(frames: list[int], fade: int, fps: int) -> None:
    """A crossfade eats into both neighbouring shots, so each must be at least twice as long."""
    if fade == 0:
        return
    for position, count in enumerate(frames, start=1):
        if count < 2 * fade:
            raise RenderError(
                f"Shot {position} lasts {count / fps:.2f} s in the video; a crossfade of "
                f"{fade / fps:.2f} s needs every shot to be at least twice as long. "
                f"Use a shorter crossfade or a longer shot."
            )


# --------------------------------------------------------------------------
# Planning (pure)
# --------------------------------------------------------------------------


def clip_candidates(shot: Shot, clips_dir: str | Path, prefer_sound: bool = True) -> list[Path]:
    """The file names a shot's clip may have, best first: the clip with sound
    (``_sound``) and the v1 silent name. Both hold the same picture."""
    order = (True, False) if prefer_sound else (False, True)
    return [clip_path(shot, clips_dir, sound) for sound in order]


def find_clip(shot: Shot, clips_dir: str | Path, prefer_sound: bool = True) -> Path:
    """The shot's clip file that exists, else the name capture would write
    (so a message can name it)."""
    candidates = clip_candidates(shot, clips_dir, prefer_sound)
    return next((path for path in candidates if path.is_file()), candidates[0])


def missing_clips(shotlist: ShotList, clips_dir: str | Path) -> list[int]:
    """Positions (from 0) of the shots that have no clip file yet (with or without sound)."""
    return [i for i, shot in enumerate(shotlist.shots) if not find_clip(shot, clips_dir).is_file()]


def missing_clips_message(missing: list[int], count: int) -> str:
    numbers = ", ".join(str(i + 1) for i in missing[:12]) + (", ..." if len(missing) > 12 else "")
    return (
        f"{len(missing)} of {count} shot(s) have no clip yet (shot {numbers}). "
        f"Capture them first: fragdirector capture <shots file>"
    )


def plan_segments(
    shotlist: ShotList,
    clips_dir: str | Path,
    clips: dict[Path, ClipInfo],
    fps: int,
    prefer_sound: bool = True,
) -> list[Segment]:
    segments = []
    for position, shot in enumerate(shotlist.shots):
        path = find_clip(shot, clips_dir, prefer_sound)
        info = clips[path]
        segments.append(Segment(
            position=position,
            clip=path,
            speed=shot.speed,
            frames=shot_frames(shot, shotlist.tick_rate, fps),
            label=" ".join(shot.label.split()),
            width=info.width,
            height=info.height,
            source_seconds=shot.ticks / shotlist.tick_rate,
            has_audio=info.has_audio,
        ))
    return segments


def _escape_filter_path(path: str) -> str:
    """A file path as one drawtext option value: forward slashes, ``:`` escaped, quoted."""
    return "'" + path.replace("\\", "/").replace(":", r"\:").replace("'", "") + "'"


def label_filter(label_file: str, font: str | None, height: int, seconds: float) -> str:
    """drawtext for the lower left corner, shown for the first ``seconds`` of the shot.

    The text comes from a file (``label_file``, relative to FFmpeg's working
    folder) and is not expanded, so no label can inject filter syntax."""
    size = max(12, round(height / FONT_SIZE_DIVISOR))
    options = [
        f"textfile={label_file}",
        "expansion=none",
        f"fontsize={size}",
        "fontcolor=white",
        "box=1",
        "boxcolor=black@0.55",
        f"boxborderw={max(4, size // 3)}",
        f"x=w*0.03",
        f"y=h*0.92-text_h",
        f"enable='lt(t,{seconds:.3f})'",
    ]
    if font:
        options.insert(1, f"fontfile={_escape_filter_path(font)}")
    return "drawtext=" + ":".join(options)


def tempo_steps(speed: float) -> list[str]:
    """``atempo`` steps for a speed: each step is kept within 0.5-2 (older FFmpeg
    builds refuse more). The pitch stays the same (decision of the v2 plan)."""
    steps = []
    remaining = speed
    while remaining > 2:
        steps.append("atempo=2")
        remaining /= 2
    while remaining < 0.5:
        steps.append("atempo=0.5")
        remaining *= 2
    steps.append(f"atempo={remaining:.10g}")
    return steps


def audio_chain(index: int, seg: Segment, fps: int) -> str:
    """One shot's sound, exactly as long as its picture (``[a<index>]``).

    Cut to the part of the clip the shot uses, slowed or sped up with the
    picture, then padded with silence or cut to the picture's length, so
    every shot's sound and picture are the same length and stay together."""
    seconds = seg.frames / fps
    if not seg.has_audio:
        return (f"anullsrc=r={AUDIO_RATE}:cl=stereo,atrim=end={seconds:.6f},"
                f"asetpts=PTS-STARTPTS[a{index}]")
    steps = [
        f"aformat=sample_fmts=fltp:sample_rates={AUDIO_RATE}:channel_layouts=stereo",
        f"atrim=end={seg.source_seconds:.6f}",
        "asetpts=PTS-STARTPTS",
    ]
    if seg.speed != 1:
        steps += tempo_steps(seg.speed)
    steps += [f"apad=whole_dur={seconds:.6f}", f"atrim=end={seconds:.6f}", "asetpts=PTS-STARTPTS"]
    return f"[{index}:a]" + ",".join(steps) + f"[a{index}]"


def audio_graph(segments: list[Segment], fps: int, fade: int) -> list[str]:
    """The sound half of the filter graph; its output is ``[aout]``. Joins like the picture:
    ``concat`` for hard cuts, ``acrossfade`` (the same overlap) for a crossfade."""
    chains = [audio_chain(index, seg, fps) for index, seg in enumerate(segments)]
    if len(segments) == 1:
        chains.append("[a0]anull[aout]")
    elif fade == 0:
        joined = "".join(f"[a{i}]" for i in range(len(segments)))
        chains.append(f"{joined}concat=n={len(segments)}:v=0:a=1[aout]")
    else:
        previous = "a0"
        for index in range(1, len(segments)):
            name = "aout" if index == len(segments) - 1 else f"ax{index}"
            chains.append(
                f"[{previous}][a{index}]acrossfade=d={fade / fps:.6f}:c1=tri:c2=tri[{name}]"
            )
            previous = name
    return chains


def filter_graph(
    segments: list[Segment],
    width: int,
    height: int,
    fps: int,
    options: RenderOptions,
    font: str | None = None,
    label_files: dict[int, str] | None = None,
) -> str:
    """The FFmpeg filter graph; its outputs are ``[vout]`` and, with ``options.sound``, ``[aout]``.

    ``label_files`` maps a shot position to the text file holding its label;
    shots without an entry get no label."""
    label_files = label_files or {}
    fade = crossfade_frames(options, fps)
    chains = []
    for index, seg in enumerate(segments):
        steps = []
        if (seg.width, seg.height) != (width, height):
            steps.append(f"scale={width}:{height}:force_original_aspect_ratio=decrease")
            steps.append(f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2")
        steps += [
            "setsar=1",
            f"setpts=(PTS-STARTPTS)/{seg.speed:.10g}",
            f"fps={fps}",
            f"trim=end_frame={seg.frames}",
            "setpts=PTS-STARTPTS",
        ]
        if index in label_files:
            steps.append(label_filter(label_files[index], font, height, options.label_seconds))
        steps.append("format=yuv420p")
        chains.append(f"[{index}:v]" + ",".join(steps) + f"[v{index}]")

    if len(segments) == 1:
        chains.append("[v0]null[vout]")
    elif fade == 0:
        joined = "".join(f"[v{i}]" for i in range(len(segments)))
        chains.append(f"{joined}concat=n={len(segments)}:v=1:a=0[vout]")
    else:
        length = segments[0].frames  # frames of the video built so far
        previous = "v0"
        for index in range(1, len(segments)):
            name = "vout" if index == len(segments) - 1 else f"x{index}"
            offset = (length - fade) / fps
            chains.append(
                f"[{previous}][v{index}]xfade=transition=fade:"
                f"duration={fade / fps:.6f}:offset={offset:.6f}[{name}]"
            )
            length += segments[index].frames - fade
            previous = name
    if options.sound:
        chains += audio_graph(segments, fps, fade)
    graph = ";".join(chains)
    if len(graph) > MAX_FILTER_CHARS:
        raise RenderError(f"The shot list has too many shots for one video ({len(segments)}); render it in parts.")
    return graph


def render_command(
    ffmpeg: str, segments: list[Segment], graph: str, out_path: Path, options: RenderOptions
) -> list[str]:
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostats", "-y"]
    for seg in segments:
        command += ["-i", str(seg.clip)]
    command += ["-filter_complex", graph, "-map", "[vout]"]
    if options.sound:
        command += ["-map", "[aout]"]
    command += ["-c:v", "libx264", "-preset", "medium", "-crf", str(options.crf), "-pix_fmt", "yuv420p"]
    if options.sound:
        command += ["-c:a", "aac", "-b:a", AUDIO_BITRATE, "-ar", str(AUDIO_RATE)]
    else:
        command += ["-an"]
    command += ["-movflags", "+faststart", "-f", "mp4", "-progress", "pipe:1", str(out_path)]
    return command


def check_output_path(out_path: Path, clips_dir: str | Path, shotlist: ShotList) -> None:
    if out_path.suffix.lower() != ".mp4":
        raise RenderError(f"The video file name must end in .mp4, got '{out_path.name}'.")
    if out_path.is_dir():
        raise RenderError(f"'{out_path}' is a folder.")
    resolved = out_path.resolve()
    for shot in shotlist.shots:
        for clip in clip_candidates(shot, clips_dir):
            if clip.exists() and clip.resolve() == resolved:
                raise RenderError("The video would overwrite one of the clips it is made from.")


# --------------------------------------------------------------------------
# FFmpeg
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Tools:
    ffmpeg: str
    ffprobe: str


def find_tools(need_drawtext: bool = False) -> Tools:
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    missing = [name for name, path in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)) if not path]
    if missing:
        raise RenderError(f"{' and '.join(missing)} not found on PATH. Install FFmpeg and try again.")
    if need_drawtext:
        filters = subprocess.run([ffmpeg, "-hide_banner", "-filters"], capture_output=True, text=True).stdout
        if not re.search(r"\bdrawtext\b", filters):
            raise RenderError("This FFmpeg was built without the drawtext filter, so labels cannot be written. "
                              "Use a full FFmpeg build or leave labels off.")
    return Tools(ffmpeg, ffprobe)


def find_font() -> str | None:
    """A TrueType font file for labels, or None to let FFmpeg choose (needs fontconfig)."""
    root = os.environ.get("SYSTEMROOT", "C:/Windows")
    for candidate in FONT_CANDIDATES:
        path = Path(candidate.format(SYSTEMROOT=root))
        if path.is_file():
            return str(path)
    return None


def parse_rate(text: str) -> Fraction:
    try:
        value = Fraction(text)
    except (ValueError, ZeroDivisionError):
        return Fraction(0)
    return value


def probe_clip(tools: Tools, path: Path) -> ClipInfo:
    result = subprocess.run(
        [tools.ffprobe, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate,nb_frames:format=duration",
         "-of", "json", str(path)],
        capture_output=True, text=True,
    )
    try:
        data = json.loads(result.stdout or "{}")
        stream = data["streams"][0]
        width, height = int(stream["width"]), int(stream["height"])
        fps = parse_rate(stream.get("r_frame_rate", "0"))
        frames = stream.get("nb_frames", "")
        if frames.isdigit() and fps > 0:
            seconds = int(frames) / float(fps)
        else:
            seconds = float(data["format"]["duration"])
    except (KeyError, IndexError, ValueError, TypeError) as error:
        raise RenderError(f"{path.name} is not a readable video clip (delete it and capture the shot again).") from error
    if width <= 0 or height <= 0 or fps <= 0:
        raise RenderError(f"{path.name} is not a readable video clip (delete it and capture the shot again).")
    return ClipInfo(width, height, fps, seconds, has_audio=has_audio_stream(tools, path))


def has_audio_stream(tools: Tools, path: Path) -> bool:
    out = subprocess.run(
        [tools.ffprobe, "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=codec_type", "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True,
    ).stdout
    return out.strip() == "audio"


def audio_seconds(tools: Tools, path: Path) -> float | None:
    """Length of the file's sound track, or None if it has none."""
    out = subprocess.run(
        [tools.ffprobe, "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=duration", "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True,
    ).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return None


def count_frames(tools: Tools, path: Path) -> int:
    out = subprocess.run(
        [tools.ffprobe, "-v", "error", "-count_frames", "-select_streams", "v:0",
         "-show_entries", "stream=nb_read_frames", "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True,
    ).stdout.strip()
    return int(out) if out.isdigit() else 0


def _run_ffmpeg(command: list[str], total_frames: int, work: Path, log: Log) -> None:
    proc = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace", cwd=work,
    )
    errors: list[str] = []
    last_logged = 0.0
    try:
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("frame="):
                now = time.monotonic()
                if now - last_logged >= 3:
                    last_logged = now
                    done = int(line.split("=", 1)[1] or 0)
                    log(f"rendering: {min(100, round(done / max(1, total_frames) * 100))}%")
            elif line and not line.startswith(_PROGRESS_KEYS):
                errors.append(line)
        code = proc.wait()
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    if code != 0:
        raise RenderError("FFmpeg could not make the video:\n" + "\n".join(errors[-8:]))


def render_video(
    shotlist: ShotList,
    clips_dir: str | Path,
    out_path: str | Path,
    options: RenderOptions = RenderOptions(),
    *,
    log: Log = print,
    overwrite: bool = False,
    dry_run: bool = False,
) -> RenderResult | None:
    """Make the video. Every shot needs its clip (``missing_clips``).

    Written as ``<name>.part.mp4`` and renamed when its length has been
    checked, so a finished file is always complete. Without ``overwrite`` an
    existing file is refused. With ``dry_run`` nothing is written; the plan is
    logged and None returned. Raises ``RenderError``.
    """
    # FFmpeg runs in a temporary folder, so every path it is given must be absolute
    out_path = Path(out_path).resolve()
    clips_dir = Path(clips_dir).resolve()
    if not shotlist.shots:
        raise RenderError("The shot list is empty; there is nothing to render.")
    check_output_path(out_path, clips_dir, shotlist)
    if out_path.exists() and not overwrite:
        raise RenderError(f"'{out_path}' already exists (use --force to replace it).")
    missing = missing_clips(shotlist, clips_dir)
    if missing:
        raise RenderError(missing_clips_message(missing, len(shotlist.shots)))

    tools = find_tools(need_drawtext=options.labels)
    prefer_sound = options.sound
    paths = [find_clip(shot, clips_dir, prefer_sound) for shot in shotlist.shots]
    clips = {path: probe_clip(tools, path) for path in dict.fromkeys(paths)}
    first = clips[paths[0]]
    fps = options.fps or max(1, round(first.fps))
    width, height = first.width, first.height

    segments = plan_segments(shotlist, clips_dir, clips, fps, prefer_sound)
    fade = crossfade_frames(options, fps)
    check_crossfade([s.frames for s in segments], fade, fps)
    for seg in segments:
        info = clips[seg.clip]
        wanted = shotlist.shots[seg.position].ticks / shotlist.tick_rate
        if info.seconds < wanted - 2 / fps:
            raise RenderError(
                f"The clip for shot {seg.position + 1} ({seg.clip.name}) lasts {info.seconds:.2f} s but the "
                f"shot is {wanted:.2f} s. Delete the clip and capture the shot again."
            )
    total = video_frames([s.frames for s in segments], fade)
    expected = total / fps
    log(f"{len(segments)} shot(s), {width}x{height} at {fps} fps, about {expected:.1f} s")
    if options.sound:
        silent = [seg.position for seg in segments if not seg.has_audio]
        if len(silent) == len(segments):
            log("none of the clips has sound; the video gets a silent sound track "
                "(capture the shots again without --no-sound for the game's sound)")
        elif silent:
            numbers = ", ".join(str(i + 1) for i in silent[:12]) + (", ..." if len(silent) > 12 else "")
            log(f"{len(silent)} clip(s) have no sound (shot {numbers}); they get silence")

    font = find_font() if options.labels else None
    work = Path(tempfile.mkdtemp(prefix="fragdirector_render_"))
    part = partial_path(out_path)
    try:
        label_files: dict[int, str] = {}
        if options.labels:
            for seg in segments:
                if seg.label:
                    name = f"label_{seg.position}.txt"
                    (work / name).write_text(seg.label, encoding="utf-8")
                    label_files[seg.position] = name
        graph = filter_graph(segments, width, height, fps, options, font, label_files)
        command = render_command(tools.ffmpeg, segments, graph, part, options)
        if dry_run:
            log("ffmpeg command: " + " ".join(command))
            return None
        out_path.parent.mkdir(parents=True, exist_ok=True)
        _run_ffmpeg(command, total, work, log)
        got = count_frames(tools, part)
        # a crossfade can land a frame either way; a hard-cut video must be exact
        slack = 0 if fade == 0 else len(segments)
        if abs(got - total) > slack:
            raise RenderError(f"The video has {got} frames; the shot list needs {total}. Nothing was kept.")
        sound_seconds = None
        if options.sound:
            sound_seconds = audio_seconds(tools, part)
            if sound_seconds is None or abs(sound_seconds - got / fps) > AUDIO_TOLERANCE_SECONDS:
                have = "no sound track" if sound_seconds is None else f"{sound_seconds:.2f} s of sound"
                raise RenderError(
                    f"The video has {got / fps:.2f} s of picture but {have}. Nothing was kept."
                )
        os.replace(part, out_path)
    finally:
        part.unlink(missing_ok=True)
        shutil.rmtree(work, ignore_errors=True)
    log(f"wrote {out_path.name}: {got} frames, {got / fps:.2f} s"
        + (f", {sound_seconds:.2f} s of sound" if sound_seconds is not None else ", no sound"))
    return RenderResult(out_path, len(segments), fps, got, got / fps, expected_seconds(shotlist, options),
                        sound_seconds)
