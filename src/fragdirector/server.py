"""The editor backend: a small local web server for the shot-list editor.

It serves the editor page (``web/``) and a JSON API over one parsed demo,
its scored moments and the shot list being edited. It uses only the standard
library and listens on 127.0.0.1 only.

The logic lives in ``EditorApp``, which knows nothing about HTTP: ``handle``
takes a method, a path and an already-parsed JSON body and returns
``(status, payload)``. ``make_server`` wraps it in an HTTP server and adds
the checks that belong to the transport (see "Safety" below).

API (all JSON; a refused request answers 4xx with ``{"error": "..."}``)::

    GET  /api/demo                  demo info: players, rounds, presets, save file
    GET  /api/moments               scored moments, best first; "id" is the
                                    position in this list for the whole run
    GET  /api/shotlist              the current shot list
    PUT  /api/shotlist              replace it   {"shotlist": {...}}
    POST /api/shotlist/save         write it to the file named at startup
    POST /api/shotlist/load         read it back from that file
    POST /api/edit/trim             {index, start_tick?, end_tick?}
    POST /api/edit/trim_by          {index, start_delta?, end_delta?}
    POST /api/edit/move             {from_index, to_index}
    POST /api/edit/delete           {index}
    POST /api/edit/duplicate        {index}
    POST /api/edit/insert           {shot, index?}   (shot.demo may be left out)
    POST /api/edit/change_pov       {index, pov_player}
    POST /api/edit/set_speed        {index, speed}
    POST /api/edit/set_label        {index, label}
    POST /api/preset                {preset, moment_ids, budget?, mode?}
                                    mode "replace" (default) or "append"
    GET  /api/capture               capture state, its log, and which shots
                                    of the current list already have a clip
    POST /api/capture               {index}  capture that shot now (starts CS2;
                                    runs in the background, one at a time)

Every endpoint that touches the shot list answers with
``{"shotlist": ..., "duration_seconds": ..., "problems": [...]}``. An edit
that is refused leaves the shot list exactly as it was.

Safety (T15 reviews this):

* binds to 127.0.0.1 only; the Host header must be localhost or 127.0.0.1
  (stops DNS rebinding), and an Origin header, if sent, must be one too
* no CORS headers are sent, and writes must be ``application/json``, which a
  foreign web page cannot send to us without a CORS preflight
* the file that is saved and loaded is fixed when the server starts; no
  request can name a path. A preset can only be named, never loaded from a path
* request bodies are limited to ``MAX_BODY_BYTES``
* static files come only from the ``web/`` folder, by plain file name
* a capture starts the game on a shot already in the server's list; the
  demo, the clips folder and every command come from the server, never
  from the request (which only gives a shot position)
"""

from __future__ import annotations

import json
import math
import re
import sys
import threading
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from fragdirector import edits
from fragdirector.capture import CaptureError, Job, JobResult, clip_path, plan_jobs
from fragdirector.detect.basic import detect_basic
from fragdirector.director import direct, load_preset, moment_title, preset_names, wrong_overlaps
from fragdirector.models import (
    Demo,
    ModelError,
    Moment,
    Shot,
    ShotList,
    demo_file_name,
    shotlist_problems,
)
from fragdirector.score import Weights, rank_moments

HOST = "127.0.0.1"
MAX_BODY_BYTES = 1024 * 1024
WEB_DIR = Path(__file__).with_name("web")
ALLOWED_HOSTS = ("localhost", "127.0.0.1")
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}
_STATIC_NAME = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]*$")


