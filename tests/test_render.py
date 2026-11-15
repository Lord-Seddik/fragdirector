"""Tests for the video assembler: timing sums and the filter graph need no
FFmpeg; the end-to-end tests make tiny clips with FFmpeg and skip without it."""

import shutil
import subprocess
from fractions import Fraction

import pytest

from fragdirector.capture import clip_path
from fragdirector.cli import main
from fragdirector.models import Shot, ShotList
from fragdirector.render import (
    ClipInfo,
    RenderError,
    RenderOptions,
    check_crossfade,
    crossfade_frames,
    expected_seconds,
    filter_graph,
    label_filter,
    missing_clips,
    plan_segments,
    render_command,
    render_video,
    shot_frames,
    video_frames,
)

RATE = 64
FPS = 30
DEMO = "demos/x.dem"


def shot(start=1000, ticks=64, pov="111", speed=1.0, label="") -> Shot:
    return Shot(demo=DEMO, start_tick=start, end_tick=start + ticks, pov_player=pov, speed=speed, label=label)


def shotlist(*shots) -> ShotList:
    return ShotList(shots=tuple(shots), tick_rate=RATE)


# -- timing -----------------------------------------------------------------


def test_shot_frames_follow_length_and_speed():
    assert shot_frames(shot(ticks=64), RATE, 60) == 60
    assert shot_frames(shot(ticks=64, speed=0.5), RATE, 60) == 120  # slow motion lasts longer
    assert shot_frames(shot(ticks=64, speed=2), RATE, 60) == 30
    assert shot_frames(shot(ticks=1, speed=8), RATE, 60) == 1  # never zero frames


def test_video_length_is_the_sum_less_the_crossfades():
    assert video_frames([60, 90, 60], 0) == 210
    assert video_frames([60, 90, 60], 12) == 186
    assert video_frames([60], 12) == 60
    assert video_frames([], 12) == 0


def test_expected_seconds_matches_the_shot_list_without_crossfade():
    shots = shotlist(shot(ticks=64), shot(1500, 128, speed=0.5), shot(2000, 64))
    assert expected_seconds(shots, RenderOptions()) == shots.duration_seconds() == 6.0
    assert expected_seconds(shots, RenderOptions(crossfade_seconds=0.5)) == 5.0


def test_a_crossfade_longer_than_half_a_shot_is_refused():
    check_crossfade([60, 60], 30, 60)  # exactly twice as long is fine
    with pytest.raises(RenderError, match="Shot 2 lasts 0.90 s"):
        check_crossfade([60, 54], 30, 60)
    check_crossfade([1], 0, 60)  # no crossfade, any length


@pytest.mark.parametrize("seconds", [-0.1, 5.1, float("nan"), float("inf")])
def test_bad_crossfade_is_refused(seconds):
    with pytest.raises(RenderError, match="crossfade"):
        RenderOptions(crossfade_seconds=seconds)


@pytest.mark.parametrize("field,value", [("crf", 52), ("crf", -1), ("crf", True), ("fps", 0), ("fps", 241),
                                         ("label_seconds", 0)])
def test_bad_options_are_refused(field, value):
    with pytest.raises(RenderError):
        RenderOptions(**{field: value})


def test_crossfade_frames():
    assert crossfade_frames(RenderOptions(), 60) == 0
    assert crossfade_frames(RenderOptions(crossfade_seconds=0.5), 60) == 30
    assert crossfade_frames(RenderOptions(crossfade_seconds=0.001), 60) == 1


# -- planning and the command ----------------------------------------------


def info(width=64, height=36, seconds=1.0) -> ClipInfo:
    return ClipInfo(width, height, Fraction(FPS), seconds)


def segments_for(shots, clips=None, tmp="clips"):
    clips = clips or {clip_path(s, tmp): info() for s in shots.shots}
    return plan_segments(shots, tmp, clips, FPS)


def test_missing_clips_lists_positions(tmp_path):
    shots = shotlist(shot(1000), shot(2000), shot(3000))
    clip_path(shots.shots[1], tmp_path).write_bytes(b"x")
    assert missing_clips(shots, tmp_path) == [0, 2]


