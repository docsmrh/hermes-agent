#!/usr/bin/env python3
"""tf3_relay — the Mac-side companion of the Hermes Bridge Transport Fever 3 mod.

Transport Fever 3 mods have no network access, and the GUI Lua has no file
library either: the mod can only exchange Lua tables through the official
``app.saveUserdata`` / ``app.loadUserdata`` API in a userdata folder
(``heightmaps``: one of the three folders build 40420 still allows, and the
only one no in-game list shows). This relay is the smallest possible bridge between those
files and the network. It holds no game logic — every op is implemented by
the mod — but it *is* the security authority on the Mac:

* listens on 127.0.0.1 only (reach it from the VPS through an SSH reverse
  tunnel; never expose it on a LAN or the internet);
* authenticates every request with an HMAC-SHA256 signature over method,
  path, timestamp, nonce and body (the shared token itself never crosses the
  wire; stale and replayed requests are refused), and signs every response
  so the plugin can tell the real relay from anything else on the port;
* enforces the Mac-side policy (``allow_actions``, ``allow_save``,
  ``allow_lua``, ``screenshot_enabled``, ``screenshot_source``) and the kill
  switch (``tf3_relay.py pause``), re-reading config.json when it changes,
  and publishes the policy to the mod, which re-checks every request;
* only forwards ops on its allowlist, with size, depth and rate limits.

Stdlib only, Python 3.9+ (the version macOS Command Line Tools ships).

Usage:
    tf3_relay.py init      # create config + token, detect the TF3 userdata folder
    tf3_relay.py token     # print the shared token for the VPS .env
    tf3_relay.py serve     # run the relay (the LaunchAgent does this)
    tf3_relay.py status    # local view: config, mod heartbeat, paused?
    tf3_relay.py pause     # kill switch: refuse every state-changing op
    tf3_relay.py resume

Wire contract: ../../PROTOCOL.md.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

RELAY_VERSION = "0.1.0"
PROTOCOL_VERSION = 1
TF3_STEAM_APP_ID = "3493540"
DEFAULT_PORT = 47800
EXCHANGE_DIRNAME = "heightmaps"
FILE_PREFIX = "hermes_bridge__"
MAX_BODY_BYTES = 256 * 1024
MAX_PENDING = 32
MAX_CALL_TIMEOUT_S = 60.0
HEARTBEAT_STALE_S = 12.0
RELAY_HEARTBEAT_S = 5.0
ABANDONED_GRACE_S = 120.0
RESULT_CACHE_SIZE = 256
PART_CHARS = 60000
MAX_OUTBOX_BYTES = 8 * 1024 * 1024
MAX_ARGS_BYTES = 32 * 1024           # one request's serialized args
MAX_PENDING_ARGS_BYTES = 512 * 1024  # all pending args together (the mod decodes the inbox in a frame)
MAX_JSON_DEPTH = 32
SIGNATURE_WINDOW_S = 60
NONCE_TTL_S = 2 * SIGNATURE_WINDOW_S
MAX_NONCES = 20000
SCREENSHOT_MIN_INTERVAL_S = 2.0
MAX_CONNECTIONS = 32
SOCKET_TIMEOUT_S = 15

log = logging.getLogger("tf3_relay")

# Op allowlist, by class. Anything not listed is rejected before it reaches
# the game. Keep in sync with hermes_bridge/content/hermes_bridge/ops.lua.
READ_OPS = frozenset({"hello", "ping", "selftest", "snapshot", "get_entity", "list_vehicle_models"})
VIEW_OPS = frozenset({"camera_focus"})
# Ops the relay itself issues (never accepted on /v1/call).
INTERNAL_OPS = frozenset({"take_screenshot"})
ACTION_OPS = frozenset({
    "set_speed",
    "line_create", "line_update", "line_delete",
    "vehicle_buy", "vehicle_set_line", "vehicle_sell", "vehicle_to_depot", "vehicle_stop", "vehicle_start",
    "build_construction", "bulldoze",
})
SAVE_OPS = frozenset({"save_game"})
LUA_OPS = frozenset({"lua_eval"})
ALL_OPS = READ_OPS | VIEW_OPS | ACTION_OPS | SAVE_OPS | LUA_OPS


class RelayError(Exception):
    def __init__(self, status: int, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retryable = retryable

    def body(self) -> dict:
        return {"ok": False, "error": {"code": self.code, "message": self.message, "retryable": self.retryable}}


# ---------------------------------------------------------------------------
# Request / response signatures (PROTOCOL.md section A)
# ---------------------------------------------------------------------------

def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def request_signature(key: str, method: str, target: str, timestamp: str, nonce: str, body: bytes) -> str:
    """HMAC-SHA256 over the canonical request; ``target`` is path plus query, as sent."""
    canonical = "\n".join(["tf3-v1", method.upper(), target, timestamp, nonce, _sha256_hex(body)])
    return hmac.new(key.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def response_signature(key: str, nonce: str, status: int, body: bytes) -> str:
    canonical = "\n".join(["tf3-v1-resp", nonce, str(int(status)), _sha256_hex(body)])
    return hmac.new(key.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


_NONCE_RE = re.compile(r"^[0-9a-f]{32,64}$")


class NonceCache:
    """Remembers recently accepted nonces so a captured request can not be replayed."""

    def __init__(self, ttl_s: float = NONCE_TTL_S, max_size: int = MAX_NONCES,
                 clock: Callable[[], float] = time.time):
        self.ttl_s = ttl_s
        self.max_size = max_size
        self.clock = clock
        self._seen: "OrderedDict[str, float]" = OrderedDict()
        self._lock = threading.Lock()

    def add(self, nonce: str) -> bool:
        """Record *nonce*; False if it was already used (a replay)."""
        now = self.clock()
        with self._lock:
            while self._seen and (now - next(iter(self._seen.values())) > self.ttl_s
                                  or len(self._seen) >= self.max_size):
                self._seen.popitem(last=False)
            if nonce in self._seen:
                return False
            self._seen[nonce] = now
            return True


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def default_home() -> Path:
    override = os.environ.get("TF3_RELAY_HOME")
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "hermes-tf3-bridge"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "hermes-tf3-bridge"


def candidate_userdata_dirs() -> List[Path]:
    """TF3 userdata folders on this Mac (Steam per-account dirs, then GOG/Epic)."""
    home = Path.home()
    found = [Path(p) for p in sorted(glob.glob(str(
        home / "Library" / "Application Support" / "Steam" / "userdata" / "*" / TF3_STEAM_APP_ID / "local")))]
    gog = home / "Library" / "Application Support" / "Transport Fever 3"
    if gog.is_dir():
        found.append(gog)
    return [p for p in found if p.is_dir()]


class Config:
    DEFAULTS: Dict[str, Any] = {
        "port": DEFAULT_PORT,
        "userdata_dir": "",
        "allow_actions": True,
        "allow_save": True,
        "allow_lua": False,
        "screenshot_enabled": True,
        # game: the game renders it (default; nothing else on the Mac is
        # visible). auto: game, else a capture of the whole display.
        # screen: always the whole display. Requests may only narrow this.
        "screenshot_source": "game",
        "screenshot_display": 1,
        "keep_game_screenshots": False,
        "rate_limit_per_s": 10,
    }

    def __init__(self, home: Path, data: Dict[str, Any]):
        self.home = home
        self.data = {**self.DEFAULTS, **data}

    @property
    def path(self) -> Path:
        return self.home / "config.json"

    @property
    def token(self) -> str:
        return str(self.data.get("token") or "")

    @property
    def port(self) -> int:
        return int(self.data["port"])

    @property
    def userdata_dir(self) -> Path:
        userdata = str(self.data.get("userdata_dir") or "")
        if not userdata:
            raise RelayError(500, "not_configured", "userdata_dir is not set; run `tf3_relay.py init --userdata-dir ...`")
        return Path(userdata).expanduser()

    @property
    def exchange_dir(self) -> Path:
        return self.userdata_dir / EXCHANGE_DIRNAME

    @property
    def paused_flag(self) -> Path:
        return self.home / "PAUSED"

    def is_paused(self) -> bool:
        return self.paused_flag.exists()

    @classmethod
    def load(cls, home: Optional[Path] = None) -> "Config":
        home = home or default_home()
        path = home / "config.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise SystemExit(f"no config at {path}; run `tf3_relay.py init` first")
        if not isinstance(data, dict) or len(str(data.get("token") or "")) < 32:
            raise SystemExit(f"{path} is missing a valid token; re-run `tf3_relay.py init --rotate-token`")
        if path.stat().st_mode & 0o077:
            log.warning("%s was readable by group/other; tightening to 0600", path)
            os.chmod(path, 0o600)
        return cls(home, data)

    def save(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        os.chmod(self.home, 0o700)
        fd, tmp = tempfile.mkstemp(dir=str(self.home), prefix=".config.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)


# ---------------------------------------------------------------------------
# Lua table literals (the on-disk format of app.saveUserdata files)
# ---------------------------------------------------------------------------

class LuaParseError(ValueError):
    pass


_LUA_TOKEN = re.compile(
    rb"""\s+|--\[(?P<cl>=*)\[.*?\](?P=cl)\]|--[^\n]*"""
    rb"""|(?P<long>\[(?P<ll>=*)\[(?P<lbody>.*?)\](?P=ll)\])"""
    rb"""|(?P<str>"(?:[^"\\\n]|\\.|\\\n)*"|'(?:[^'\\\n]|\\.|\\\n)*')"""
    rb"""|(?P<num>0[xX][0-9a-fA-F]+|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)"""
    rb"""|(?P<name>[A-Za-z_][A-Za-z0-9_]*)"""
    rb"""|(?P<sym>[{}\[\]=,;()-])""",
    re.S,
)

