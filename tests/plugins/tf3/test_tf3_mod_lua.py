"""The in-game Hermes Bridge mod, run under a real Lua 5.2 interpreter.

The mod's Lua is executed unmodified against fake_game.lua (a stand-in for
the TF3 script runtime: entity components, api.cmd semantics, game-script
state, and app.*Userdata backed by real files). Requests go through the real
relay ModChannel, so the userdata file protocol is exercised on both ends.

Needs the optional ``lupa`` package; skipped without it.
"""

from __future__ import annotations

import json
import time
import uuid

import pytest

pytest.importorskip("lupa.lua52")

from tests.plugins.tf3.harness import MOD_CONTENT, MOD_DIR, FakeGame, load_relay  # noqa: E402

R = load_relay()


@pytest.fixture
def game(tmp_path):
    g = FakeGame(tmp_path / "userdata")
    g.run(3)
    yield g
    g.stop()


@pytest.fixture
def channel(game):
    ch = R.ModChannel(game.userdata / "heightmaps", poll_s=0.01)
    ch.poll_once()
    ch.start()
    game.start()
    yield ch
    ch.stop()


def call(ch, op, args=None, request_id=None, timeout=10):
    """Run an op through the channel, raising mod errors the way the relay does."""
    res = ch.call(op, args or {}, request_id or uuid.uuid4().hex, timeout)
    if not res["ok"]:
        err = res.get("error") or {}
        raise R.RelayError(422, err.get("code") or "op_failed", err.get("message") or "", retryable=bool(err.get("retryable")))
    return res


# ---------------------------------------------------------------------------
# JSON codec
# ---------------------------------------------------------------------------

def test_json_codec_round_trips(game):
    j = game.rt.execute((MOD_CONTENT / "hermes_bridge" / "json.lua").read_text())
    sample = {"a": 1, "b": [1, 2.5, "x"], "c": "é\n\"q\"", "d": None, "e": True, "f": [], "g": {"h": []}}
    text = json.dumps(sample, ensure_ascii=False)
    assert json.loads(j.encode(j.decode(text))) == sample
    for bad in ['{"a":}', "[1,]", '"open', '{"a":1} trailing']:
        with pytest.raises(Exception):
            j.decode(bad)


# ---------------------------------------------------------------------------
# Mod lifecycle and file protocol
# ---------------------------------------------------------------------------

def test_mod_announces_itself_in_heightmaps_only(game):
    names = sorted(p.name for p in (game.userdata / "heightmaps").iterdir())
    assert names[0].startswith("hermes_bridge__in_") and names[1].startswith("hermes_bridge__out_")
    assert not (game.userdata / "mod_presets").exists()  # never pollutes the preset window


def test_reevaluated_script_keeps_its_stamp(game):
    before = {p.name for p in (game.userdata / "heightmaps").glob("hermes_bridge__in_*")}
    game.load_script()  # the game may evaluate the script file again at any time
    game.run(3)
    after = {p.name for p in (game.userdata / "heightmaps").glob("hermes_bridge__in_*")}
    assert before == after


def test_hello_and_snapshot(channel, game):
    assert call(channel, "hello")["result"]["mod_version"]
    snap = call(channel, "snapshot", {"sections": ["game", "finance", "lines", "vehicles", "stations", "depots",
                                                    "towns", "industries", "problems"]})["result"]
    assert snap["game"]["speedup"] == 1 and snap["game"]["date"]["year"] == 1905
    assert snap["finance"] == {"balance": 5000000, "loan": 1000000, "earnings": 123456}
    line = snap["lines"]["items"][0]
    assert line["name"] == "Bus 1" and line["vehicle_count"] == 2
    assert [s["station_group_id"] for s in line["stops"]] == [300, 301]  # read through a native vector
    assert line["load"]["ratio"] == 0.75  # read from a per-cargo map
    assert line["transport_modes"] == ["BUS"]  # enum value mapped back to its name
    vehicle = next(v for v in snap["vehicles"]["items"] if v["id"] == 500)
    assert (vehicle["state"], vehicle["carrier"]) == ("EN_ROUTE", "ROAD")
    station = next(s for s in snap["stations"]["items"] if s["id"] == 200)
    assert station["index_in_group"] == 0 and station["position"] == [100, 200, 5]
    assert snap["depots"]["items"][0]["id"] == 400
    assert snap["problems"]["no_path_vehicle_ids"] == [501]
    assert snap["industries"]["items"][0]["type"] == "::/industry/coal_mine.con"
    assert game.G.fatal_reads == 0