def test_graph_for_hard_cuts_uses_concat_and_applies_speed():
    shots = shotlist(shot(1000, 64), shot(2000, 64, speed=0.5))
    graph = filter_graph(segments_for(shots), 64, 36, FPS, RenderOptions())
    assert "[0:v]setsar=1,setpts=(PTS-STARTPTS)/1,fps=30,trim=end_frame=30,setpts=PTS-STARTPTS,format=yuv420p[v0]" in graph
    assert "setpts=(PTS-STARTPTS)/0.5,fps=30,trim=end_frame=60" in graph
    assert graph.endswith("[v0][v1]concat=n=2:v=1:a=0[vout]")
    assert "scale=" not in graph  # every clip already has the output size


def test_graph_scales_a_clip_of_another_size():
    shots = shotlist(shot(1000), shot(2000))
    clips = {clip_path(shots.shots[0], "clips"): info(), clip_path(shots.shots[1], "clips"): info(128, 72)}
    graph = filter_graph(segments_for(shots, clips), 64, 36, FPS, RenderOptions())
    first, second = graph.split(";")[:2]
    assert "scale=" not in first
    assert "scale=64:36:force_original_aspect_ratio=decrease,pad=64:36:(ow-iw)/2:(oh-ih)/2" in second


def test_graph_with_crossfade_chains_xfades_at_the_right_offsets():
    shots = shotlist(shot(1000, 64), shot(2000, 128), shot(3000, 64))  # 30, 60, 30 frames
    options = RenderOptions(crossfade_seconds=0.2)  # 6 frames
    graph = filter_graph(segments_for(shots), 64, 36, FPS, options)
    assert "[v0][v1]xfade=transition=fade:duration=0.200000:offset=0.800000[x1]" in graph  # (30-6)/30
    assert "[x1][v2]xfade=transition=fade:duration=0.200000:offset=2.600000[vout]" in graph  # (30+60-6-6)/30
    assert "concat" not in graph


def test_a_single_shot_needs_no_join():
    graph = filter_graph(segments_for(shotlist(shot())), 64, 36, FPS, RenderOptions(crossfade_seconds=0.2))
    assert graph.endswith("[v0]null[vout]")


def test_labels_come_from_files_and_are_not_expanded():
    shots = shotlist(shot(1000, label="100% %{pid} a:b;c'd"), shot(2000))
    segments = segments_for(shots)
    assert segments[0].label == "100% %{pid} a:b;c'd"
    graph = filter_graph(segments, 64, 36, FPS, RenderOptions(labels=True), None, {0: "label_0.txt"})
    assert "drawtext=textfile=label_0.txt:expansion=none" in graph
    assert "enable='lt(t,3.000)'" in graph
    assert "%{pid}" not in graph and "a:b" not in graph  # the text itself never reaches the graph
    assert graph.count("drawtext") == 1  # the second shot has no label


def test_label_filter_escapes_a_windows_font_path():
    text = label_filter("label_0.txt", "C:\\Windows\\Fonts\\arial.ttf", 1080, 3)
    assert r"fontfile='C\:/Windows/Fonts/arial.ttf'" in text
    assert "fontsize=45" in text


def test_command_lists_every_clip_and_writes_no_sound(tmp_path):
    shots = shotlist(shot(1000), shot(2000))
    segments = segments_for(shots, tmp=tmp_path)
    command = render_command("ffmpeg", segments, "GRAPH", tmp_path / "out.part.mp4", RenderOptions(crf=20))
    assert command.count("-i") == 2 and "-an" in command
    assert command[command.index("-filter_complex") + 1] == "GRAPH"
    assert command[command.index("-crf") + 1] == "20"
    assert command[-1].endswith("out.part.mp4")


def test_too_many_shots_for_one_command_line_is_refused():
    shots = shotlist(*[shot(1000 + 100 * i, label="x") for i in range(200)])
    with pytest.raises(RenderError, match="too many shots"):
        filter_graph(segments_for(shots), 64, 36, FPS, RenderOptions(labels=True), "C:/f.ttf",
                     {i: f"label_{i}.txt" for i in range(200)})


# -- refusals before FFmpeg runs ------------------------------------------------