_SIMPLE_ESCAPES = {ord("a"): 7, ord("b"): 8, ord("f"): 12, ord("n"): 10, ord("r"): 13, ord("t"): 9,
                   ord("v"): 11, ord("\\"): 92, ord('"'): 34, ord("'"): 39, ord("\n"): 10}


def _unescape_lua(body: bytes) -> bytes:
    out = bytearray()
    i, n = 0, len(body)
    while i < n:
        c = body[i]
        if c != 92:  # backslash
            out.append(c)
            i += 1
            continue
        i += 1
        if i >= n:
            raise LuaParseError("dangling escape")
        e = body[i]
        if e in _SIMPLE_ESCAPES:
            out.append(_SIMPLE_ESCAPES[e])
            i += 1
        elif e == ord("x"):
            out.append(int(body[i + 1:i + 3], 16))
            i += 3
        elif e == ord("z"):
            i += 1
            while i < n and body[i] in b" \t\r\n\f\v":
                i += 1
        elif 48 <= e <= 57:
            j = i
            while j < n and j < i + 3 and 48 <= body[j] <= 57:
                j += 1
            value = int(body[i:j])
            if value > 255:
                raise LuaParseError("decimal escape too large")
            out.append(value)
            i = j
        elif e == ord("u") and i + 1 < n and body[i + 1] == ord("{"):
            j = body.index(b"}", i)
            out += chr(int(body[i + 2:j], 16)).encode("utf-8")
            i = j + 1
        else:
            raise LuaParseError(f"bad escape \\{chr(e)}")
    return bytes(out)


def _lua_tokens(src: bytes) -> List[Tuple[str, Any]]:
    tokens: List[Tuple[str, Any]] = []
    pos = 0
    while pos < len(src):
        m = _LUA_TOKEN.match(src, pos)
        if not m:
            raise LuaParseError(f"unexpected byte at {pos}")
        pos = m.end()
        if m.group("long") is not None:
            body = m.group("lbody")
            tokens.append(("str", body[1:] if body.startswith(b"\n") else body))
        elif m.group("str") is not None:
            tokens.append(("str", _unescape_lua(m.group("str")[1:-1])))
        elif m.group("num") is not None:
            raw = m.group("num").decode("ascii")
            if raw.lower().startswith("0x"):
                tokens.append(("num", int(raw, 16)))
            elif raw.isdigit():
                tokens.append(("num", int(raw)))
            else:
                tokens.append(("num", float(raw)))
        elif m.group("name") is not None:
            tokens.append(("name", m.group("name").decode("ascii")))
        elif m.group("sym") is not None:
            tokens.append(("sym", m.group("sym").decode("ascii")))
    return tokens


def parse_lua_table_file(src: bytes) -> Any:
    """Parse the value returned by a userdata file.

    Accepts ``function data() return {...} end``, ``return {...}`` and
    ``data = {...}``: the first table constructor in the file is parsed.
    Strings come back as ``bytes`` (Lua strings are byte strings); keys are
    decoded to ``str``.
    """
    tokens = _lua_tokens(src)
    try:
        start = next(i for i, t in enumerate(tokens) if t == ("sym", "{"))
    except StopIteration:
        raise LuaParseError("no table constructor found")
    value, _ = _parse_lua_value(tokens, start)
    return value


