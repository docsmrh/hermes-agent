"""The VPS-side Hermes plugin: client, guardrails, tool handlers, discovery."""

from __future__ import annotations

import base64
import json
from pathlib import Path

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
    """httpx transport that answers like tf3_relay.py and records calls."""

    def __init__(self, balance=5_000_000):
        self.calls = []
        self.balance = balance

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        if request.url.path == "/v1/health":
            return httpx.Response(200, json={"ok": True, "mod": {"connected": True}, "policy": {}})
        if request.url.path == "/v1/screenshot":
            self.calls.append(("screenshot", dict(request.url.params)))
            return httpx.Response(200, content=b"\xff\xd8\xff\xe0jpeg",
                                  headers={"Content-Type": "image/jpeg", "X-Image-Width": "1600",
                                           "X-Image-Height": "900", "X-Image-Source": "game"})
        body = json.loads(request.content)
        self.calls.append((body["op"], body["args"]))
        assert len(body["request_id"]) == 32
        if body["op"] == "snapshot":
            return httpx.Response(200, json={"ok": True, "result": {"finance": {"balance": self.balance},
                                                                    "game": {"speedup": 1}}})
        if body["op"] == "line_delete":
            return httpx.Response(422, json={"ok": False, "error": {"code": "not_found", "message": "no line 9",
                                                                    "retryable": False}})
        return httpx.Response(200, json={"ok": True, "result": {"accepted": True}})


@pytest.fixture
def wired(plugin, monkeypatch):
    """Bind a fake ctx and route the plugin's HTTP client to FakeRelay."""
    relay = FakeRelay()
    ctx = FakeCtx()
    plugin.tools.bind(ctx)
    plugin.tools._budget = plugin.guardrails.ActionBudget()
    real_client = plugin.client.BridgeClient

    def make_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(relay)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(plugin.tools, "BridgeClient", make_client)
    monkeypatch.setattr(plugin.tools.time, "sleep", lambda s: None)
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
                                        ("http://user:pw@127.0.0.1:1", False), ("ftp://127.0.0.1", False)])
def test_token_never_goes_to_untrusted_hosts(plugin, url, allow):
    with pytest.raises(ValueError):
        plugin.client.validate_bridge_url(url, allow_remote=allow)
    assert plugin.client.validate_bridge_url("https://mac.tailnet.example", allow_remote=True)


