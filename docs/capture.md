# Capture: how fragdirector gets video out of CS2

Result of spike T4 (2026-11-14, CS2 build 10955, Windows 11, FFmpeg 9.0.2).
Working code: `spikes/capture_one.py`.

## Chosen method

**Drive CS2 through its TCP console, record the game window with FFmpeg.**

```
python spikes/capture_one.py demos/x.dem --player sedx --tick 117100
```

| Step | How |
|---|---|
| Start the game | `steam.exe -applaunch 730 -insecure -novid -netconport <port> -netconpassword <random>` |
| Send commands, read replies | Plain text over TCP to `127.0.0.1:<port>`; first line `PASS <password>` |
| Load the demo | `playdemo "<absolute path>"` (spaces are fine; nothing is copied into the game folder) |
| Jump to a tick | `demo_gototick <tick>`, wait for `Demo Skipping finished at tick <tick>` |
| Lock the view | `spec_mode 1`, `spec_player "<name>"` |
| Play an exact range | `demo_step_tick <n>` plays n ticks and pauses; CS2 then prints `Demo paused at engine time …, demo tick <tick>` |
| Record | FFmpeg `gfxcapture` (Windows.Graphics.Capture) on the `cs2.exe` window, frames stamped with wall-clock time |
| Cut | The wall-clock time of the final pause line gives the time of every tick; FFmpeg cuts the clip and re-encodes to constant 60 fps |
| Finish | Restore changed settings, `quit` |

Needs no third-party program besides FFmpeg, changes no game files, and
leaves the user's saved settings as they were.

## Measured

- 5 of 5 runs with the final script produced a 1920x1080, 60 fps, 600-frame,
  10.00 s `.mp4`; one more run captured a second player (the victim).
- One run takes 62–75 s for a 10 s clip: about 25 s game start, 10 s demo
  load, 8 s seek and settle, 12 s playback, 5 s cut.
- Accuracy, read off the on-screen tick counter (`--demo-ui`): the picture is
  about 4 ticks (60 ms) behind the requested tick, the same amount from start
  to end. Three runs of the same shot differ by 1–2 frames.
- Playback stayed real time: 16–22 ms drift over 12 s.
- Parser ticks (demoparser2) are the same ticks `demo_gototick` uses. The
  "game tick" in CS2's log is demo tick + `server_start_tick` (2321 here).

## What was tried

| Method | Result |
|---|---|
| `-netconport` TCP console | **Works.** Commands in, console output back. Reported dead for CS2 on forums; it is not in this build. |
| VConsole port 29000 (`-vconsole`) | Port opens without `-tools`. Not pursued: binary protocol, and netcon already works. Fallback if netcon is removed. |
| `startmovie` (tga, jpg, raw, mp4, webm, wav) | Command exists but writes nothing and prints nothing. Dead in the retail game. |
| `playdemo <demo> mp4 framerate 60 start= end=` | Plays the range in real time, records nothing (363 frames in 10 s, no file). `start=`/`end=` do work as a plain range player. |
| FFmpeg `gfxcapture` window capture | **Works**, also when the game window is behind other windows. |
| HLAE, CS Demo Manager plugin | Not tested: both need a download and load code into the game. See "Open decisions". |
| Keyboard simulation | Not needed. |

## Traps found (T12 must keep these)

1. **"Paused" after a seek is not reliable.** CS2 prints `Demo Skipping
   finished` and only then runs its own pause → unpause → pause. A command
   sent in that window is lost and can leave the demo playing. Two of the
   first four runs hit this. Fix in the spike: wait, then step one tick
   twice, a couple of seconds apart; only if the second lands exactly one
   tick after the first is the demo really paused. After recording, the
   final tick must equal the requested end tick, otherwise seek and record
   again (up to 3 times in the same game session).
2. **`CGameRules - unpaused` is not always printed.** Only the
   `Demo paused at engine time` line is dependable, so timing is anchored on
   the end of the range.
3. **`playdemo` is ignored until the main menu is up.** Repeat it every 5 s
   until `Requesting playback of` appears.
4. **`demo_ui_mode 0` only hides the playback bar if set before the demo
   loads.**
5. **`spec_player` gives no error for an unknown name.** Check the name
   against the parsed player list before starting the game.
6. **An unfocused game sleeps 20 ms per frame** (`engine_no_focus_sleep`,
   about 36 fps). The spike sets it to 0 and puts the old value back; this
   setting is saved by the game, so the restore matters.
7. **A dead player's view turns into a chase camera on the body.** Victim
   shots should end at or just after the death tick (T8).
