"""The VPS-side Hermes plugin: client, guardrails, tool handlers, discovery."""

from __future__ import annotations

import base64
import json
import re

import httpx
import pytest
import yaml

from tests.plugins.tf3.harness import PLUGIN_DIR, load_plugin

TOKEN = "x" * 43


@pytest.fixture
def plugin(monkeypatch):
    monkeypatch.setenv("TF3_BRIDGE_TOKEN", TOKEN)
    return load_plugin()


class FakeState:
    def __init__(self):
        self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        json.dumps(value)  # same constraint as PluginState
        self.data[key] = value


class FakeCtx:
    def __init__(self, settings=None):
        self.settings = dict(settings or {})
        self.state = FakeState()

    def get_config(self, key, default=None):
        return self.settings.get(key, default)


class FakeRelay:
    """httpx transport that answers like tf3_relay.py: it checks the request
    signature, signs its response, and records the calls it served."""

    def __init__(self, plugin, balance=5_000_000):
        self.client_mod = plugin.client
        self.calls = []
        self.balance = balance
        self.paused = False
        self.game_time = 1000
        self.cost = 60_000
        self.script = {}      # op -> list of canned (status, json) answers, consumed in order
        self.sign = True

    def _respond(self, request, status, body):
        content = body if isinstance(body, bytes) else json.dumps(body).encode()
        headers = {"Content-Type": "image/jpeg" if isinstance(body, bytes) else "application/json"}
        if self.sign:
            headers["X-TF3-Response-Signature"] = self.client_mod.response_signature(
                TOKEN, request.headers["X-TF3-Nonce"], status, content)
        return content, status, headers

    def __call__(self, request: httpx.Request) -> httpx.Response:
        target = request.url.raw_path.decode()
        expected = self.client_mod.request_signature(
            TOKEN, request.method, target, request.headers["X-TF3-Timestamp"], request.headers["X-TF3-Nonce"],
            request.content)
        assert request.headers["X-TF3-Signature"] == expected
        assert "Authorization" not in request.headers  # the token itself never travels
        content, status, headers = self.handle(request, target)
        return httpx.Response(status, content=content, headers=headers)

    def handle(self, request, target):
        if target.startswith("/v1/health"):
            return self._respond(request, 200, {"ok": True, "mod": {"connected": True}, "policy": {}})
        if target.startswith("/v1/screenshot"):
            self.calls.append(("screenshot", dict(request.url.params)))
            content, status, headers = self._respond(request, 200, b"\xff\xd8\xff\xe0jpeg")
            headers.update({"X-Image-Width": "1600", "X-Image-Height": "900", "X-Image-Source": "game"})
            return content, status, headers
        body = json.loads(request.content)
        self.calls.append((body["op"], body["args"], body["request_id"]))
        canned = self.script.get(body["op"])
        if canned:
            entry = canned.pop(0)
            if isinstance(entry, Exception):
                raise entry  # a broken connection, as httpx reports it
            status, payload = entry
            return self._respond(request, status, payload)
        if body["op"] == "snapshot":
            return self._respond(request, 200, {"ok": True, "result": {
                "finance": {"balance": self.balance},
                "game": {"speedup": 0 if self.paused else 1, "paused": self.paused, "game_time": self.game_time}}})
        if body["op"] == "line_delete":
            return self._respond(request, 422, {"ok": False, "error": {"code": "not_found", "message": "no line 9",
                                                                       "retryable": False}})
        if body["args"].get("dry_run"):
            return self._respond(request, 200, {"ok": True, "result": {"dry_run": True, "valid": True,
                                                                       "cost": self.cost}})
        return self._respond(request, 200, {"ok": True, "result": {"accepted": True}})

    def ops(self):
        return [c[0] for c in self.calls]


@pytest.fixture
def wired(plugin, monkeypatch):
    """Bind a fake ctx and route the plugin's HTTP client to FakeRelay."""
    relay = FakeRelay(plugin)
    ctx = FakeCtx()
    plugin.tools.bind(ctx)
    real_client = plugin.client.BridgeClient

    def make_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(relay)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(plugin.tools, "BridgeClient", make_client)
    monkeypatch.setattr(plugin.tools.time, "sleep", lambda s: None)
    monkeypatch.setattr(plugin.tools, "_lookup_conversation_root", lambda sid: sid.split("/", 1)[0])
    plugin.tools._root_cache.clear()
    return plugin, ctx, relay


