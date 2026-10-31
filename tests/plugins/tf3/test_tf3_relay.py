"""Mac relay (tf3_relay.py): userdata file protocol, policy, and HTTP surface.

The mod side is simulated in Python here (writing outbox files the way the
game's app.saveUserdata does); test_tf3_mod_lua.py and test_tf3_e2e.py run
the real mod Lua.
"""

from __future__ import annotations

import http.client
import json
import os
import threading
import time
from pathlib import Path

import pytest

from tests.plugins.tf3.harness import load_relay

R = load_relay()
TOKEN = "t" * 43


# ---------------------------------------------------------------------------
# Lua table literal parsing (on-disk format of app.saveUserdata files)
# ---------------------------------------------------------------------------

def test_parses_game_style_userdata_file():
    # Layout as recorded from a real TF3 export: tab indents, trailing commas,
    # "{ }" for empty tables, bracketed string keys, %g floats, raw UTF-8,
    # and no final newline.
    src = (
        'function data()\nreturn { \n\t\tbalance = 1293309969,\n\t\tempty = { },\n'
        '\t\tnames = { \n\t\t\t"Zürich",\n\t\t\t"say \\"hi\\"\\n",\n\t\t},\n'
        '\t\trunning_cost = 1.61114e+06,\n\t\tx = -2846.68,\n\t\t["0"] = 13,\n'
        '\t\tok = true,\n\t\tlong = [[raw]],\n\t\t-- comment\n\t}\nend'
    ).encode("utf-8")
    t = R.parse_lua_table_file(src)
    assert t["balance"] == 1293309969
    assert t["empty"] in ([], {})  # an empty Lua table is ambiguous; either is fine
    assert t["names"] == ["Zürich".encode("utf-8"), b'say "hi"\n']
    assert t["running_cost"] == pytest.approx(1.61114e6)
    assert t["x"] == pytest.approx(-2846.68)
    assert t["0"] == 13
    assert t["ok"] is True
    assert t["long"] == b"raw"


@pytest.mark.parametrize("escaped,expected", [
    (r'"\065\066"', b"AB"), (r'"\x41"', b"A"), ('"a\\\nb"', b"a\nb"), (r'"tab\tend"', b"tab\tend"),
    (r'"\z   x"', b"x"), (r'"\u{e9}"', "é".encode("utf-8")),
])
def test_lua_string_escapes(escaped, expected):
    assert R.parse_lua_table_file(("return { " + escaped + " }").encode())[0] == expected


def test_parts_file_round_trip_large_and_unicode():
    payload = {"v": 1, "session": "s", "requests": [{"id": "x", "args": {"name": "Zürich \"Hbf\"\n" * 5000}}]}
    rendered = R.render_parts_file(payload)
    assert rendered.startswith(b"function data()\nreturn {")
    assert rendered.count(b'",') > 1  # split into several parts
    assert R.parse_parts_file(rendered) == payload


def test_parse_parts_file_rejects_garbage():
    assert R.parse_parts_file(b"function data() return { v = 1, parts = { 12 } } end") is None
    assert R.parse_parts_file(b"function data() return { v = 2, parts = { } } end") is None
    assert R.parse_parts_file(b"not lua at all {") is None
    assert R.parse_parts_file(b'function data() return { v = 1, parts = { "{\\"a\\"" } } end') is None


# ---------------------------------------------------------------------------
# ModChannel against a Python-simulated mod
# ---------------------------------------------------------------------------

def _lua_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


class SimMod:
    """Writes outbox files like the mod (fresh name per write) and reads the inbox."""

    def __init__(self, folder: Path, stamp: str = "1700000000123456"):
        self.folder = folder
        self.stamp = stamp
        self.k = 0
        self.executed: list = []
        (folder / f"hermes_bridge__in_{stamp}.lua").write_text("function data()\nreturn { v = 1, parts = { } }\nend\n")

    def inbox(self):
        return R.parse_parts_file((self.folder / f"hermes_bridge__in_{self.stamp}.lua").read_bytes()) or {}

    def write_outbox(self, session: str = "", results=None) -> None:
        self.k += 1
        text = json.dumps({"v": 1, "stamp": self.stamp, "session": session, "mod_version": "sim",
                           "transport": "userdata", "status": "connected", "results": results or []})
        body = f"function data()\nreturn {{ \n\t\tk = {self.k},\n\t\tparts = {{ \n\t\t\t{_lua_str(text)},\n\t\t}},\n\t\tv = 1,\n\t}}\nend"
        (self.folder / f"hermes_bridge__out_{self.stamp}_{self.k}.lua").write_text(body, encoding="utf-8")

    def serve_once(self) -> None:
        inbox = self.inbox()
        results = []
        for req in inbox.get("requests", []):
            if req["id"] not in self.executed:
                self.executed.append(req["id"])
            results.append({"id": req["id"], "seq": req["seq"], "status": "done", "ok": True,
                            "result": {"op": req["op"], "echo": req["args"]}})
        self.write_outbox(inbox.get("session", ""), results)


