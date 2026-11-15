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
   shots should end at or just after the death tick (T8). T21 measured it:
   see "After death (spike T21)".
8. **`startmovie`, `endmovie` do not show up in `find`** but exist; do not
   conclude from `find` that a command is missing.
9. **A forward seek from a paused demo never moves** (found in T12). CS2
   prints `Demo Skipping: skipping forward to demo tick …` and stays where it
   is. Backward seeks reload from an earlier point and work while paused. The
   capture resumes the demo before every forward seek.
10. **The first seek right after a demo loads can be dropped** without any
    reply (found in T12). Every seek CS2 takes prints a `Demo Skipping` line;
    the capture sends the seek again if none comes within 5 s.

## Sound (spike T20)

Result of spike T20 (2026-11-15, same PC and game). Working code:
`spikes/capture_sound.py`.

```
python spikes/capture_sound.py demos/x.dem --player sedx --tick 23200 --test-tone
```

**Record only the game's sound with Windows' per-process loopback**, through
`ctypes`: no new package, no driver, no "Stereo Mix", no HLAE.

| Step | How |
|---|---|
| Find the game | `cs2.exe`'s process id (`tasklist`), once its console answers |
| Open the recorder | `ActivateAudioInterfaceAsync("VAD\\Process_Loopback")` with `AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK`, the process id, and "include the process tree"; needs Windows 10 2004 (build 19041) or newer |
| Format | Asked for, not read (process loopback has no mix format): 48 kHz, stereo, 16-bit PCM, with `AUTOCONVERTPCM` |
| Read | Event-driven `IAudioCaptureClient` on its own thread; packets of 10 ms |
| Timing | Each packet's `u64QPCPosition` (performance counter at its first sample) turned into wall-clock time with `GetSystemTimePreciseAsFileTime`, the clock FFmpeg stamps the picture with |
| Lay out | Packets placed on that time line into one WAV; a jump of more than 5 ms would be padded with silence (none happened) |
| Cut | Picture cut as before (output `-ss`, re-encode), **then** sound added in a second FFmpeg run (input `-ss` on the WAV, AAC 192 kb/s) |
| Settings | `snd_mute_losefocus` (default `true`: the game goes silent when its window is not in front) set to `0` and put back, like `engine_no_focus_sleep` |

### Measured

- 3 runs of the same 10 s shot (sedx, tick 23200, AK bursts): every clip
  1920x1080, 600 frames, 10.000 s of 48 kHz stereo AAC; every run had
  1371–1525 packets, **no gaps, no packet without a timestamp, no
  discontinuity or silent flags**. One run needed the known "demo ran past
  the shot start" retry.
- **Other programs are not recorded.** A 1 kHz tone played by another
  process during runs 1 and 2 is absent from the clips (its level is that
  of noise, 3.7x and 5.2x the surrounding spectrum; a present tone is
  hundreds of times higher). Recording `explorer.exe` while the tone played
  gave pure silence. Recording the tone's own process gave the tone at
  exactly 1000 Hz. Note that child processes of the target are included.
- **Sync.** For the first shot of each AK burst: first frame with a muzzle
  flash (checked by eye on 4 bursts, the rest from the script; the script
  picked a wrong frame once, caught by eye) against the
  first loud millisecond of sound. With no correction the sound is
  **85–131 ms late** in the clip. The sound itself is steady (it starts
  45–70 ms after the demo's `weapon_fire` tick); the spread comes from the
  picture, whose timing already varied 1–2 frames between runs in T4.
  With the sound cut moved **0.12 s** later (it then plays 0.12 s earlier):

  | Run | Shift | Sound − flash per burst |
  |---|---|---|
  | 1 (re-cut offline) | 0.12 s | −35, −10, −9, +11 ms |
  | 2 | 0.12 s (measured with 0.11, 10 ms added) | +39, +3, +19 ms |
  | 3 | 0.12 s | −9, −5, +14 ms |

  Worst case 39 ms (2.3 frames at 60 fps), most within one frame.
  0.12 s = the 60 ms the picture cut is already moved + about 60 ms by which
  the sound reaches the recorder after the picture.
- The muzzle flash comes 1–5 frames **before** the `weapon_fire` tick in
  these clips (whose picture cut includes the 60 ms correction). The game
  event is stamped a little after what is drawn; nothing depends on it.
- Game sound settings on this PC: `volume 0.5041`, peak level in the clips
  0.74–0.77 of full scale. A higher `volume` would clip loud fights.

### Traps

1. **One FFmpeg run cannot cut picture and sound at different points with
   an output `-ss`**: it applies to every stream, so the sound came out
   1.4 s late in the first try. Cut the picture alone, then add the sound.
2. **The game mutes itself when its window loses focus** (`snd_mute_losefocus`,
   saved setting). Not checked what the clip sounds like with it left on;
   it is simply always turned off during a capture.
3. A 1 kHz test tone started as a **child** of the recording script was
   recorded when the script's own process was the target: "process tree"
   includes children. Harmless for `cs2.exe`.

### Not checked

- Whether Windows' volume mixer (per-app or master volume, mute) changes the
  recorded level. Process loopback is expected to take the sound before the
  master volume.
- Another Windows version, another sound device, Bluetooth headphones,
  a sample rate other than 48 kHz on the device.
- Sound during a long capture (a whole shot list) and across seeks: the
  recorder ran for about 14 s per run here.

## After death (spike T21)

Result of spike T21 (2026-11-15, same PC and game). Working code:
`spikes/capture_after_death.py`.

```
python spikes/capture_after_death.py demos/x.dem --death emlight:117243 --death VAN:118411 \
    --after 6 --set spec_freeze_time=10
```

**Safe limit: 5 s after the death**, if the capture sets
`spec_freeze_time 10` for the session. Without that setting the limit is
**2.5 s**. Never past the round's end tick.

| Step | How |
|---|---|
| Lock the camera | As before: `spec_mode 1`, `spec_player "<name>"` while the player is alive; nothing more after the death |
| Hold it on the body | `spec_freeze_time 10` (console variable, not saved by the game; read it first, put it back afterwards like the others) |
| Check | A console line `Scanned <n> entities, activated prediction for …` printed after the death tick and before the shot's end means the camera left the victim: retake or fail the clip |

### What CS2 does when the watched player dies

1. **0 to ~0.4 s:** the first-person view drops and cuts to a third-person
   camera next to the body (the "death cam"). A red damage flash comes just
   before the death.
2. **Then the camera stays still on the body**, facing roughly where the shot
   came from. The world plays on: other players walk through the picture.
3. **At deathanim + travel + freeze time − 0.05 s** CS2 switches on its own to
   **the killer's first-person view**. In this demo that is
   0.8 + 0.3 + 2 = 3.1 s, measured **3.02–3.08 s**. If the killer dies later,
   the camera goes on to the killer's own death cam (VAN → Lumera,
   takete → KeTаmin).
