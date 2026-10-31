"""tf3 tool schemas and handlers.

Every handler returns a JSON string (``tool_result`` / ``tool_error``) except
``tf3_screenshot``, which returns the registry's ``_multimodal`` envelope so
vision-capable models receive the frame as an image part.
"""

from __future__ import annotations

import base64
import logging
import threading
import time
import uuid
from typing import Any, Callable, Dict, Optional, Tuple

from tools.registry import tool_error, tool_result

from .client import DEFAULT_BRIDGE_URL, BridgeClient, BridgeError
from .guardrails import (
    ACTION_CLASSES,
    ActionBudget,
    GuardrailError,
    Settings,
    check_action,
    check_cash_reserve,
    effective_action,
)

logger = logging.getLogger(__name__)

TOKEN_ENV = "TF3_BRIDGE_TOKEN"
SNAPSHOT_SECTIONS = ("game", "finance", "lines", "vehicles", "stations", "depots", "towns", "industries", "problems")
DEFAULT_SECTIONS = ["game", "finance", "lines", "problems"]
# Errors after which a state change may still take effect in the game.
_OUTCOME_UNKNOWN = frozenset({"timeout", "transport_error", "game_reloaded"})
# Errors worth re-asking about with the same request id (the relay keeps the
# request and hands back its late result; it is never queued twice).
_RETRY_SAME_ID = frozenset({"timeout", "transport_error"})
_RETRY_WINDOW_S = 110.0
_JOURNAL_MAX_ENTRIES = 300
_JOURNAL_MAX_TEXT = 1500
_PLAN_MAX_TEXT = 4000
_ACTION_LOG_MAX = 200

# The PluginContext is bound once in register(); handlers read settings and
# durable state through it so config edits apply without a restart.
_ctx: Any = None
_budget = ActionBudget()


def bind(ctx: Any) -> None:
    global _ctx, _budget
    _ctx = ctx
    _budget = ActionBudget(get=_state_get, set=_state_set)


def _setting(key: str, default: Any = None) -> Any:
    if _ctx is None:
        return default
    try:
        value = _ctx.get_config(key, default)
    except Exception:  # malformed config must not crash a tool call
        logger.debug("tf3: reading setting %s failed", key, exc_info=True)
        return default
    return default if value is None else value


def _settings() -> Settings:
    return Settings.from_mapping({
        "readonly": _setting("readonly"),
        "max_actions_per_session": _setting("max_actions_per_session"),
        "min_cash_reserve": _setting("min_cash_reserve"),
        "blocked_actions": _setting("blocked_actions"),
        "allow_unpause": _setting("allow_unpause"),
    })


def _token() -> str:
    try:
        from agent.secret_scope import get_secret

        return (get_secret(TOKEN_ENV) or "").strip()
    except Exception:  # UnscopedSecretError under multiplexing, import errors in tests
        return ""


def _client() -> BridgeClient:
    return BridgeClient(
        str(_setting("bridge_url", DEFAULT_BRIDGE_URL)),
        _token(),
        timeout_s=float(_setting("request_timeout_s", 30)),
        allow_remote=bool(_setting("allow_remote_bridge", False) is True),
    )


def check_tf3_available() -> bool:
    """Tools are offered only once the bridge token is configured.

    Deliberately network-free: check_fn results are cached process-wide and
    run during schema assembly, so reachability is reported by the handlers
    (with an actionable message) instead of hiding the tools.
    """
    return bool(_token())


def check_tf3_lua_available() -> bool:
    return check_tf3_available() and _setting("enable_lua_tool", False) is True


def _state_get(key: str, default: Any) -> Any:
    if _ctx is None:
        return default
    try:
        return _ctx.state.get(key, default)
    except Exception:
        logger.warning("tf3: reading plugin state %s failed", key, exc_info=True)
        return default


def _state_set(key: str, value: Any) -> None:
    if _ctx is None:
        return
    _ctx.state.set(key, value)


# ---------------------------------------------------------------------------
# Run identity (what the action budget counts against)
# ---------------------------------------------------------------------------