def run(plugin, name, args, session="s1"):
    out = plugin.tools.handler_for(name)(args, session_id=session)
    return out if isinstance(out, dict) else json.loads(out)


# ---------------------------------------------------------------------------
# client
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", ["http://127.0.0.1:47800", "http://localhost:9", "https://[::1]:1"])
def test_loopback_urls_are_accepted(plugin, url):
    assert plugin.client.validate_bridge_url(url)


@pytest.mark.parametrize("url,allow", [("http://10.0.0.5:47800", True), ("https://example.com", False),
                                        ("http://user:pw@127.0.0.1:1", False), ("ftp://127.0.0.1", False),
                                        ("http://127.0.0.1:1/v1?x=1", False)])
def test_untrusted_bridge_urls_are_refused(plugin, url, allow):
    with pytest.raises(ValueError):
        plugin.client.validate_bridge_url(url, allow_remote=allow)
    assert plugin.client.validate_bridge_url("https://mac.tailnet.example", allow_remote=True)


def test_client_maps_relay_errors(plugin):
    relay = FakeRelay(plugin)
    relay.script["hello"] = [(503, {"ok": False, "error": {"code": "mod_not_connected", "message": "start TF3",
                                                           "retryable": True}})]
    client = plugin.client.BridgeClient("http://127.0.0.1:1", TOKEN, transport=httpx.MockTransport(relay))
    with pytest.raises(plugin.client.BridgeError) as exc:
        client.call("hello")
    assert (exc.value.code, exc.value.retryable, exc.value.status) == ("mod_not_connected", True, 503)


def test_unsigned_or_forged_responses_are_rejected(plugin):
    """Something other than the relay on the port (the tunnel is down and a
    local user grabbed it) can not answer convincingly."""
    relay = FakeRelay(plugin)
    relay.sign = False
    client = plugin.client.BridgeClient("http://127.0.0.1:1", TOKEN, transport=httpx.MockTransport(relay))
    with pytest.raises(plugin.client.BridgeError) as exc:
        client.health()
    assert exc.value.code == "relay_unauthenticated" and exc.value.retryable is False

    def impostor(request):
        return httpx.Response(200, json={"ok": True, "result": {"accepted": True}},
                              headers={"X-TF3-Response-Signature": "0" * 64})
    client = plugin.client.BridgeClient("http://127.0.0.1:1", TOKEN, transport=httpx.MockTransport(impostor))
    with pytest.raises(plugin.client.BridgeError) as exc:
        client.call("vehicle_sell", {"vehicle_id": 1})
    assert exc.value.code == "relay_unauthenticated"


def test_connection_refused_is_retried_then_reported(plugin):
    attempts = []

    def handler(request):
        attempts.append(request.headers["X-TF3-Nonce"])
        raise httpx.ConnectError("refused")
    client = plugin.client.BridgeClient("http://127.0.0.1:1", TOKEN, transport=httpx.MockTransport(handler))
    with pytest.raises(plugin.client.BridgeError) as exc:
        client.call("hello")
    assert exc.value.code == "bridge_unreachable" and exc.value.retryable
    assert len(attempts) == 2 and attempts[0] != attempts[1]  # each attempt is signed afresh


def test_oversized_requests_are_refused_before_sending(wired):
    """The relay would refuse them before authenticating, with an unsigned
    answer that looks like an impostor's."""
    plugin, ctx, relay = wired
    out = run(plugin, "tf3_act", {"action": "line_update", "params": {"line_id": 1, "name": "x" * 300_000},
                                  "reason": "x"})
    assert (out["code"], out["outcome"]) == ("body_too_large", "not_done") and relay.calls == []


def test_missing_token_is_not_configured(plugin):
    with pytest.raises(plugin.client.BridgeError) as exc:
        plugin.client.BridgeClient("http://127.0.0.1:1", "")
    assert exc.value.code == "not_configured"


# ---------------------------------------------------------------------------
# availability gates
# ---------------------------------------------------------------------------