4. **At the round's end tick** (`Round.end_tick`, 7 s after the deciding
   kill), the camera goes to the same player alive in the next round's freeze
   time, whatever the settings.

HUD after the death: the bottom player panel (health, armour, name, ammo)
goes away, while the money (bottom left), radar, top bar and kill feed stay;
the "Terrorists win" banner and MVP panel show when the death ends the round.

The three times come from `spec_freeze_deathanim_time` (0.8),
`spec_freeze_traveltime` (0.3) and `spec_freeze_time`. All three are marked
"replicated": the demo's server sent `spec_freeze_time 2` (the game's
default is 3), so the value differs between demos. demoparser2 cannot read
it (its API has no console variables, and the packets are compressed). Once
the demo is loaded, the console gives the value in force.

### Measured

Round 17 of the sample demo (the 5k round), 3 s before and 6 or 12 s after
each death, 7 different victims and 3 different killers, 16 clips in 5 game
sessions. Every clip had the expected frame count; playback drift was
+13…+33 ms.

| Pass | Setting | Victims | Camera leaves the victim at (after the death) |
|---|---|---|---|
| A | none | emlight, OutubroAK42, Mobin, KeTаmin (killer sedx); LaWmaty, takete (KeTаmin); VAN (Lumera) | +3.02 … +3.08 s, to the killer, in all 7 |
| B | `spec_player` sent again every 0.25 s from the death | emlight, LaWmaty, VAN | +3.03 … +3.05 s, to the killer: **no effect** |
| C | `spec_freeze_time 10`, `spec_freeze_deathanim_time 4` | emlight, LaWmaty, VAN | never, within 6 s |
| D | `spec_freeze_time 10` only, 12 s after | emlight, LaWmaty | +11.05, +11.07 s (expected 11.1) |
| D | same | KeTаmin (death ends the round) | +7.02 s: round end, next round |

The switch time was read off the picture (FFmpeg `scdet` scene cut, then
checked by eye on contact sheets) and off the console line above: they
agree within 0.05 s in all 13 switches, and the line is absent in the 3
clips that did not switch.

Why 5 s: with `spec_freeze_time 10` the camera stays on the victim for
11 s; 5 s leaves 6 s of margin and is plenty for a highlight (the presets
use 1.5 s). Without the setting, 3.0 s is the measured switch time and 2.5 s
leaves a 0.5 s margin.

### Traps

1. **`spec_player` on a dead player is ignored**, with no error. It cannot
   hold the camera, and it cannot bring the camera back after CS2 has
   switched.
2. **The death cam's length depends on the demo** (replicated settings). Do
   not rely on the 3.1 s measured here; set `spec_freeze_time`.
3. Setting a replicated variable locally **does work** during demo playback
   and survives seeks within the session (tested across 3 seeks and one
   round change). `spec_freeze_time` and `spec_freeze_deathanim_time` are
   not saved by the game (no `archive` flag in `find`), but restore them
   anyway.
4. `find` shows `mp_deathcam_skippable` and `spec_freeze_time_lock` (1 s):
   these are for live players skipping the death cam, not used here.

### Not checked

