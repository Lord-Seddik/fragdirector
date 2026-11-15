"""Tests for the editor backend: every endpoint, over real HTTP on port 0."""

import http.client
import json
import threading
import time

import pytest

from conftest import DEMO_PATH, P1, P2, P3, build_demo
from fragdirector.capture import CaptureError, JobResult, clip_path
from fragdirector.cli import main
from fragdirector.models import Shot, ShotList
from fragdirector.render import RenderError, RenderResult
from fragdirector.server import MAX_BODY_BYTES, build_app, make_server


def shot(start=1700, end=1900, pov=P1, **extra) -> Shot:
    return Shot(demo=DEMO_PATH, start_tick=start, end_tick=end, pov_player=pov, **extra)


def three_shots() -> ShotList:
    return ShotList(
        shots=(shot(1700, 1900), shot(2000, 2400, P2), shot(5100, 5300, P1)), tick_rate=64
    )


class Client:
    def __init__(self, port: int) -> None:
        self.port = port

    def request(self, method, path, body=None, headers=None, raw=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            headers = dict(headers or {})
            data = raw
            if raw is None and body is not None:
                data = json.dumps(body).encode("utf-8")
                headers.setdefault("Content-Type", "application/json")
            elif method in ("POST", "PUT") and data is None:
                data = b""
                headers.setdefault("Content-Type", "application/json")
            connection.request(method, path, body=data, headers=headers)
            response = connection.getresponse()
            payload = response.read()
            return response.status, response.getheaders(), payload
        finally:
            connection.close()

    def json(self, method, path, body=None, **kwargs):
        status, _, payload = self.request(method, path, body, **kwargs)
        return status, json.loads(payload)

    def get(self, path):
        return self.json("GET", path)

    def post(self, path, body=None):
        return self.json("POST", path, {} if body is None else body)


@pytest.fixture
def make_client(tmp_path):
    servers = []

    def make(shotlist=None, save=True, **app_options) -> Client:
        app = build_app(
            build_demo(), shotlist=shotlist, save_path=tmp_path / "shots.json" if save else None,
            **app_options,
        )
        server = make_server(app, 0)
        threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()
        servers.append(server)
        client = Client(server.server_address[1])
        client.app = app
        return client

    yield make
    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.fixture
def client(make_client):
    return make_client(three_shots())


# -- read endpoints ----------------------------------------------------------


def test_binds_to_loopback_only(client):
    assert client.app is not None
    server = make_server(client.app, 0)
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()


def test_demo_endpoint(client, tmp_path):
    status, data = client.get("/api/demo")
    assert status == 200
    assert data["path"] == DEMO_PATH and data["tick_rate"] == 64 and data["map_name"] == "de_mirage"
    assert [p["id"] for p in data["players"]][:2] == [P1, P2]
    assert [r["number"] for r in data["rounds"]] == [1, 2]
    assert data["rounds"][0]["lives"][0]["player"] == P1
    assert "killer" in data["presets"] and "victims" in data["presets"]
    assert data["save_path"] == str(tmp_path / "shots.json")


def test_moments_endpoint_matches_analyze(client):
    status, data = client.get("/api/moments")
    assert status == 200
    moments = data["moments"]
    assert moments, "the hand-built demo has at least one multi-kill"
    assert [m["id"] for m in moments] == list(range(len(moments)))
    scores = [m["score"] for m in moments]
    assert scores == sorted(scores, reverse=True)
    first = moments[0]
    assert {"kind", "round", "player", "player_name", "title", "tags", "kills", "clock_seconds"} <= set(first)
    assert first["kills"][0]["victim_name"]
    assert len(moments) == len(client.app.moments)


def test_shotlist_endpoint(client):
    status, data = client.get("/api/shotlist")
    assert status == 200
    assert len(data["shotlist"]["shots"]) == 3
    assert data["shotlist"]["version"] == 1
    assert data["duration_seconds"] == pytest.approx((200 + 400 + 200) / 64)
    assert data["problems"] == []


def test_non_ascii_player_name_survives(client):
    status, data = client.get("/api/demo")
    assert "Ünïcödé ☆" in [p["name"] for p in data["players"]]


# -- the placeholder page ----------------------------------------------------


def test_serves_the_placeholder_page(client):
    status, headers, body = client.request("GET", "/")
    assert status == 200
    assert b"/api" in client.request("GET", "/app.js")[2]
    assert b"<title>fragdirector</title>" in body
    assert dict(headers)["Content-Type"].startswith("text/html")
    assert client.request("GET", "/style.css")[0] == 200


@pytest.mark.parametrize("path", ["/../pyproject.toml", "/..%2fpyproject.toml", "/server.py",
                                  "/%2e%2e/models.py", "/missing.js", "//etc/passwd"])
def test_static_files_only_come_from_the_web_folder(client, path):
    assert client.request("GET", path)[0] == 404


# -- edits -------------------------------------------------------------------


def ticks(data):
    return [(s["start_tick"], s["end_tick"], s["pov_player"]) for s in data["shotlist"]["shots"]]


def test_trim(client):
    status, data = client.post("/api/edit/trim", {"index": 0, "start_tick": 1600, "end_tick": 2000})
    assert status == 200
    assert ticks(data)[0] == (1600, 2000, P1)
    status, data = client.post("/api/edit/trim", {"index": 0, "end_tick": 1950})
    assert ticks(data)[0] == (1600, 1950, P1)
    assert client.get("/api/shotlist")[1] == data  # the server keeps it


def test_trim_by(client):
    status, data = client.post("/api/edit/trim_by", {"index": 1, "start_delta": -32, "end_delta": 32})
    assert status == 200
    assert ticks(data)[1] == (1968, 2432, P2)


def test_move(client):
    status, data = client.post("/api/edit/move", {"from_index": 0, "to_index": 2})
    assert status == 200
    assert [t[0] for t in ticks(data)] == [2000, 5100, 1700]


def test_delete(client):
    status, data = client.post("/api/edit/delete", {"index": 1})
    assert status == 200
    assert [t[0] for t in ticks(data)] == [1700, 5100]


def test_duplicate(client):
    status, data = client.post("/api/edit/duplicate", {"index": 0})
    assert status == 200
    assert [t[0] for t in ticks(data)] == [1700, 1700, 2000, 5100]


def test_insert_at_a_position_and_at_the_end(client):
    new = {"start_tick": 3000, "end_tick": 3100, "pov_player": P2, "label": "mine"}
    status, data = client.post("/api/edit/insert", {"shot": new, "index": 0})
    assert status == 200
    assert data["shotlist"]["shots"][0]["label"] == "mine"
    assert data["shotlist"]["shots"][0]["demo"] == DEMO_PATH  # filled in
    status, data = client.post("/api/edit/insert", {"shot": {**new, "demo": DEMO_PATH}})
    assert len(data["shotlist"]["shots"]) == 5 and data["shotlist"]["shots"][-1]["start_tick"] == 3000


def test_change_pov(client):
    status, data = client.post("/api/edit/change_pov", {"index": 0, "pov_player": P2})
    assert status == 200
    assert ticks(data)[0] == (1700, 1900, P2)


def test_set_speed_and_label(client):
    status, data = client.post("/api/edit/set_speed", {"index": 2, "speed": 0.5})
    assert status == 200 and data["shotlist"]["shots"][2]["speed"] == 0.5
    assert data["duration_seconds"] == pytest.approx((200 + 400 + 400) / 64)
    status, data = client.post("/api/edit/set_label", {"index": 2, "label": "slow"})
    assert status == 200 and data["shotlist"]["shots"][2]["label"] == "slow"


INVALID_EDITS = [
    ("trim", {"index": 0, "end_tick": 5200}, "round"),                    # past the end of the round
    ("trim", {"index": 0, "start_tick": 1900}, "start before it ends"),  # start == end
    ("trim_by", {"index": 0, "start_delta": -10000}, ""),
    ("move", {"from_index": 0, "to_index": 9}, "no shot at position 9"),
    ("delete", {"index": 7}, "no shot at position 7"),
    ("duplicate", {"index": -1}, "no shot at position -1"),
    ("insert", {"shot": {"start_tick": 4900, "end_tick": 5100, "pov_player": P1}}, "round"),
    ("insert", {"shot": {"start_tick": 3000, "end_tick": 3100, "pov_player": "nobody"}}, "not in this demo"),
    ("insert", {"shot": {"start_tick": 3100, "end_tick": 3000, "pov_player": P1}}, "start before it ends"),
    ("insert", {"shot": {"start_tick": 3000, "end_tick": 3100, "pov_player": P1, "demo": "other.dem"}}, "other.dem"),
    ("insert", {"shot": {"start_tick": 3000, "end_tick": 3100, "pov_player": P1, "bogus": 1}}, "bogus"),
    ("change_pov", {"index": 2, "pov_player": P1 + "x"}, "not in this demo"),
    ("set_speed", {"index": 0, "speed": 0}, "more than 0"),
    ("set_speed", {"index": 0, "speed": -1}, "more than 0"),
    ("set_label", {"index": 0, "label": 5}, "text"),
    ("trim", {"index": "0"}, "whole number"),
    ("trim", {"index": True}, "whole number"),
    ("trim", {}, "required"),
    ("trim", {"index": 0, "color": "red"}, "color"),
    ("delete", [1], "JSON object"),
]


@pytest.mark.parametrize("operation,body,fragment", INVALID_EDITS)
def test_invalid_edits_are_refused_and_change_nothing(client, operation, body, fragment):
    before = client.get("/api/shotlist")[1]
    status, data = client.json("POST", f"/api/edit/{operation}", body)
    assert 400 <= status < 500, data
    assert fragment in data["error"]
    assert client.get("/api/shotlist")[1] == before


def test_edit_that_kills_the_pov_player_is_refused_with_a_clear_message(client):
    # Round 1: p3 dies at tick 3000.
    client.post("/api/edit/insert", {"shot": {"start_tick": 2900, "end_tick": 3000, "pov_player": P3}})
    before = client.get("/api/shotlist")[1]
    status, data = client.post("/api/edit/trim", {"index": 3, "end_tick": 3001})
    assert status == 422
    assert "donk is no longer alive from tick 3000" in data["error"]
    assert client.get("/api/shotlist")[1] == before


def test_unknown_edit_and_wrong_method(client):
    assert client.post("/api/edit/explode", {})[0] == 404
    assert client.get("/api/edit/trim")[0] == 405
    assert client.post("/api/nothing", {})[0] == 404
    assert client.json("POST", "/api/moments", {})[0] == 405
    assert client.json("DELETE", "/api/shotlist")[0] == 405


# -- replace / save / load ---------------------------------------------------


def test_put_shotlist_replaces_it(client):
    new = ShotList(shots=(shot(3000, 3500),), tick_rate=64).to_dict()
    status, data = client.json("PUT", "/api/shotlist", {"shotlist": new})
    assert status == 200 and data["shotlist"] == new and data["problems"] == []
    assert client.get("/api/shotlist")[1]["shotlist"] == new


def test_put_shotlist_reports_problems_but_refuses_garbage(client):
    outside = ShotList(shots=(shot(10, 500),), tick_rate=64).to_dict()
    status, data = client.json("PUT", "/api/shotlist", {"shotlist": outside})
    assert status == 200 and data["problems"]

    before = client.get("/api/shotlist")[1]
    for bad in ({"shotlist": {"version": 2, "shots": []}}, {"shotlist": "x"}, {}, {"shotlist": {
            "version": 1, "tick_rate": 64, "shots": [{"demo": "a"}]}}):
        status, data = client.json("PUT", "/api/shotlist", bad)
        assert 400 <= status < 500 and data["error"]
        assert client.get("/api/shotlist")[1] == before


def test_save_and_load_use_the_file_given_at_startup(client, tmp_path):
    path = tmp_path / "shots.json"
    assert client.post("/api/shotlist/load")[0] == 404  # nothing saved yet
    status, data = client.post("/api/shotlist/save")
    assert status == 200 and data["saved"] == str(path) and data["shots"] == 3
    assert ShotList.load(path) == three_shots()

    client.post("/api/edit/delete", {"index": 0})
    assert len(client.get("/api/shotlist")[1]["shotlist"]["shots"]) == 2
    status, data = client.post("/api/shotlist/load")
    assert status == 200 and len(data["shotlist"]["shots"]) == 3


def test_save_cannot_be_pointed_at_another_file(client, tmp_path):
    status, data = client.post("/api/shotlist/save", {"path": str(tmp_path / "evil.json")})
    assert status == 400 and "path" in data["error"]
    assert not (tmp_path / "evil.json").exists()
    assert client.post("/api/shotlist/load", {"path": "x"})[0] == 400


def test_load_of_a_broken_file_is_refused(client, tmp_path):
    (tmp_path / "shots.json").write_text("{not json", encoding="utf-8")
    before = client.get("/api/shotlist")[1]
    status, data = client.post("/api/shotlist/load")
    assert status == 422 and "not a valid JSON" in data["error"]
    assert client.get("/api/shotlist")[1] == before


def test_save_and_load_without_a_file(make_client):
    client = make_client(save=False)
    assert client.post("/api/shotlist/save")[0] == 409
    assert client.post("/api/shotlist/load")[0] == 409
    assert client.get("/api/demo")[1]["save_path"] is None


# -- preset ------------------------------------------------------------------


def test_preset_makes_a_draft_from_selected_moments(make_client):
    client = make_client()
    moments = client.get("/api/moments")[1]["moments"]
    status, data = client.post("/api/preset", {"preset": "killer", "moment_ids": [moments[0]["id"]]})
    assert status == 200, data
    assert data["shotlist"]["shots"] and data["problems"] == [] and data["overlaps"] == []
    assert {s["pov_player"] for s in data["shotlist"]["shots"]} == {moments[0]["player"]}
    assert client.get("/api/shotlist")[1]["shotlist"] == data["shotlist"]


def test_preset_victims_adds_victim_views_and_append_keeps_existing_shots(client):
    moments = client.get("/api/moments")[1]["moments"]
    ids = [m["id"] for m in moments]
    status, data = client.post("/api/preset", {"preset": "victims", "moment_ids": ids, "mode": "append"})
    assert status == 200, data
    shots = data["shotlist"]["shots"]
    assert shots[:3] == three_shots().to_dict()["shots"]
    assert len(shots) > 3
    status, data = client.post("/api/preset", {"preset": "victims", "moment_ids": ids})
    assert len(data["shotlist"]["shots"]) == len(shots) - 3  # replaced, not appended


def test_preset_budget(make_client):
    client = make_client()
    ids = [m["id"] for m in client.get("/api/moments")[1]["moments"]]
    status, data = client.post("/api/preset", {"preset": "budget", "moment_ids": ids, "budget": 12})
    assert status == 200, data
    assert "budget_met" in data and isinstance(data["notes"], list)


@pytest.mark.parametrize("body,fragment", [
    ({"preset": "killer", "moment_ids": []}, "moment_ids"),
    ({"preset": "killer", "moment_ids": [999]}, "no moment with id 999"),
    ({"preset": "killer", "moment_ids": [-1]}, "no moment with id -1"),
    ({"preset": "killer", "moment_ids": [0, 0]}, "twice"),
    ({"preset": "killer", "moment_ids": ["0"]}, "whole number"),
    ({"preset": "killer", "moment_ids": [True]}, "whole number"),
    ({"preset": "nope", "moment_ids": [0]}, "no preset named"),
    ({"preset": "killer"}, "moment_ids"),
    ({"preset": "killer", "moment_ids": [0], "budget": -5}, "budget"),
    ({"preset": "killer", "moment_ids": [0], "budget": "ten"}, "budget"),
    ({"preset": "killer", "moment_ids": [0], "mode": "merge"}, "mode"),
    ({"moment_ids": [0]}, "preset"),
])
def test_invalid_preset_requests(client, body, fragment):
    before = client.get("/api/shotlist")[1]
    status, data = client.post("/api/preset", body)
    assert 400 <= status < 500 and fragment in data["error"]
    assert client.get("/api/shotlist")[1] == before


def test_preset_cannot_load_a_file_path(client, tmp_path):
    preset = tmp_path / "mine.toml"
    preset.write_text('base = "killer"\n', encoding="utf-8")
    status, data = client.post("/api/preset", {"preset": str(preset), "moment_ids": [0]})
    assert status == 400 and "no preset named" in data["error"]


# -- security basics ---------------------------------------------------------


@pytest.mark.parametrize("host", ["evil.example", "evil.example:80", "127.0.0.1.evil.example",
                                  "localhost.evil.example", "192.168.1.5", "[::1]", ""])
def test_wrong_host_header_is_refused(client, host):
    for path in ("/api/shotlist", "/"):
        status, _, body = client.request("GET", path, headers={"Host": host})
        assert status == 403, (host, path)
    status, _, _ = client.request("POST", "/api/edit/delete", {"index": 0}, headers={"Host": host})
    assert status == 403
    assert len(client.get("/api/shotlist")[1]["shotlist"]["shots"]) == 3


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "LOCALHOST:8080", "127.0.0.1:1"])
def test_localhost_host_headers_are_accepted(client, host):
    assert client.request("GET", "/api/shotlist", headers={"Host": host})[0] == 200


