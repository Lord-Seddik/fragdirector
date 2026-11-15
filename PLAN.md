# PLAN — fragdirector

Status: **approved 2026-11-14**. T1–T14 done. T15: review done 2026-11-15, publishing waits for your decisions (see "Notes from T15"). Next task after that: T16.

## What it is

An open-source tool that reads a CS2 demo (`.dem`), finds the moments worth
showing, and turns them into a finished video — including the part nobody
wants to do by hand: showing a play from the killer's view **and** from each
victim's view in the seconds before they died.

**The only input is the `.dem` file.** You never record gameplay yourself and
the tool never analyses video files. The demo holds exact data (who killed
whom, when, where everyone was looking), which is what makes the detection
reliable. Finding highlights inside ordinary video files is a much harder
problem and is left as a possible later project.

```
.dem ──parse──> events ──detect──> moments ──score──> ranked moments
                                                          │
                                                          ▼
                              director ──> shot list  <──> visual editor (you adjust)
                                                          │
                                                          ▼
                                      capture (CS2 replays the demo) ──> clips ──assemble──> video
```

Two ideas hold the design together:

1. **The shot list.** Every shot is
   `{demo, start_tick, end_tick, pov_player, speed, label}`. The tool
   generates it; nothing downstream reads anything else.
2. **Everything generated is a suggestion you can edit.** The visual editor
   opens the shot list as a timeline where you add a second here, cut a shot
   there, add a moment the tool missed, change whose view is shown, reorder.
   The tool never goes straight from demo to video without you being able to
   step in (though you can let it, with `--auto`).

### What "capture" means

A demo is not a video — it is a recording of game data that only CS2 can
play back. So to get video, the tool starts CS2, makes it replay the demo,
jumps to each shot, locks the camera to the right player and saves the
footage. You do not play, spectate or press record; it runs by itself. This
step is called **capture** in this plan to avoid confusion with recording
gameplay.

## Decisions (change any of these before approving)

| Decision | Choice | Why |
|---|---|---|
| Name | `fragdirector` | Chosen in T1. `povcut` was taken (PyPI package and GitHub repo `povcut-app/povcut`); `demoreel` is free on PyPI but has many unrelated GitHub repos. |
| Language | Python 3.12 (already installed) | Best demo parser is a pip install; easy for others to run. |
| Demo parser | `demoparser2` | Fast, CS2-native, gives kill flags (headshot, noscope, through smoke, wallbang, blind) and per-tick view angles. |
| Interface | Visual editor in the browser, started with `fragdirector open demo.dem`; plain commands underneath for people who script | Editing by hand has to be easy. A local web page needs no installer and no build step, and works on any OS. |
| Editor tech | One HTML page with plain JavaScript, served by a small local Python server | No Node toolchain for contributors; nothing leaves the user's machine. |
| Video cutting | FFmpeg (already installed) | Concat, slow-mo, transitions. |
| Capture method | CS2's TCP console (`-netconport`, with password) to drive the demo + FFmpeg window capture (`gfxcapture`) to record | Chosen in T4: needs nothing but FFmpeg, changes no game files, 5 of 5 runs good. CS2's own `startmovie` writes nothing. No sound yet — see `docs/capture.md`. |
| License | MIT | Standard for community tools. |
| Game | CS2 only | CS:GO demos are a different format. |

## What gets detected (moments)

- **Multi-kills**: 2k–5k by one player inside a time window; ace; whole-round reel
- **Clutches**: 1vX won, with X and time left
- **By weapon**: AWP, Deagle, knife, Zeus, any filter
- **Flicks**: large view-angle change in the last ~200 ms before the killing shot
- **Timings**: very short gap between kills, opening kill, trade kill, kill in the last seconds of a round, last-second defuse
- **Style flags**: noscope, wallbang, through smoke, while blind, in the air, headshot-only
- **Deaths**: every death of a chosen player (for "how did I die" reels)

Each moment gets tags and a score from a weights file, so ranking is tunable.

## Director presets (how time is given to each shot)

- `killer` — killer POV only, pre-roll and post-roll scale with the gap between kills
- `victims` — each victim's POV for N seconds before their death
- `major-replay` — killer POV in real time, then the victims' POVs one by one, last kill in slow motion
- `budget` — "make it 30 seconds": time is split between shots by score, lowest-score shots dropped first