_root_lock = threading.Lock()
_root_cache: Dict[str, Tuple[str, float]] = {}
_ROOT_RECHECK_S = 60.0


def _lookup_conversation_root(session_id: str) -> str:
    try:
        from hermes_state import SessionDB

        db = SessionDB()
        try:
            return str(db.get_conversation_root(session_id) or session_id)
        finally:
            db.close()
    except Exception:
        logger.debug("tf3: session lineage lookup failed", exc_info=True)
        return session_id


def _run_key(kwargs: dict) -> str:
    """The session-lineage root: context compression rotates session_id and
    delegate subagents get their own, but all of them share one root, so a
    run cannot reset its action budget by compressing or delegating."""
    sid = str(kwargs.get("session_id") or "")
    if not sid:
        return str(kwargs.get("task_id") or "default")
    now = time.monotonic()
    with _root_lock:
        cached = _root_cache.get(sid)
        # A root equal to the id itself may just mean the parent row is not
        # written yet, so that answer is re-checked after a while.
        if cached and (cached[0] != sid or now - cached[1] < _ROOT_RECHECK_S):
            return cached[0]
    root = _lookup_conversation_root(sid)
    with _root_lock:
        if len(_root_cache) > 1024:
            _root_cache.clear()
        _root_cache[sid] = (root, now)
    return root


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _bridge_error(exc: BridgeError) -> str:
    return tool_error(exc.message, code=exc.code, retryable=exc.retryable)


def _guarded(fn: Callable[..., Any]) -> Callable[..., Any]:
    def wrapper(args: dict, **kwargs: Any) -> Any:
        try:
            return fn(args if isinstance(args, dict) else {}, **kwargs)
        except GuardrailError as exc:
            return tool_error(exc.message, code=exc.code, retryable=False)
        except BridgeError as exc:
            return _bridge_error(exc)
        except ValueError as exc:
            return tool_error(str(exc), code="invalid_argument")
        except Exception as exc:
            logger.exception("tf3 tool failed")
            return tool_error(f"tf3 tool failed: {type(exc).__name__}: {exc}")

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