def test_foreign_origin_is_refused(client):
    status, _, _ = client.request("POST", "/api/edit/delete", {"index": 0},
                                  headers={"Origin": "https://evil.example"})
    assert status == 403
    status, _, _ = client.request("GET", "/api/shotlist", headers={"Origin": "https://evil.example"})
    assert status == 403
    assert len(client.get("/api/shotlist")[1]["shotlist"]["shots"]) == 3
    status, _, _ = client.request("GET", "/api/shotlist", headers={"Origin": "http://localhost:3000"})
    assert status == 200


def test_no_cors_headers_are_sent(client):
    for method, path in (("GET", "/api/shotlist"), ("GET", "/"), ("GET", "/api/missing")):
        _, headers, _ = client.request(method, path)
        assert not [name for name, _ in headers if name.lower().startswith("access-control-")]
    status, headers, _ = client.request("OPTIONS", "/api/shotlist")
    assert status >= 400
    assert not [name for name, _ in headers if name.lower().startswith("access-control-")]


def test_writes_must_be_json(client):
    status, _, _ = client.request("POST", "/api/edit/delete", raw=b'{"index": 0}',
                                  headers={"Content-Type": "text/plain"})
    assert status == 415
    status, _, _ = client.request("POST", "/api/edit/delete", raw=b'{"index": 0}',
                                  headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert status == 415
    assert len(client.get("/api/shotlist")[1]["shotlist"]["shots"]) == 3


def test_oversized_and_malformed_bodies(client):
    too_big = b" " * (MAX_BODY_BYTES + 1)
    status, _, payload = client.request("POST", "/api/edit/delete", raw=too_big,
                                        headers={"Content-Type": "application/json"})
    assert status == 413 and b"larger than" in payload
    status, _, payload = client.request("POST", "/api/edit/delete", raw=b"{nope",
                                        headers={"Content-Type": "application/json"})
    assert status == 400 and b"not valid JSON" in payload
    status, _, _ = client.request("POST", "/api/edit/delete", raw=b"\xff\xfe",
                                  headers={"Content-Type": "application/json"})
    assert status == 400
    # a number with more digits than Python will read is a plain ValueError
    status, _, payload = client.request("POST", "/api/edit/delete", raw=b'{"index": ' + b"1" * 5000 + b"}",
                                        headers={"Content-Type": "application/json"})
    assert status == 400 and b"not valid JSON" in payload
    assert len(client.get("/api/shotlist")[1]["shotlist"]["shots"]) == 3


def test_values_that_would_break_the_answer_are_refused_and_change_nothing(client):
    """Found in the T15 review: a speed of 1e-320 or a 400-digit tick used to be
    kept, and every answer after that failed."""
    before = client.get("/api/shotlist")[1]
    status, data = client.post("/api/edit/set_speed", {"index": 0, "speed": 1e-320})
    assert status == 422 and "between 0.01 and 100" in data["error"]

    bad = three_shots().to_dict()
    bad["shots"][0]["speed"] = 1e-320
    status, data = client.json("PUT", "/api/shotlist", {"shotlist": bad})
    assert status == 422 and "between 0.01 and 100" in data["error"]

    bad = three_shots().to_dict()
    bad["shots"][0]["end_tick"] = 10**400
    status, data = client.json("PUT", "/api/shotlist", {"shotlist": bad})
    assert status == 422 and "end_tick" in data["error"]

    status, data = client.post("/api/edit/trim", {"index": 0, "end_tick": 10**400})
    assert status == 422
    assert client.get("/api/shotlist")[1] == before


def test_a_shot_list_that_cannot_be_answered_for_is_not_kept(client, monkeypatch):
    """The answer is worked out before the list is replaced."""
    import fragdirector.server as server_module

    before = client.get("/api/shotlist")[1]

    def boom(shotlist, demo):
        if len(shotlist.shots) != 3:
            raise RuntimeError("cannot answer")
        return []

    monkeypatch.setattr(server_module, "shotlist_problems", boom)
    status, data = client.post("/api/edit/delete", {"index": 0})
    assert status == 500 and data == {"error": "Internal error in the editor server."}
    assert client.get("/api/shotlist")[1] == before
    status, _, _ = client.request("POST", "/api/edit/delete", raw=b"[" * 100000,
                                  headers={"Content-Type": "application/json"})
    assert status == 400
    assert len(client.get("/api/shotlist")[1]["shotlist"]["shots"]) == 3


def test_missing_content_length_is_refused(client):
    connection = http.client.HTTPConnection("127.0.0.1", client.port, timeout=10)
    try:
        connection.putrequest("POST", "/api/edit/delete")
        connection.putheader("Content-Type", "application/json")
        connection.endheaders()
        assert connection.getresponse().status == 411
    finally:
        connection.close()


# -- capture -----------------------------------------------------------------


class FakeRunner:
    """Stands in for the game: writes the clip file, or fails, when released."""

    def __init__(self, fail=None):
        self.fail = fail
        self.release = threading.Event()
        self.jobs = []

    def __call__(self, jobs, log):
        self.jobs.append(jobs)
        log("starting CS2 (fake)")
        self.release.wait(10)
        if self.fail is not None:
            raise CaptureError(self.fail)
        for job in jobs:
            job.clip.parent.mkdir(parents=True, exist_ok=True)
            job.clip.write_bytes(b"clip")
        return [JobResult(job, "captured", "fake") for job in jobs]


def wait_for_capture(client, state, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, data = client.get("/api/capture")
        if data["capture"]["state"] == state:
            return data
        time.sleep(0.02)
    raise AssertionError(f"capture never reached {state!r}: {data}")


def test_capture_status_lists_which_shots_have_clips(make_client, tmp_path):
    clips = tmp_path / "clips"
    client = make_client(three_shots(), clips_dir=clips)
    status, data = client.get("/api/capture")
    assert status == 200
    assert data["capture"]["state"] == "idle" and data["clips_dir"] == str(clips)
    assert data["captured"] == [False, False, False]
    clips.mkdir()
    clip_path(three_shots().shots[1], clips).write_bytes(b"x")
    assert client.get("/api/capture")[1]["captured"] == [False, True, False]


def test_capture_button_runs_one_shot_in_the_background(make_client, tmp_path):
    runner = FakeRunner()
    client = make_client(three_shots(), clips_dir=tmp_path / "clips", capture_runner=runner)
    status, data = client.post("/api/capture", {"index": 1})
    assert status == 200 and data["capture"]["state"] == "running" and data["capture"]["shot"] == 1

    # the editor keeps answering, and a second capture is refused while one runs
    assert client.get("/api/shotlist")[0] == 200
    status, data = client.post("/api/capture", {"index": 0})
    assert status == 409 and "being captured" in data["error"]

    runner.release.set()
    data = wait_for_capture(client, "done")
    assert data["captured"] == [False, True, False]
    assert "Captured shot 2" in data["capture"]["message"]
    assert "starting CS2 (fake)" in data["capture"]["log"]
    [jobs] = runner.jobs
    assert [job.shots for job in jobs] == [(1,)] and jobs[0].pov_player == P2


def test_capture_failure_is_reported(make_client, tmp_path):
    runner = FakeRunner(fail="CS2 is already running.")
    runner.release.set()
    client = make_client(three_shots(), clips_dir=tmp_path / "clips", capture_runner=runner)
    assert client.post("/api/capture", {"index": 0})[0] == 200
    data = wait_for_capture(client, "failed")
    assert "CS2 is already running." in data["capture"]["message"]
    assert data["captured"] == [False, False, False]


@pytest.mark.parametrize("body, status, fragment", [
    ({"index": 5}, 400, "no shot 6"),
    ({"index": "0"}, 400, "whole number"),
    ({}, 400, "required"),
    ({"index": 0, "path": "C:/x.dem"}, 400, "Unknown field"),
])
def test_invalid_capture_requests(make_client, tmp_path, body, status, fragment):
    runner = FakeRunner()
    client = make_client(three_shots(), clips_dir=tmp_path / "clips", capture_runner=runner)
    got, data = client.post("/api/capture", body)
    assert got == status and fragment in data["error"]
    assert not runner.jobs


def test_capture_of_an_invalid_shot_is_refused(make_client, tmp_path):
    bad = ShotList(shots=(shot(2000, 3100, P3),), tick_rate=64)  # donk dies at 3000
    runner = FakeRunner()
    client = make_client(bad, clips_dir=tmp_path / "clips", capture_runner=runner)
    status, data = client.post("/api/capture", {"index": 0})
    assert status == 422 and "no longer alive" in data["error"]
    assert not runner.jobs


def test_capture_needs_a_clips_folder(make_client):
    client = make_client(three_shots())
    status, data = client.post("/api/capture", {"index": 0})
    assert status == 409 and "clips folder" in data["error"]
    assert client.get("/api/capture")[1]["captured"] == []


# -- render ------------------------------------------------------------------


class FakeRender:
    """Stands in for FFmpeg: writes the video file, or fails, when released."""

    def __init__(self, fail=None):
        self.fail = fail
        self.release = threading.Event()
        self.calls = []

    def __call__(self, shotlist, clips_dir, out_path, options, log):
        self.calls.append((shotlist, clips_dir, out_path, options))
        log("rendering (fake)")
        self.release.wait(10)
        if self.fail is not None:
            raise RenderError(self.fail)
        out_path.write_bytes(b"video")
        return RenderResult(out_path, len(shotlist), 60, 600, 10.0, 10.0)


def wait_for_render(client, state, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, data = client.get("/api/render")
        if data["render"]["state"] == state:
            return data
        time.sleep(0.02)
    raise AssertionError(f"render never reached {state!r}: {data}")


def write_clips(shots, folder):
    folder.mkdir(parents=True, exist_ok=True)
    for s in shots.shots:
        clip_path(s, folder).write_bytes(b"clip")


def test_render_status_names_the_video_file(make_client, tmp_path):
    client = make_client(three_shots(), clips_dir=tmp_path / "clips")
    status, data = client.get("/api/render")
    assert status == 200 and data["render"]["state"] == "idle"
    assert data["video_path"] == str(tmp_path / "shots.mp4") and data["video_exists"] is False
    (tmp_path / "shots.mp4").write_bytes(b"x")
    assert client.get("/api/render")[1]["video_exists"] is True


def test_make_video_runs_in_the_background(make_client, tmp_path):
    shots = three_shots()
    write_clips(shots, tmp_path / "clips")
    runner = FakeRender()
    client = make_client(shots, clips_dir=tmp_path / "clips", render_runner=runner)
    status, data = client.post("/api/render", {"crossfade": 0.5, "labels": True})
    assert status == 200 and data["render"]["state"] == "running"

    assert client.get("/api/shotlist")[0] == 200  # the editor keeps answering
    status, data = client.post("/api/render", {})
    assert status == 409 and "already being made" in data["error"]

    runner.release.set()
    data = wait_for_render(client, "done")
    assert "rendering (fake)" in data["render"]["log"]
    assert str(tmp_path / "shots.mp4") in data["render"]["message"]
    assert data["video_exists"] is True
    [(sent, clips_dir, out_path, options)] = runner.calls
    assert sent == shots and clips_dir == tmp_path / "clips" and out_path == tmp_path / "shots.mp4"
    assert options.crossfade_seconds == 0.5 and options.labels is True


def test_make_video_defaults_to_hard_cuts_without_labels(make_client, tmp_path):
    shots = three_shots()
    write_clips(shots, tmp_path / "clips")
    runner = FakeRender()
    runner.release.set()
    client = make_client(shots, clips_dir=tmp_path / "clips", render_runner=runner)
    assert client.post("/api/render", {})[0] == 200
    wait_for_render(client, "done")
    options = runner.calls[0][3]
    assert options.crossfade_seconds == 0 and options.labels is False


def test_make_video_failure_is_reported(make_client, tmp_path):
    shots = three_shots()
    write_clips(shots, tmp_path / "clips")
    runner = FakeRender(fail="FFmpeg could not make the video: boom")
    runner.release.set()
    client = make_client(shots, clips_dir=tmp_path / "clips", render_runner=runner)
    assert client.post("/api/render", {})[0] == 200
    data = wait_for_render(client, "failed")
    assert "boom" in data["render"]["message"] and data["video_exists"] is False


def test_make_video_needs_every_clip(make_client, tmp_path):
    shots = three_shots()
    write_clips(ShotList(shots=shots.shots[:1], tick_rate=64), tmp_path / "clips")
    runner = FakeRender()
    client = make_client(shots, clips_dir=tmp_path / "clips", render_runner=runner)
    status, data = client.post("/api/render", {})
    assert status == 422 and "2 of 3 shot(s) have no clip yet (shot 2, 3)" in data["error"]
    assert not runner.calls and client.get("/api/render")[1]["render"]["state"] == "idle"


@pytest.mark.parametrize("body, status, fragment", [
    ({"crossfade": 9}, 400, "between 0 and 5"),
    ({"crossfade": -1}, 400, "between 0 and 5"),
    ({"crossfade": "0.5"}, 400, "must be a number"),
    ({"labels": "yes"}, 400, "true or false"),
    ({"out": "C:/x.mp4"}, 400, "Unknown field"),
    ({"crossfade": 0.5, "path": "x"}, 400, "Unknown field"),
])
def test_invalid_render_requests(make_client, tmp_path, body, status, fragment):
    shots = three_shots()
    write_clips(shots, tmp_path / "clips")
    runner = FakeRender()
    client = make_client(shots, clips_dir=tmp_path / "clips", render_runner=runner)
    got, data = client.post("/api/render", body)
    assert got == status and fragment in data["error"]
    assert not runner.calls


def test_make_video_of_an_empty_timeline_is_refused(make_client, tmp_path):
    runner = FakeRender()
    client = make_client(ShotList(tick_rate=64), clips_dir=tmp_path / "clips", render_runner=runner)
    status, data = client.post("/api/render", {})
    assert status == 409 and "empty" in data["error"] and not runner.calls


def test_make_video_needs_a_clips_folder_and_a_shot_list_file(make_client, tmp_path):
    shots = three_shots()
    assert make_client(shots)  # no clips folder
    status, data = make_client(shots).post("/api/render", {})
    assert status == 409 and "clips folder" in data["error"]
    status, data = make_client(shots, save=False, clips_dir=tmp_path / "clips").post("/api/render", {})
    assert status == 409 and "shot list file" in data["error"]
    assert make_client(shots, save=False).get("/api/render")[1]["video_path"] is None


def test_make_video_waits_for_a_running_capture(make_client, tmp_path):
    shots = three_shots()
    write_clips(shots, tmp_path / "clips")
    capture_runner, render_runner = FakeRunner(), FakeRender()
    client = make_client(shots, clips_dir=tmp_path / "clips", capture_runner=capture_runner,
                         render_runner=render_runner)
    assert client.post("/api/capture", {"index": 0})[0] == 200
    status, data = client.post("/api/render", {})
    assert status == 409 and "being captured" in data["error"] and not render_runner.calls
    capture_runner.release.set()
    wait_for_capture(client, "done")


def test_capture_waits_for_a_running_render(make_client, tmp_path):
    shots = three_shots()
    write_clips(shots, tmp_path / "clips")
    capture_runner, render_runner = FakeRunner(), FakeRender()
    client = make_client(shots, clips_dir=tmp_path / "clips", capture_runner=capture_runner,
                         render_runner=render_runner)
    assert client.post("/api/render", {})[0] == 200
    status, data = client.post("/api/capture", {"index": 0})
    assert status == 409 and "video is being made" in data["error"] and not capture_runner.jobs
    render_runner.release.set()
    wait_for_render(client, "done")


# -- command line ------------------------------------------------------------


def test_cli_open_rejects_a_bad_port(capsys):
    assert main(["open", "whatever.dem", "--port", "70000"]) == 1
    assert "--port" in capsys.readouterr().err


def test_cli_open_reports_a_missing_demo(capsys):
    assert main(["open", "no-such-file.dem", "--no-browser"]) == 1
    assert "fragdirector:" in capsys.readouterr().err