def test_tools_need_the_token(plugin, monkeypatch):
    plugin.tools.bind(FakeCtx())
    assert plugin.tools.check_tf3_available()
    monkeypatch.delenv("TF3_BRIDGE_TOKEN")
    assert not plugin.tools.check_tf3_available()


def test_lua_tool_is_opt_in(plugin):
    plugin.tools.bind(FakeCtx())
    assert not plugin.tools.check_tf3_lua_available()
    plugin.tools.bind(FakeCtx({"enable_lua_tool": True}))
    assert plugin.tools.check_tf3_lua_available()
    plugin.tools.bind(FakeCtx({"enable_lua_tool": "yes"}))  # only a real boolean true opts in
    assert not plugin.tools.check_tf3_lua_available()


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------

def test_observe_returns_snapshot_and_guardrails(wired):
    plugin, ctx, relay = wired
    out = run(plugin, "tf3_observe", {"sections": ["finance", "lines"], "limit": 7})
    assert out["snapshot"]["finance"]["balance"] == 5_000_000
    assert out["guardrails"]["max_actions_per_session"] == 40 and out["guardrails"]["allow_unpause"] is False
    assert relay.calls[-1][:2] == ("snapshot", {"sections": ["finance", "lines"], "limit": 7})
    assert "error" in run(plugin, "tf3_observe", {"sections": ["secrets"]})


def test_observe_variants(wired):
    plugin, ctx, relay = wired
    run(plugin, "tf3_observe", {"entity_id": 5})
    run(plugin, "tf3_observe", {"model_search": "bus"})
    run(plugin, "tf3_observe", {"selftest": True})
    assert relay.ops() == ["get_entity", "list_vehicle_models", "selftest"]


def test_act_dispatches_and_logs(wired):
    plugin, ctx, relay = wired
    out = run(plugin, "tf3_act", {"action": "line_create", "params": {"name": "B", "stops": [1, 2]},
                                  "reason": "connect towns"})
    assert out["result"]["accepted"] and out["actions_used_this_run"] == 1
    assert relay.calls[-1][:2] == ("line_create", {"name": "B", "stops": [1, 2], "dry_run": False})
    log = ctx.state.get("action_log")
    assert log[-1]["action"] == "line_create" and log[-1]["ok"] and log[-1]["reason"] == "connect towns"
    assert log[-1]["request_id"] == out["request_id"]


def test_dry_run_inside_params_is_honoured(wired):
    plugin, ctx, relay = wired
    out = run(plugin, "tf3_act", {"action": "bulldoze", "params": {"entity_id": 4242, "dry_run": True},
                                  "reason": "check the removal first"})
    assert out["dry_run"] is True
    assert relay.calls == [("bulldoze", {"entity_id": 4242, "dry_run": True}, relay.calls[0][2])]
    assert not ctx.state.get("action_log")  # nothing happened, nothing logged


@pytest.mark.parametrize("flag", [True, "true", "True", 1, "yes"])
def test_any_dry_run_spelling_is_a_dry_run(wired, flag):
    plugin, ctx, relay = wired
    out = run(plugin, "tf3_act", {"action": "vehicle_sell", "params": {"vehicle_id": 1, "dry_run": flag},
                                  "reason": "x"})
    assert out["dry_run"] is True and relay.calls[-1][1]["dry_run"] is True
    assert not ctx.state.get("action_log")


def test_explicit_false_dry_run_is_a_real_action(wired):
    plugin, ctx, relay = wired
    out = run(plugin, "tf3_act", {"action": "vehicle_stop", "params": {"vehicle_id": 1, "dry_run": "false"},
                                  "reason": "x"})
    assert out["dry_run"] is False and relay.calls[-1][1]["dry_run"] is False


def test_null_params_are_dropped(wired):
    plugin, ctx, relay = wired
    run(plugin, "tf3_act", {"action": "line_update", "params": {"line_id": 5, "name": None,
                                                                "stops": [{"station_id": 1, "terminal": None}, 2]},
                            "reason": "x"})
    assert relay.calls[-1][1] == {"line_id": 5, "stops": [{"station_id": 1}, 2], "dry_run": False}


def test_act_reports_mod_errors(wired):
    plugin, ctx, relay = wired
    out = run(plugin, "tf3_act", {"action": "line_delete", "params": {"line_id": 9}, "reason": "cleanup"})
    assert out["code"] == "not_found" and out["outcome"] == "not_done"
    assert ctx.state.get("action_log")[-1]["ok"] is False