def _int_arg(args: dict, key: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(args.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(lo, min(hi, value))


def _drop_nulls(value: Any) -> Any:
    """Models often send null for "not given"; treat it as absent everywhere."""
    if isinstance(value, dict):
        return {k: _drop_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_drop_nulls(v) for v in value]
    return value


def _call_state_change(client: BridgeClient, op: str, args: dict) -> Tuple[str, dict]:
    """Run a state-changing op; after a timeout ask again with the *same*
    request id, which returns the late result instead of queueing it twice.
    Returns (request_id, result); errors carry ``request_id``."""
    request_id = uuid.uuid4().hex
    started = time.monotonic()
    while True:
        try:
            return request_id, client.call(op, args, request_id=request_id)
        except BridgeError as exc:
            exc.request_id = request_id  # type: ignore[attr-defined]
            if exc.code in _RETRY_SAME_ID and time.monotonic() - started < _RETRY_WINDOW_S:
                if exc.code == "transport_error":
                    time.sleep(1.0)  # a timeout already waited; a broken connection should not spin
                continue
            raise


def _record_action(entry: dict) -> None:
    try:
        log = list(_state_get("action_log", []))[-(_ACTION_LOG_MAX - 1):]
        log.append(entry)
        _state_set("action_log", log)
    except Exception:
        logger.warning("tf3: could not persist action log entry", exc_info=True)


def _failure_result(exc: BridgeError, *, request_id: str) -> str:
    if exc.code in _OUTCOME_UNKNOWN:
        return tool_error(exc.message, code=exc.code, retryable=False, outcome="unknown", request_id=request_id,
                          next_step="Re-observe and check whether the change happened before trying again.")
    return tool_error(exc.message, code=exc.code, retryable=exc.retryable, outcome="not_done",
                      request_id=request_id)


# ---------------------------------------------------------------------------
# tf3_observe
# ---------------------------------------------------------------------------

TF3_OBSERVE_SCHEMA = {
    "name": "tf3_observe",
    "description": (
        "Read the live Transport Fever 3 game state from the user's Mac through the hermes_bridge mod. "
        "Returns structured data (entity ids, names, money, dates, line/vehicle/station stats) that "
        "tf3_act consumes. Call this before acting and again after acting to verify the outcome. "
        "Sections: game (date, speed, paused), finance (balance, loan, earnings), lines (stops, vehicles, "
        "load, last-year balance), vehicles, stations, depots, towns, industries, problems (lines/vehicles "
        "the game flags). Pass model_search to list buyable vehicle models instead."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "sections": {
                "type": "array",
                "items": {"type": "string", "enum": list(SNAPSHOT_SECTIONS)},
                "description": f"Which sections to return. Default: {DEFAULT_SECTIONS}.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 200,
                "description": "Max entries per list section (default 50).",
            },
            "entity_id": {
                "type": "integer",
                "description": "Optional: return details for one entity id instead of a snapshot.",
            },
            "model_search": {
                "type": "string",
                "description": (
                    "Optional: list vehicle model files whose path contains this text (e.g. 'bus', 'tram'), "
                    "with id, availability years, price and whether each is available in the current game year."
                ),
            },
            "selftest": {
                "type": "boolean",
                "description": "Optional: report which game API entry points this TF3 build provides.",
            },
        },
        "required": [],
    },
}


@_guarded
def handle_tf3_observe(args: dict, **kwargs: Any) -> str:
    client = _client()
    if args.get("selftest") is True:
        return tool_result({"selftest": client.call("selftest", {}), "bridge": client.health()})
    if args.get("entity_id") is not None:
        result = client.call("get_entity", {"id": int(args["entity_id"])})
        return tool_result({"entity": result})
    if args.get("model_search"):
        result = client.call("list_vehicle_models", {"contains": str(args["model_search"]),
                                                     "limit": _int_arg(args, "limit", 50, 1, 200)})
        return tool_result({"models": result})
    raw_sections = args.get("sections") or DEFAULT_SECTIONS
    if isinstance(raw_sections, str):
        raw_sections = [raw_sections]
    sections = [s for s in raw_sections if s in SNAPSHOT_SECTIONS]
    unknown = sorted(set(raw_sections) - set(sections))
    if unknown:
        raise ValueError(f"unknown sections {unknown}; valid: {list(SNAPSHOT_SECTIONS)}")
    snapshot = client.call("snapshot", {"sections": sections, "limit": _int_arg(args, "limit", 50, 1, 200)})
    settings = _settings()
    return tool_result({
        "snapshot": snapshot,
        "guardrails": {
            "readonly": settings.readonly,
            "actions_used_this_run": _budget.used(_run_key(kwargs)),
            "max_actions_per_session": settings.max_actions_per_session,
            "min_cash_reserve": settings.min_cash_reserve,
            "allow_unpause": settings.allow_unpause,
        },
    })


# ---------------------------------------------------------------------------
# tf3_screenshot
# ---------------------------------------------------------------------------

TF3_SCREENSHOT_SCHEMA = {
    "name": "tf3_screenshot",
    "description": (
        "Take a Transport Fever 3 screenshot on the Mac and return it as an image. The game renders it itself "
        "(nothing else on the user's screen is captured unless the Mac owner allowed that); optionally point "
        "the camera at an entity or position first. Use it to judge map layout, terrain and placement that "
        "tf3_observe cannot describe. Images are expensive: prefer tf3_observe for numbers."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "max_width": {
                "type": "integer",
                "minimum": 320,
                "maximum": 3840,
                "description": "Downscale so the image is at most this many pixels wide (default from settings, 1600).",
            },
            "focus_entity_id": {"type": "integer", "description": "Move the camera to this entity first."},
            "focus_position": {
                "type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 3,
                "description": "Move the camera to world position [x, y, z] first.",
            },
            "distance": {"type": "number", "description": "Camera distance for focus_position (default 500)."},
        },
        "required": [],
    },
}