def _parse_lua_value(tokens: List[Tuple[str, Any]], i: int) -> Tuple[Any, int]:
    kind, val = tokens[i]
    if kind in {"str", "num"}:
        return val, i + 1
    if kind == "sym" and val == "-" and tokens[i + 1][0] == "num":
        return -tokens[i + 1][1], i + 2
    if kind == "name":
        if val in {"true", "false"}:
            return val == "true", i + 1
        if val == "nil":
            return None, i + 1
        raise LuaParseError(f"unexpected name {val!r}")
    if (kind, val) == ("sym", "{"):
        return _parse_lua_table(tokens, i + 1)
    raise LuaParseError(f"unexpected token {val!r}")


def _parse_lua_table(tokens: List[Tuple[str, Any]], i: int) -> Tuple[Any, int]:
    array: List[Any] = []
    mapping: Dict[Any, Any] = {}
    while True:
        if tokens[i] == ("sym", "}"):
            i += 1
            break
        if tokens[i] == ("sym", "["):
            key, i = _parse_lua_value(tokens, i + 1)
            if tokens[i] != ("sym", "]") or tokens[i + 1] != ("sym", "="):
                raise LuaParseError("expected ']='")
            value, i = _parse_lua_value(tokens, i + 2)
            mapping[key.decode("utf-8", "replace") if isinstance(key, bytes) else key] = value
        elif tokens[i][0] == "name" and tokens[i + 1] == ("sym", "="):
            key = tokens[i][1]
            value, i = _parse_lua_value(tokens, i + 2)
            mapping[key] = value
        else:
            value, i = _parse_lua_value(tokens, i)
            array.append(value)
        if tokens[i][0] == "sym" and tokens[i][1] in {",", ";"}:
            i += 1
    if mapping:
        for idx, item in enumerate(array, start=1):
            mapping.setdefault(idx, item)
        keys = list(mapping)
        if keys and all(isinstance(k, int) for k in keys) and sorted(keys) == list(range(1, len(keys) + 1)):
            return [mapping[k] for k in range(1, len(keys) + 1)], i
        return mapping, i
    return array, i


def lua_string(text: str) -> str:
    """Quote ASCII text as a Lua string literal (callers pass ensure_ascii JSON)."""
    out = []
    for ch in text:
        code = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif code < 32 or code == 127:
            out.append(f"\\{code:03d}")
        elif code > 127:
            raise ValueError("lua_string expects ASCII input")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def render_parts_file(payload: dict) -> bytes:
    """Render ``{v=1, parts={json chunks}}`` in the userdata file format."""
    text = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    chunks = [text[i:i + PART_CHARS] for i in range(0, len(text), PART_CHARS)] or [""]
    body = ",\n\t\t".join(lua_string(c) for c in chunks)
    return (f"function data()\nreturn {{\n\tv = {PROTOCOL_VERSION},\n\tparts = {{\n\t\t{body},\n\t}},\n}}\nend\n"
            ).encode("ascii")


def parse_parts_file(src: bytes) -> Optional[dict]:
    """Inverse of render_parts_file for files the mod wrote; None if unreadable."""
    try:
        table = parse_lua_table_file(src)
    except (LuaParseError, IndexError, ValueError):
        return None
    if not isinstance(table, dict) or table.get("v") != PROTOCOL_VERSION:
        return None
    parts = table.get("parts")
    if isinstance(parts, dict) and not parts:
        parts = []
    if not isinstance(parts, list) or not all(isinstance(p, bytes) for p in parts):
        return None
    try:
        # One stray non-UTF-8 byte (say a mod's Latin-1 name) must not wedge
        # the channel, so undecodable bytes become U+FFFD.
        decoded = json.loads(b"".join(parts).decode("utf-8", "replace"))
    except ValueError:
        return None
    return decoded if isinstance(decoded, dict) else None


def atomic_write_bytes(path: Path, data: bytes) -> None:
    # The temp name does not match FILE_PREFIX, so neither side mistakes it
    # for a protocol file.
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tf3relay-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# File channel to the mod (PROTOCOL.md section B)
# ---------------------------------------------------------------------------

_OUTBOX_RE = re.compile(r"^" + re.escape(FILE_PREFIX) + r"out_(?P<stamp>\d+)_(?P<k>\d+)\.lua$")
_INBOX_RE = re.compile(r"^" + re.escape(FILE_PREFIX) + r"in_(?P<stamp>\d+)\.lua$")


class _Pending:
    __slots__ = ("request_id", "seq", "op", "args", "stamp", "size", "event", "result", "abandoned_at")

    def __init__(self, request_id: str, seq: int, op: str, args: dict, stamp: Optional[str], size: int):
        self.request_id = request_id
        self.seq = seq
        self.op = op
        self.args = args
        self.stamp = stamp
        self.size = size
        self.event = threading.Event()
        self.result: Optional[dict] = None
        self.abandoned_at: Optional[float] = None


# Policy published to the mod when nothing better is known: reads only.
FAIL_CLOSED_POLICY = {"paused": False, "allow_actions": False, "allow_save": False, "allow_lua": False}


