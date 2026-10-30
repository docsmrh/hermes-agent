"""End to end: Hermes tool -> HTTP -> Mac relay -> userdata files -> mod Lua.

Everything here is the shipped code except the game itself (fake_game.lua)
and the SSH tunnel (the relay listens on 127.0.0.1, exactly where the tunnel
would deliver the VPS's requests). Needs the optional ``lupa`` package.
"""

from __future__ import annotations

import base64
import json
import threading

import pytest

pytest.importorskip("lupa.lua52")

from tests.plugins.tf3.harness import FakeGame, load_plugin, load_relay  # noqa: E402

R = load_relay()
TOKEN = "e2e-token-" + "z" * 40


class Ctx:
    def __init__(self, settings):
        self.settings = settings
        self.state_data = {}
        self.state = self

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def get(self, key, default=None):
        return self.state_data.get(key, default)

    def set(self, key, value):
        self.state_data[key] = value


@pytest.fixture
def stack(tmp_path, monkeypatch):
    game = FakeGame(tmp_path / "userdata")
    game.run(3)
    cfg = R.Config(tmp_path / "relayhome", {"token": TOKEN, "userdata_dir": str(game.userdata)})
    cfg.save()
    channel = R.ModChannel(cfg.exchange_dir, poll_s=0.01)
    channel.poll_once()
    channel.start()
    game.start()
    # No sips on Linux CI: pass the game's PNG through unchanged.
    relay = R.Relay(cfg, channel, convert_fn=lambda p, **kw: (p.read_bytes(), 1920, 1080),
                    screen_fn=lambda **kw: (_ for _ in ()).throw(AssertionError("should use the game")))
    server = R.build_server(relay, port=0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()

    monkeypatch.setenv("TF3_BRIDGE_TOKEN", TOKEN)
    plugin = load_plugin()
    ctx = Ctx({"bridge_url": f"http://127.0.0.1:{server.server_address[1]}", "request_timeout_s": 15})
    plugin.tools.bind(ctx)
    monkeypatch.setattr(plugin.tools.time, "sleep", lambda s: None)
    yield plugin, ctx, game, cfg
    server.shutdown()
    server.server_close()
    game.stop()
    channel.stop()


def tool(plugin, name, args, session="e2e"):
    out = plugin.tools.handler_for(name)(args, session_id=session)
    return out if isinstance(out, dict) else json.loads(out)


def test_observe_act_verify_loop(stack):
    plugin, ctx, game, cfg = stack
    snap = tool(plugin, "tf3_observe", {"sections": ["finance", "lines", "stations", "depots"]})["snapshot"]
    assert snap["finance"]["balance"] == 5_000_000
    stations = [s["id"] for s in snap["stations"]["items"]]
    depot = snap["depots"]["items"][0]["id"]

    created = tool(plugin, "tf3_act", {"action": "line_create", "params": {"name": "Express", "stops": stations},
                                       "reason": "link both stations"})
    assert created["result"]["accepted"], created
    line_id = created["result"]["line_id"]

    models = tool(plugin, "tf3_observe", {"model_search": "bus"})["models"]["models"]
    bought = tool(plugin, "tf3_act", {"action": "vehicle_buy",
                                      "params": {"depot_id": depot, "model": models[0]["file"], "line_id": line_id},
                                      "reason": "first bus for Express"})
    assert bought["result"]["line_assigned"] is True

    after = tool(plugin, "tf3_observe", {"sections": ["lines"]})["snapshot"]["lines"]["items"]
    assert "Express" in [line["name"] for line in after]
    assert [e["action"] for e in ctx.get("action_log")] == ["line_create", "vehicle_buy"]
    assert game.G.fatal_reads == 0


def test_in_game_screenshot(stack):
    plugin, ctx, game, cfg = stack
    out = tool(plugin, "tf3_screenshot", {"focus_entity_id": 100})
    assert out["_multimodal"] is True and out["meta"]["source"] == "game"
    data = base64.b64decode(out["content"][1]["image_url"]["url"].split(",", 1)[1])
    assert data.startswith(b"\x89PNG")
    with game.lock:
        assert game.G.camera.entity == 100
    assert not list((game.userdata / "screenshots").iterdir())  # relay removed the game's file


def test_kill_switch_on_the_mac(stack):
    plugin, ctx, game, cfg = stack
    cfg.paused_flag.write_text("stop")
    out = tool(plugin, "tf3_act", {"action": "set_speed", "params": {"speedup": 4}, "reason": "faster"})
    assert out["code"] == "paused"
    assert "snapshot" in tool(plugin, "tf3_observe", {})  # observing still works
    assert "speed" not in game.log_kinds()


def test_lua_needs_mac_side_opt_in(stack):
    plugin, ctx, game, cfg = stack
    out = tool(plugin, "tf3_lua", {"code": "return 1", "reason": "probe"})
    assert out["code"] == "policy_denied"
    cfg.data["allow_lua"] = True
    out = tool(plugin, "tf3_lua", {"code": "return api.engine.util.getYear()", "reason": "probe"})
    assert out["result"]["value"] == 1905


def test_mod_errors_reach_the_agent(stack):
    plugin, ctx, game, cfg = stack
    out = tool(plugin, "tf3_act", {"action": "vehicle_sell", "params": {"vehicle_id": 999999}, "reason": "x"})
    assert out["code"] == "not_found" and "999999" in out["error"]
    assert game.G.fatal_reads == 0