def test_act_rejects_unknown_actions(wired):
    plugin, ctx, relay = wired
    out = run(plugin, "tf3_act", {"action": "lua_eval", "params": {}, "reason": "x"})
    assert out["code"] == "unknown_action" and relay.calls == []


def test_readonly_and_blocked_actions(wired):
    plugin, ctx, relay = wired
    ctx.settings["readonly"] = True
    assert run(plugin, "tf3_act", {"action": "set_speed", "params": {"speedup": 2}, "reason": "x"})["code"] == "readonly"
    ctx.settings.update(readonly=False, blocked_actions=["bulldoze", "vehicle_sell"])
    assert run(plugin, "tf3_act", {"action": "bulldoze", "params": {"entity_id": 1}, "reason": "x"})["code"] == "blocked_action"
    # Selling on arrival is selling: it is blocked along with vehicle_sell.
    out = run(plugin, "tf3_act", {"action": "vehicle_to_depot", "params": {"vehicle_id": 5, "sell_on_arrival": True},
                                  "reason": "x"})
    assert out["code"] == "blocked_action"
    assert relay.calls == []
    assert "error" not in run(plugin, "tf3_act", {"action": "vehicle_to_depot", "params": {"vehicle_id": 5},
                                                  "reason": "service"})


def test_action_budget_is_per_run_and_persistent(wired):
    plugin, ctx, relay = wired
    ctx.settings["max_actions_per_session"] = 2
    act = {"action": "vehicle_stop", "params": {"vehicle_id": 1}, "reason": "x"}
    assert "error" not in run(plugin, "tf3_act", act, session="run1")
    assert "error" not in run(plugin, "tf3_act", {**act, "dry_run": True}, session="run1")  # dry runs are free
    # A compressed continuation or a delegated subagent shares the run's budget.
    assert "error" not in run(plugin, "tf3_act", act, session="run1/compressed")
    assert run(plugin, "tf3_act", act, session="run1/subagent")["code"] == "action_budget_exhausted"
    plugin.tools.bind(ctx)  # a gateway restart re-binds the plugin...
    assert run(plugin, "tf3_act", act, session="run1")["code"] == "action_budget_exhausted"  # ...budget kept
    assert "error" not in run(plugin, "tf3_act", act, session="run2")  # the next cron run starts fresh


def test_cash_reserve_accounts_for_the_cost(wired):
    plugin, ctx, relay = wired
    ctx.settings["min_cash_reserve"] = 4_950_000
    buy = {"action": "vehicle_buy", "params": {"depot_id": 1, "model": "m"}, "reason": "x"}
    out = run(plugin, "tf3_act", buy)  # 5,000,000 - 60,000 < 4,950,000
    assert out["code"] == "cash_reserve"
    assert relay.ops() == ["vehicle_buy", "snapshot"] and relay.calls[0][1]["dry_run"] is True  # priced, not bought
    relay.calls.clear()
    relay.cost = 10_000
    assert "error" not in run(plugin, "tf3_act", buy)
    relay.cost = None
    assert run(plugin, "tf3_act", buy)["code"] == "cost_unknown"
    assert "error" not in run(plugin, "tf3_act", {"action": "vehicle_stop", "params": {"vehicle_id": 1},
                                                  "reason": "x"})


def test_timeouts_are_retried_with_the_same_request_id(wired):
    plugin, ctx, relay = wired
    timeout = (504, {"ok": False, "error": {"code": "timeout", "message": "slow", "retryable": True}})
    relay.script["vehicle_buy"] = [timeout, timeout]
    out = run(plugin, "tf3_act", {"action": "vehicle_buy", "params": {"depot_id": 1, "model": "m"}, "reason": "x"})
    assert out["result"]["accepted"]
    ids = {c[2] for c in relay.calls if c[0] == "vehicle_buy"}
    assert len([c for c in relay.calls if c[0] == "vehicle_buy"]) == 3 and len(ids) == 1