class ModChannel:
    """Relay side of the userdata file protocol (heightmaps folder).

    The mod announces itself by writing ``hermes_bridge__out_<stamp>_<k>``
    files (a fresh name per write) and creates its own empty inbox
    ``hermes_bridge__in_<stamp>``. The relay follows the newest outbox (by
    mtime, then k), rewrites that stamp's inbox atomically with the pending
    requests plus the current policy, and deletes consumed outbox files.

    A new stamp means the game was (re)loaded. State-changing requests that
    were still pending are then failed with ``game_reloaded`` instead of
    being sent into the new game, where their entity ids mean something else;
    reads are simply re-sent. Each request also carries the stamp it is meant
    for, and the mod refuses any other.
    """

    def __init__(self, exchange_dir: Path, *, poll_s: float = 0.1,
                 policy_fn: Optional[Callable[[], Dict[str, bool]]] = None,
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time):
        self.dir = exchange_dir
        self.poll_s = poll_s
        self.policy_fn = policy_fn or (lambda: dict(FAIL_CLOSED_POLICY))
        self._clock = clock
        self._wall = wall
        self.session = uuid.uuid4().hex
        self._lock = threading.Lock()
        self._seq = 0
        self._pending: "OrderedDict[str, _Pending]" = OrderedDict()
        self._done: "OrderedDict[str, dict]" = OrderedDict()
        self._stamp: Optional[str] = None
        self._last_seen: Optional[Tuple[float, int]] = None
        self._outbox_mtime: Optional[float] = None
        self._mod_info: Dict[str, Any] = {}
        self._inbox_written_at = 0.0
        self._published_policy: Optional[Dict[str, bool]] = None
        self._oversize_warned: Optional[Tuple[float, int]] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._poll_loop, name="tf3-outbox-poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _poll_loop(self) -> None:
        while not self._stop.wait(self.poll_s):
            try:
                self.poll_once()
            except Exception:  # never let the poller die
                log.exception("mod channel poll failed")

    # -- discovery ---------------------------------------------------------

    def _scan(self) -> Dict[str, List[Tuple[int, Path, float]]]:
        by_stamp: Dict[str, List[Tuple[int, Path, float]]] = {}
        try:
            entries = list(os.scandir(self.dir))
        except FileNotFoundError:
            return by_stamp
        for entry in entries:
            m = _OUTBOX_RE.match(entry.name)
            if not m:
                continue
            try:
                mtime = entry.stat().st_mtime
            except FileNotFoundError:
                continue
            by_stamp.setdefault(m.group("stamp"), []).append((int(m.group("k")), Path(entry.path), mtime))
        return by_stamp

    @staticmethod
    def newest(by_stamp: Dict[str, List[Tuple[int, Path, float]]]) -> Tuple[str, int, Path, float]:
        """The stamp and outbox the mod wrote most recently (mtime first, then k)."""
        stamp = max(by_stamp, key=lambda s: max((m, k) for k, _, m in by_stamp[s]))
        k, path, mtime = max(by_stamp[stamp], key=lambda f: (f[2], f[0]))
        return stamp, k, path, mtime

    def _cleanup(self, by_stamp: Dict[str, List[Tuple[int, Path, float]]], current: str,
                 newest: Tuple[float, int]) -> None:
        now = self._wall()
        for stamp, files in by_stamp.items():
            for k, path, mtime in files:
                stale_other = stamp != current and now - mtime > ABANDONED_GRACE_S
                consumed = stamp == current and (mtime, k) < newest
                if stale_other or consumed:
                    try:
                        path.unlink()
                    except OSError:
                        pass
        # Inboxes of earlier game loads are dead weight once a day old.
        try:
            for entry in os.scandir(self.dir):
                m = _INBOX_RE.match(entry.name)
                if m and m.group("stamp") != current and now - entry.stat().st_mtime > 86400:
                    os.unlink(entry.path)
        except OSError:
            pass

    # -- inbox -------------------------------------------------------------

    def _current_policy(self) -> Dict[str, bool]:
        try:
            pol = self.policy_fn()
        except Exception:
            log.exception("policy lookup failed; publishing fail-closed policy")
            pol = FAIL_CLOSED_POLICY
        return {key: bool(pol.get(key)) for key in FAIL_CLOSED_POLICY}

    def _write_inbox_locked(self, policy: Optional[Dict[str, bool]] = None) -> None:
        if self._stamp is None:
            return
        policy = policy or self._current_policy()
        payload = {
            "v": PROTOCOL_VERSION,
            "session": self.session,
            "relay_t": int(self._wall()),
            "policy": policy,
            "requests": [{"id": p.request_id, "seq": p.seq, "op": p.op, "args": p.args, "stamp": p.stamp}
                         for p in self._pending.values()],
        }
        atomic_write_bytes(self.dir / f"{FILE_PREFIX}in_{self._stamp}.lua", render_parts_file(payload))
        self._inbox_written_at = self._clock()
        self._published_policy = policy

    def _finish_locked(self, pending: _Pending, final: dict) -> None:
        pending.result = final
        self._remember_locked(pending.request_id, final)
        pending.event.set()

    def _adopt_stamp_locked(self, stamp: str) -> None:
        log.info("mod announced stamp %s (game load)", stamp)
        previous = self._stamp
        if previous is not None:
            # Leave nothing actionable in the old load's inbox.
            try:
                atomic_write_bytes(self.dir / f"{FILE_PREFIX}in_{previous}.lua", render_parts_file({
                    "v": PROTOCOL_VERSION, "session": self.session, "relay_t": int(self._wall()),
                    "policy": dict(FAIL_CLOSED_POLICY), "requests": []}))
            except OSError:
                log.warning("could not clear the previous inbox %s", previous)
        self._stamp, self._last_seen = stamp, None
        for rid, p in list(self._pending.items()):
            if p.op in READ_OPS or p.op in VIEW_OPS or p.op in INTERNAL_OPS:
                p.stamp = stamp  # harmless to answer from the new game
                continue
            self._pending.pop(rid)
            self._finish_locked(p, {"ok": False, "result": None, "error": {
                "code": "game_reloaded", "retryable": False,
                "message": "the game was reloaded before this action was confirmed; it may have run in the "
                           "previous game and was not repeated in this one. Re-observe."}})

    # -- outbox ------------------------------------------------------------

    def poll_once(self) -> None:
        by_stamp = self._scan()
        changed = False
        with self._lock:
            if by_stamp:
                stamp, k, path, mtime = self.newest(by_stamp)
                if stamp != self._stamp:
                    self._adopt_stamp_locked(stamp)
                    changed = True
                if (mtime, k) != self._last_seen:
                    outbox = None
                    try:
                        size = path.stat().st_size
                        if size <= MAX_OUTBOX_BYTES:
                            outbox = parse_parts_file(path.read_bytes())
                        elif self._oversize_warned != (mtime, k):
                            self._oversize_warned = (mtime, k)
                            log.warning("outbox %s is %d bytes (limit %d); skipping it", path.name, size,
                                        MAX_OUTBOX_BYTES)
                            self._outbox_mtime = mtime  # the mod is alive, just over budget
                    except OSError:
                        outbox = None
                    if outbox is not None:
                        self._last_seen = (mtime, k)
                        self._outbox_mtime = mtime
                        changed = self._absorb_locked(outbox) or changed
                        self._cleanup(by_stamp, stamp, (mtime, k))
                else:
                    self._outbox_mtime = mtime
            changed = self._expire_abandoned_locked() or changed
            policy = self._current_policy()
            if policy != self._published_policy:
                changed = True  # e.g. `tf3_relay.py pause`: tell the mod right away
            if changed or (self._stamp and self._clock() - self._inbox_written_at >= RELAY_HEARTBEAT_S):
                self._write_inbox_locked(policy)

    def _absorb_locked(self, outbox: dict) -> bool:
        self._mod_info = {k: outbox.get(k) for k in ("mod_version", "stamp", "transport", "status", "frame")}
        if outbox.get("session") != self.session:
            return False
        changed = False
        for res in outbox.get("results") or []:
            if not isinstance(res, dict) or res.get("status") != "done":
                continue
            pending = self._pending.pop(str(res.get("id")), None)
            if pending is None:
                continue
            self._finish_locked(pending, {"ok": bool(res.get("ok")), "result": res.get("result"),
                                          "error": res.get("error")})
            changed = True
        return changed

    def _expire_abandoned_locked(self) -> bool:
        now = self._clock()
        stale = [rid for rid, p in self._pending.items()
                 if p.abandoned_at is not None and now - p.abandoned_at > ABANDONED_GRACE_S]
        for rid in stale:
            self._pending.pop(rid, None)
        return bool(stale)

    def _remember_locked(self, request_id: str, result: dict) -> None:
        self._done[request_id] = result
        while len(self._done) > RESULT_CACHE_SIZE:
            self._done.popitem(last=False)

    # -- public ------------------------------------------------------------

    def heartbeat_age(self) -> Optional[float]:
        if self._outbox_mtime is None:
            return None
        return max(0.0, self._wall() - self._outbox_mtime)

    def connected(self) -> bool:
        age = self.heartbeat_age()
        return age is not None and age <= HEARTBEAT_STALE_S

    def mod_info(self) -> Dict[str, Any]:
        with self._lock:
            info = dict(self._mod_info)
            info["pending"] = len(self._pending)
        return info

    def call(self, op: str, args: dict, request_id: str, timeout_s: float) -> dict:
        size = len(json.dumps(args, ensure_ascii=True, separators=(",", ":")))
        with self._lock:
            if request_id in self._done:
                return self._done[request_id]
            pending = self._pending.get(request_id)
            if pending is None:
                if len(self._pending) >= MAX_PENDING:
                    raise RelayError(429, "too_many_pending", "too many requests waiting on the game", retryable=True)
                if sum(p.size for p in self._pending.values()) + size > MAX_PENDING_ARGS_BYTES:
                    raise RelayError(429, "too_much_pending", "too much request data waiting on the game",
                                     retryable=True)
                if not self.connected():
                    raise RelayError(503, "mod_not_connected",
                                     "the Hermes Bridge mod is not answering: start TF3 and load a save with the "
                                     "mod enabled (it stays silent in the main menu)", retryable=True)
                self._seq += 1
                pending = _Pending(request_id, self._seq, op, args, self._stamp, size)
                self._pending[request_id] = pending
                try:
                    self._write_inbox_locked()
                except OSError as exc:
                    # Not queued anywhere the mod can see: report a clean "not sent".
                    self._pending.pop(request_id, None)
                    raise RelayError(503, "relay_io_error", f"could not write the exchange file: {exc}",
                                     retryable=True)
            pending.abandoned_at = None
        if not pending.event.wait(timeout_s):
            with self._lock:
                if pending.result is None:
                    # Keep it queued for a while so a retry with the same
                    # request_id picks up the late result instead of re-running.
                    pending.abandoned_at = self._clock()
            if pending.result is None:
                raise RelayError(504, "timeout",
                                 f"the game did not answer within {timeout_s:.0f}s (paused, loading, or in a menu?)",
                                 retryable=True)
        assert pending.result is not None
        return pending.result