A preset only produces the first draft. You change it in the editor.

## The visual editor

Left side: every detected moment in the demo, with round, players, weapon,
tags and score. Filter by player, weapon, tag or round; tick the ones you want.

Main area: the timeline of shots for the video. For each shot you can:

- drag either end, or press `+0.5s / −0.5s` buttons, to lengthen or shorten it
- delete it, duplicate it, drag it to reorder
- switch the POV to any player alive at that time
- set speed (slow motion) and a label
- add a new shot by hand: pick round, time and player
- apply a preset to one moment ("add victims' views for this kill")

Each shot shows which kills fall inside it and the round clock, so you can
judge it without capturing first. Undo/redo, running total length, and
save/load of the shot list file. A "capture this shot" button gives a quick
preview once T10 exists.

## Tasks

Sample demo: put one of your `.dem` files in `demos/` (git-ignored). T3 onward needs it.

| ID | Goal | Files | Verify | Tag | Status |
|---|---|---|---|---|---|
| T1 | Scaffold repo: package, CLI entry point, tests folder, `.gitignore`, MIT license, `git init`; check name is free | `pyproject.toml`, `src/fragdirector/__init__.py`, `src/fragdirector/cli.py`, `tests/`, `.gitignore`, `LICENSE` | `pip install -e .` then `fragdirector --help` prints; `pytest` runs 0 tests cleanly | [easy] | done |
| T2 | Data model: Kill, Round, Moment, Shot, ShotList, with JSON save/load and the edit operations (trim, move, delete, insert, change POV) as plain functions | `src/fragdirector/models.py`, `src/fragdirector/edits.py`, `tests/conftest.py`, `tests/test_models.py`, `tests/test_edits.py`, `docs/shotlist.md` | `pytest` — round-trip save/load passes; every edit keeps the shot list valid (inside the round, POV player alive, start < end) | [hard] | done |
| T3 | Parser: `.dem` → rounds, kills (all flags), bomb events, player list, who is alive when | `src/fragdirector/parse.py`, `tests/test_parse.py` | `fragdirector analyze demos/x.dem --dump-kills` matches the in-game scoreboard kill counts | [medium] | done |
| T4 | Capture spike: find the most reliable way to make CS2 jump to a tick, lock a player's POV and save video with no manual input; write up the result | `docs/capture.md`, `spikes/capture_one.py` | Script produces one 10 s `.mp4` of a named player at a named tick, twice in a row | [hard] | done |
| T5 | Basic detectors: multi-kill, clutch, weapon filter, style flags, player deaths | `src/fragdirector/detect/basic.py`, `tests/test_detect_basic.py` | `pytest` on hand-built kill lists; `fragdirector analyze` lists the 5-kill from the sample demo | [medium] | done |
| T6 | Aim and timing detectors: flick angle, kill gaps, opening/trade/late-round | `src/fragdirector/detect/aim.py`, `src/fragdirector/detect/timing.py`, tests | `pytest` on synthetic tick data; flick value looks right on 3 kills checked by eye in the demo | [medium] | done |
| T7 | Scoring: weights file → score and rank moments | `src/fragdirector/score.py`, `src/fragdirector/weights.default.toml`, tests | `pytest`; changing a weight changes the ranking in `fragdirector analyze` output | [medium] | done |
| T8 | Director: moments + preset → shot list, incl. victim-POV shots and time budgeting | `src/fragdirector/director.py`, `src/fragdirector/presets/*.toml`, tests | `pytest`: shots never overlap wrongly, never leave the round, total length within 5% of budget; `fragdirector plan` writes a valid shot list for the sample 5k | [hard] | done |
| T9 | Editor backend: local server exposing moments, the shot list and the T2 edit operations; `fragdirector open` launches it and the browser | `src/fragdirector/server.py`, `src/fragdirector/cli.py`, `tests/test_server.py` | `pytest` hits every endpoint; `fragdirector open demos/x.dem` opens a page that shows the demo's moments as JSON-backed list | [medium] | done |
| T10 | Editor — moments panel: list, filters, tick to include, apply preset, generates the first-draft timeline | `src/fragdirector/web/index.html`, `src/fragdirector/web/app.js`, `src/fragdirector/web/style.css` | In the browser: filter to one player, tick 2 moments, timeline shows their shots; reload keeps them | [medium] | done |
| T11 | Editor — timeline editing: trim by drag and by ±0.5 s, delete, duplicate, reorder, change POV, speed, label, add shot by hand, undo/redo, total length | `src/fragdirector/web/app.js`, `src/fragdirector/web/timeline.js`, `src/fragdirector/web/style.css` | In the browser: do each edit once on the sample 5k, save, reopen — the saved shot list matches what was on screen; invalid edits are refused with a message | [medium] | done |
| T12 | Capture: implement the method chosen in T4, one clip per shot, resumable; "capture this shot" button in the editor | `src/fragdirector/capture/`, `src/fragdirector/server.py`, tests for command generation | `fragdirector capture shots.json` produces one clip per shot for the sample 5k with correct POVs | [hard] | done |
| T13 | Assembler: FFmpeg concat, slow-mo, optional crossfade and player-name label; "make video" button in the editor | `src/fragdirector/render.py`, tests | `fragdirector render shots.json` outputs a playable `.mp4` whose length matches the shot list | [medium] | done |
| T14 | README with install, quick start, screenshots of the editor, a demo GIF; contribution notes | `README.md`, `CONTRIBUTING.md`, `docs/images/editor.png`, `docs/images/demo.gif` | A fresh clone following the README reaches an edited shot list without help | [easy] | done |
| T15 | Final review: code, security of the local server, of launching the game and of running FFmpeg with user paths; then publish to GitHub | whole repo | Review notes resolved; repo public (you push it) | [hard] | in progress (review done and fixes verified; not pushed yet) |
| T16 | Make `test_demo_files_are_found_as_written_or_beside_the_shot_list` (`tests/test_capture.py`) independent of the current folder: run it with `monkeypatch.chdir(tmp_path)`. It fails when `demos/match.dem` exists under the working directory | `tests/test_capture.py` | `pytest` passes from the repo root and from a folder that has a `demos/match.dem` | [easy] | todo |
| T17 | Text from a demo that is printed to the terminal (player names, labels, notes) can hold control characters; a hand-made demo could use them to change what the terminal shows. Replace control characters with `?` where the CLI prints (not in the model: capture needs the real name) | `src/fragdirector/cli.py`, `tests/` | `pytest`: a player named with an escape sequence prints without it in `analyze` and `plan` | [easy] | todo |
| T18 | Capture loose ends found in T15: (a) the recorder's FFmpeg error output is only read after it stops, so a flood of errors could stall the recording until the time-outs; send it to a file. (b) if the game starts but its console never opens (180 s), the game is left running; say so in the error. | `src/fragdirector/capture/game.py`, `src/fragdirector/capture/run.py`, `tests/test_capture.py` | `pytest` with a fake recorder and console; one real capture still works | [medium] | todo |