@_guarded
def handle_tf3_screenshot(args: dict, **kwargs: Any) -> Any:
    default_width = _int_arg({"w": _setting("screenshot_max_width", 1600)}, "w", 1600, 320, 3840)
    client = _client()
    if args.get("focus_entity_id") is not None:
        client.call("camera_focus", {"entity_id": int(args["focus_entity_id"])})
        time.sleep(1.0)  # let the camera settle before the frame is rendered
    elif args.get("focus_position"):
        client.call("camera_focus", {"position": list(args["focus_position"]),
                                     "distance": float(args.get("distance") or 500)})
        time.sleep(1.0)
    source = _setting("screenshot_source", None)
    shot = client.screenshot(max_width=_int_arg(args, "max_width", default_width, 320, 3840),
                             source=str(source) if source in {"game", "auto", "screen"} else None)
    b64 = base64.b64encode(shot.data).decode("ascii")
    dims = f"{shot.width}x{shot.height}" if shot.width and shot.height else "unknown size"
    summary = (f"Transport Fever 3 screenshot from the Mac ({dims}, {len(shot.data):,} bytes, "
               f"source: {shot.source or 'unknown'}).")
    return {
        "_multimodal": True,
        "content": [
            {"type": "text", "text": summary},
            {"type": "image_url", "image_url": {"url": f"data:{shot.mime};base64,{b64}"}},
        ],
        "text_summary": summary + " (Image omitted: the active model cannot view images.)",
        "meta": {"width": shot.width, "height": shot.height, "bytes": len(shot.data), "mime": shot.mime,
                 "source": shot.source},
    }


# ---------------------------------------------------------------------------
# tf3_act
# ---------------------------------------------------------------------------

TF3_ACT_SCHEMA = {
    "name": "tf3_act",
    "description": (
        "Change the Transport Fever 3 game through the hermes_bridge mod. Pick an action and pass its params. "
        "Entity ids come from tf3_observe. Commands run in the simulation, so they are refused while the game "
        "is paused. The result says whether the game accepted them; re-observe to verify. "
        "Params by action:\n"
        "set_speed {speedup:int 0-4, 0 = pause}; "
        "line_create {name, stops:[station_id,...] or [{station_id, terminal?},...], color?:[r,g,b] 0-1}; "
        "line_update {line_id, name?, stops?}; line_delete {line_id} (sell its vehicles first); "
        "vehicle_buy {depot_id (a depot from the depots section, or a depot construction), model (file path "
        "from model_search; pick one with available=true) or models:[...] for multi-part trains, line_id?}; "
        "vehicle_set_line {vehicle_id, line_id, stop_index?}; vehicle_sell {vehicle_id}; "
        "vehicle_to_depot {vehicle_id, sell_on_arrival?}; vehicle_stop {vehicle_id}; vehicle_start {vehicle_id}; "
        "build_construction {file (e.g. '::/depots/road/road_depot/road_depot.con'), position:[x,y,z], "
        "rotation?:radians, params?:{...}, name?} — dry-run it first, that reports errors and cost; the "
        "result lists depot_ids/station_ids inside the new construction; "
        "bulldoze {entity_id of a construction}; save_game {name} (saved as hermes-<name>)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": sorted(ACTION_CLASSES)},
            "params": {"type": "object", "description": "Action parameters (see description)."},
            "reason": {
                "type": "string",
                "description": "One sentence on why — recorded in the action log for the user.",
            },
            "dry_run": {
                "type": "boolean",
                "description": "Validate params in the mod without executing (does not use the action budget).",
            },
        },
        "required": ["action", "reason"],
    },
}


def _finance_balance(client: BridgeClient) -> Optional[float]:
    finance = client.call("snapshot", {"sections": ["finance"], "limit": 1}).get("finance") or {}
    balance = finance.get("balance")
    return float(balance) if isinstance(balance, (int, float)) and not isinstance(balance, bool) else None