# ---------------------------------------------------------------------------
# Screenshots
# ---------------------------------------------------------------------------

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".tga", ".tif", ".tiff"}


def _run(cmd: List[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, check=False)


def _parse_sips_dims(text: str) -> Tuple[Optional[int], Optional[int]]:
    dims: Dict[str, int] = {}
    for line in text.splitlines():
        key, _, value = line.strip().partition(":")
        if key in {"pixelWidth", "pixelHeight"}:
            try:
                dims[key] = int(value.strip())
            except ValueError:
                pass
    return dims.get("pixelWidth"), dims.get("pixelHeight")


def to_jpeg(src: Path, *, max_width: int, quality: int) -> Tuple[bytes, int, int]:
    """Convert/downscale an image to JPEG with macOS sips."""
    if not shutil.which("sips"):
        raise RelayError(501, "screenshot_unsupported", "image conversion needs macOS sips")
    with tempfile.TemporaryDirectory(prefix="tf3shot-") as tmp:
        out = Path(tmp) / "out.jpg"
        size = _run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(src)], 15)
        width, height = _parse_sips_dims(size.stdout.decode("utf-8", "replace"))
        cmd = ["sips", "-s", "format", "jpeg", "-s", "formatOptions", str(int(quality))]
        if width and width > max_width:
            cmd += ["--resampleWidth", str(int(max_width))]
        conv = _run(cmd + [str(src), "--out", str(out)], 30)
        if conv.returncode != 0 or not out.exists():
            raise RelayError(500, "capture_failed", "sips could not convert the screenshot")
        if width and width > max_width:
            height = int(round(height * max_width / width)) if height else None
            width = max_width
        return out.read_bytes(), width or 0, height or 0


def capture_screen(*, max_width: int, quality: int, display: int = 1) -> Tuple[bytes, int, int]:
    """Capture a display with screencapture (needs Screen Recording permission)."""
    if sys.platform != "darwin" or not shutil.which("screencapture"):
        raise RelayError(501, "screenshot_unsupported", "screen capture needs macOS screencapture")
    with tempfile.TemporaryDirectory(prefix="tf3shot-") as tmp:
        raw = Path(tmp) / "raw.png"
        cap = _run(["screencapture", "-x", "-t", "png", "-D", str(int(display)), str(raw)], 20)
        if cap.returncode != 0 or not raw.exists() or raw.stat().st_size == 0:
            raise RelayError(500, "capture_failed",
                             "screencapture failed; grant Screen Recording to the relay's python3 in System "
                             "Settings > Privacy & Security, then restart the relay")
        return to_jpeg(raw, max_width=max_width, quality=quality)


def newest_image_since(root: Path, since: float, *, max_depth: int = 2) -> Optional[Path]:
    """Newest image file under root (bounded depth) modified at/after since."""
    best: Optional[Tuple[float, Path]] = None
    stack = [(root, 0)]
    while stack:
        folder, depth = stack.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if depth < max_depth and entry.name not in {"mods", "staging_area", "save", EXCHANGE_DIRNAME}:
                        stack.append((Path(entry.path), depth + 1))
                    continue
                if Path(entry.name).suffix.lower() not in _IMAGE_SUFFIXES:
                    continue
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            if mtime >= since and (best is None or mtime > best[0]):
                best = (mtime, Path(entry.path))
    return best[1] if best else None


# ---------------------------------------------------------------------------
# Request policy + dispatch (independent of the HTTP plumbing, so testable)
# ---------------------------------------------------------------------------

class RateLimiter:
    def __init__(self, per_s: float, clock: Callable[[], float] = time.monotonic):
        self.rate = max(1.0, float(per_s))
        self.capacity = self.rate * 2
        self.tokens = self.capacity
        self.clock = clock
        self.updated = clock()
        self.lock = threading.Lock()

    def allow(self) -> bool:
        with self.lock:
            now = self.clock()
            self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
            self.updated = now
            if self.tokens < 1:
                return False
            self.tokens -= 1
            return True


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")


