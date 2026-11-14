# The shot list

The shot list is the one file every later step reads. The director writes
it, the editor changes it, capture and render follow it. It is plain JSON,
so you can also edit it by hand.

## File format

```json
{
  "version": 1,
  "tick_rate": 64,
  "shots": [
    {
      "demo": "demos/match.dem",
      "start_tick": 51200,
      "end_tick": 51840,
      "pov_player": "76561198000000001",
      "speed": 1.0,
      "label": "4k, kill 1"
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `version` | Format version. Currently `1`. |
| `tick_rate` | Demo ticks per second, used to turn ticks into seconds. |
| `shots` | The shots, in the order they appear in the video. |

Each shot:

| Field | Meaning |
|---|---|
| `demo` | Path of the `.dem` file the shot comes from. |
| `start_tick` | First tick shown. |
| `end_tick` | Tick the shot stops at; this tick itself is not shown. |
| `pov_player` | Id of the player whose view is shown (see below). |
| `speed` | `1.0` is real time, `0.5` is half speed. Optional, default `1.0`. |
| `label` | Free text shown in the editor. Optional, default empty. |

A shot is `end_tick - start_tick` ticks long. In the finished video it lasts
`(end_tick - start_tick) / tick_rate / speed` seconds.

Shots may overlap in demo time. That is how the same play is shown twice,
once from the killer's view and once from a victim's.

### Rules the director adds

These hold for every shot list the director writes (`director.py`). They
are not part of the validity check, so a hand-edited file may break them.

- **No repeated footage.** Two shots that show the same player's view at
  the same speed never share a tick; shots that would are merged into one.
  The same ticks through another player, or through the same player at
  another speed (the slow-motion replay), are fine. `wrong_overlaps` in
  `director.py` checks this.
- **A victim's shot ends exactly on the death tick**, the latest end the
  check below allows.
- **Length means length in the finished video**, slow motion included (the
  formula above). The minimum shot length and `--budget` both use it.

Player ids are text. For a human player it is their SteamID64; it is kept as
text because numbers that long are damaged by JavaScript. Treat it as an
opaque key: the parser also gives ids to bots, which have no SteamID.

## What makes a shot valid

Checked when the file is loaded (an error names the field):

- the fields above and no others; ticks are whole numbers, 0 or more
- `start_tick` is before `end_tick`
- `speed` is more than 0

Checked against the demo (`shot_problems` / `shotlist_problems` in
`models.py`):

- **Inside one round.** The shot starts inside a round and ends no later
  than that round's `end_tick`. A round runs from the spawn at the start of
  freeze time until the next round begins, so a shot may continue after the
  last kill.
- **POV player alive.** The player is alive from `start_tick` to
  `end_tick`. A player counts as alive up to, but not on, the tick of their
  death, so a victim's shot can end exactly on their death tick and no
  later.
- **Same demo.** The file name in `demo` matches the demo being checked.
  Only the file name is compared, so `demos/match.dem` and
  `C:\Demos\match.dem` are the same demo.
- **Same tick rate** as the demo (for the list as a whole).

## Edit operations

All in `fragdirector/edits.py`. Each takes a shot list and returns a new
one; the original is never changed, which is what makes undo simple.
Positions count from 0.

| Function | What it does |
|---|---|
| `insert(shotlist, demo, shot, index=None)` | Add a shot at a position, or at the end. |
| `delete(shotlist, index)` | Remove a shot. |
| `move(shotlist, from_index, to_index)` | Reorder: the shot ends up at `to_index`. |
| `duplicate(shotlist, index)` | Put a copy right after the shot. |
| `trim(shotlist, demo, index, start_tick=None, end_tick=None)` | Set a new start and/or end. |
| `trim_by(shotlist, demo, index, start_delta=0, end_delta=0)` | Shift start and/or end by ticks (`end_delta=32` is +0.5 s at 64 ticks). |
| `change_pov(shotlist, demo, index, pov_player)` | Show the same span through another player. |
| `set_speed(shotlist, index, speed)` | Set slow motion or speed-up. |
| `set_label(shotlist, index, label)` | Set the label. |

An edit that would break one of the rules above is refused with an
`EditError`. Its message is written for the user, for example
`donk is no longer alive from tick 3000; the shot ends at tick 3001.`
Nothing is clamped silently. To know in advance how far a shot can be
stretched, or which players it can switch to, use `Demo.allowed_span` and
`Demo.players_alive_during`.

Only the shot being edited is checked, so a hand-edited file with a problem
in one shot can still be repaired in the editor.

## The demo data behind the checks

`models.py` also defines what the parser produces: `Demo` holds `Player`s,
`Round`s (each with one `Life` per player: side, and from which tick to
which tick they were alive), `Kill`s and `BombEvent`s. `Moment` is a detected
highlight before it becomes shots. All of them convert to and from JSON with
`to_dict` / `from_dict`; `Demo` and `ShotList` also have `save` / `load`.