def test_an_empty_shot_list_is_refused(tmp_path):
    with pytest.raises(RenderError, match="empty"):
        render_video(shotlist(), tmp_path, tmp_path / "out.mp4")


def test_missing_clips_are_reported_with_their_numbers(tmp_path):
    shots = shotlist(shot(1000), shot(2000))
    with pytest.raises(RenderError, match=r"2 of 2 shot\(s\) have no clip yet \(shot 1, 2\)"):
        render_video(shots, tmp_path, tmp_path / "out.mp4")


def test_the_video_must_be_an_mp4_and_not_a_clip(tmp_path):
    shots = shotlist(shot(1000))
    with pytest.raises(RenderError, match=r"\.mp4"):
        render_video(shots, tmp_path, tmp_path / "out.mkv")
    clip = clip_path(shots.shots[0], tmp_path)
    clip.write_bytes(b"x")
    with pytest.raises(RenderError, match="overwrite one of the clips"):
        render_video(shots, tmp_path, clip, overwrite=True)


def test_an_existing_video_is_kept_unless_overwrite_is_asked_for(tmp_path):
    out = tmp_path / "out.mp4"
    out.write_bytes(b"old")
    with pytest.raises(RenderError, match="already exists"):
        render_video(shotlist(shot(1000)), tmp_path, out)
    assert out.read_bytes() == b"old"


# -- with FFmpeg --------------------------------------------------------------------

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="FFmpeg is not installed"
)


def make_clip(path, seconds, size="64x36", fps=FPS, rate="testsrc2"):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"{rate}=size={size}:rate={fps}:duration={seconds}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
        check=True,
    )


def frames_of(path) -> int:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries",
         "stream=nb_read_frames", "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    return int(out.strip())


def clips_for(shots, folder, size="64x36"):
    for s in shots.shots:
        make_clip(clip_path(s, folder), s.ticks / RATE, size)


@needs_ffmpeg
def test_hard_cuts_give_exactly_the_shot_list_length(tmp_path):
    shots = shotlist(shot(1000, 64), shot(2000, 128), shot(3000, 32, speed=0.5))  # 1 + 2 + 1 s
    clips_for(shots, tmp_path / "clips")
    result = render_video(shots, tmp_path / "clips", tmp_path / "out.mp4", RenderOptions(), log=lambda _: None)
    assert result.frames == frames_of(tmp_path / "out.mp4") == round(shots.duration_seconds() * FPS) == 120
    assert result.fps == FPS and result.shots == 3
    assert abs(result.seconds - shots.duration_seconds()) < 1 / FPS
    assert not (tmp_path / "out.part.mp4").exists()


@needs_ffmpeg
def test_slow_motion_and_fast_motion_change_the_length(tmp_path):
    shots = shotlist(shot(1000, 64, speed=0.5), shot(2000, 64, speed=2))  # 2 s + 0.5 s
    clips_for(shots, tmp_path / "clips")
    result = render_video(shots, tmp_path / "clips", tmp_path / "out.mp4", log=lambda _: None)
    assert result.frames == 75


@needs_ffmpeg
def test_crossfade_shortens_the_video_by_one_overlap_per_join(tmp_path):
    shots = shotlist(shot(1000, 128), shot(2000, 128), shot(3000, 128))  # 3 x 2 s
    clips_for(shots, tmp_path / "clips")
    options = RenderOptions(crossfade_seconds=0.5)
    result = render_video(shots, tmp_path / "clips", tmp_path / "out.mp4", options, log=lambda _: None)
    assert abs(result.frames - 150) <= 3  # 6 s - 2 joins x 0.5 s = 5 s
    assert abs(result.seconds - expected_seconds(shots, options)) < 0.1


@needs_ffmpeg
def test_a_clip_of_another_size_is_fitted(tmp_path):
    shots = shotlist(shot(1000, 64), shot(2000, 64))
    folder = tmp_path / "clips"
    make_clip(clip_path(shots.shots[0], folder), 1, "64x36")
    make_clip(clip_path(shots.shots[1], folder), 1, "96x54")
    result = render_video(shots, folder, tmp_path / "out.mp4", log=lambda _: None)
    assert result.frames == 60