- A death with no killer (fall damage, bomb, world), a team kill, or a
  killer who is already dead when the switch comes.
- Other demos (FACEIT, community servers) and their replicated values.
- `spec_freeze_time` held over a whole shot list in one session (tested
  over 3 clips).
- The 0.05 s between the computed and measured switch at other frame rates.
- Clips (git-ignored): `output/t21_<victim>_<tick>_<pass>.mp4`, with a
  `.console.txt` (console lines stamped in seconds from the death) and a
  `.sheet.png` (a frame every 0.5 s, starting 2 s before the death) for each.

## Limits of this method

- **Sound only since T20**, with the method above; T12's capture still
  writes clips without sound until T25.
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
- The saved settings `engine_no_focus_sleep` and `r_show_build_info` (and the
  ones named under "Sound (T25)" and "After death (T26)") are read, changed
  and put back; one that cannot be read is left alone.
- A clip that fails is reported and the rest go on; a game that quits or
  crashes stops the run.

### Sound (T25)

```
fragdirector capture shots.json             # clips with the game's sound
fragdirector capture shots.json --no-sound  # picture only, as in v1
```

T20's method, in `capture/sound.py` (the recorder) and `capture/run.py`
(cutting). Standard library only.

- **Only the game's sound.** The recorder takes the sound of `cs2.exe`'s
  process (found with `tasklist` once its console answers) and its children.
  Music, voice chat and other programs are not recorded. Needs Windows 10
  2004 (build 19041) or newer; on an older Windows the capture stops before
  the game starts and tells you to use `--no-sound`.
- **Per clip.** The sound recorder starts just before the picture recorder
  and stops after it, for every clip. Packets go on their wall-clock time line
  (`lay_out`); a hole or overlap of more than 5 ms counts as a gap. A gap
  **inside the shot** makes that take count as failed, and the clip is
  recorded again (up to the usual 3 attempts; the last one is kept with a
  WARNING). Gaps before or after the shot are ignored. In the first real run,
  1 of 8 clips had a 16 ms hole inside the shot.
- **Cutting.** The picture is cut as before. The sound is then added in a
  second FFmpeg run (`mux_command`: input `-ss` on the WAV, AAC 192 kb/s,
  padded and cut to the picture's length). The sound's cut point is worked
  out from the same shot start as the picture's, moved **0.12 s** later
  (`CaptureOptions.sound_delay_seconds`, measured in T20).
- **Checked before the rename:** the frame count as before; that the sound
  recording covers the whole shot; that the clip's audio track is as long
  as its picture to within one frame. Any of these failing fails the clip.
  A clip whose sound is all silence is kept, but the log line says WARNING.
- **Settings.** Along with the two above, `snd_mute_losefocus` (to `0`) and
  `volume` (to `0.5`) are read, changed and put back. With `--no-sound` they
  are left alone.
- **Clip names.** A clip with sound ends in `_sound.mp4`, e.g.
  `118241-119378_76561198000000001_b334da78_sound.mp4`. A `--no-sound` clip
  has the v1 name. So silent clips from v1 are never used as clips with
  sound, and they stay on disk. `render` (until T27) looks only for clips
  with sound.

### After death (T26)

A shot may run up to 5 s past its POV player's death (T22; the presets use
1.5 s). What T21 found is applied in `capture/plan.py`, `game.py` and `run.py`:

- **The camera is held.** `spec_freeze_time` is read, set to `10` for the
  whole session and put back, with the other settings (always, with or
  without sound). Without it CS2 moves to the killer about 3 s after the
  death. `spec_player` is sent only while seeking, before the shot, never
  again: the game ignores it for a dead player.
- **Checked on every take.** A `Job` knows its player's death tick
  (`death_tick`, `None` for a survivor or a death at the round's end). For a
  shot that ends after it, the console lines from the start of the playback
  are searched for `Scanned <n> entities, activated prediction` between the
  death (counted back from the final pause, like the shot's start) and the
  end. One means the camera left the victim: the take is thrown away and
  recorded again (up to 3 attempts), and the clip fails with "the camera left
  <name> <x> s after the death" if every take did.
- **Log.** The job line says "1.5 s after the death" for such a shot.
- **Real run (2026-11-15):** 3 victim shots (emlight, OutubroAK42, Mobin of
  round 17 of the sample demo, `victims` preset, each ending 1.5 s after the
  death) captured with sound in one session: 390 frames each, the view drops
  at the death and then stays on the body to the last frame in all three. No
  take was retaken for the camera. `spec_freeze_time` read `2` and was set to
  `10` and put back. Shot 4 had a playback drift of +316 ms (the log warns
  about it); the clip is still correct by eye.
- **Not checked with the real game:** the retake path for a camera that left
  (tests only; no clip in the run switched), a shot more than 1.5 s past the
  death, a death with no killer, other demos' replicated values.

## Open decisions

1. **Sound.** Answered by T20: per-process loopback of `cs2.exe`, see
   "Sound (spike T20)". No package and no HLAE needed. Built in T25, see
   "Sound (T25)".
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