def _check_unpause(client: BridgeClient, params: dict, settings: Settings) -> None:
    """Do not resume a game the player paused, unless the user allowed it or
    the pause was the agent's own and the game has not moved since."""
    try:
        speedup = int(params.get("speedup"))
    except (TypeError, ValueError):
        return  # the mod rejects it with a proper message
    if speedup <= 0 or settings.allow_unpause:
        return
    game = client.call("snapshot", {"sections": ["game"], "limit": 1}).get("game") or {}
    if game.get("paused") is not True:
        return
    own = _state_get("agent_pause", None)
    if isinstance(own, dict) and own.get("game_time") == game.get("game_time"):
        return
    raise GuardrailError(
        "player_paused",
        "the game is paused and tf3 does not resume a game the player paused "
        "(plugins.entries.tf3.settings.allow_unpause: true allows it). Journal it and end the turn.",
    )


def _note_speed_change(client: BridgeClient, params: dict) -> None:
    try:
        speedup = int(params.get("speedup"))
    except (TypeError, ValueError):
        return
    if speedup == 0:
        game = client.call("snapshot", {"sections": ["game"], "limit": 1}).get("game") or {}
        _state_set("agent_pause", {"game_time": game.get("game_time"), "ts": _now_iso()})
    else:
        _state_set("agent_pause", None)


@_guarded
def handle_tf3_act(args: dict, **kwargs: Any) -> str:
    action = str(args.get("action") or "").strip()
    params = args.get("params") or {}
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    params = _drop_nulls(params)
    # A dry run asked for anywhere stays a dry run; never let one side
    # silently turn the other into a real action.
    dry_run = args.get("dry_run") is True or params.pop("dry_run", None) is True
    reason = str(args.get("reason") or "").strip()[:500]
    settings = _settings()
    action_class = check_action(action, settings, params)
    client = _client()
    run = _run_key(kwargs)
    session = str(kwargs.get("session_id") or "")
    started = time.monotonic()

    if dry_run:
        result = client.call(action, {**params, "dry_run": True})
        return tool_result({"action": action, "dry_run": True, "result": result,
                            "elapsed_ms": int((time.monotonic() - started) * 1000)})

    if effective_action(action, params) == "set_speed":
        _check_unpause(client, params, settings)
    if action_class == "spend" and settings.min_cash_reserve > 0:
        preview = client.call(action, {**params, "dry_run": True})
        cost = preview.get("cost")
        check_cash_reserve(_finance_balance(client),
                           float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None,
                           settings)
    used = _budget.consume(run, settings.max_actions_per_session)

    entry = {"ts": _now_iso(), "session": session, "run": run, "action": action, "params": params,
             "reason": reason}
    try:
        request_id, result = _call_state_change(client, action, {**params, "dry_run": False})
    except BridgeError as exc:
        request_id = getattr(exc, "request_id", "")
        _record_action({**entry, "request_id": request_id, "ok": False, "error": exc.to_dict()})
        return _failure_result(exc, request_id=request_id)
    _record_action({**entry, "request_id": request_id, "ok": True, "result": result})
    if action == "set_speed":
        _note_speed_change(client, params)
    return tool_result({
        "action": action,
        "dry_run": False,
        "result": result,
        "request_id": request_id,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "actions_used_this_run": used,
        "next_step": "Re-observe to confirm the game state changed as intended.",
    })


# ---------------------------------------------------------------------------
# tf3_lua (opt-in, hidden unless enable_lua_tool is true)
# ---------------------------------------------------------------------------

TF3_LUA_SCHEMA = {
    "name": "tf3_lua",
    "description": (
        "Advanced: run a Lua 5.2 chunk inside Transport Fever 3's GUI script state via the hermes_bridge mod "
        "and return its result (the chunk should `return` a table/string/number). Use only when tf3_act "
        "lacks a needed command, e.g. complex construction proposals. The TF3 API (api.engine, api.cmd, "
        "api.type) is available; globals are not. Requires the Mac relay to allow Lua."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Lua source; max 20000 characters."},
            "reason": {"type": "string", "description": "Why tf3_act was insufficient."},
        },
        "required": ["code", "reason"],
    },
}