def test_unknown_outcomes_are_flagged(wired, monkeypatch):
    plugin, ctx, relay = wired
    monkeypatch.setattr(plugin.tools, "_RETRY_WINDOW_S", 0.0)
    relay.script["vehicle_buy"] = [(504, {"ok": False, "error": {"code": "timeout", "message": "slow",
                                                                 "retryable": True}})]
    out = run(plugin, "tf3_act", {"action": "vehicle_buy", "params": {"depot_id": 1, "model": "m"}, "reason": "x"})
    assert out["code"] == "timeout" and out["outcome"] == "unknown" and out["retryable"] is False
    assert "Re-observe" in out["next_step"] and out["request_id"]
    relay.script["vehicle_sell"] = [(422, {"ok": False, "error": {"code": "game_reloaded", "message": "reloaded"}})]
    out = run(plugin, "tf3_act", {"action": "vehicle_sell", "params": {"vehicle_id": 1}, "reason": "x"})
    assert out["outcome"] == "unknown"
    relay.script["vehicle_stop"] = [(422, {"ok": False, "error": {"code": "engine_timeout", "message": "expired",
                                                                  "retryable": True}})]
    out = run(plugin, "tf3_act", {"action": "vehicle_stop", "params": {"vehicle_id": 1}, "reason": "x"})
    assert out["outcome"] == "not_done"  # the mod guarantees an expired command never runs


def test_a_later_refusal_does_not_settle_an_uncertain_attempt(wired):
    """Once an attempt may have reached the game, only a success settles it:
    a refusal of the retry (paused, tunnel down) says nothing about it."""
    plugin, ctx, relay = wired
    timeout = (504, {"ok": False, "error": {"code": "timeout", "message": "slow", "retryable": True}})
    paused = (403, {"ok": False, "error": {"code": "paused", "message": "paused on the Mac"}})
    relay.script["vehicle_buy"] = [timeout, paused]
    out = run(plugin, "tf3_act", {"action": "vehicle_buy", "params": {"depot_id": 1, "model": "m"}, "reason": "x"})
    assert (out["code"], out["outcome"]) == ("paused", "unknown")
    relay.script["vehicle_sell"] = [httpx.ReadError("tunnel dropped"), httpx.ConnectError("refused"),
                                    httpx.ConnectError("refused")]
    out = run(plugin, "tf3_act", {"action": "vehicle_sell", "params": {"vehicle_id": 1}, "reason": "x"})
    assert (out["code"], out["outcome"]) == ("bridge_unreachable", "unknown")
    relay.script["vehicle_stop"] = [paused]  # refused on the only attempt: cleanly not done
    out = run(plugin, "tf3_act", {"action": "vehicle_stop", "params": {"vehicle_id": 1}, "reason": "x"})
    assert out["outcome"] == "not_done"


def test_unauthenticated_answers_leave_the_outcome_unknown(wired):
    """Whoever answered without the relay's signature holds our signed request
    and can still deliver it to the real relay."""
    plugin, ctx, relay = wired
    relay.sign = False
    out = run(plugin, "tf3_act", {"action": "vehicle_stop", "params": {"vehicle_id": 1}, "reason": "x"})
    assert (out["code"], out["outcome"]) == ("relay_unauthenticated", "unknown")
    assert relay.ops().count("vehicle_stop") == 1  # and it is not sent again
    relay.sign = True
    relay.script["vehicle_stop"] = [(401, {"ok": False, "error": {"code": "replayed_request", "message": "used"}})]
    out = run(plugin, "tf3_act", {"action": "vehicle_stop", "params": {"vehicle_id": 1}, "reason": "x"})
    assert out["outcome"] == "unknown"  # someone else used our signature first


def test_gui_side_ops_are_asked_once(wired):
    """The simulation runs an engine op at most once per request id, even across
    a relay restart; a GUI-side op asked again after a restart would run again."""
    plugin, ctx, relay = wired
    timeout = (504, {"ok": False, "error": {"code": "timeout", "message": "slow", "retryable": True}})
    relay.script["set_speed"] = [timeout, timeout]
    out = run(plugin, "tf3_act", {"action": "set_speed", "params": {"speedup": 0}, "reason": "x"})
    assert out["outcome"] == "unknown" and relay.ops().count("set_speed") == 1
    relay.script["lua_eval"] = [timeout, timeout]
    assert run(plugin, "tf3_lua", {"code": "return 1", "reason": "x"})["outcome"] == "unknown"
    assert relay.ops().count("lua_eval") == 1