def _json_depth_ok(value: Any, limit: int = MAX_JSON_DEPTH) -> bool:
    stack = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if depth > limit:
            return False
        if isinstance(item, dict):
            stack.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list):
            stack.extend((v, depth + 1) for v in item)
    return True


def parse_request_json(raw: bytes) -> Any:
    """Strict JSON for request bodies: no NaN/Infinity, bounded nesting."""
    try:
        body = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError):
        raise RelayError(400, "invalid_request", "body is not valid JSON")
    if not _json_depth_ok(body):
        raise RelayError(400, "invalid_request", f"body nests deeper than {MAX_JSON_DEPTH} levels")
    return body


class Relay:
    def __init__(self, config: Config, channel: ModChannel, *,
                 screen_fn: Callable[..., Tuple[bytes, int, int]] = capture_screen,
                 convert_fn: Callable[..., Tuple[bytes, int, int]] = to_jpeg,
                 sleep: Callable[[float], None] = time.sleep,
                 wall: Callable[[], float] = time.time):
        self.config = config
        self.channel = channel
        self.screen_fn = screen_fn
        self.convert_fn = convert_fn
        self.sleep = sleep
        self.wall = wall
        self.limiter = RateLimiter(float(config.data.get("rate_limit_per_s") or 10))
        self.nonces = NonceCache(clock=wall)
        self.started = time.time()
        self._shot_lock = threading.Lock()
        self._last_shot = 0.0
        self._config_sig = self._stat_config()
        self._config_checked = time.monotonic()
        self._config_lock = threading.Lock()
        channel.policy_fn = self.mod_policy

    # -- config ------------------------------------------------------------

    def _stat_config(self) -> Optional[Tuple[int, int]]:
        try:
            st = self.config.path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def refresh_config(self, *, force: bool = False) -> None:
        """Pick up config.json edits (token rotation, policy flags) without a restart."""
        now = time.monotonic()
        with self._config_lock:
            if not force and now - self._config_checked < 1.0:
                return
            self._config_checked = now
            sig = self._stat_config()
            if sig is None or sig == self._config_sig:
                return
            try:
                fresh = Config.load(self.config.home)
            except (SystemExit, ValueError, OSError) as exc:
                log.error("config.json changed but could not be loaded (%s); keeping the previous config", exc)
                self._config_sig = sig
                return
            self._config_sig = sig
            old_rate = self.config.data.get("rate_limit_per_s")
            self.config = fresh
            if fresh.data.get("rate_limit_per_s") != old_rate:
                self.limiter = RateLimiter(float(fresh.data.get("rate_limit_per_s") or 10))
            log.info("reloaded %s", fresh.path)

    # -- auth --------------------------------------------------------------

    def verify_request(self, method: str, target: str, headers: Any, body: bytes) -> str:
        """Check the request signature; return its nonce, or raise 401."""
        self.refresh_config()
        ts = (headers.get("X-TF3-Timestamp") or "").strip()
        nonce = (headers.get("X-TF3-Nonce") or "").strip().lower()
        presented = (headers.get("X-TF3-Signature") or "").strip().lower()
        key = self.config.token
        if not (key and ts.isdigit() and _NONCE_RE.match(nonce) and presented):
            raise RelayError(401, "unauthorized", "missing or malformed request signature")
        expected = request_signature(key, method, target, ts, nonce, body)
        if not hmac.compare_digest(presented.encode("ascii", "replace"), expected.encode("ascii")):
            raise RelayError(401, "unauthorized", "bad request signature (wrong TF3_BRIDGE_TOKEN?)")
        if abs(self.wall() - int(ts)) > SIGNATURE_WINDOW_S:
            raise RelayError(401, "stale_request", "request timestamp is outside the allowed window (check clocks)")
        if not self.nonces.add(nonce):
            raise RelayError(401, "replayed_request", "this request was already used")
        return nonce

    def sign_response(self, nonce: Optional[str], status: int, body: bytes) -> Optional[str]:
        if not nonce or not _NONCE_RE.match(nonce) or not self.config.token:
            return None
        return response_signature(self.config.token, nonce, status, body)

    # -- policy ------------------------------------------------------------

    def policy(self) -> dict:
        self.refresh_config()
        d = self.config.data
        return {
            "paused": self.config.is_paused(),
            "allow_actions": bool(d.get("allow_actions")),
            "allow_save": bool(d.get("allow_save")),
            "allow_lua": bool(d.get("allow_lua")),
            "screenshot_enabled": bool(d.get("screenshot_enabled")),
            "screenshot_source": self.screenshot_ceiling(),
        }

    def mod_policy(self) -> Dict[str, bool]:
        pol = self.policy()
        return {key: pol[key] for key in FAIL_CLOSED_POLICY}

    def screenshot_ceiling(self) -> str:
        source = str(self.config.data.get("screenshot_source") or "game")
        return source if source in {"game", "auto", "screen"} else "game"

    def health(self) -> dict:
        mod = self.channel.mod_info()
        age = self.channel.heartbeat_age()
        mod.update({"connected": self.channel.connected(),
                    "heartbeat_age_s": None if age is None else round(age, 2)})
        return {"ok": True, "relay_version": RELAY_VERSION, "protocol": PROTOCOL_VERSION,
                "uptime_s": int(time.time() - self.started), "policy": self.policy(), "mod": mod}

    def check_op(self, op: str) -> None:
        if op not in ALL_OPS:
            raise RelayError(400, "unknown_op", f"op {op!r} is not on the relay allowlist")
        pol = self.policy()
        if op in READ_OPS:
            return
        if op in VIEW_OPS:
            if not pol["screenshot_enabled"]:
                raise RelayError(403, "policy_denied", "screenshots/camera are disabled in the Mac relay config")
            return
        if pol["paused"]:
            raise RelayError(403, "paused", "automation is paused on the Mac (tf3_relay.py resume to re-enable)")
        if op in ACTION_OPS and not pol["allow_actions"]:
            raise RelayError(403, "policy_denied", "allow_actions is false in the Mac relay config")
        if op in SAVE_OPS and not pol["allow_save"]:
            raise RelayError(403, "policy_denied", "allow_save is false in the Mac relay config")
        if op in LUA_OPS and not pol["allow_lua"]:
            raise RelayError(403, "policy_denied", "allow_lua is false in the Mac relay config")

    def _run_op(self, op: str, args: dict, request_id: str, timeout_s: float) -> dict:
        res = self.channel.call(op, args, request_id, timeout_s)
        if not res.get("ok"):
            err = res.get("error") if isinstance(res.get("error"), dict) else {}
            raise RelayError(422, str(err.get("code") or "op_failed"),
                             str(err.get("message") or "the mod rejected the op"),
                             retryable=bool(err.get("retryable")))
        return res

    def call(self, body: Any) -> dict:
        if not isinstance(body, dict):
            raise RelayError(400, "invalid_request", "body must be a JSON object")
        op = body.get("op")
        args = body.get("args") or {}
        request_id = str(body.get("request_id") or uuid.uuid4().hex)
        if not isinstance(op, str) or not isinstance(args, dict):
            raise RelayError(400, "invalid_request", "op must be a string and args an object")
        if not (1 <= len(request_id) <= 64) or not all(c.isalnum() or c in "-_" for c in request_id):
            raise RelayError(400, "invalid_request", "request_id must be 1-64 chars of [A-Za-z0-9_-]")
        if len(json.dumps(args, ensure_ascii=True, separators=(",", ":"))) > MAX_ARGS_BYTES:
            raise RelayError(413, "args_too_large", f"args must serialize to at most {MAX_ARGS_BYTES} bytes")
        # The mod runs an action for real unless dry_run is exactly true, so a
        # "true" string would act. Refuse anything that is not a boolean.
        if args.get("dry_run") is not None and not isinstance(args["dry_run"], bool):
            raise RelayError(400, "invalid_request", "args.dry_run must be a boolean")
        try:
            timeout_s = float(body.get("timeout_s") or 30)
        except (TypeError, ValueError):
            raise RelayError(400, "invalid_request", "timeout_s must be a number")
        timeout_s = max(1.0, min(timeout_s, MAX_CALL_TIMEOUT_S))
        self.check_op(op)
        if not self.limiter.allow():
            raise RelayError(429, "rate_limited", "too many requests", retryable=True)
        started = time.monotonic()
        res = self._run_op(op, args, request_id, timeout_s)
        return {"ok": True, "request_id": request_id, "result": res.get("result"),
                "elapsed_ms": int((time.monotonic() - started) * 1000)}

    def screenshot(self, query: Dict[str, List[str]]) -> Tuple[bytes, int, int, str]:
        """Return (jpeg, width, height, source).

        The Mac config's screenshot_source is a ceiling: by default only the
        game's own renderer is used, so nothing else on the user's screen can
        leave the Mac. A request may narrow the source, never widen it.
        """
        pol = self.policy()
        if not pol["screenshot_enabled"]:
            raise RelayError(403, "policy_denied", "screenshots are disabled in the Mac relay config")
        ceiling = pol["screenshot_source"]
        source = (query.get("source") or [ceiling])[0]
        if source not in {"auto", "game", "screen"}:
            raise RelayError(400, "invalid_request", "source must be auto, game or screen")
        if source != "game" and ceiling == "game":
            raise RelayError(403, "policy_denied", "only in-game screenshots are allowed (screenshot_source: game)")
        if source == "screen" and ceiling == "auto":
            source = "auto"  # auto still prefers the game

        def _q(name: str, default: int, lo: int, hi: int) -> int:
            try:
                return max(lo, min(hi, int((query.get(name) or [default])[0])))
            except ValueError:
                return default

        max_width, quality = _q("max_width", 1600, 320, 3840), _q("quality", 70, 30, 95)
        with self._shot_lock:
            now = time.monotonic()
            if now - self._last_shot < SCREENSHOT_MIN_INTERVAL_S:
                raise RelayError(429, "rate_limited", "one screenshot every 2 s at most", retryable=True)
            self._last_shot = now
            if source in {"auto", "game"} and self.channel.connected():
                try:
                    return (*self._game_screenshot(max_width, quality), "game")
                except RelayError:
                    if source == "game":
                        raise
                    log.info("in-game screenshot failed; falling back to screencapture", exc_info=True)
            elif source == "game":
                raise RelayError(503, "mod_not_connected", "in-game screenshots need the mod running", retryable=True)
            return (*self.screen_fn(max_width=max_width, quality=quality,
                                    display=int(self.config.data.get("screenshot_display") or 1)), "screen")

    def _game_screenshot(self, max_width: int, quality: int) -> Tuple[bytes, int, int]:
        since = time.time() - 1.0
        self._run_op("take_screenshot", {"scale": 1}, uuid.uuid4().hex, 15.0)
        root = self.config.userdata_dir
        deadline = time.monotonic() + 15.0
        found: Optional[Path] = None
        last_size = -1
        while time.monotonic() < deadline:
            found = newest_image_since(root, since)
            if found is not None:
                size = found.stat().st_size
                if size > 0 and size == last_size:
                    break  # stable: the game finished writing it
                last_size = size
            self.sleep(0.25)
        if found is None:
            raise RelayError(504, "capture_failed", "the game did not write a screenshot file")
        try:
            return self.convert_fn(found, max_width=max_width, quality=quality)
        finally:
            if not self.config.data.get("keep_game_screenshots"):
                try:
                    found.unlink()
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

