# Contributing to fragdirector

Thanks for looking at this. The project is small; the [README](README.md)
shows how the parts fit together.

## Setup

```bash
pip install -e ".[dev]"
pytest
```

The tests do not need CS2. Only the one test that parses a real demo needs a
`.dem` file, and it skips itself if none is present. To try the real tools, put a `.dem` file in
`demos/` (Git ignores that folder).

## Layout

The code is in `src/fragdirector/`. Each stage is a module with a small
interface, so one can be replaced without touching the others:

- `parse.py` reads the demo; nothing else reads `.dem` files directly.
- `detect/`, `score.py`, `director.py` work on plain data (kills, moments,
  shots) and do not touch files.
- `models.py` and `edits.py` define the shot list. Every edit goes through
  `edits.py`, so the shot list stays valid.
- `server.py` and `web/` are the local editor. `capture/` and `render.py`
  run the game and FFmpeg. `capture/sound.py` records the game's sound
  (Windows only, standard library only).

The shot list is the one thing passed between stages. See
[docs/shotlist.md](docs/shotlist.md).

## Working on a change

- Keep each change to one thing. If you find something else that needs
  fixing, open an issue or a separate pull request rather than fixing it in
  the same change.
- Add or update tests for the change, then run `pytest`.
- For changes to the editor, check the page in a browser: the server's
  Content-Security-Policy blocks inline scripts and styles, so keep them in
  the `.js` and `.css` files.
- Do not commit demos, rendered video, or anything under `output/`. They are
  large and personal; `.gitignore` already covers them.

## Things to keep in mind

- **The local server** answers only to a Host of `localhost` or `127.0.0.1`
  (and an Origin of the same, when sent), and accepts only JSON writes. Keep those checks on every new endpoint, and never let
  a request name a file path on disk (the save and clip locations are fixed by
  the command line).
- **Capture starts CS2** and talks to it over a local console port with a
  per-run password. Do not weaken either without discussing it first.
- **Game settings changed by a capture** must be put back afterwards. The
  list is in `capture/plan.py` (`SAVED_SETTINGS`, `SOUND_SETTINGS`,
  `AFTER_DEATH_SETTINGS`); add a new setting there, not in the code that uses it.
- **FFmpeg** is run with paths that come from the shot list, not from free
  text. Keep it that way.
- **Game and format changes:** CS2 updates can break the demo parser and the
  console commands. The notes in `docs/capture.md` record what was checked and
  against which game build.

## Commit messages and pull requests

Short imperative subject, for example `Add crossfade option to render`. In the
description, say what changed, how you checked it, and what you did not check.

## License

By contributing you agree that your changes are released under the MIT
license in [LICENSE](LICENSE).