@pytest.fixture
def exchange(tmp_path):
    folder = tmp_path / "userdata" / "heightmaps"
    folder.mkdir(parents=True)
    return folder


def _serve_in_background(channel, sim, stop):
    def loop():
        while not stop.is_set():
            channel.poll_once()
            sim.serve_once()
            channel.poll_once()
            time.sleep(0.01)
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t


def test_not_connected_until_the_mod_writes(exchange):
    ch = R.ModChannel(exchange)
    ch.poll_once()
    assert not ch.connected()
    with pytest.raises(R.RelayError) as exc:
        ch.call("hello", {}, "a1", 1)
    assert exc.value.code == "mod_not_connected" and exc.value.status == 503


def test_call_round_trip_and_cleanup(exchange):
    sim = SimMod(exchange)
    sim.write_outbox()
    ch = R.ModChannel(exchange)
    ch.poll_once()
    assert ch.connected() and ch.mod_info()["stamp"] == sim.stamp
    stop = threading.Event()
    t = _serve_in_background(ch, sim, stop)
    try:
        res = ch.call("snapshot", {"limit": 3}, "req-1", 5)
    finally:
        stop.set()
        t.join()
    assert res == {"ok": True, "result": {"op": "snapshot", "echo": {"limit": 3}}, "error": None}
    outboxes = sorted(p.name for p in exchange.glob("hermes_bridge__out_*"))
    assert outboxes == [f"hermes_bridge__out_{sim.stamp}_{sim.k}.lua"]  # consumed files deleted
    assert sim.inbox()["requests"] == []  # completed request dropped from the inbox


def test_same_request_id_is_answered_once(exchange):
    sim = SimMod(exchange)
    sim.write_outbox()
    ch = R.ModChannel(exchange)
    ch.poll_once()
    stop = threading.Event()
    t = _serve_in_background(ch, sim, stop)
    try:
        first = ch.call("vehicle_buy", {"depot_id": 1}, "idem-1", 5)
        second = ch.call("vehicle_buy", {"depot_id": 1}, "idem-1", 5)
    finally:
        stop.set()
        t.join()
    assert first == second
    assert sim.executed.count("idem-1") == 1


def test_timeout_keeps_request_for_a_retry(exchange):
    sim = SimMod(exchange)
    sim.write_outbox()
    ch = R.ModChannel(exchange)
    ch.poll_once()
    with pytest.raises(R.RelayError) as exc:
        ch.call("hello", {}, "slow-1", 1)
    assert exc.value.code == "timeout" and exc.value.retryable
    assert [r["id"] for r in sim.inbox()["requests"]] == ["slow-1"]  # still queued
    sim.serve_once()  # the game answers late
    ch.poll_once()
    assert ch.call("hello", {}, "slow-1", 1)["ok"] is True  # retry gets the late result


def test_new_stamp_resends_pending_requests(exchange):
    old = SimMod(exchange, stamp="1700000000000001")
    old.write_outbox()
    ch = R.ModChannel(exchange)
    ch.poll_once()
    with pytest.raises(R.RelayError):
        ch.call("hello", {}, "carry-1", 1)
    time.sleep(0.02)
    new = SimMod(exchange, stamp="1700000000000002")  # the game reloaded
    new.write_outbox()
    ch.poll_once()
    assert ch.mod_info()["stamp"] == new.stamp
    assert [r["id"] for r in new.inbox()["requests"]] == ["carry-1"]


def test_results_for_another_session_are_ignored(exchange):
    sim = SimMod(exchange)
    sim.write_outbox(session="someone-else", results=[{"id": "x", "status": "done", "ok": True}])
    ch = R.ModChannel(exchange)
    ch.poll_once()
    assert ch.connected() and ch.mod_info()["pending"] == 0


# ---------------------------------------------------------------------------
# Relay policy
# ---------------------------------------------------------------------------