def test_engine_ops_match_the_mod(plugin):
    from tests.plugins.tf3.harness import MOD_CONTENT
    source = (MOD_CONTENT / "hermes_bridge" / "ops.lua").read_text(encoding="utf-8")
    block = re.search(r"M\.ENGINE_OPS = \{(.*?)\}", source, re.S).group(1)
    assert set(re.findall(r"(\w+) = true", block)) == plugin.tools._ENGINE_OPS


def test_player_pause_is_respected(wired):
    plugin, ctx, relay = wired
    relay.paused = True
    out = run(plugin, "tf3_act", {"action": "set_speed", "params": {"speedup": 1}, "reason": "resume"})
    assert out["code"] == "player_paused" and "set_speed" not in relay.ops()
    # The agent may resume a pause it made itself, while the game has not moved since.
    relay.paused = False
    assert "error" not in run(plugin, "tf3_act", {"action": "set_speed", "params": {"speedup": 0}, "reason": "p"})
    relay.paused = True
    assert "error" not in run(plugin, "tf3_act", {"action": "set_speed", "params": {"speedup": 2}, "reason": "go"})
    ctx.settings["allow_unpause"] = True
    relay.paused, relay.game_time = True, 5000
    assert "error" not in run(plugin, "tf3_act", {"action": "set_speed", "params": {"speedup": 1}, "reason": "go"})


@pytest.mark.parametrize("speedup", ["1.0", "1e0", "0x1", " 2 ", 1.5, "3", True, 10**400, float("inf")])
def test_no_spelling_of_resume_slips_past_the_pause_guard(wired, speedup):
    plugin, ctx, relay = wired
    relay.paused = True
    out = run(plugin, "tf3_act", {"action": "set_speed", "params": {"speedup": speedup}, "reason": "go"})
    assert out["code"] in {"player_paused", "invalid_argument"} and "set_speed" not in relay.ops()


def test_speedup_reaches_the_mod_as_a_plain_integer(wired):
    plugin, ctx, relay = wired
    assert "error" not in run(plugin, "tf3_act", {"action": "set_speed", "params": {"speedup": " 3 "}, "reason": "x"})
    assert relay.calls[-1][1]["speedup"] == 3
    assert "error" not in run(plugin, "tf3_act", {"action": "set_speed", "params": {"speedup": 9}, "reason": "x"})
    assert relay.calls[-1][1]["speedup"] == 4


def test_unknown_pause_state_counts_as_the_players(wired):
    plugin, ctx, relay = wired
    relay.game_time = None  # the bridge can not read the game clock
    assert "error" not in run(plugin, "tf3_act", {"action": "set_speed", "params": {"speedup": 0}, "reason": "p"})
    relay.paused = True  # paused again later, by the agent or the player: no way to tell
    out = run(plugin, "tf3_act", {"action": "set_speed", "params": {"speedup": 2}, "reason": "go"})
    assert out["code"] == "player_paused"
    relay.paused, relay.game_time = None, 1000  # the bridge can not tell whether it is paused
    out = run(plugin, "tf3_act", {"action": "set_speed", "params": {"speedup": 2}, "reason": "go"})
    assert out["code"] == "player_paused" and "may be paused" in out["error"]
    assert relay.ops().count("set_speed") == 1


def test_screenshot_is_multimodal(wired):
    plugin, ctx, relay = wired
    out = run(plugin, "tf3_screenshot", {"max_width": 800, "focus_entity_id": 42})
    assert out["_multimodal"] is True
    url = out["content"][1]["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,")
    assert base64.b64decode(url.split(",", 1)[1]).startswith(b"\xff\xd8")
    assert out["meta"]["source"] == "game" and "text_summary" in out
    assert relay.calls[0][:2] == ("camera_focus", {"entity_id": 42})
    assert relay.calls[1] == ("screenshot", {"max_width": "800", "quality": "70"})


