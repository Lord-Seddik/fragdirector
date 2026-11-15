# Making the video (T13)

`fragdirector render shots.json` joins the clips that `fragdirector capture`
wrote into one `.mp4`. The editor's **Make video** button does the same.

```
fragdirector render shots.json                      # hard cuts, writes shots.mp4
fragdirector render shots.json --crossfade 0.4      # fade between shots
fragdirector render shots.json --labels             # write each shot's label on the picture
fragdirector render shots.json --out reel.mp4 --force
fragdirector render shots.json --dry-run            # check the clips, print the FFmpeg command
```

Options: `--clips DIR` (default: `clips` next to the shot list), `--out PATH`
(must end in `.mp4`; default: the shot list's name with `.mp4`), `--crf N`
(x264 quality, 0-51, default 18), `--force` (replace an existing video).

## What it does

One FFmpeg run, no intermediate files:

1. Each shot's clip is found from the shot list alone (`capture.clip_path`).
   A shot with no clip stops the render and the message lists which ones to
   capture.
2. Each clip is cut to the shot's on-screen length and its **speed** is
   applied. Slow motion repeats frames, so 0.5x of a 60 fps clip shows 30
   different pictures per second; fast motion drops frames.
3. With `--labels`, the shot's label is drawn bottom-left for its first
   3 seconds.
4. Shots are joined with a hard cut, or with `--crossfade SECONDS`.
5. The result is encoded as H.264, yuv420p, at the first clip's frame rate
   and size (a clip of another size is fitted with black bars).

The video has **no sound**, like the clips.

## Length

Without a crossfade the video is exactly the shot list's length (the sum of
each shot's ticks / tick rate / speed), to the frame; the render checks this
and keeps nothing if it is off. A crossfade makes two neighbouring shots
overlap, so the video is shorter by `crossfade x (shots - 1)`. Every shot must
be at least twice the crossfade long, otherwise the render is refused.

## Notes

- Rendering runs at about half real time or slower on a laptop (8 clips,
  44.8 s of 1080p60 took about 85 s). The editor does it in the background.
- The video is written as `name.part.mp4` and renamed when finished, so a
  complete file is never half-written. The editor always writes
  `<shot list name>.mp4` next to the shot list and replaces it; the command
  line refuses to replace a file without `--force`.
- Labels are read from files and not expanded, so a label such as `100% %{x}`
  is written as it is. The font is Arial on Windows (DejaVu or Arial elsewhere);
  characters the font lacks (for example some CJK names) show as boxes. Labels
  need an FFmpeg with the `drawtext` filter (the "full" builds have it).
- Needs `ffmpeg` and `ffprobe` on PATH. Unlike capture, it does not need
  FFmpeg 8 or Windows (tested here with FFmpeg 9.0.2 on Windows only).
- A shot list of more than about 270 shots (about 90 with labels) is refused, because
  the FFmpeg command line would get too long; render it in parts.