class StubChannel:
    def __init__(self, connected=True):
        self.calls = []
        self._connected = connected

    def call(self, op, args, request_id, timeout_s):
        self.calls.append(op)
        return {"ok": True, "result": {"op": op}}

    def connected(self):
        return self._connected

    def heartbeat_age(self):
        return 0.5

    def mod_info(self):
        return {"stamp": "1", "pending": 0}


def make_relay(tmp_path, channel=None, **overrides):
    home = tmp_path / "relayhome"
    cfg = R.Config(home, {"token": TOKEN, "userdata_dir": str(tmp_path / "userdata"), **overrides})
    cfg.save()
    return R.Relay(cfg, channel or StubChannel(), screen_fn=lambda **kw: (b"\xff\xd8jpeg", 800, 600),
                   convert_fn=lambda p, **kw: (b"\xff\xd8game", 1600, 900), sleep=lambda s: None)


def test_plugin_and_relay_sign_identically():
    from tests.plugins.tf3.harness import load_plugin
    client = load_plugin().client
    args = ("k" * 43, "POST", "/v1/screenshot?max_width=800", "1700000000", "ab" * 16, b'{"x":1}')
    assert client.request_signature(*args) == R.request_signature(*args)
    assert client.response_signature("k" * 43, "ab" * 16, 422, b"{}") == R.response_signature("k" * 43, "ab" * 16, 422, b"{}")


def test_policy_allowlist_and_kill_switch(tmp_path):
    relay = make_relay(tmp_path)
    with pytest.raises(R.RelayError) as exc:
        relay.call({"op": "os_execute", "args": {}})
    assert exc.value.code == "unknown_op"
    assert relay.call({"op": "line_create", "args": {}})["ok"]
    relay.config.paused_flag.write_text("x")
    with pytest.raises(R.RelayError) as exc:
        relay.call({"op": "line_create", "args": {}})
    assert exc.value.code == "paused" and exc.value.status == 403
    assert relay.call({"op": "snapshot", "args": {}})["ok"]  # reads still work while paused


@pytest.mark.parametrize("op,flag", [("lua_eval", "allow_lua"), ("save_game", "allow_save"),
                                     ("vehicle_buy", "allow_actions"), ("camera_focus", "screenshot_enabled")])
def test_policy_flags_deny(tmp_path, op, flag):
    relay = make_relay(tmp_path, **{flag: False})
    with pytest.raises(R.RelayError) as exc:
        relay.call({"op": op, "args": {}})
    assert exc.value.code == "policy_denied"


def test_lua_is_off_by_default(tmp_path):
    with pytest.raises(R.RelayError) as exc:
        make_relay(tmp_path).call({"op": "lua_eval", "args": {"code": "return 1"}})
    assert exc.value.code == "policy_denied"


def test_rejects_bad_request_ids(tmp_path):
    relay = make_relay(tmp_path)
    for bad in ["../etc", "x" * 65, "a b"]:
        with pytest.raises(R.RelayError) as exc:
            relay.call({"op": "hello", "request_id": bad})
        assert exc.value.code == "invalid_request"


def test_mod_errors_map_to_422(tmp_path):
    class Failing(StubChannel):
        def call(self, *a):
            return {"ok": False, "error": {"code": "not_found", "message": "line 7 does not exist"}}
    with pytest.raises(R.RelayError) as exc:
        make_relay(tmp_path, Failing()).call({"op": "line_delete", "args": {"line_id": 7}})
    assert (exc.value.status, exc.value.code) == (422, "not_found")


def _shooting_channel(shots):
    class Shooting(StubChannel):
        def call(self, op, args, request_id, timeout_s):
            if op == "take_screenshot":
                (shots / "shot.png").write_bytes(b"\x89PNG")
            return super().call(op, args, request_id, timeout_s)
    return Shooting()


def test_screenshot_is_game_only_by_default(tmp_path):
    relay = make_relay(tmp_path)
    shots = tmp_path / "userdata" / "screenshots"
    shots.mkdir(parents=True)
    relay.channel = _shooting_channel(shots)
    data, w, h, source = relay.screenshot({})
    assert (data, source) == (b"\xff\xd8game", "game")
    assert not (shots / "shot.png").exists()  # cleaned up by default
    relay._last_shot = 0
    with pytest.raises(R.RelayError) as exc:  # a caller can not widen the Mac-side ceiling
        relay.screenshot({"source": ["screen"]})
    assert exc.value.code == "policy_denied"
    relay.channel = StubChannel(connected=False)
    relay._last_shot = 0
    with pytest.raises(R.RelayError) as exc:  # and the mod being away never exposes the desktop
        relay.screenshot({})
    assert exc.value.code == "mod_not_connected"