8. **`startmovie`, `endmovie` do not show up in `find`** but exist; do not
   conclude from `find` that a command is missing.
9. **A forward seek from a paused demo never moves** (found in T12). CS2
   prints `Demo Skipping: skipping forward to demo tick …` and stays where it
   is. Backward seeks reload from an earlier point and work while paused. The
   capture resumes the demo before every forward seek.
10. **The first seek right after a demo loads can be dropped** without any
    reply (found in T12). Every seek CS2 takes prints a `Demo Skipping` line;
    the capture sends the seek again if none comes within 5 s.

## Limits of this method

- **No sound yet.** FFmpeg cannot record what the speakers play, and the
  only audio package already installed (`sounddevice`) has no loopback
  device. Needs one small extra package (see below).
- **Real time only.** A 10 s shot takes 10 s plus about 8 s of seeking; a
  slow PC that cannot hold 60 fps gives duplicated frames, not slow motion.
- **Windows only**, FFmpeg 8.0 or newer (for `gfxcapture`), the game window
  must not be minimised, and the game takes over the screen while it runs.
- **HUD is the spectator HUD** (player bar on top, radar). The build line
  bottom-left stays (`r_show_build_info` is a saved setting; hide and
  restore it the same way as the focus setting).
- **The console port listens on all network interfaces**, not just this
  PC. The random password is what protects it; never start the game with
  `-netconport` and no password.
- CS2 must be closed before a capture; the script refuses to run otherwise
  and never closes a game it did not start.

## In fragdirector (T12)

```
fragdirector capture shots.json            # every shot; skips clips already made
fragdirector capture shots.json --dry-run  # list the clips, start nothing
fragdirector capture shots.json --only 2,5 --force
```

Code: `src/fragdirector/capture/` (`plan.py` pure: jobs, clip names,
commands, timing; `game.py` console, game session, recorder; `run.py` one
session for a list of clips). The editor's "Capture" button on a shot runs
the same code for that one shot in the background (`POST /api/capture`).

- **One game session for the whole list.** Clips are captured in demo order;
  the demo is loaded once.
- **One clip per distinct footage.** Shots that differ only in speed or label
  share a clip; slow motion is applied when the video is assembled (T13).
  The clip name comes from the shot alone (`clip_path`), e.g.
  `118241-119378_76561198000000001_b334da78.mp4` in `clips/` next to the
  shot list.
- **Resumable.** A clip is written as `.part.mp4`, its frame count checked,
  then renamed. An existing clip is complete; a failed or interrupted run is
  continued by running the same command again.
- **Checked before the game starts:** every shot against the demo (round,
  POV player alive), the player name (in the demo, unique, no `"`, `;` or
  control characters, since `spec_player` gives no error), the demo path, the
  FFmpeg `gfxcapture` filter, Steam, CS2 not running, the console port free.
- **Seek point:** 2 s before the shot, but not before the POV player spawned
  (seeking into the previous round would point the camera at a dead player).
- **Picture delay:** the cut is moved 60 ms later to make up for the
  picture trailing the tick (see "Measured").
- Both saved settings, `engine_no_focus_sleep` and `r_show_build_info`, are
  read, changed and put back; one that cannot be read is left alone.
- A clip that fails is reported and the rest go on; a game that quits or
  crashes stops the run.

## Open decisions

1. **Sound.** Either add a loopback recorder (`soundcard` or `PyAudioWPatch`
   from PyPI, records whatever the PC plays, so other apps must be quiet),
   or go to HLAE, which records the game's own sound.
2. **HLAE as an optional second backend.** It renders frame by frame, so
   quality does not depend on PC speed and slow motion is smooth, and it has
   sound. Costs: a third-party program injected into the game, breaks for a
   few days after some CS2 updates, Windows only. Untested here.
3. **One game session for many shots.** The spike starts and quits CS2 per
   clip (25 s overhead). T12 should keep one session open for a whole shot
   list; all commands used here work repeatedly in one session.

## Other commands

Tested: `demo_info` (prints the demo header incl. `server_start_tick`),
`playdemo <demo> start=<tick> end=<tick>` (plays a range, then leaves the
demo), `demoui` (toggles the playback bar), `cl_trueview_show_status 0`
(hides the "TrueView" label; not saved).

Seen in the console's `find` output, not tested: `demo_pauseatservertick`,
`demo_timescale`, `demo_marktick`, `cl_draw_only_deathnotices`,
`cl_drawhud_force_radar`, `cl_drawhud_force_deathnotices`.