@needs_ffmpeg
def test_the_same_clip_can_be_used_twice(tmp_path):
    one = shot(1000, 64)
    shots = shotlist(one, shot(1000, 64, speed=0.5, label="replay"))  # same clip, different speed
    clips_for(shotlist(one), tmp_path / "clips")
    result = render_video(shots, tmp_path / "clips", tmp_path / "out.mp4", log=lambda _: None)
    assert result.frames == 30 + 60


@needs_ffmpeg
def test_labels_with_awkward_text_still_render(tmp_path):
    from fragdirector.render import find_tools

    try:
        find_tools(need_drawtext=True)
    except RenderError:
        pytest.skip("this FFmpeg has no drawtext")
    shots = shotlist(shot(1000, 64, label="KeTаmin's view: 100% %{pid} ;[x]"), shot(2000, 64, label=""))
    clips_for(shots, tmp_path / "clips")
    result = render_video(shots, tmp_path / "clips", tmp_path / "out.mp4", RenderOptions(labels=True),
                          log=lambda _: None)
    assert result.frames == 60


@needs_ffmpeg
def test_a_clip_shorter_than_its_shot_is_refused(tmp_path):
    shots = shotlist(shot(1000, 128))
    make_clip(clip_path(shots.shots[0], tmp_path / "clips"), 1)  # the shot needs 2 s
    with pytest.raises(RenderError, match="capture the shot again"):
        render_video(shots, tmp_path / "clips", tmp_path / "out.mp4", log=lambda _: None)


@needs_ffmpeg
def test_an_unreadable_clip_is_reported_and_nothing_is_written(tmp_path):
    shots = shotlist(shot(1000, 64))
    clip = clip_path(shots.shots[0], tmp_path / "clips")
    clip.parent.mkdir()
    clip.write_bytes(b"not a video")
    with pytest.raises(RenderError, match="not a readable video clip"):
        render_video(shots, tmp_path / "clips", tmp_path / "out.mp4", log=lambda _: None)
    assert not (tmp_path / "out.mp4").exists()


@needs_ffmpeg
def test_dry_run_writes_nothing(tmp_path):
    shots = shotlist(shot(1000, 64))
    clips_for(shots, tmp_path / "clips")
    lines = []
    assert render_video(shots, tmp_path / "clips", tmp_path / "out.mp4", log=lines.append, dry_run=True) is None
    assert any(line.startswith("ffmpeg command:") for line in lines)
    assert not (tmp_path / "out.mp4").exists()


@needs_ffmpeg
def test_overwrite_replaces_the_old_video(tmp_path):
    shots = shotlist(shot(1000, 64))
    clips_for(shots, tmp_path / "clips")
    out = tmp_path / "out.mp4"
    out.write_bytes(b"old")
    render_video(shots, tmp_path / "clips", out, log=lambda _: None, overwrite=True)
    assert frames_of(out) == 30


# -- the command ---------------------------------------------------------------------


@needs_ffmpeg
def test_render_command_end_to_end(tmp_path, capsys):
    shots = shotlist(shot(1000, 64), shot(2000, 64))
    shots_file = tmp_path / "shots.json"
    shots.save(shots_file)
    clips_for(shots, tmp_path / "clips")  # the default folder: 'clips' next to the shot list
    assert main(["render", str(shots_file)]) == 0
    assert frames_of(tmp_path / "shots.mp4") == 60  # default name: the shot list's, with .mp4
    assert "Wrote" in capsys.readouterr().out
    assert main(["render", str(shots_file)]) == 1  # refuses to replace it
    assert "already exists" in capsys.readouterr().err
    assert main(["render", str(shots_file), "--force", "--crossfade", "0.2"]) == 0


def test_render_command_reports_missing_clips(tmp_path, capsys):
    shots_file = tmp_path / "shots.json"
    shotlist(shot(1000, 64)).save(shots_file)
    assert main(["render", str(shots_file)]) == 1
    assert "no clip yet" in capsys.readouterr().err


def test_render_command_reports_a_missing_shot_list(tmp_path, capsys):
    assert main(["render", str(tmp_path / "nope.json")]) == 1
    assert "does not exist" in capsys.readouterr().err