def test_desktop_capture_needs_mac_opt_in(tmp_path):
    relay = make_relay(tmp_path, screenshot_source="auto")
    relay.channel = StubChannel(connected=False)
    assert relay.screenshot({})[3] == "screen"  # auto: game if possible, else the display
    relay._last_shot = 0
    shots = tmp_path / "userdata" / "screenshots"
    shots.mkdir(parents=True)
    relay.channel = _shooting_channel(shots)
    assert relay.screenshot({"source": ["game"]})[3] == "game"  # narrowing is always allowed


def test_screenshots_are_rate_limited(tmp_path):
    relay = make_relay(tmp_path, screenshot_source="screen")
    relay.screenshot({})
    with pytest.raises(R.RelayError) as exc:
        relay.screenshot({})
    assert exc.value.status == 429


def test_take_screenshot_is_internal_only(tmp_path):
    with pytest.raises(R.RelayError) as exc:
        make_relay(tmp_path).call({"op": "take_screenshot", "args": {"scale": 4}})
    assert exc.value.code == "unknown_op"


def test_request_size_and_shape_limits(tmp_path):
    relay = make_relay(tmp_path)
    with pytest.raises(R.RelayError) as exc:
        relay.call({"op": "hello", "args": {"blob": "x" * (R.MAX_ARGS_BYTES + 1)}})
    assert exc.value.status == 413
    for raw in [b'{"op":"hello","args":{"x":NaN}}', b'{"op":"hello","args":{"x":Infinity}}',
                b'{"op":"hello","args":' + b'[' * 40 + b']' * 40 + b'}']:
        with pytest.raises(R.RelayError) as exc:
            R.parse_request_json(raw)
        assert exc.value.code == "invalid_request"


def test_config_changes_apply_without_restart(tmp_path):
    relay = make_relay(tmp_path)
    assert relay.call({"op": "vehicle_stop", "args": {}})["ok"]
    cfg = json.loads(relay.config.path.read_text())
    cfg["allow_actions"] = False
    cfg["token"] = "n" * 43  # rotated
    relay.config.path.write_text(json.dumps(cfg))
    relay.refresh_config(force=True)
    with pytest.raises(R.RelayError) as exc:
        relay.call({"op": "vehicle_stop", "args": {}})
    assert exc.value.code == "policy_denied"
    assert relay.config.token == "n" * 43


# ---------------------------------------------------------------------------
# ModChannel: policy, stamps, robustness
# ---------------------------------------------------------------------------

def test_inbox_carries_policy_and_stamp_and_follows_pause(exchange):
    sim = SimMod(exchange)
    sim.write_outbox()
    policy = {"paused": False, "allow_actions": True, "allow_save": True, "allow_lua": False}
    ch = R.ModChannel(exchange, policy_fn=lambda: dict(policy))
    ch.poll_once()
    inbox = sim.inbox()
    assert inbox["policy"] == policy
    policy["paused"] = True
    ch.poll_once()  # the policy change is published right away, not at the next heartbeat
    assert sim.inbox()["policy"]["paused"] is True


def test_default_policy_fails_closed(exchange):
    sim = SimMod(exchange)
    sim.write_outbox()
    ch = R.ModChannel(exchange)
    ch.poll_once()
    assert sim.inbox()["policy"] == R.FAIL_CLOSED_POLICY


def test_game_reload_fails_pending_actions_and_resends_reads(exchange):
    old = SimMod(exchange, stamp="1700000000000001")
    old.write_outbox()
    ch = R.ModChannel(exchange)
    ch.poll_once()
    for op, rid in [("vehicle_sell", "act-1"), ("snapshot", "read-1")]:
        with pytest.raises(R.RelayError):
            ch.call(op, {}, rid, 1)
    assert {r["stamp"] for r in old.inbox()["requests"]} == {old.stamp}
    time.sleep(0.02)
    new = SimMod(exchange, stamp="1700000000000002")
    new.write_outbox()
    ch.poll_once()
    assert [(r["id"], r["stamp"]) for r in new.inbox()["requests"]] == [("read-1", new.stamp)]
    assert old.inbox()["requests"] == []  # nothing left behind for the old load
    res = ch.call("vehicle_sell", {}, "act-1", 1)
    assert res["ok"] is False and res["error"]["code"] == "game_reloaded"