class ApiError(Exception):
    """A request that is refused; ``status`` and the message go to the client."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# --------------------------------------------------------------------------
# Reading request bodies
# --------------------------------------------------------------------------


def _body(body: Any, allowed: tuple[str, ...]) -> dict:
    if body is None:
        body = {}
    if not isinstance(body, dict):
        raise ApiError(400, "The request body must be a JSON object.")
    unknown = sorted(str(key) for key in body if key not in allowed)
    if unknown:
        raise ApiError(400, f"Unknown field(s): {', '.join(unknown)}.")
    return body


def _whole(body: dict, name: str, required: bool = True) -> int | None:
    if name not in body or body[name] is None:
        if required:
            raise ApiError(400, f"Field '{name}' is required.")
        return None
    value = body[name]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApiError(400, f"Field '{name}' must be a whole number, got {value!r}.")
    return value


def _text(body: dict, name: str) -> str:
    if name not in body:
        raise ApiError(400, f"Field '{name}' is required.")
    value = body[name]
    if not isinstance(value, str):
        raise ApiError(400, f"Field '{name}' must be text, got {value!r}.")
    return value


def _number(body: dict, name: str, required: bool = True) -> float | None:
    if name not in body or body[name] is None:
        if required:
            raise ApiError(400, f"Field '{name}' is required.")
        return None
    value = body[name]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ApiError(400, f"Field '{name}' must be a number, got {value!r}.")
    return float(value)


# --------------------------------------------------------------------------
# Capturing in the background
# --------------------------------------------------------------------------

CaptureRunner = Callable[[list[Job], Callable[[str], None]], list[JobResult]]
CAPTURE_LOG_LINES = 60


def default_capture_runner(jobs: list[Job], log: Callable[[str], None]) -> list[JobResult]:
    from fragdirector.capture import capture_jobs

    return capture_jobs(jobs, log=log, force=True)  # the button means "capture (again) now"


class CaptureState:
    """The one capture the editor may run at a time. Has its own lock, so the
    editor keeps answering while the game records."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.state = "idle"  # idle, running, done, failed
        self.shot: int | None = None
        self.message = ""
        self.log: deque[str] = deque(maxlen=CAPTURE_LOG_LINES)
        self.thread: threading.Thread | None = None

    def add_line(self, line: str) -> None:
        with self.lock:
            self.log.append(line)

    def finish(self, state: str, message: str) -> None:
        with self.lock:
            self.state = state
            self.message = message

    def to_json(self) -> dict:
        with self.lock:
            return {"state": self.state, "shot": self.shot, "message": self.message, "log": list(self.log)}


# --------------------------------------------------------------------------
# The app
# --------------------------------------------------------------------------


