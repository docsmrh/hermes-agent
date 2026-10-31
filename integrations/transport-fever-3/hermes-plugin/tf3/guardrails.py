"""VPS-side guardrails for autonomous play.

The Mac relay is the authority on what the game will accept (it holds the
kill switch and the allow_actions / allow_save / allow_lua policy, and the
mod re-checks it). These checks sit in front of it and keep an autonomous
agent within the budget the user set in ``plugins.entries.tf3.settings``:

* ``readonly``                — refuse every non-read action.
* ``max_actions_per_session`` — cap mutating calls per *run*: one cron run or
  one conversation, including its compressed continuations and delegated
  subagents (keyed on the session-lineage root, persisted across restarts).
* ``min_cash_reserve``        — refuse spending actions whose cost would take
  the in-game balance below this amount (or whose cost is unknown).
* ``blocked_actions``         — action names the agent may never use.
* ``allow_unpause``           — let the agent resume a game the player paused.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional

# Action name -> class. Classes: "action" (free state change), "spend"
# (costs in-game money), "destructive" (removes something), "save".
# Keep in sync with the mod's op table (tf3-mod/hermes_bridge/content/hermes_bridge/ops.lua)
# and PROTOCOL.md (op table).
ACTION_CLASSES: Dict[str, str] = {
    "set_speed": "action",
    "line_create": "action",
    "line_update": "action",
    "line_delete": "destructive",
    "vehicle_buy": "spend",
    "vehicle_set_line": "action",
    "vehicle_sell": "destructive",
    "vehicle_to_depot": "action",
    "vehicle_stop": "action",
    "vehicle_start": "action",
    "build_construction": "spend",
    "bulldoze": "destructive",
    "save_game": "save",
}

_DEFAULTS = {
    "readonly": False,
    "max_actions_per_session": 40,
    "min_cash_reserve": 0,
    "blocked_actions": [],
    "allow_unpause": False,
}


@dataclass
class Settings:
    readonly: bool = False
    max_actions_per_session: int = 40
    min_cash_reserve: float = 0.0
    blocked_actions: frozenset = field(default_factory=frozenset)
    allow_unpause: bool = False

    @classmethod
    def from_mapping(cls, raw: Optional[Mapping[str, Any]]) -> "Settings":
        raw = dict(raw or {})
        merged = {**_DEFAULTS, **{k: v for k, v in raw.items() if v is not None}}
        try:
            max_actions = max(0, int(merged["max_actions_per_session"]))
        except (TypeError, ValueError):
            max_actions = _DEFAULTS["max_actions_per_session"]
        try:
            reserve = float(merged["min_cash_reserve"])
        except (TypeError, ValueError):
            reserve = 0.0
        blocked = merged.get("blocked_actions") or []
        if isinstance(blocked, str):
            blocked = [blocked]
        return cls(
            readonly=_truthy(merged["readonly"]),
            max_actions_per_session=max_actions,
            min_cash_reserve=max(0.0, reserve),
            blocked_actions=frozenset(str(b).strip() for b in blocked if str(b).strip()),
            allow_unpause=_truthy(merged["allow_unpause"]),
        )


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


class GuardrailError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def effective_action(action: str, params: Mapping[str, Any]) -> str:
    """The action as far as policy is concerned: sending a vehicle to the
    depot with sell_on_arrival sells it, so it is a vehicle_sell."""
    if action == "vehicle_to_depot" and _truthy(params.get("sell_on_arrival", False)):
        return "vehicle_sell"
    return action


def check_action(action: str, settings: Settings, params: Optional[Mapping[str, Any]] = None) -> str:
    """Validate the action against static policy; return its effective class."""
    if action not in ACTION_CLASSES:
        raise GuardrailError("unknown_action", f"unknown action {action!r}; valid: {', '.join(sorted(ACTION_CLASSES))}")
    if settings.readonly:
        raise GuardrailError("readonly", "tf3 is in readonly mode (plugins.entries.tf3.settings.readonly)")
    effective = effective_action(action, params or {})
    for name in {action, effective}:
        if name in settings.blocked_actions:
            raise GuardrailError("blocked_action",
                                 f"action {name!r} is in plugins.entries.tf3.settings.blocked_actions")
    return ACTION_CLASSES[effective]


def check_cash_reserve(balance: Optional[float], cost: Optional[float], settings: Settings) -> None:
    if settings.min_cash_reserve <= 0:
        return
    if balance is None:
        raise GuardrailError(
            "balance_unknown",
            "min_cash_reserve is set but the current balance could not be read; refusing to spend",
        )
    if cost is None:
        raise GuardrailError(
            "cost_unknown",
            "min_cash_reserve is set but the game did not report what this would cost; refusing to spend",
        )
    if balance - cost < settings.min_cash_reserve:
        raise GuardrailError(
            "cash_reserve",
            f"this costs about {cost:,.0f}; the balance {balance:,.0f} would drop below the configured "
            f"reserve {settings.min_cash_reserve:,.0f}. Spending is paused until income recovers.",
        )


class ActionBudget:
    """Counts mutating calls per run key, persisted through ``get``/``set``
    (the plugin's durable state) so a gateway restart does not hand out a
    fresh budget. Entries untouched for a week are dropped."""

    STATE_KEY = "action_budget"
    _MAX_KEYS = 500
    _TTL_S = 7 * 86400

    def __init__(self, get: Optional[Callable[[str, Any], Any]] = None,
                 set: Optional[Callable[[str, Any], None]] = None, clock: Callable[[], float] = time.time):
        self._lock = threading.Lock()
        self._memory: Dict[str, Any] = {}
        self._get = get or (lambda key, default: self._memory.get(key, default))
        self._set = set or (lambda key, value: self._memory.__setitem__(key, value))
        self._clock = clock

    def _load(self) -> Dict[str, dict]:
        try:
            data = self._get(self.STATE_KEY, {})
        except Exception:
            data = {}
        return dict(data) if isinstance(data, dict) else {}

    def used(self, key: str) -> int:
        with self._lock:
            entry = self._load().get(key) or {}
            return int(entry.get("n", 0))

    def consume(self, key: str, limit: int) -> int:
        with self._lock:
            data = self._load()
            now = self._clock()
            data = {k: v for k, v in data.items()
                    if isinstance(v, dict) and now - float(v.get("t", 0)) <= self._TTL_S}
            used = int((data.get(key) or {}).get("n", 0))
            if used >= limit:
                raise GuardrailError(
                    "action_budget_exhausted",
                    f"this run already used {used}/{limit} tf3 actions "
                    "(plugins.entries.tf3.settings.max_actions_per_session). Observe, journal your plan, "
                    "and continue in the next run.",
                )
            data[key] = {"n": used + 1, "t": now}
            if len(data) > self._MAX_KEYS:
                for old in sorted(data, key=lambda k: data[k].get("t", 0))[: len(data) - self._MAX_KEYS]:
                    data.pop(old, None)
            self._set(self.STATE_KEY, data)
            return used + 1