def test_newest_outbox_wins_even_with_a_lower_k(exchange):
    sim = SimMod(exchange)
    sim.k = 40
    sim.write_outbox()  # k=41
    ch = R.ModChannel(exchange)
    ch.poll_once()
    time.sleep(0.02)
    sim.k = 0  # the mod restarted its counter under the same stamp
    sim.write_outbox(session=ch.session, results=[{"id": "z", "status": "done", "ok": True}])
    ch.poll_once()
    assert ch.connected()
    assert ch._last_seen[1] == 1


def test_bad_utf8_in_an_outbox_does_not_wedge_the_channel(exchange):
    sim = SimMod(exchange)
    sim.write_outbox()
    ch = R.ModChannel(exchange)
    ch.poll_once()
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            inbox = sim.inbox()
            results = [{"id": r["id"], "seq": r["seq"], "status": "done", "ok": True, "result": {"name": "@@"}}
                       for r in inbox.get("requests", [])]
            sim.k += 1
            text = json.dumps({"v": 1, "stamp": sim.stamp, "session": inbox.get("session", ""), "results": results})
            raw = ("function data()\nreturn { k = %d, parts = { %s }, v = 1, }\nend" % (sim.k, _lua_str(text))).encode()
            (exchange / f"hermes_bridge__out_{sim.stamp}_{sim.k}.lua").write_bytes(raw.replace(b"@@", b"\xe9t\xe9"))
            ch.poll_once()
            time.sleep(0.01)
    t = threading.Thread(target=serve, daemon=True)
    t.start()
    try:
        res = ch.call("snapshot", {}, "latin1", 5)
    finally:
        stop.set()
        t.join()
    assert res["ok"] and "�" in res["result"]["name"]


def test_failed_inbox_write_is_reported_as_not_sent(exchange, monkeypatch):
    sim = SimMod(exchange)
    sim.write_outbox()
    ch = R.ModChannel(exchange)
    ch.poll_once()

    def broken(path, data):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(R, "atomic_write_bytes", broken)
    with pytest.raises(R.RelayError) as exc:
        ch.call("vehicle_sell", {}, "io-1", 1)
    assert (exc.value.status, exc.value.code, exc.value.retryable) == (503, "relay_io_error", True)
    assert ch.mod_info()["pending"] == 0