def test_unknown_entity_is_never_read(channel, game):
    res = call(channel, "get_entity", {"id": 424242})["result"]
    assert res == {"id": 424242, "exists": False}
    with pytest.raises(R.RelayError) as exc:
        call(channel, "vehicle_sell", {"vehicle_id": 424242})
    assert exc.value.code == "not_found"
    assert game.G.fatal_reads == 0  # would have crashed the real game


def test_vehicle_models_report_availability(channel):
    res = call(channel, "list_vehicle_models", {"contains": "vehicle/"})["result"]
    by_file = {m["file"]: m for m in res["models"]}
    assert by_file["vehicle/bus/old_bus.mdl"]["available"] is True and res["year"] == 1905
    assert by_file["vehicle/tram/old_tram.mdl"]["available"] is False  # not invented until 1950
    only = call(channel, "list_vehicle_models", {"available_only": True})["result"]["models"]
    assert [m["file"] for m in only] == ["vehicle/bus/old_bus.mdl"]


def test_selftest_reports_api_surface(channel):
    res = call(channel, "selftest")["result"]
    assert "cmd.makeLineCreateCmd" in res["available"]
    assert res["line_stop_index_base"] == 0


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def test_set_speed_runs_in_the_gui_so_it_can_unpause(channel, game):
    assert call(channel, "set_speed", {"speedup": 0})["result"]["speedup"] == 0
    with game.lock:
        assert game.G.components[2].GAME_SPEED.speedup == 0
    assert "event" not in game.log_kinds()
    assert game.find_logged("speed").speedup == 0


def test_line_create_goes_through_the_simulation(channel, game):
    res = call(channel, "line_create", {"name": "Bus 2", "stops": [200, {"station_id": 201, "terminal": 1}],
                                        "color": [1, 0, 0]})["result"]
    assert res["accepted"] and res["line_id"] > 1000
    kinds = game.log_kinds()
    assert kinds.index("event") < kinds.index("line_create")
    created = game.find_logged("line_create")
    assert created.name == "Bus 2"
    stops = created.line.stops
    assert (stops[1].stationGroup, stops[1].station, stops[2].stationGroup, stops[2].terminal) == (300, 0, 301, 1)
    snap = call(channel, "snapshot", {"sections": ["lines"]})["result"]
    assert "Bus 2" in [item["name"] for item in snap["lines"]["items"]]


def test_vehicle_buy_and_assign(channel, game):
    res = call(channel, "vehicle_buy", {"depot_id": 400, "model": "vehicle/bus/old_bus.mdl", "line_id": 100})["result"]
    assert res["vehicle_id"] and res["line_assigned"] is True
    buy = game.find_logged("vehicle_buy")
    assert buy.depot == 400 and buy.config.vehicles[1].part.modelId == 77
    assert game.find_logged("vehicle_set_line").line == 100
    with pytest.raises(R.RelayError) as exc:
        call(channel, "vehicle_buy", {"depot_id": 400, "model": "vehicle/bus/nope.mdl"})
    assert exc.value.code == "not_found"


def test_dry_run_executes_nothing(channel, game):
    before = len(game.log_kinds())
    res = call(channel, "build_construction", {"file": "::/depots/road/road_depot/road_depot.con",
                                               "position": [5, 6, 0], "dry_run": True})["result"]
    assert res["dry_run"] and res["valid"] and res["costs"] == 75000
    assert call(channel, "line_delete", {"line_id": 100, "dry_run": True})["result"]["dry_run"]
    assert len(game.log_kinds()) == before


def test_build_and_bulldoze(channel, game):
    res = call(channel, "build_construction", {"file": "::/depots/road/road_depot/road_depot.con",
                                               "position": [5, 6, 0], "rotation": 1.5708,
                                               "params": {"modules": {}}})["result"]
    assert res["accepted"] and res["entity_ids"] and res["depot_ids"]
    # The new depot is usable straight away, by depot id or by construction id.
    for depot_ref in (res["depot_ids"][0], res["entity_ids"][0]):
        bought = call(channel, "vehicle_buy", {"depot_id": depot_ref, "model": "vehicle/bus/old_bus.mdl"})["result"]
        assert bought["vehicle_id"]
        assert game.find_logged("vehicle_buy").depot == res["depot_ids"][0]
    proposal = game.find_logged("proposal")
    ce = proposal.proposal.constructionsToAdd[1]
    assert ce.fileName.endswith("road_depot.con") and ce.params.year == 1905 and ce.params.seed == 0
    assert proposal.playerInitiated is True
    assert call(channel, "bulldoze", {"entity_id": 210})["result"]["accepted"]
    with pytest.raises(R.RelayError) as exc:
        call(channel, "bulldoze", {"entity_id": 100})  # a line, not a construction
    assert exc.value.code == "invalid_argument"