Notes from T3 (not tasks yet; decide if any are wanted):

- `BombEvent.site` holds the game's raw bombsite id (for example `"165"`), not `A`/`B`; the demo does not say which is which.
- The in-game scoreboard counts a death made before the match starts (a player typing `kill`); the parser drops it because that player is alive again afterwards, so deaths can be 1 lower. Kills and assists match exactly.
- A player who disconnects mid-round counts as alive until the round ends.

Notes from T5 (not tasks yet; decide if any are wanted):

- T5 also touched `src/fragdirector/cli.py` (not in its file list): `analyze` now prints a "Moments:" list of multi-kills and clutches.
- The weapon, style-flag and player-death detectors exist as functions but have no CLI flags yet (for example `--player`, `--weapon`).
- An ace is reported as one `5k` even when its kills are further apart than the 10 s multi-kill window (the sample demo's ace has a 19.6 s gap after the first kill).
- Clutch "time left" is 115 s minus time since freeze end; it ignores the bomb timer. T7/T8 need a bomb-aware value if they use it.
- Knife and Zeus weapon names (`knife*`, `bayonet`, `karambit`, `taser`) were not checked against a real demo that has such kills.
- Non-ASCII player names print as `?` on the Windows console (`KeT?min`).

Notes from T4 (not tasks yet; decide if any are wanted). Details in `docs/capture.md`:

- **Clips have no sound.** Decide before T12: add a loopback recorder package (`soundcard` or `PyAudioWPatch`), or test HLAE, which records the game's own sound. Either is a small spike of its own.
- HLAE was not tested (needs a download and loads code into the game). It would give frame-by-frame rendering and smooth slow motion; worth a spike only if real-time capture proves too weak.
- T12 should keep one CS2 session open for a whole shot list; the spike restarts the game per clip (about 25 s each time).
- T12 must keep the pause check and the end-tick check from the spike: CS2 sometimes keeps playing after reporting a pause.
- T12 should check the player name against the parsed demo before starting the game; CS2 gives no error for a wrong name.
- T8: a victim's view becomes a chase camera on the body after death, so victim shots should end at or just after the death tick.
- Capture needs FFmpeg 8.0 or newer and Windows; the README (T14) should say so.
- T15 review item: the console port listens on all network interfaces; the per-run random password is the only protection.
- The picture is about 60 ms behind the requested tick. Fine for now; T12 can subtract it if it matters.

Notes from T6 (not tasks yet; decide if any are wanted):

- **The sample demo has no real flicks, and the 30° default correctly flags none.** Over 141 kills the view turned a median 1.6° in the 200 ms before the kill, 95% of kills under 7.4°, largest 18.8°. The default stays at 30° until a demo with real flicks is available to tune on.
- By-eye check done on the three largest (ticks 44124, 100431, 24598; clips in `output/t6_flick_*.mp4`): the measured turn matches what the view does, and the numbers match an independent calculation from the raw angles. All three are sprays or tracking — the player is already firing before the window starts — not flicks. Lowering the threshold would only add these.
- Possible improvement (new task if wanted): only count a flick when the attacker was not already firing in the ~0.5 s before the kill shot. Needs `weapon_fire` events, which the parser does not read yet.
- Timing detectors on the sample demo: 18 opening kills (one per round), 22 trade kills, 3 quick-kill pairs, 1 late kill. The late kill (round 4, tick 34367) is after the clock ran out, with 0.0 s left; decide whether kills after the round is decided should count.
- Late kills and late defuses assume a 115 s round and a 40 s bomb timer; `Round` does not store the real values.
- Nothing from T6 is shown by `fragdirector analyze` yet; `detect/__init__.py` and the CLI were not touched.

Notes from T7 (not tasks yet; decide if any are wanted):

- T7 also touched `src/fragdirector/cli.py` (`analyze` now prints moments best first with their score, and takes `--weights PATH`) and `pyproject.toml` (ships `weights.default.toml` in the built package; checked in a built wheel).
- Score = weight of the moment's kind + a bonus per tag + per-kill bonuses for style flags and weapon. The default numbers are first guesses (5k = 100, ace +50, 1v3 = 40, noscope +15 per kill, ...) and have not been tuned.
- Scoring is purely additive: a 2k with two AWP noscopes can outrank a plain 3k. Fine for now; revisit when tuning.
- Clutch "time left" and flick size are not used in the score.
- `analyze` still lists only multi-kills and clutches; the T6 detectors and the weapon/style/death detectors are scored correctly but not shown.

Notes from T8 (not tasks yet; decide if any are wanted):

- T8 also touched `src/fragdirector/cli.py` (new `fragdirector plan <demo> [--preset] [--top] [--player] [--round] [--budget] [--out] [--weights]`), `pyproject.toml` (ships `presets/*.toml`) and `docs/shotlist.md` (section "Rules the director adds").
- Overlap rule: two shots of the same player's view at the same speed never share a tick (they are merged). The same ticks from another player's view, or the same player at another speed (the replay), are allowed.
- Budget rule: length is on-screen time (ticks / tick rate / speed). A budget is applied after any preset: lowest-score shots are dropped first, the rest are trimmed or lengthened by score. **A budget also lengthens a short draft** (the ace's 20.8 s killer draft becomes 30.0 s); set `max_stretch = 1` in a preset to turn that off.
- When a budget cannot be met (smaller than the shortest possible list, larger than the longest) `plan` gets as close as it can and prints a note. A multi-kill shot is never split, so the ace cannot go under 13.3 s.
- **Decision wanted:** a victim's shot ends on the death tick, because the validity rule needs the POV player alive, so the fatal moment itself is not shown from the victim's view. Allowing about 0.25 s past death in `shot_problems` (`models.py`) would fix it; T4 showed the view turns into a chase camera only after that.
- A clutch's killer view shows its kills only, not the defuse or the wait for the clock.
- `plan` uses the same moments as `analyze` (multi-kills and clutches). With no `--top`, `--player` or `--round` it takes every moment.
- Preset numbers (pre-roll 2 s + 0.5 s per second of kill gap, victim view 4–5 s, replay at 0.5x, minimum shot 2 s, ...) are first guesses, not tuned.
- `director.py` (607 lines) was checked by its 88 tests, by running `plan` on the ace, and by an independent validity check of the written files; it was not reviewed line by line. T15 should read it.

Notes from T9 (not tasks yet; decide if any are wanted). The API is listed at the top of `server.py`:

- T9 also touched `pyproject.toml` (ships `web/*`) and created a placeholder `web/index.html`, `web/app.js` and `web/style.css` that only list the moments; T10 replaces them. The server serves any `.html`, `.js` or `.css` file by plain name from `web/`, so T10 needs no server change for new files such as `timeline.js`.
- `fragdirector open <demo> [--shots PATH] [--weights PATH] [--port N] [--no-browser]`. `--shots` (default `shots.json`) is the one file the editor loads at startup (if it exists) and saves to; no request can name another path. Without `--port` a free port is chosen.
- Moments are the same as `analyze` (multi-kills and clutches), best first; a moment's `id` is its position in that list and is only stable while the server runs. The preset endpoint takes `moment_ids`, a built-in preset name (a file path is refused) and an optional `budget`; `mode` is `replace` (default) or `append`.
- Edit endpoints exist for every function in `edits.py` (trim, trim_by, move, delete, duplicate, insert, change_pov, set_speed, set_label). A refused edit answers 422 with the `EditError` text and the shot list is unchanged. Malformed requests answer 400 (413 body too big, 415 not JSON, 403 wrong Host or Origin).
- Undo/redo is left to the browser: every edit answers with the whole new shot list, and `PUT /api/shotlist` puts an older one back. `PUT` and load accept a list that has problems and report them in `problems`, like `edits.py` does.
- The server keeps all state in memory with one lock; it is meant for one user and one tab. Two tabs would overwrite each other's shot list.
- Checked against `demos/` (18 rounds, 23 moments; a `major-replay` draft from the 5k via the endpoint; save wrote the file; the page lists the moments in the browser pane). Not checked: `open` without `--no-browser` (the default `webbrowser.open` path), a non-Windows platform.
- T15 should read the "Safety" list in `server.py`: Host and Origin checks, JSON-only writes, body cap, no CORS, fixed save path, static file names. There is no authentication; any local process can use the server.

Notes from T10 (not tasks yet; decide if any are wanted):

- The page has the moments panel (filters: player, round, kind, weapon, tag; tick boxes; preset, optional budget, replace/append) and a **read-only** timeline (shot list with player, round clock, length, label and the kills inside each shot, plus Save / Reload-from-file buttons). Editing the timeline is T11.
- Reload keeps the timeline because the server holds it; the ticked moments and filters are kept in the browser's `localStorage`, keyed by demo path and by moment (kind, round, player, start tick), since moment ids only last while the server runs. Ticks and filters are per browser; the shot list is per server run.
- The default preset in the dropdown is `killer`. The `budget` preset has its own default length, so choosing it for one short moment reports "budget could not be met exactly" (the server's `budget_met`).
- Checked in the browser pane on the sample demo (23 moments): filtered to one player, ticked 2 moments, built 3 shots (23.8 s, no problems), reloaded and found the same filter, ticks and timeline. `pytest` 305 passed. Not checked: other browsers, a phone-sized window beyond the CSS rules, `Save to file` / `Reload from file` buttons (the server endpoints are covered by `test_server.py`).
- The page uses no inline scripts or styles (the server's Content-Security-Policy would block them); keep it that way in T11.

Notes from T11 (not tasks yet; decide if any are wanted):

- T11 also touched `src/fragdirector/web/index.html` (undo/redo buttons, "Unsaved changes" mark, the add-a-shot form, `timeline.js` script tag). The timeline code moved from `app.js` to the new `timeline.js`; `app.js` now starts on `DOMContentLoaded` so both files are loaded first. No Python changed; `pytest` still 305 passed.
- Every shot card has: POV dropdown (players alive for the whole shot), a bar with the shot inside its round (green = POV player alive, red lines = kills, blue handles to drag either end), Start/End `-0.5s`/`+0.5s`, speed, label, Up/Down, Duplicate, Delete. Reordering also works by dragging the `::` grip.
- Undo/redo (also Ctrl+Z / Ctrl+Y) are kept in the browser, up to 200 steps, and put an older list back with `PUT /api/shotlist`. Building a draft and "Reload from file" count as steps. They are lost when the page is reloaded.
- Add by hand: round, start in seconds on the round clock, length, player (only those alive the whole span), speed, label; always added at the end (move it with Up/Down). A span that runs outside the chosen round is refused by the page, because it would otherwise land in the next round.
- Checked in the browser pane on the sample demo (5k draft, 12 shots): each edit once (nudge, drag by mouse, delete, duplicate, reorder by button and by drag, POV, speed, label, add by hand), 12 undos and 12 redos back to the same list, save, file matched the screen, restarted the server and the page showed the same 14 shots and 69.6 s. Refused edits (end past the death tick, speed 0, shot ending before it starts, dead player) show the server's message and change nothing. No console errors apart from those intentional 422 answers.
- Not checked: touch screens, other browsers, a very small window beyond the CSS rules, keyboard-only use of the drag handles (use the `+/-0.5s` buttons instead).
- Dragging the end of a victim's shot past the death tick is refused (the T8 "decision wanted" note: allowing about 0.25 s past death in `shot_problems` would let the fatal moment show).
- Two tabs on one server still overwrite each other (T9 note); the editor does not warn about it.

Notes from T12 (not tasks yet; decide if any are wanted). Details in `docs/capture.md`, section "In fragdirector (T12)":

- **Sound: decided "video only for now"** (asked before T12). Clips have no audio track. Sound is still open: a loopback recorder (`soundcard` / `PyAudioWPatch`) or an HLAE spike, each a task of its own.
- T12 also touched `src/fragdirector/cli.py` (new `fragdirector capture <shots.json> [--clips DIR] [--demo PATH] [--only N,N] [--force] [--dry-run] [--fps] [--steam] [--port] [--demo-ui]`; `open` takes `--clips DIR`), `src/fragdirector/web/index.html`, `timeline.js`, `style.css` (Capture button and "clip ready" mark per shot, capture status box with the log), `docs/capture.md`, and `tests/test_server.py` (capture endpoint tests; the client fixture takes app options).
- Clips go to `clips/` next to the shot list, named from the shot alone (`capture.clip_path(shot, clips_dir)`), so **T13 finds each shot's clip with `clip_path`**. Shots that differ only in speed or label share one clip; **T13 must apply `speed` itself**. A 0.5x shot is captured at 60 fps real time, so it plays at 30 distinct frames per second when slowed; smoother slow motion needs a higher capture fps (`--fps 120`, untested) or frame interpolation.
- Checked: `fragdirector capture output/t12/shots.json` on the sample 5k (`major-replay`, 8 shots, 5 players incl. a non-ASCII name) wrote 8 clips, 1920x1080, 60 fps, exact frame counts; the HUD name in every clip is the shot's POV player; playback drift 13–38 ms. It took 3 runs; the resumable rerun picked up where each stopped. `pytest` 358 passed. The editor (browser pane) shows "clip ready" on all 8 shots.
- Two new CS2 traps found and handled (now in the code and tests): **a forward `demo_gototick` from a paused demo never moves** (it prints "skipping forward" and stays), so the demo is resumed before a forward seek; and **the first seek right after loading can be dropped silently**, so a seek is resent when CS2 does not answer in 5 s.
- After a forward seek the demo sometimes runs past the shot start before the pause check stops it; the clip is then retried with a backward seek (cost: about 15 s, seen twice in 8 clips).
- Not checked: the editor's Capture button against the real game (the endpoint is tested with a fake runner; it calls the same code as the CLI); a shot list with two demos; a shot starting within 2 s of a spawn; `--fps` other than 60.
- T15 review items: `POST /api/capture` starts the game from a local web request (protected by the same Host/Origin/JSON checks as the other endpoints, request gives only a shot position); the console password is now 32 hex characters per run and the port is checked free before the game starts.

Notes from T13 (not tasks yet; decide if any are wanted). Details in `docs/render.md`:

- T13 also touched `src/fragdirector/cli.py` (new `fragdirector render <shots.json> [--clips] [--out] [--crossfade SECONDS] [--labels] [--crf] [--force] [--dry-run]`), `src/fragdirector/server.py` (`GET`/`POST /api/render`; the capture state class is now `TaskState`, used for both), `src/fragdirector/web/index.html`, `timeline.js`, `style.css` (a "Make the video" form with crossfade, labels, button and status box), and `tests/test_server.py`.
- **Label = the shot's own `label` text**, not a looked-up player name: the shot list does not hold names, and the director already writes "<name>'s view" labels. Edit the label in the editor to change it. Shown bottom-left for the first 3 s (`RenderOptions.label_seconds`; no flag yet).
- Length is exact to the frame for hard cuts (checked: the sample 5k, 8 shots, 44.77 s = the shot list's 44.77 s, 2686 frames, also with one shot at 0.5x). With a crossfade it is `crossfade x (shots - 1)` shorter (checked: 0.4 s gave 41.97 s = 44.77 - 7 x 0.4) and the frame count may differ by up to one frame per join.
- Slow motion repeats frames (30 distinct pictures per second at 0.5x of a 60 fps clip). Smoother needs a higher capture `--fps` or frame interpolation (`minterpolate`, slow); not done.
- Speed: about 85 s for 44.8 s of 1080p60 on this laptop (x264 `medium`, CPU only). A faster preset or `h264_nvenc` would cut it; not done.
- The editor's Make video button always writes `<shot list name>.mp4` next to the shot list and replaces it; the CLI refuses to replace a file without `--force`. The page does not play or link the video; it shows the path.
- Checked in the browser pane: the button, its hint and status box, and a real render of the 8 sample clips (done, file 1920x1080, 2686 frames). Not checked in the browser: the crossfade and labels inputs (the automation could not fill them; the same options are covered by the CLI run and by `test_server.py`). `pytest` 413 passed. One run showed a single failure of the old `test_oversized_and_malformed_bodies` (connection reset); three later full runs passed.
- T15 review items: `render_video` runs FFmpeg with the shot list's clip paths (derived from the shot, not free text), label text goes through files, and the editor request gives only a number and a switch. A very long shot list is refused at about 30 000 characters of filter graph (measured: about 270 shots, or about 90 with labels).

Notes from T14 (not tasks yet; decide if any are wanted):

- T14 also created `CONTRIBUTING.md`, `docs/images/editor.png` (headless Chrome screenshot of the editor on the sample demo) and `docs/images/demo.gif` (9 s → 7 s of the sample render at 480 px, 10 fps, 3.9 MB). The GIF is cut from `output/t12/shots.mp4`, which is git-ignored, so it can be regenerated.
- The README's clone URL is a placeholder (`<your-account>`) until T15 publishes the repo.
- Fresh-copy check: the tracked and untracked (not ignored) files were copied into a new folder, installed into a new venv with `pip install -e ".[dev]"`, then `analyze`, `plan --preset major-replay --top 5`, `capture --dry-run` and `pytest` were run. The copy has no `demos/*.dem` except the one added by hand. This found T16.
- Not checked: the Windows `open` default (opening a browser), `capture` for real (needs CS2), the Make-video button after the README changes.

Notes from T15 (the review; what is left is for you to decide):

- **Read line by line:** `server.py`, `capture/` (`game.py`, `run.py`, `plan.py`), `render.py`, `director.py`, `models.py`, `edits.py`, `parse.py`, `cli.py`. **Checked for one thing only:** `web/*.js` and `index.html` for text from a demo reaching the page as HTML (it cannot: the page only makes elements and text nodes, and the server's Content-Security-Policy blocks inline script), `score.py` for file loading. **Not read line by line:** `detect/*.py`, the rest of the page code, `spikes/capture_one.py`, the tests. The running server was also tried with hostile requests (wrong Host, foreign and `null` Origin, non-JSON writes, path tricks, oversized and odd bodies).
- **Fixed, each with a test (`pytest`: 422 passed, was 413):**
  - A request body holding a number of more than 4300 digits crashed the request handler (the connection was dropped, a traceback printed). It now answers 400. The same in a shot list file now gives the normal "not a valid JSON file" message.
  - A shot with a speed such as `1e-320`, or a tick of hundreds of digits, was accepted and kept; after that every answer about the shot list failed until the list was replaced. A shot's speed must now be between 0.01 and 100 and its ticks at most 2147483647 (`models.py`, `docs/shotlist.md`). The server also works out its answer before it keeps a new shot list, so a list it cannot answer for is never kept.
  - `fragdirector capture` on a shot list whose `demo` is a network path (`\\server\share\x.dem`) looked for the file there. On Windows that sends your login (as a hash) to that server, so a shot list from someone else could collect it. Such a path is no longer opened; only a file of that name next to the shot list is used, or the one you pass with `--demo`.
  - The editor let a capture start while the video was being made from the same clips; it now answers 409, like the other way round already did.
  - An internal error in the editor (500) printed nothing anywhere; the traceback now goes to the console (the browser still gets no details).
- **Reviewed, no change needed:** the Host and Origin checks, JSON-only writes, no CORS, the body cap, static files by plain name; the save and video paths fixed at startup; console commands (player name and demo path are refused if they hold a quote, semicolon or control character, in two places); every FFmpeg and FFprobe call takes a list of arguments, never a shell; label text reaches FFmpeg through a file; clip names are built from ticks, a cleaned player id and a hash. `director.py`: no problem found reading it; the budget sums keep every shot inside its bounds.
- **Known and left as it is:** the game's console port listens on every network interface while a capture runs, protected only by the 32-character per-run password, which is also visible to other programs on this PC in the game's command line (CS2 has no option to listen on this PC only). The editor has no login, so any program on this PC can use it. A request whose Origin is another port on localhost is accepted (a T9 decision, kept by a test); a web page there still cannot write, because a JSON write needs a CORS preflight that the server refuses. All three are now in the README under "What it does on your machine".
- **Also changed for publishing:** a test path that held this PC's user folder name, and a real SteamID in a `docs/capture.md` example, were replaced with made-up ones. README has the new section named above.
- **Your decisions before pushing:**
  1. All 3 commits carry your personal e-mail address as author; it becomes public with the push. To hide it, set GitHub's no-reply address and rewrite the 3 commits before the first push.
  2. `docs/images/editor.png` and `demo.gif` show the player names of your sample demo, and `docs/capture.md` and the spike use the name `sedx` in an example. Fine if you are happy for those to be public.
  3. The repository's name and your GitHub account, to replace `<your-account>` in the README's clone line. `gh` is not installed here, so the repo is made on github.com.
  4. `PLAN.md`, `CLAUDE.md` and `.claude/agents/` are tracked and would be published (the README lists `PLAN.md`).
- Nothing was committed or pushed in T15. There is no Git remote yet, and the branch is `master`.

After T11 the tool is already useful without capture: you get an edited shot
list with exact round times and players, which you can follow by hand in the
CS2 demo viewer.

## Risks

1. **Automated capture in CS2 (T4 — answered).** T4 found a method that
   works with no extra software: the game's TCP console plus FFmpeg window
   capture (`docs/capture.md`). What is left of the risk: it has no sound
   yet, it records in real time so a slow PC gives a choppy clip, and a CS2
   update could remove the TCP console (the VConsole port and HLAE are the
   fallbacks; the last resort is still exporting the shot list as a console
   script and capturing with OBS).
2. **Flick and timing detection quality (T6).** Angle change alone flags
   spray transfers and panic turns as flicks. Thresholds will need tuning on
   real demos. The editor is the safety net: wrong picks are one click to
   remove.
3. **No video preview before capture.** The editor shows what happens inside
   a shot as data (kills, clock, players), not as footage. Seeing footage
   needs CS2 to render it, so preview is "capture this one shot", which takes
   some seconds.
4. **Game updates.** Demo format and console commands change; parser and
   capture are kept behind small interfaces so one can be swapped.

## Out of scope for v1

Finding highlights in video files, kill-feed/HUD redraw overlays, free-camera
cinematic shots, 2D map replay in the editor, music sync, CS:GO demos, cloud
rendering.
