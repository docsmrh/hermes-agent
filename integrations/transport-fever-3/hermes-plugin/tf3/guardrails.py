"""VPS-side guardrails for autonomous play.

The Mac relay is the authority on what the game will accept (it holds the
kill switch and the allow_actions / allow_lua policy). These checks sit in
front of it and keep an autonomous agent within the budget the user set in
``plugins.entries.tf3.settings``:

* ``readonly``                — refuse every non-read action.
* ``max_actions_per_session`` — cap mutating calls per Hermes session (a cron
  run is one session, so this is also a per-tick budget).
* ``min_cash_reserve``        — refuse spending actions while the in-game
  balance is below this amount.
* ``blocked_actions``         — action names the agent may never use.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional

# Action name -> class. Classes: "action" (free state change), "spend"
# (costs in-game money), "destructive" (removes something), "save".
# Keep in sync with the mod's op table (tf3-mod/hermes_bridge/content/hermes_bridge/ops.lua)
# and PROTOCOL.md section C.
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
}


@dataclass
class Settings:
    readonly: bool = False
    max_actions_per_session: int = 40
    min_cash_reserve: float = 0.0
    blocked_actions: frozenset = field(default_factory=frozenset)

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


class ActionBudget:
    """Per-session counter of mutating calls. Process-local by design: a
    gateway restart resets it, which only ever makes the budget stricter
    relative to wall-clock (never lets a single session exceed it)."""

    _MAX_TRACKED_SESSIONS = 512

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: Dict[str, int] = {}

    def used(self, session_id: str) -> int:
        with self._lock:
            return self._counts.get(session_id, 0)

    def consume(self, session_id: str, limit: int) -> int:
        with self._lock:
            used = self._counts.get(session_id, 0)
            if used >= limit:
                raise GuardrailError(
                    "action_budget_exhausted",
                    f"this session already used {used}/{limit} tf3 actions "
                    "(plugins.entries.tf3.settings.max_actions_per_session). Observe, journal your plan, "
                    "and continue in the next run.",
                )
            if session_id not in self._counts and len(self._counts) >= self._MAX_TRACKED_SESSIONS:
                self._counts.pop(next(iter(self._counts)))
            self._counts[session_id] = used + 1
            return used + 1


def check_action(action: str, settings: Settings) -> str:
    """Validate the action name against static policy; return its class."""
    cls = ACTION_CLASSES.get(action)
    if cls is None:
        raise GuardrailError("unknown_action", f"unknown action {action!r}; valid: {', '.join(sorted(ACTION_CLASSES))}")
    if settings.readonly:
        raise GuardrailError("readonly", "tf3 is in readonly mode (plugins.entries.tf3.settings.readonly)")
    if action in settings.blocked_actions:
        raise GuardrailError("blocked_action", f"action {action!r} is in plugins.entries.tf3.settings.blocked_actions")
    return cls


def check_cash_reserve(balance: Optional[float], settings: Settings) -> None:
    if settings.min_cash_reserve <= 0:
        return
    if balance is None:
        raise GuardrailError(
            "balance_unknown",
            "min_cash_reserve is set but the current balance could not be read; refusing to spend",
        )
    if balance < settings.min_cash_reserve:
        raise GuardrailError(
            "cash_reserve",
            f"balance {balance:,.0f} is below the configured reserve {settings.min_cash_reserve:,.0f}; "
            "spending actions are paused until income recovers",
        )