def test_pending_payload_is_bounded(exchange):
    sim = SimMod(exchange)
    sim.write_outbox()
    ch = R.ModChannel(exchange)
    ch.poll_once()
    big = {"blob": "x" * (R.MAX_ARGS_BYTES - 100)}
    with pytest.raises(R.RelayError) as exc:
        for i in range(R.MAX_PENDING_ARGS_BYTES // R.MAX_ARGS_BYTES + 2):
            try:
                ch.call("ping", big, f"big-{i}", 0.01)
            except R.RelayError as inner:
                if inner.code != "timeout":
                    raise
    assert exc.value.code == "too_much_pending"


# ---------------------------------------------------------------------------
# HTTP surface (requests are HMAC-signed; responses are signed back)
# ---------------------------------------------------------------------------

@pytest.fixture
def server(tmp_path):
    relay = make_relay(tmp_path, screenshot_source="screen")
    srv = R.build_server(relay, port=0)
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def _req(port, method, path, *, token=TOKEN, body=None, host=None, raw_length=None, ts=None, nonce=None,
         sign=True):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    data = json.dumps(body).encode() if body is not None else b""
    headers = {}
    if sign:
        ts = str(int(time.time())) if ts is None else ts
        nonce = os.urandom(16).hex() if nonce is None else nonce
        headers.update({"X-TF3-Timestamp": ts, "X-TF3-Nonce": nonce,
                        "X-TF3-Signature": R.request_signature(token, method, path, ts, nonce, data)})
    if host is not None:
        headers["Host"] = host
    if raw_length is not None:
        headers["Content-Length"] = str(raw_length)
    conn.request(method, path, body=data or None, headers=headers)
    resp = conn.getresponse()
    out = resp.status, dict(resp.getheaders()), resp.read()
    conn.close()
    if sign and "X-TF3-Response-Signature" in out[1]:
        assert out[1]["X-TF3-Response-Signature"] == R.response_signature(TOKEN, nonce, out[0], out[2])
    return out


def test_http_requires_a_valid_signature(server):
    assert _req(server, "GET", "/v1/health", sign=False)[0] == 401
    assert _req(server, "GET", "/v1/health", token="w" * 43)[0] == 401
    status, headers, body = _req(server, "GET", "/v1/health")
    assert status == 200 and json.loads(body)["policy"]["allow_lua"] is False
    assert "X-TF3-Response-Signature" in headers  # verified inside _req


def test_http_rejects_stale_and_replayed_requests(server):
    assert _req(server, "GET", "/v1/health", ts=str(int(time.time()) - 600))[0] == 401
    nonce = os.urandom(16).hex()
    assert _req(server, "GET", "/v1/health", nonce=nonce)[0] == 200
    status, _, body = _req(server, "GET", "/v1/health", nonce=nonce)
    assert status == 401 and json.loads(body)["error"]["code"] == "replayed_request"


def test_signature_covers_the_body(server):
    conn = http.client.HTTPConnection("127.0.0.1", server, timeout=5)
    signed = json.dumps({"op": "hello", "args": {}}).encode()
    sent = json.dumps({"op": "vehicle_sell", "args": {"vehicle_id": 1}}).encode()
    ts, nonce = str(int(time.time())), os.urandom(16).hex()
    conn.request("POST", "/v1/call", body=sent, headers={
        "X-TF3-Timestamp": ts, "X-TF3-Nonce": nonce,
        "X-TF3-Signature": R.request_signature(TOKEN, "POST", "/v1/call", ts, nonce, signed)})
    assert conn.getresponse().status == 401
    conn.close()


def test_http_rejects_foreign_host_header(server):
    assert _req(server, "GET", "/v1/health", host="evil.example:47800")[0] == 421
    assert _req(server, "GET", "/v1/health", host="localhost:50000")[0] == 200  # tunnel port may differ


def test_http_call_and_limits(server):
    status, _, body = _req(server, "POST", "/v1/call", body={"op": "hello", "args": {}, "request_id": "h1"})
    assert status == 200 and json.loads(body)["result"] == {"op": "hello"}
    status, _, body = _req(server, "POST", "/v1/call", body={"op": "rm_rf", "args": {}})
    assert status == 400 and json.loads(body)["error"]["code"] == "unknown_op"
    assert _req(server, "POST", "/v1/call", raw_length=R.MAX_BODY_BYTES + 1, sign=False)[0] == 413
    assert _req(server, "GET", "/v2/anything")[0] == 404


def test_http_screenshot_returns_jpeg(server):
    status, headers, body = _req(server, "GET", "/v1/screenshot?max_width=800&source=screen")
    assert status == 200 and headers["Content-Type"] == "image/jpeg" and body.startswith(b"\xff\xd8")
    assert headers["X-Image-Source"] == "screen"


def test_idle_connections_are_closed(tmp_path, monkeypatch):
    import socket
    monkeypatch.setattr(R, "SOCKET_TIMEOUT_S", 1)
    srv = R.build_server(make_relay(tmp_path), port=0)
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    try:
        idle = socket.create_connection(("127.0.0.1", srv.server_address[1]), timeout=5)
        idle.sendall(b"GET /v1/health HTTP/1.1\r\n")  # never finishes the request
        start = time.monotonic()
        idle.recv(1024)  # returns once the server gives up on the half-sent request
        assert time.monotonic() - start < 4  # it hung up after ~1 s instead of waiting forever
        idle.close()
        assert _req(srv.server_address[1], "GET", "/v1/health")[0] == 200
    finally:
        srv.shutdown()
        srv.server_close()


def test_server_binds_loopback_only(tmp_path):
    srv = R.build_server(make_relay(tmp_path), port=0)
    try:
        assert srv.server_address[0] == "127.0.0.1"
    finally:
        srv.server_close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_init_writes_private_config(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TF3_RELAY_HOME", str(tmp_path / "home"))
    userdata = tmp_path / "userdata"
    userdata.mkdir()
    assert R.main(["init", "--userdata-dir", str(userdata)]) == 0
    cfg_path = tmp_path / "home" / "config.json"
    assert cfg_path.stat().st_mode & 0o777 == 0o600
    data = json.loads(cfg_path.read_text())
    assert len(data["token"]) >= 32
    assert data["allow_lua"] is False  # Lua escape hatch is off unless the user opts in
    assert data["token"] not in capsys.readouterr().out  # token is never printed by init
    assert (userdata / "heightmaps").is_dir()
    assert R.main(["pause"]) == 0 and (tmp_path / "home" / "PAUSED").exists()
    assert R.main(["resume"]) == 0 and not (tmp_path / "home" / "PAUSED").exists()
    assert R.main(["status"]) == 0