def make_handler(relay: Relay):
    # The tunnel's VPS-side port may differ from ours, so only the host part
    # is pinned. This blocks DNS-rebinding style requests from a browser.
    allowed_hosts = {"127.0.0.1", "localhost", "[::1]"}

    class Handler(BaseHTTPRequestHandler):
        server_version = "tf3-relay/" + RELAY_VERSION
        sys_version = ""
        timeout = SOCKET_TIMEOUT_S  # a stalled client must not pin a thread
        _nonce: Optional[str] = None

        def log_message(self, fmt: str, *args: Any) -> None:  # never log headers
            log.info("%s %s", self.address_string(), fmt % args)

        def _send(self, status: int, body: bytes, content_type: str, extra: Optional[Dict[str, str]] = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for name, value in (extra or {}).items():
                self.send_header(name, value)
            signature = relay.sign_response(self._nonce, status, body)
            if signature:
                self.send_header("X-TF3-Response-Signature", signature)
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, status: int, payload: dict) -> None:
            data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self._send(status, data, "application/json; charset=utf-8")

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").strip().lower()
            if not host.endswith("]"):
                host = host.rsplit(":", 1)[0]
            if host not in allowed_hosts:
                self._send_json(421, RelayError(421, "bad_host", "unexpected Host header").body())
                return False
            return True

        def _authenticate(self, body: bytes) -> bool:
            # Sign the 401 too when the caller sent a well-formed nonce, so a
            # plugin with the right token can tell "rejected" from "impostor".
            self._nonce = (self.headers.get("X-TF3-Nonce") or "").strip().lower() or None
            try:
                relay.verify_request(self.command, self.path, self.headers, body)
            except RelayError as exc:
                self._send_json(exc.status, exc.body())
                return False
            return True

        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            if not self._host_ok() or not self._authenticate(b""):
                return
            url = urlsplit(self.path)
            try:
                if url.path == "/v1/health":
                    self._send_json(200, relay.health())
                elif url.path == "/v1/screenshot":
                    data, width, height, source = relay.screenshot(parse_qs(url.query))
                    self._send(200, data, "image/jpeg", {"X-Image-Width": str(width),
                                                         "X-Image-Height": str(height),
                                                         "X-Image-Source": source})
                else:
                    self._send_json(404, RelayError(404, "not_found", "no such endpoint").body())
            except RelayError as exc:
                self._send_json(exc.status, exc.body())
            except Exception:
                log.exception("GET %s failed", url.path)
                self._send_json(500, RelayError(500, "internal", "relay error (see relay log)").body())

        def do_POST(self) -> None:  # noqa: N802
            if not self._host_ok():
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                length = -1
            if length <= 0 or length > MAX_BODY_BYTES:
                self._send_json(413, RelayError(413, "body_too_large",
                                                f"body must be 1..{MAX_BODY_BYTES} bytes").body())
                return
            raw = self.rfile.read(length)
            if not self._authenticate(raw):
                return
            url = urlsplit(self.path)
            try:
                if url.path != "/v1/call":
                    raise RelayError(404, "not_found", "no such endpoint")
                self._send_json(200, relay.call(parse_request_json(raw)))
            except RelayError as exc:
                self._send_json(exc.status, exc.body())
            except Exception:
                log.exception("POST %s failed", url.path)
                self._send_json(500, RelayError(500, "internal", "relay error (see relay log)").body())

    return Handler


class RelayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)

    def process_request(self, request: Any, client_address: Any) -> None:
        # Bound concurrent connections so idle sockets can not exhaust threads
        # or file descriptors; excess connections are closed straight away.
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def build_server(relay: Relay, port: Optional[int] = None) -> RelayServer:
    # Loopback only, by construction: there is no option to bind elsewhere.
    return RelayServer(("127.0.0.1", relay.config.port if port is None else port), make_handler(relay))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_init(args: argparse.Namespace) -> int:
    home = default_home()
    path = home / "config.json"
    existing: Dict[str, Any] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    cfg = Config(home, existing)
    if args.rotate_token or len(cfg.token) < 32:
        cfg.data["token"] = secrets.token_urlsafe(32)
    if args.userdata_dir:
        cfg.data["userdata_dir"] = str(Path(args.userdata_dir).expanduser())
    elif not cfg.data.get("userdata_dir"):
        candidates = candidate_userdata_dirs()
        if len(candidates) == 1:
            cfg.data["userdata_dir"] = str(candidates[0])
        else:
            print("Could not pick the TF3 userdata folder automatically. Candidates:" if candidates
                  else "No TF3 userdata folder found (start the game once first).")
            for c in candidates:
                print(f"  {c}")
            print("Re-run with --userdata-dir <path> (in game: Settings > Advanced > Open User Data Folder).")
            return 2
    if args.port:
        cfg.data["port"] = int(args.port)
    cfg.save()
    cfg.exchange_dir.mkdir(parents=True, exist_ok=True)
    print(f"config:      {cfg.path}")
    print(f"userdata:    {cfg.userdata_dir}")
    print(f"listen:      127.0.0.1:{cfg.port} (loopback only)")
    print("token:       run `tf3_relay.py token` to print it once for the VPS .env (TF3_BRIDGE_TOKEN)")
    return 0


def cmd_token(args: argparse.Namespace) -> int:
    print(Config.load().token)
    return 0


def cmd_pause(args: argparse.Namespace) -> int:
    cfg = Config.load()
    cfg.paused_flag.write_text(time.strftime("%Y-%m-%dT%H:%M:%S") + "\n", encoding="utf-8")
    print("paused: the relay and the mod now refuse every state-changing op, including ones already "
          "queued (reads and screenshots still work)")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    cfg = Config.load()
    try:
        cfg.paused_flag.unlink()
    except FileNotFoundError:
        pass
    print("resumed")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = Config.load()
    channel = ModChannel(cfg.exchange_dir)
    by_stamp = channel._scan()
    info: Dict[str, Any] = {"config": str(cfg.path), "exchange_dir": str(cfg.exchange_dir), "paused": cfg.is_paused(),
                            "port": cfg.port, "allow_actions": cfg.data.get("allow_actions"),
                            "allow_save": cfg.data.get("allow_save"), "allow_lua": cfg.data.get("allow_lua")}
    if by_stamp:
        stamp = max(by_stamp, key=lambda s: max(m for _, _, m in by_stamp[s]))
        k, path, mtime = max(by_stamp[stamp])
        age = time.time() - mtime
        outbox = parse_parts_file(path.read_bytes()) or {}
        info["mod"] = {"stamp": stamp, "outbox_age_s": round(age, 1), "fresh": age <= HEARTBEAT_STALE_S,
                       **{k2: outbox.get(k2) for k2 in ("mod_version", "status")}}
    else:
        info["mod"] = {"note": "no outbox yet: is TF3 running a save with the Hermes Bridge mod enabled?"}
    print(json.dumps(info, indent=2))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = Config.load()
    channel = ModChannel(cfg.exchange_dir)
    relay = Relay(cfg, channel)  # publishes the Mac policy to the mod through the channel
    channel.start()
    server = build_server(relay)
    log.info("tf3 relay %s listening on 127.0.0.1:%d, exchange folder %s", RELAY_VERSION, cfg.port, cfg.exchange_dir)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        channel.stop()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="tf3_relay.py", description="Hermes Bridge relay for Transport Fever 3")
    sub = parser.add_subparsers(dest="command", required=True)
    p_init = sub.add_parser("init", help="create/update config and token")
    p_init.add_argument("--userdata-dir", help="TF3 userdata folder (the one containing mods/ and mod_presets/)")
    p_init.add_argument("--port", type=int, help=f"loopback port (default {DEFAULT_PORT})")
    p_init.add_argument("--rotate-token", action="store_true", help="generate a new token")
    p_init.set_defaults(func=cmd_init)
    sub.add_parser("token", help="print the shared token (HMAC key)").set_defaults(func=cmd_token)
    p_serve = sub.add_parser("serve", help="run the relay")
    p_serve.add_argument("-v", "--verbose", action="store_true")
    p_serve.set_defaults(func=cmd_serve)
    sub.add_parser("status", help="show local relay and mod status").set_defaults(func=cmd_status)
    sub.add_parser("pause", help="kill switch: refuse state-changing ops").set_defaults(func=cmd_pause)
    sub.add_parser("resume", help="lift the kill switch").set_defaults(func=cmd_resume)
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