def test_client_maps_relay_errors(plugin):
    def handler(request):
        return httpx.Response(503, json={"ok": False, "error": {"code": "mod_not_connected", "message": "start TF3",
                                                                "retryable": True}})
    client = plugin.client.BridgeClient("http://127.0.0.1:1", TOKEN, transport=httpx.MockTransport(handler))
    with pytest.raises(plugin.client.BridgeError) as exc:
        client.call("hello")
    assert (exc.value.code, exc.value.retryable, exc.value.status) == ("mod_not_connected", True, 503)

    client = plugin.client.BridgeClient("http://127.0.0.1:1", TOKEN,
                                        transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    with pytest.raises(plugin.client.BridgeError) as exc:
        client.health()
    assert exc.value.code == "unauthorized"


def test_connection_refused_is_retried_then_reported(plugin):
    attempts = []

    def handler(request):
        attempts.append(1)
        raise httpx.ConnectError("refused")
    client = plugin.client.BridgeClient("http://127.0.0.1:1", TOKEN, transport=httpx.MockTransport(handler))
    with pytest.raises(plugin.client.BridgeError) as exc:
        client.call("hello")
    assert exc.value.code == "bridge_unreachable" and exc.value.retryable and len(attempts) == 2


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
    assert out["guardrails"]["max_actions_per_session"] == 40
    assert relay.calls[-1] == ("snapshot", {"sections": ["finance", "lines"], "limit": 7})
    assert "error" in run(plugin, "tf3_observe", {"sections": ["secrets"]})


def test_observe_variants(wired):
    plugin, ctx, relay = wired
    run(plugin, "tf3_observe", {"entity_id": 5})
    run(plugin, "tf3_observe", {"model_search": "bus"})
    run(plugin, "tf3_observe", {"selftest": True})
    assert [c[0] for c in relay.calls] == ["get_entity", "list_vehicle_models", "selftest"]


def test_act_dispatches_and_logs(wired):
    plugin, ctx, relay = wired
    out = run(plugin, "tf3_act", {"action": "line_create", "params": {"name": "B", "stops": [1, 2]},
                                  "reason": "connect towns"})
    assert out["result"]["accepted"] and out["actions_used_this_session"] == 1
    assert relay.calls[-1] == ("line_create", {"name": "B", "stops": [1, 2], "dry_run": False})
    log = ctx.state.get("action_log")
    assert log[-1]["action"] == "line_create" and log[-1]["ok"] and log[-1]["reason"] == "connect towns"


def test_act_reports_mod_errors(wired):
    plugin, ctx, relay = wired
    out = run(plugin, "tf3_act", {"action": "line_delete", "params": {"line_id": 9}, "reason": "cleanup"})
    assert out["code"] == "not_found" and out["retryable"] is False
    assert ctx.state.get("action_log")[-1]["ok"] is False


def test_act_rejects_unknown_actions(wired):
    plugin, ctx, relay = wired
    out = run(plugin, "tf3_act", {"action": "lua_eval", "params": {}, "reason": "x"})
    assert out["code"] == "unknown_action" and relay.calls == []


def test_readonly_and_blocked_actions(wired):
    plugin, ctx, relay = wired
    ctx.settings["readonly"] = True
    assert run(plugin, "tf3_act", {"action": "set_speed", "params": {"speedup": 2}, "reason": "x"})["code"] == "readonly"
    ctx.settings.update(readonly=False, blocked_actions=["bulldoze"])
    assert run(plugin, "tf3_act", {"action": "bulldoze", "params": {"entity_id": 1}, "reason": "x"})["code"] == "blocked_action"
    assert relay.calls == []


def test_action_budget_per_session(wired):
    plugin, ctx, relay = wired
    ctx.settings["max_actions_per_session"] = 2
    act = {"action": "vehicle_stop", "params": {"vehicle_id": 1}, "reason": "x"}
    assert "error" not in run(plugin, "tf3_act", act)
    assert "error" not in run(plugin, "tf3_act", {**act, "dry_run": True})  # dry runs are free
    assert "error" not in run(plugin, "tf3_act", act)
    assert run(plugin, "tf3_act", act)["code"] == "action_budget_exhausted"
    assert "error" not in run(plugin, "tf3_act", act, session="next-cron-run")


def test_cash_reserve_blocks_spending_only(wired):
    plugin, ctx, relay = wired
    ctx.settings["min_cash_reserve"] = 10_000_000
    out = run(plugin, "tf3_act", {"action": "vehicle_buy", "params": {"depot_id": 1, "model": "m"}, "reason": "x"})
    assert out["code"] == "cash_reserve"
    assert [c[0] for c in relay.calls] == ["snapshot"]  # checked the balance, bought nothing
    assert "error" not in run(plugin, "tf3_act", {"action": "vehicle_stop", "params": {"vehicle_id": 1}, "reason": "x"})


def test_screenshot_is_multimodal(wired):
    plugin, ctx, relay = wired
    out = run(plugin, "tf3_screenshot", {"max_width": 800, "focus_entity_id": 42})
    assert out["_multimodal"] is True
    url = out["content"][1]["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,")
    assert base64.b64decode(url.split(",", 1)[1]).startswith(b"\xff\xd8")
    assert out["meta"]["source"] == "game" and "text_summary" in out
    assert relay.calls[0] == ("camera_focus", {"entity_id": 42})
    assert relay.calls[1] == ("screenshot", {"max_width": "800", "quality": "70"})


def test_lua_tool_counts_against_the_budget(wired):
    plugin, ctx, relay = wired
    ctx.settings["max_actions_per_session"] = 1
    assert "error" not in run(plugin, "tf3_lua", {"code": "return 1", "reason": "x"})
    assert run(plugin, "tf3_lua", {"code": "return 1", "reason": "x"})["code"] == "action_budget_exhausted"


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