class EditorApp:
    """One demo, its ranked moments and the shot list being edited."""

    def __init__(
        self,
        demo: Demo,
        moments: list[Moment],
        shotlist: ShotList | None = None,
        save_path: str | Path | None = None,
        clips_dir: str | Path | None = None,
        capture_runner: CaptureRunner | None = None,
    ) -> None:
        self.demo = demo
        self.moments = list(moments)
        self.shotlist = shotlist if shotlist is not None else ShotList(tick_rate=demo.tick_rate)
        self.save_path = Path(save_path) if save_path is not None else None
        self.clips_dir = Path(clips_dir) if clips_dir is not None else None
        self.capture_runner = capture_runner or default_capture_runner
        self.capture = CaptureState()
        self._lock = threading.Lock()
        self._edit_routes: dict[str, Callable[[dict], ShotList]] = {
            "trim": self._trim,
            "trim_by": self._trim_by,
            "move": self._move,
            "delete": self._delete,
            "duplicate": self._duplicate,
            "insert": self._insert,
            "change_pov": self._change_pov,
            "set_speed": self._set_speed,
            "set_label": self._set_label,
        }

    # -- entry point -------------------------------------------------------

    def handle(self, method: str, path: str, body: Any = None) -> tuple[int, dict]:
        """Answer one API request. Never raises: problems become 4xx/5xx."""
        try:
            with self._lock:
                return 200, self._route(method, path, body)
        except ApiError as error:
            return error.status, {"error": error.message}
        except Exception:  # a bug; do not leak details to the client
            return 500, {"error": "Internal error in the editor server."}

    def _route(self, method: str, path: str, body: Any) -> dict:
        routes: dict[str, dict[str, Callable[[Any], dict]]] = {
            "/api/demo": {"GET": self._get_demo},
            "/api/moments": {"GET": self._get_moments},
            "/api/shotlist": {"GET": self._get_shotlist, "PUT": self._put_shotlist},
            "/api/shotlist/save": {"POST": self._save},
            "/api/shotlist/load": {"POST": self._load},
            "/api/preset": {"POST": self._preset},
            "/api/capture": {"GET": self._get_capture, "POST": self._start_capture},
        }
        if path.startswith("/api/edit/"):
            name = path.removeprefix("/api/edit/")
            if name not in self._edit_routes:
                raise ApiError(404, f"There is no edit operation '{name}'.")
            if method != "POST":
                raise ApiError(405, "Edit operations use POST.")
            return self._edited(name, body)
        methods = routes.get(path)
        if methods is None:
            raise ApiError(404, f"There is nothing at {path}.")
        if method not in methods:
            raise ApiError(405, f"{method} is not allowed here (use {', '.join(methods)}).")
        return methods[method](body)

    # -- reading -----------------------------------------------------------

    def _get_demo(self, _body: Any) -> dict:
        demo = self.demo
        return {
            "path": demo.path,
            "map_name": demo.map_name,
            "tick_rate": demo.tick_rate,
            "players": [player.to_dict() for player in demo.players],
            "rounds": [rnd.to_dict() for rnd in demo.rounds],
            "presets": preset_names(),
            "save_path": str(self.save_path) if self.save_path is not None else None,
        }

    def _moment_json(self, moment_id: int, moment: Moment) -> dict:
        demo = self.demo
        data = moment.to_dict()
        rnd = demo.round_number(moment.round)
        data["id"] = moment_id
        data["title"] = moment_title(moment, demo)
        data["player_name"] = demo.player_name(moment.player)
        data["clock_seconds"] = (
            max(0, moment.start_tick - rnd.freeze_end_tick) / demo.tick_rate if rnd else None
        )
        for kill_data in data["kills"]:
            kill_data["attacker_name"] = (
                demo.player_name(kill_data["attacker"]) if kill_data["attacker"] else None
            )
            kill_data["victim_name"] = demo.player_name(kill_data["victim"])
        return data

    def _get_moments(self, _body: Any) -> dict:
        return {"moments": [self._moment_json(i, m) for i, m in enumerate(self.moments)]}

    def _shotlist_json(self, extra: dict | None = None) -> dict:
        shotlist = self.shotlist
        return {
            "shotlist": shotlist.to_dict(),
            "duration_seconds": shotlist.duration_seconds(),
            "problems": shotlist_problems(shotlist, self.demo),
            **(extra or {}),
        }

    def _get_shotlist(self, _body: Any) -> dict:
        return self._shotlist_json()

    # -- replacing, saving, loading ---------------------------------------

    def _put_shotlist(self, body: Any) -> dict:
        body = _body(body, ("shotlist",))
        if "shotlist" not in body:
            raise ApiError(400, "Field 'shotlist' is required.")
        try:
            shotlist = ShotList.from_dict(body["shotlist"])
        except ModelError as error:
            raise ApiError(422, str(error)) from error
        self.shotlist = shotlist
        return self._shotlist_json()

    def _save(self, body: Any) -> dict:
        _body(body, ())
        if self.save_path is None:
            raise ApiError(409, "No shot list file was given when the editor was started.")
        try:
            self.shotlist.save(self.save_path)
        except OSError as error:
            raise ApiError(500, f"Cannot write {self.save_path}: {error.strerror or error}") from error
        return {"saved": str(self.save_path), "shots": len(self.shotlist)}

    def _load(self, body: Any) -> dict:
        _body(body, ())
        if self.save_path is None:
            raise ApiError(409, "No shot list file was given when the editor was started.")
        try:
            shotlist = ShotList.load(self.save_path)
        except FileNotFoundError:
            raise ApiError(404, f"{self.save_path} does not exist yet; save first.") from None
        except OSError as error:
            raise ApiError(500, f"Cannot read {self.save_path}: {error.strerror or error}") from error
        except ModelError as error:
            raise ApiError(422, str(error)) from error
        self.shotlist = shotlist
        return self._shotlist_json({"loaded": str(self.save_path)})

    # -- edits -------------------------------------------------------------

    def _edited(self, name: str, body: Any) -> dict:
        try:
            shotlist = self._edit_routes[name](body)
        except (edits.EditError, ModelError) as error:
            raise ApiError(422, str(error)) from error
        self.shotlist = shotlist  # only reached when the edit worked
        return self._shotlist_json()

    def _trim(self, body: Any) -> ShotList:
        body = _body(body, ("index", "start_tick", "end_tick"))
        return edits.trim(
            self.shotlist, self.demo, _whole(body, "index"),
            _whole(body, "start_tick", False), _whole(body, "end_tick", False),
        )

    def _trim_by(self, body: Any) -> ShotList:
        body = _body(body, ("index", "start_delta", "end_delta"))
        return edits.trim_by(
            self.shotlist, self.demo, _whole(body, "index"),
            _whole(body, "start_delta", False) or 0, _whole(body, "end_delta", False) or 0,
        )

    def _move(self, body: Any) -> ShotList:
        body = _body(body, ("from_index", "to_index"))
        return edits.move(self.shotlist, _whole(body, "from_index"), _whole(body, "to_index"))

    def _delete(self, body: Any) -> ShotList:
        body = _body(body, ("index",))
        return edits.delete(self.shotlist, _whole(body, "index"))

    def _duplicate(self, body: Any) -> ShotList:
        body = _body(body, ("index",))
        return edits.duplicate(self.shotlist, _whole(body, "index"))

    def _insert(self, body: Any) -> ShotList:
        body = _body(body, ("shot", "index"))
        shot_data = body.get("shot")
        if not isinstance(shot_data, dict):
            raise ApiError(400, "Field 'shot' must be a JSON object.")
        if "demo" not in shot_data:
            shot_data = {**shot_data, "demo": self.demo.path}
        shot = Shot.from_dict(shot_data)  # a ModelError becomes a 422 in _edited
        return edits.insert(self.shotlist, self.demo, shot, _whole(body, "index", False))

    def _change_pov(self, body: Any) -> ShotList:
        body = _body(body, ("index", "pov_player"))
        return edits.change_pov(self.shotlist, self.demo, _whole(body, "index"), _text(body, "pov_player"))

    def _set_speed(self, body: Any) -> ShotList:
        body = _body(body, ("index", "speed"))
        return edits.set_speed(self.shotlist, _whole(body, "index"), _number(body, "speed"))

    def _set_label(self, body: Any) -> ShotList:
        body = _body(body, ("index", "label"))
        return edits.set_label(self.shotlist, _whole(body, "index"), _text(body, "label"))

    # -- presets -----------------------------------------------------------

    def _preset(self, body: Any) -> dict:
        body = _body(body, ("preset", "moment_ids", "budget", "mode"))
        name = _text(body, "preset")
        if name not in preset_names():
            raise ApiError(
                400, f"There is no preset named {name!r} (the presets are {', '.join(preset_names())})."
            )
        mode = body.get("mode", "replace")
        if mode not in ("replace", "append"):
            raise ApiError(400, "Field 'mode' must be 'replace' or 'append'.")
        budget = _number(body, "budget", required=False)
        if budget is not None and budget <= 0:
            raise ApiError(400, "Field 'budget' must be more than 0 seconds.")

        ids = body.get("moment_ids")
        if not isinstance(ids, list) or not ids:
            raise ApiError(400, "Field 'moment_ids' must be a list with at least one moment id.")
        chosen: list[Moment] = []
        for moment_id in ids:
            if isinstance(moment_id, bool) or not isinstance(moment_id, int):
                raise ApiError(400, f"A moment id must be a whole number, got {moment_id!r}.")
            if not 0 <= moment_id < len(self.moments):
                raise ApiError(400, f"There is no moment with id {moment_id}.")
            chosen.append(self.moments[moment_id])
        if len(set(ids)) != len(ids):
            raise ApiError(400, "A moment id is listed twice.")
        chosen.sort(key=lambda m: (m.round, m.start_tick, m.player, m.kind))  # demo order

        try:
            plan = direct(chosen, self.demo, load_preset(name), budget)
            if mode == "append":
                shotlist = self.shotlist
                for shot in plan.shotlist.shots:
                    shotlist = edits.insert(shotlist, self.demo, shot)
            else:
                shotlist = plan.shotlist
        except ValueError as error:  # director, preset and edit errors
            raise ApiError(422, str(error)) from error
        self.shotlist = shotlist
        return self._shotlist_json({
            "notes": list(plan.notes),
            "budget_met": plan.budget_met,
            "overlaps": wrong_overlaps(plan.shotlist, self.demo),
        })


    # -- capture -----------------------------------------------------------

    def _get_capture(self, _body: Any) -> dict:
        captured = (
            [clip_path(shot, self.clips_dir).is_file() for shot in self.shotlist.shots]
            if self.clips_dir is not None else []
        )
        return {
            "capture": self.capture.to_json(),
            "clips_dir": str(self.clips_dir) if self.clips_dir is not None else None,
            "captured": captured,
        }

    def _start_capture(self, body: Any) -> dict:
        body = _body(body, ("index",))
        index = _whole(body, "index")
        if self.clips_dir is None:
            raise ApiError(409, "No clips folder was given when the editor was started.")
        if not 0 <= index < len(self.shotlist.shots):
            raise ApiError(400, f"There is no shot {index + 1}.")
        capture = self.capture
        with capture.lock:
            if capture.state == "running":
                raise ApiError(409, f"Shot {capture.shot + 1} is being captured; wait for it to finish.")
        demos = {demo_file_name(self.demo.path): (self.demo, Path(self.demo.path))}
        try:
            jobs = plan_jobs(self.shotlist, demos, self.clips_dir, only=[index])
        except CaptureError as error:
            raise ApiError(422, str(error)) from error

        with capture.lock:
            capture.state = "running"
            capture.shot = index
            capture.message = f"Capturing shot {index + 1}: CS2 is starting."
            capture.log.clear()
        thread = threading.Thread(target=self._run_capture, args=(index, jobs), daemon=True)
        capture.thread = thread
        thread.start()
        return self._get_capture(None)

    def _run_capture(self, index: int, jobs: list[Job]) -> None:
        capture = self.capture
        try:
            results = self.capture_runner(jobs, capture.add_line)
        except CaptureError as error:
            capture.finish("failed", f"Shot {index + 1} was not captured: {error}")
            return
        except Exception as error:  # a bug; keep the editor running and say so
            capture.finish("failed", f"Shot {index + 1} was not captured (internal error: {error}).")
            return
        failed = [r for r in results if r.status == "failed"]
        if failed or not results:
            reason = failed[0].message if failed else "nothing was captured"
            capture.finish("failed", f"Shot {index + 1} was not captured: {reason}")
        else:
            capture.finish("done", f"Captured shot {index + 1} to {results[0].job.clip.name}.")