@_guarded
def handle_tf3_lua(args: dict, **kwargs: Any) -> str:
    code = str(args.get("code") or "")
    if not code.strip():
        raise ValueError("code is required")
    if len(code) > 20000:
        raise ValueError("code exceeds 20000 characters")
    settings = _settings()
    if settings.readonly:
        raise GuardrailError("readonly", "tf3 is in readonly mode (plugins.entries.tf3.settings.readonly)")
    run = _run_key(kwargs)
    used = _budget.consume(run, settings.max_actions_per_session)
    entry = {"ts": _now_iso(), "session": str(kwargs.get("session_id") or ""), "run": run, "action": "lua_eval",
             "reason": str(args.get("reason") or "")[:500], "code_chars": len(code)}
    try:
        request_id, result = _call_state_change(_client(), "lua_eval", {"code": code})
    except BridgeError as exc:
        request_id = getattr(exc, "request_id", "")
        _record_action({**entry, "request_id": request_id, "ok": False, "error": exc.to_dict()})
        return _failure_result(exc, request_id=request_id)
    _record_action({**entry, "request_id": request_id, "ok": True})
    return tool_result({"result": result, "request_id": request_id, "actions_used_this_run": used})


# ---------------------------------------------------------------------------
# tf3_journal
# ---------------------------------------------------------------------------

TF3_JOURNAL_SCHEMA = {
    "name": "tf3_journal",
    "description": (
        "Persistent Transport Fever 3 notebook that survives across sessions and scheduled autopilot runs. "
        "read: current strategic plan + recent entries (+ recent tf3_act log). append: add a short entry "
        "(what you did, what you observed, what to try next). set_plan: replace the standing strategy."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["read", "append", "set_plan"]},
            "text": {"type": "string", "description": "Entry or plan text (append/set_plan)."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Entries to return on read (default 20)."},
        },
        "required": ["action"],
    },
}


@_guarded
def handle_tf3_journal(args: dict, **kwargs: Any) -> str:
    action = str(args.get("action") or "read")
    if _ctx is None:
        raise RuntimeError("tf3 plugin is not initialised")
    if action == "read":
        limit = _int_arg(args, "limit", 20, 1, 100)
        entries = list(_state_get("journal", []))
        return tool_result({
            "plan": _state_get("plan", ""),
            "plan_updated": _state_get("plan_updated", None),
            "entries": entries[-limit:],
            "total_entries": len(entries),
            "recent_actions": list(_state_get("action_log", []))[-10:],
        })
    text = str(args.get("text") or "").strip()
    if not text:
        raise ValueError(f"text is required for action={action!r}")
    if action == "append":
        entries = list(_state_get("journal", []))[-(_JOURNAL_MAX_ENTRIES - 1):]
        entries.append({"ts": _now_iso(), "session": str(kwargs.get("session_id") or ""),
                        "text": text[:_JOURNAL_MAX_TEXT]})
        _state_set("journal", entries)
        return tool_result({"ok": True, "total_entries": len(entries), "truncated": len(text) > _JOURNAL_MAX_TEXT})
    if action == "set_plan":
        _state_set("plan", text[:_PLAN_MAX_TEXT])
        _state_set("plan_updated", _now_iso())
        return tool_result({"ok": True, "truncated": len(text) > _PLAN_MAX_TEXT})
    raise ValueError(f"unknown journal action {action!r}")


TOOLS = (
    ("tf3_observe", TF3_OBSERVE_SCHEMA, handle_tf3_observe, check_tf3_available, "🚆"),
    ("tf3_screenshot", TF3_SCREENSHOT_SCHEMA, handle_tf3_screenshot, check_tf3_available, "📸"),
    ("tf3_act", TF3_ACT_SCHEMA, handle_tf3_act, check_tf3_available, "🛠️"),
    ("tf3_lua", TF3_LUA_SCHEMA, handle_tf3_lua, check_tf3_lua_available, "🌙"),
    ("tf3_journal", TF3_JOURNAL_SCHEMA, handle_tf3_journal, check_tf3_available, "📓"),
)


def handler_for(name: str) -> Optional[Callable[..., Any]]:
    for tool_name, _schema, handler, _check, _emoji in TOOLS:
        if tool_name == name:
            return handler
    return None