def test_rejected_command_reports_failure(channel, game):
    with game.lock:
        game.G.reject.line_destroy = True
    with pytest.raises(R.RelayError) as exc:
        call(channel, "line_delete", {"line_id": 100})
    assert exc.value.code == "command_failed"


def test_engine_executes_each_request_once_across_relay_restarts(channel, game):
    rid = "exactly-once-1"
    call(channel, "vehicle_stop", {"vehicle_id": 500}, request_id=rid)
    channel.stop()
    restarted = R.ModChannel(game.userdata / "heightmaps", poll_s=0.01)  # new session, same request id
    restarted.poll_once()
    restarted.start()
    try:
        assert restarted.call("vehicle_stop", {"vehicle_id": 500}, rid, 10)["ok"]
    finally:
        restarted.stop()
    assert game.log_kinds().count("vehicle_stopped") == 1


def test_lua_eval_and_save(channel, game):
    res = call(channel, "lua_eval", {"code": "print('hi', 2) return { year = api.engine.util.getYear() }"})["result"]
    assert res == {"value": {"year": 1905}, "printed": ["hi\t2"]}
    with pytest.raises(R.RelayError) as exc:
        call(channel, "lua_eval", {"code": "return ("})
    assert exc.value.code == "lua_syntax"
    assert call(channel, "save_game", {"name": "hermes-autosave"})["result"]["saved"] == "hermes-autosave"
    with pytest.raises(R.RelayError):
        call(channel, "save_game", {"name": "../../evil"})


def test_engine_ops_are_refused_while_paused(channel, game):
    call(channel, "set_speed", {"speedup": 0})
    with pytest.raises(R.RelayError) as exc:
        call(channel, "vehicle_stop", {"vehicle_id": 500})
    assert exc.value.code == "game_paused"
    assert "vehicle_stopped" not in game.log_kinds()  # nothing queued to run on resume
    call(channel, "set_speed", {"speedup": 1})
    assert call(channel, "vehicle_stop", {"vehicle_id": 500})["result"]["accepted"]


def test_paused_simulation_times_out_engine_ops(tmp_path):
    g = FakeGame(tmp_path / "userdata")
    ch = R.ModChannel(g.userdata / "heightmaps", poll_s=0.01)
    g.run(3)
    ch.poll_once()
    ch.start()
    stop = False

    def frames_only():
        while not stop:
            g.frame()  # GUI frames keep coming, but the simulation never steps
            time.sleep(0.004)

    import threading
    t = threading.Thread(target=frames_only, daemon=True)
    t.start()
    try:
        rid = uuid.uuid4().hex
        with pytest.raises(R.RelayError) as exc:
            call(ch, "line_delete", {"line_id": 100}, request_id=rid, timeout=1)
        assert exc.value.code == "timeout"  # relay gave up waiting; request stays queued
        g.advance_time(120)
        with pytest.raises(R.RelayError) as exc:
            call(ch, "line_delete", {"line_id": 100}, request_id=rid, timeout=5)
        assert exc.value.code == "engine_timeout"
    finally:
        stop = True
        t.join()
        ch.stop()


def test_gui_errors_are_contained(game):
    game.rt.execute("app.loadUserdata = function() error('boom') end")
    game.run(50)  # must not raise into the game
    assert game.debug_log == [] or all("hermes_bridge" in line for line in game.debug_log)


def test_mod_package_is_consistent():
    mod = json.loads((MOD_DIR / "mod.json").read_text())
    assert mod["modId"] == "hermes_bridge" and isinstance(mod["revision"], int)
    info = json.loads((MOD_DIR / "_metadata" / "modinfo.json").read_text())
    assert len(info["name"]) <= 32 and len(info["summary"]) <= 100
    listed = set(json.loads((MOD_DIR / "_content.json").read_text())["files"])
    on_disk = {str(p.relative_to(MOD_CONTENT)) for p in MOD_CONTENT.rglob("*.lua")}
    assert listed == on_disk
    # TF3's mod guidelines forbid executables/scripts inside a mod folder.
    assert not [p for p in MOD_DIR.rglob("*") if p.suffix in {".py", ".sh", ".command", ".exe", ".bat"}]