def build_app(
    demo: Demo,
    weights: Weights | None = None,
    shotlist: ShotList | None = None,
    save_path: str | Path | None = None,
    clips_dir: str | Path | None = None,
    capture_runner: CaptureRunner | None = None,
) -> EditorApp:
    """An app for a parsed demo; its moments are the ones ``analyze`` lists."""
    return EditorApp(
        demo, rank_moments(detect_basic(demo), weights), shotlist, save_path, clips_dir, capture_runner
    )


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


def _host_name(value: str) -> str:
    """The host part of a Host header (``localhost:8000`` -> ``localhost``)."""
    value = value.strip().lower()
    if value.startswith("["):  # IPv6 literal, never allowed
        return value
    return value.rsplit(":", 1)[0] if ":" in value else value


class _Handler(BaseHTTPRequestHandler):
    server_version = "fragdirector"
    sys_version = ""

    @property
    def app(self) -> EditorApp:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:  # keep the console quiet
        pass

    # -- replies -----------------------------------------------------------

    def _send(self, status: int, data: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self._send(status, data, "application/json; charset=utf-8")

    # -- checks ------------------------------------------------------------

    def _check_origin(self) -> None:
        host = self.headers.get("Host")
        if host is None or _host_name(host) not in ALLOWED_HOSTS:
            raise ApiError(403, "Only localhost may use this server (check the Host header).")
        origin = self.headers.get("Origin")
        if origin is not None:
            origin_host = urlsplit(origin).hostname
            if origin_host not in ALLOWED_HOSTS:
                raise ApiError(403, "Requests from other web sites are refused.")

    def _read_body(self) -> Any:
        if self.headers.get("Transfer-Encoding") is not None:
            raise ApiError(411, "Send the body with a Content-Length.")
        length_text = self.headers.get("Content-Length")
        if length_text is None:
            raise ApiError(411, "A Content-Length header is required.")
        if not length_text.isascii() or not length_text.isdigit():
            raise ApiError(400, "Content-Length must be a number.")
        length = int(length_text)
        if length > MAX_BODY_BYTES:
            raise ApiError(413, f"The request body is larger than {MAX_BODY_BYTES} bytes.")
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type != "application/json":
            raise ApiError(415, "The request body must be application/json.")
        raw = self.rfile.read(length)
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
            raise ApiError(400, f"The request body is not valid JSON: {error}") from error

    # -- dispatch ----------------------------------------------------------

    def _dispatch(self, method: str) -> None:
        try:
            self._check_origin()
            path = urlsplit(self.path).path
            if path.startswith("/api/"):
                body = self._read_body() if method in ("POST", "PUT") else None
                status, payload = self.app.handle(method, path, body)
                self._send_json(status, payload)
            elif method == "GET":
                self._send_static(path)
            else:
                raise ApiError(405, f"{method} is not allowed here.")
        except ApiError as error:
            self.close_connection = True
            self._send_json(error.status, {"error": error.message})

    def _send_static(self, path: str) -> None:
        name = "index.html" if path == "/" else path.lstrip("/")
        file = WEB_DIR / name
        content_type = CONTENT_TYPES.get(Path(name).suffix)
        if not _STATIC_NAME.match(name) or content_type is None or not file.is_file():
            raise ApiError(404, f"There is nothing at {path}.")
        self._send(200, file.read_bytes(), content_type)

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")


def make_server(app: EditorApp, port: int = 0) -> ThreadingHTTPServer:
    """An HTTP server for ``app`` on 127.0.0.1; port 0 picks a free port.

    Not started: call ``serve_forever`` (or run it in a thread).
    """
    server = ThreadingHTTPServer((HOST, port), _Handler)
    server.daemon_threads = True
    server.app = app  # type: ignore[attr-defined]
    return server


def server_url(server: ThreadingHTTPServer) -> str:
    return f"http://{HOST}:{server.server_address[1]}/"


def serve(app: EditorApp, port: int = 0, open_browser: bool = True) -> None:
    """Run the editor until interrupted (Ctrl+C)."""
    server = make_server(app, port)
    url = server_url(server)
    print(f"Editor running at {url}  (Ctrl+C to stop)")
    if open_browser and not webbrowser.open(url):
        print(f"Could not open a browser; open {url} yourself.", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