def test_lua_tool_counts_against_the_budget_and_flags_timeouts(wired, monkeypatch):
    plugin, ctx, relay = wired
    ctx.settings["max_actions_per_session"] = 2
    assert "error" not in run(plugin, "tf3_lua", {"code": "return 1", "reason": "x"})
    monkeypatch.setattr(plugin.tools, "_RETRY_WINDOW_S", 0.0)
    relay.script["lua_eval"] = [(504, {"ok": False, "error": {"code": "timeout", "message": "slow"}})]
    out = run(plugin, "tf3_lua", {"code": "return 2", "reason": "x"})
    assert out["outcome"] == "unknown" and ctx.state.get("action_log")[-1]["ok"] is False
    assert run(plugin, "tf3_lua", {"code": "return 3", "reason": "x"})["code"] == "action_budget_exhausted"


def test_journal_persists_and_caps(wired):
    plugin, ctx, relay = wired
    assert run(plugin, "tf3_journal", {"action": "set_plan", "text": "Grow bus network."})["ok"]
    for i in range(305):
        run(plugin, "tf3_journal", {"action": "append", "text": f"turn {i}"})
    assert run(plugin, "tf3_journal", {"action": "append", "text": "y" * 5000})["truncated"] is True
    out = run(plugin, "tf3_journal", {"action": "read", "limit": 3})
    assert out["plan"] == "Grow bus network." and out["total_entries"] == 300
    assert [e["text"][:4] for e in out["entries"]] == ["turn", "turn", "yyyy"]
    assert "error" in run(plugin, "tf3_journal", {"action": "append"})


def test_unreachable_bridge_is_a_clear_error(plugin, monkeypatch):
    plugin.tools.bind(FakeCtx({"bridge_url": "http://127.0.0.1:9"}))
    real_client = plugin.client.BridgeClient

    def refusing(*args, **kwargs):
        def handler(request):
            raise httpx.ConnectError("refused")
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)
    monkeypatch.setattr(plugin.tools, "BridgeClient", refusing)
    out = run(plugin, "tf3_observe", {})
    assert out["code"] == "bridge_unreachable" and "SSH reverse tunnel" in out["error"]


def test_action_classes_match_the_relay_allowlist(plugin):
    from tests.plugins.tf3.harness import load_relay
    relay = load_relay()
    plugin_actions = set(plugin.guardrails.ACTION_CLASSES)
    assert plugin_actions <= relay.ACTION_OPS | relay.SAVE_OPS
    assert plugin_actions == set(plugin.tools.TF3_ACT_SCHEMA["parameters"]["properties"]["action"]["enum"])


@pytest.mark.parametrize("value,expected", [("15m", "every 15m"), ("every 1h", "every 1h"), ("2h", "every 2h"),
                                            ("0 9 * * *", "0 9 * * *")])
def test_autopilot_schedule_is_always_recurring(plugin, value, expected):
    assert plugin.cli.recurring_schedule(value) == expected


def test_autopilot_rejects_one_shot_schedules(plugin):
    with pytest.raises(ValueError):
        plugin.cli.recurring_schedule("2026-11-01T09:00")


# ---------------------------------------------------------------------------
# discovery through the real PluginManager
# ---------------------------------------------------------------------------

def test_plugin_loads_through_plugin_manager(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "plugins").mkdir(parents=True)
    (home / "plugins" / "tf3").symlink_to(PLUGIN_DIR, target_is_directory=True)
    (home / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": ["tf3"]}}))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("TF3_BRIDGE_TOKEN", TOKEN)

    from hermes_cli import plugins as pmod
    from tools.registry import registry

    mgr = pmod.PluginManager()
    mgr.discover_and_load()
    loaded = mgr._plugins["tf3"]
    assert loaded.enabled, loaded.error
    assert set(loaded.tools_registered) == {"tf3_observe", "tf3_screenshot", "tf3_act", "tf3_lua", "tf3_journal"}
    assert registry.get_toolset_for_tool("tf3_act") == "tf3"
    assert "tf3" in mgr._cli_commands
    assert "tf3:autopilot" in mgr._plugin_skills


def test_autopilot_skill_meets_authoring_standards():
    text = (PLUGIN_DIR / "skills" / "autopilot" / "SKILL.md").read_text()
    front = yaml.safe_load(text.split("---", 2)[1])
    assert len(front["description"]) <= 60 and front["description"].endswith(".")
    for section in ["## When to Use", "## Prerequisites", "## How to Run", "## Quick Reference",
                    "## Procedure", "## Pitfalls", "## Verification"]:
        assert section in text
