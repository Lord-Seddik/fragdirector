"""Command-line entry point for fragdirector."""

import argparse
import sys
import unicodedata

from fragdirector import __version__


def _printable(text: object) -> str:
    """Text from a demo or shot list, safe to print: control characters (escape
    codes, line breaks, tabs) and format characters (right-to-left override,
    zero-width marks) become '?', so they cannot change the terminal's display.
    The model keeps the real text; only the printed copy is changed."""
    return "".join(
        "?" if unicodedata.category(ch) in ("Cc", "Cf") else ch for ch in str(text)
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fragdirector",
        description="Turn a CS2 demo (.dem) into a finished multi-POV highlight video.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    commands = parser.add_subparsers(dest="command")

    analyze = commands.add_parser("analyze", help="read a demo and print what is in it")
    analyze.add_argument("demo", help="path to a CS2 .dem file")
    analyze.add_argument(
        "--dump-kills",
        action="store_true",
        help="list every kill, round by round",
    )
    analyze.add_argument(
        "--weights",
        metavar="PATH",
        help="a weights file (TOML) that changes the default scoring",
    )

    plan = commands.add_parser(
        "plan", help="turn a demo's best moments into a first-draft shot list"
    )
    plan.add_argument("demo", help="path to a CS2 .dem file")
    plan.add_argument(
        "--preset",
        default="killer",
        metavar="NAME_OR_PATH",
        help="killer (default), victims, major-replay, budget, or your own preset file (TOML)",
    )
    plan.add_argument("--top", type=int, metavar="N", help="use only the N best moments")
    plan.add_argument("--player", metavar="NAME", help="use only this player's moments")
    plan.add_argument("--round", type=int, metavar="N", help="use only moments from this round")
    plan.add_argument(
        "--budget",
        type=float,
        metavar="SECONDS",
        help="fit the video to this length (shots are trimmed, stretched or left out)",
    )
    plan.add_argument(
        "--out", default="shots.json", metavar="PATH", help="shot list file to write (default shots.json)"
    )
    plan.add_argument(
        "--weights",
        metavar="PATH",
        help="a weights file (TOML) that changes the default scoring",
    )

    open_ = commands.add_parser(
        "open", help="open a demo in the shot-list editor (a local web page)"
    )
    open_.add_argument("demo", help="path to a CS2 .dem file")
    open_.add_argument(
        "--shots",
        default="shots.json",
        metavar="PATH",
        help="shot list file the editor loads (if it exists) and saves to (default shots.json)",
    )
    open_.add_argument(
        "--weights",
        metavar="PATH",
        help="a weights file (TOML) that changes the default scoring",
    )
    open_.add_argument(
        "--port", type=int, default=0, metavar="N", help="port to listen on (default: any free port)"
    )
    open_.add_argument(
        "--no-browser", action="store_true", help="do not open the browser; only print the address"
    )
    open_.add_argument(
        "--clips",
        metavar="DIR",
        help="folder for captured clips (default: 'clips' next to the shot list file)",
    )

    capture = commands.add_parser(
        "capture",
        help="record one clip per shot from CS2 (Windows; starts the game)",
        description="Start CS2, record every shot of a shot list from the game window and "
        "write one .mp4 per shot with the game's sound (only CS2's own sound, not other "
        "programs'). Clips that already exist are skipped, so an interrupted capture can "
        "simply be run again. Needs Steam running, CS2 closed, FFmpeg 8.0 or newer on PATH, "
        "and for the sound Windows 10 2004 or newer.",
    )
    capture.add_argument("shots", help="shot list file (from 'plan' or the editor)")
    capture.add_argument(
        "--clips",
        metavar="DIR",
        help="folder for the clips (default: 'clips' next to the shot list file)",
    )
    capture.add_argument(
        "--demo", metavar="PATH", help="use this .dem file for the shots from a demo of the same name"
    )
    capture.add_argument(
        "--only", metavar="N,N,...", help="capture only these shots (numbers as the editor shows them)"
    )
    capture.add_argument("--force", action="store_true", help="capture again even if a clip exists")
    capture.add_argument(
        "--dry-run", action="store_true", help="print what would be captured, without starting the game"
    )
    capture.add_argument("--fps", type=int, default=60, help="frames per second (default 60)")
    capture.add_argument(
        "--no-sound",
        action="store_true",
        help="record the picture only (clips get the names silent clips had before sound was added)",
    )
    capture.add_argument("--steam", metavar="PATH", help="path to steam.exe (default: from the registry)")
    capture.add_argument(
        "--port", type=int, metavar="N", help="local port for the CS2 console (default: a free one)"
    )
    capture.add_argument(
        "--demo-ui", action="store_true", help="keep the playback bar visible (shows the tick, for checking)"
    )

    render = commands.add_parser(
        "render",
        help="put the captured clips together into one video (needs FFmpeg)",
        description="Join the clips 'fragdirector capture' made into one .mp4: each shot is cut to "
        "its length, slow motion is applied, and shots are joined with a hard cut or a crossfade. "
        "The clips' sound is carried along (silence for a clip without), unless --no-sound. "
        "Every shot needs its clip; capture the missing ones first.",
    )
    render.add_argument("shots", help="shot list file (from 'plan' or the editor)")
    render.add_argument(
        "--clips",
        metavar="DIR",
        help="folder the clips are in (default: 'clips' next to the shot list file)",
    )
    render.add_argument(
        "--out",
        metavar="PATH",
        help="the video to write, ending in .mp4 (default: the shot list's name with .mp4)",
    )
    render.add_argument(
        "--crossfade",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="fade between shots for this long (default 0: hard cuts). The video is this much "
        "shorter for every join.",
    )
    render.add_argument(
        "--labels", action="store_true", help="write each shot's label on the picture for its first seconds"
    )
    render.add_argument(
        "--no-sound", action="store_true", help="write a video with no sound track (default: with the clips' sound)"
    )
    render.add_argument("--crf", type=int, default=18, help="x264 quality, 0-51, lower is better (default 18)")
    render.add_argument("--force", action="store_true", help="replace the video if it already exists")
    render.add_argument(
        "--dry-run", action="store_true", help="check the clips and print the FFmpeg command, write nothing"
    )
    return parser


def _clock(demo, kill) -> str:
    """Time on the round clock, counted from the end of freeze time."""
    rnd = demo.round_number(kill.round)
    seconds = max(0, kill.tick - rnd.freeze_end_tick) / demo.tick_rate
    return f"{int(seconds // 60)}:{int(seconds % 60):02d}"


def _flags(kill) -> str:
    flags = []
    if kill.headshot:
        flags.append("headshot")
    if kill.noscope:
        flags.append("noscope")
    if kill.through_smoke:
        flags.append("smoke")
    if kill.penetrated:
        flags.append(f"wallbang x{kill.penetrated}")
    if kill.attacker_blind:
        flags.append("blind")
    if kill.attacker_in_air:
        flags.append("air")
    if kill.flash_assist:
        flags.append("flash assist")
    return ", ".join(flags)


def print_kills(demo) -> None:
    from fragdirector.parse import is_team_kill

    for rnd in demo.rounds:
        kills = [kill for kill in demo.kills if kill.round == rnd.number]
        winner = rnd.winner or "-"
        print(_printable(f"Round {rnd.number}  (winner {winner}, {rnd.reason or 'unknown reason'})"))
        for kill in kills:
            attacker = demo.player_name(kill.attacker) if kill.attacker else "(no one)"
            line = (
                f"  tick {kill.tick:>6}  {_clock(demo, kill):>5}  "
                f"{attacker} [{kill.weapon or '?'}] > {demo.player_name(kill.victim)}"
            )
            if kill.assister:
                line += f"  (assist {demo.player_name(kill.assister)})"
            if is_team_kill(demo, kill):
                line += "  TEAM KILL"
            flags = _flags(kill)
            if flags:
                line += f"  <{flags}>"
            print(_printable(line))
        if not kills:
            print("  (no kills)")


def print_summary(demo) -> None:
    from fragdirector.parse import scoreboard

    print(_printable(f"Map: {demo.map_name or 'unknown'}   Rounds: {len(demo.rounds)}   "
                     f"Kills: {len(demo.kills)}   Bomb events: {len(demo.bomb_events)}"))
    print()
    print(f"{'Player':<24}{'K':>4}{'D':>4}{'A':>4}")
    for line in scoreboard(demo):
        name = _printable(demo.player_name(line.player))
        print(f"{name:<24}{line.kills:>4}{line.deaths:>4}{line.assists:>4}")


def print_moments(demo, weights) -> None:
    from fragdirector.detect.basic import detect_basic
    from fragdirector.score import rank_moments

    moments = rank_moments(detect_basic(demo), weights)
    print(f"Moments: {len(moments)} (best first)")
    for moment in moments:
        rnd = demo.round_number(moment.round)
        seconds = max(0, moment.start_tick - rnd.freeze_end_tick) / demo.tick_rate
        line = (
            f"  {moment.score:6.1f}  round {moment.round:>2}  "
            f"{int(seconds // 60)}:{int(seconds % 60):02d}  "
            f"{moment.kind:<9} {demo.player_name(moment.player)}  [{', '.join(moment.tags)}]"
        )
        if moment.kind == "clutch":
            line += f"  {moment.details['time_left_seconds']:.0f}s left"
        print(_printable(line))


def analyze(args: argparse.Namespace) -> int:
    from fragdirector.parse import DemoParseError, parse_demo
    from fragdirector.score import ScoreError, load_weights

    try:
        weights = load_weights(args.weights)
        demo = parse_demo(args.demo)
    except (DemoParseError, ScoreError) as error:
        print(f"fragdirector: {error}", file=sys.stderr)
        return 1
    if args.dump_kills:
        print_kills(demo)
        print()
    print_summary(demo)
    print()
    print_moments(demo, weights)
    return 0


def _choose_moments(demo, moments, args) -> list:
    """The ranked moments the options ask for, put back in demo order."""
    if args.player is not None:
        wanted = args.player.casefold()
        ids = [p.id for p in demo.players if p.id == args.player or p.name.casefold() == wanted]
        if not ids:
            names = ", ".join(sorted(p.name for p in demo.players))
            raise ValueError(f"No player named '{_printable(args.player)}' in this demo. Players: {_printable(names)}")
        moments = [m for m in moments if m.player in ids]
    if args.round is not None:
        if demo.round_number(args.round) is None:
            raise ValueError(f"The demo has no round {args.round}.")
        moments = [m for m in moments if m.round == args.round]
    if args.top is not None:
        if args.top < 1:
            raise ValueError("--top must be 1 or more.")
        moments = moments[:args.top]
    if not moments:
        raise ValueError("No moments match; nothing to plan.")
    return sorted(moments, key=lambda m: (m.round, m.start_tick, m.player, m.kind))


def print_plan(demo, result) -> None:
    shotlist = result.shotlist
    print(f"Shots: {len(shotlist)}   Length: {shotlist.duration_seconds():.1f} s")
    for number, shot in enumerate(shotlist.shots, start=1):
        name = _printable(demo.player_name(shot.pov_player))
        print(_printable(
            f"  {number:>2}  {shot.start_tick:>7} - {shot.end_tick:<7}  "
            f"{name:<16} {shot.speed:g}x  "
            f"{shot.duration_seconds(shotlist.tick_rate):5.1f} s  {shot.label}"
        ))
    for note in result.notes:
        print(f"Note: {_printable(note)}")


def plan(args: argparse.Namespace) -> int:
    from fragdirector.detect.basic import detect_basic
    from fragdirector.director import direct, load_preset, wrong_overlaps
    from fragdirector.models import shotlist_problems
    from fragdirector.parse import DemoParseError, parse_demo
    from fragdirector.score import load_weights, rank_moments

    try:
        preset = load_preset(args.preset)
        weights = load_weights(args.weights)
        demo = parse_demo(args.demo)
        moments = _choose_moments(demo, rank_moments(detect_basic(demo), weights), args)
        result = direct(moments, demo, preset, args.budget)
    except (DemoParseError, ValueError) as error:  # preset, weights and edit errors are ValueErrors
        print(f"fragdirector: {_printable(error)}", file=sys.stderr)
        return 1

    print(f"Preset: {preset.name}   Moments: {len(moments)}")
    print_plan(demo, result)
    problems = shotlist_problems(result.shotlist, demo) + wrong_overlaps(result.shotlist, demo)
    if not result.shotlist.shots:
        problems.append("The shot list is empty.")
    if problems:
        print("fragdirector: the shot list has problems and was not written:", file=sys.stderr)
        for problem in problems:
            print(f"  {_printable(problem)}", file=sys.stderr)
        return 1
    try:
        result.shotlist.save(args.out)
    except OSError as error:
        print(f"fragdirector: cannot write {args.out}: {error}", file=sys.stderr)
        return 1
    print(f"Wrote {args.out}")
    return 0


def open_editor(args: argparse.Namespace) -> int:
    from pathlib import Path

    from fragdirector.models import ShotList, shotlist_problems
    from fragdirector.parse import DemoParseError, parse_demo
    from fragdirector.score import load_weights
    from fragdirector.server import build_app, serve

    if not 0 <= args.port <= 65535:
        print("fragdirector: --port must be between 0 and 65535.", file=sys.stderr)
        return 1
    try:
        weights = load_weights(args.weights)
        demo = parse_demo(args.demo)
        shotlist = None
        if Path(args.shots).exists():
            shotlist = ShotList.load(args.shots)
            print(f"Loaded {len(shotlist)} shots from {args.shots}")
            for problem in shotlist_problems(shotlist, demo):
                print(f"Warning: {_printable(problem)}", file=sys.stderr)
        clips = Path(args.clips) if args.clips else Path(args.shots).parent / "clips"
        app = build_app(demo, weights, shotlist, args.shots, clips_dir=clips)
    except (DemoParseError, ValueError) as error:  # weights and shot list errors are ValueErrors
        print(f"fragdirector: {error}", file=sys.stderr)
        return 1
    try:
        serve(app, args.port, open_browser=not args.no_browser)
    except OSError as error:
        print(f"fragdirector: cannot start the editor server: {error}", file=sys.stderr)
        return 1
    return 0


def _shot_numbers(text: str) -> list[int]:
    """'1,3,5' -> [0, 2, 4] (positions from 0)."""
    numbers = []
    for part in text.split(","):
        part = part.strip()
        if not part.isdigit() or int(part) < 1:
            raise ValueError(f"--only takes shot numbers from 1 up, separated by commas; got {part!r}.")
        numbers.append(int(part) - 1)
    return numbers


def capture(args: argparse.Namespace) -> int:
    from pathlib import Path

    from fragdirector.capture import (
        CaptureError,
        CaptureOptions,
        capture_jobs,
        demo_files,
        plan_jobs,
    )
    from fragdirector.models import ShotList
    from fragdirector.parse import DemoParseError, parse_demo

    def log(message: str) -> None:
        print(f"[capture] {_printable(message)}", flush=True)

    shots_file = Path(args.shots)
    clips = Path(args.clips) if args.clips else shots_file.parent / "clips"
    try:
        options = CaptureOptions(fps=args.fps, demo_ui=args.demo_ui, sound=not args.no_sound)
        only = _shot_numbers(args.only) if args.only else None
        shotlist = ShotList.load(shots_file)
        files = demo_files(shotlist, shots_file, Path(args.demo) if args.demo else None)
        demos = {}
        for key, path in files.items():
            log(f"reading {path}")
            demos[key] = (parse_demo(path), path)
        jobs = plan_jobs(shotlist, demos, clips, options, only)
    except FileNotFoundError as error:
        print(f"fragdirector: {error.filename or error} does not exist.", file=sys.stderr)
        return 1
    except (CaptureError, DemoParseError, ValueError, OSError) as error:
        print(f"fragdirector: cannot capture:\n{_printable(error)}", file=sys.stderr)
        return 1

    log(f"{len(jobs)} clip(s) for {len(only) if only else len(shotlist)} shot(s), into {clips}")
    if args.dry_run:
        for job in jobs:
            state = "exists" if job.clip.exists() and not args.force else "to capture"
            print(_printable(f"  {job.describe()}  ->  {job.clip.name}  [{state}]  (seek {job.seek_tick})"))
        return 0
    if sys.platform != "win32":
        print("fragdirector: capturing only works on Windows.", file=sys.stderr)
        return 1
    try:
        results = capture_jobs(jobs, options, log=log, force=args.force, steam=args.steam, port=args.port)
    except CaptureError as error:
        print(f"fragdirector: capture stopped: {_printable(error)}", file=sys.stderr)
        print("Clips written so far are kept; run the same command again to continue.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nfragdirector: capture interrupted; run the same command again to continue.", file=sys.stderr)
        return 130
    counts = {status: sum(r.status == status for r in results) for status in ("captured", "skipped", "failed")}
    log(f"done: {counts['captured']} captured, {counts['skipped']} already there, {counts['failed']} failed")
    for result in results:
        if result.status == "failed":
            print(_printable(f"  failed: {result.job.describe()}: {result.message}"), file=sys.stderr)
    return 1 if counts["failed"] else 0


def render(args: argparse.Namespace) -> int:
    from pathlib import Path

    from fragdirector.models import ShotList
    from fragdirector.render import RenderError, RenderOptions, expected_seconds, render_video

    shots_file = Path(args.shots)
    clips = Path(args.clips) if args.clips else shots_file.parent / "clips"
    out = Path(args.out) if args.out else shots_file.with_suffix(".mp4")

    def log(message: str) -> None:
        print(f"[render] {_printable(message)}", flush=True)

    try:
        options = RenderOptions(crossfade_seconds=args.crossfade, labels=args.labels, crf=args.crf,
                                sound=not args.no_sound)
        shotlist = ShotList.load(shots_file)
        result = render_video(
            shotlist, clips, out, options, log=log, overwrite=args.force, dry_run=args.dry_run
        )
    except FileNotFoundError as error:
        print(f"fragdirector: {error.filename or error} does not exist.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nfragdirector: render interrupted; no video was written.", file=sys.stderr)
        return 130
    except (RenderError, ValueError, OSError) as error:
        print(f"fragdirector: cannot render:\n{_printable(error)}", file=sys.stderr)
        return 1
    if result is not None:
        print(f"Wrote {result.path} ({result.seconds:.1f} s; the shot list asks for "
              f"{expected_seconds(shotlist, options):.1f} s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    # Player names can hold characters the Windows console cannot show.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "analyze":
        return analyze(args)
    if args.command == "plan":
        return plan(args)
    if args.command == "open":
        return open_editor(args)
    if args.command == "capture":
        return capture(args)
    if args.command == "render":
        return render(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
