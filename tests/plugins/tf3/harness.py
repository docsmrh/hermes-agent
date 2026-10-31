"""Test harness for the Hermes Bridge TF3 integration.

Loads the three components straight from ``integrations/transport-fever-3``:

* the Hermes plugin package (imported as ``hermes_plugins.tf3`` the same way
  the PluginManager imports user plugins),
* the Mac relay script (``tf3_relay.py``),
* the in-game mod Lua, run under a stock Lua 5.2 (lupa) against
  ``fake_game.lua``, a stand-in for the TF3 script runtime.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
import time
import types
from pathlib import Path
from typing import Any, Optional

REPO = Path(__file__).resolve().parents[3]
INTEGRATION = REPO / "integrations" / "transport-fever-3"
PLUGIN_DIR = INTEGRATION / "hermes-plugin" / "tf3"
RELAY_PATH = INTEGRATION / "tf3-mod" / "relay" / "tf3_relay.py"
MOD_DIR = INTEGRATION / "tf3-mod" / "hermes_bridge"
MOD_CONTENT = MOD_DIR / "content"
FAKE_GAME = Path(__file__).with_name("fake_game.lua")


def load_relay() -> types.ModuleType:
    name = "tf3_relay_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, RELAY_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def load_plugin() -> types.ModuleType:
    """Import the plugin as the PluginManager does (hermes_plugins.<slug>)."""
    if "hermes_plugins" not in sys.modules:
        ns = types.ModuleType("hermes_plugins")
        ns.__path__ = []
        sys.modules["hermes_plugins"] = ns
    name = "hermes_plugins.tf3"
    for key in [k for k in sys.modules if k == name or k.startswith(name + ".")]:
        del sys.modules[key]
    spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / "__init__.py",
                                                  submodule_search_locations=[str(PLUGIN_DIR)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class _NativeVector:
    """Python object exposed to Lua as userdata with :size() and :at(i),
    1-based like TF3's native vectors (community mods iterate them as
    ``for i = 1, v:size() do v:at(i) end``); out-of-range access raises.
    Lua's colon call passes the object itself as an extra first argument,
    hence the *args handling."""

    def __init__(self, items):
        self._items = list(items)

    def size(self, *_):
        return len(self._items)

    def at(self, *args):
        i = int(args[-1])
        if not 1 <= i <= len(self._items):
            raise IndexError(f"native vector index {i} out of range 1..{len(self._items)}")
        return self._items[i - 1]


class FakeGame:
    """The mod running inside a fake TF3 runtime, with real files on disk."""

    def __init__(self, userdata: Path):
        import lupa.lua52 as lua52  # optional test dependency

        self.userdata = userdata
        (userdata / "heightmaps").mkdir(parents=True, exist_ok=True)
        (userdata / "screenshots").mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.rt = lua52.LuaRuntime(unpack_returned_tuples=True)
        factory = self.rt.execute(FAKE_GAME.read_text(encoding="utf-8"))
        self.G = factory(str(userdata), lambda tbl: _NativeVector(tbl.values()))
        lua_globals = self.rt.globals()
        lua_globals.api = self.G.api
        lua_globals.app = self.G.app
        self.debug_log: list = []
        lua_globals.debugPrint = lambda *a: self.debug_log.append(" ".join(str(x) for x in a))
        lua_globals.getOwningModId = lambda: "hermes_bridge"
        content = str(MOD_CONTENT)
        self.rt.execute(
            """
            local content = ...
            function ug_require(ref)
              local path = ref:match('^hermes_bridge::(/.*)$')
              if path == nil then error('ug_require: not found ' .. ref) end
              local chunk = assert(loadfile(content .. path, 't', _ENV))
              return chunk()
            end
            """,
            content,
        )
        # A controllable wall clock (the mod reads os.time for its timeouts).
        self.rt.execute("FAKE_NOW = 1700000000; os.time = function() return FAKE_NOW end")
        self.load_script()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def load_script(self) -> None:
        """(Re-)evaluate the mod's entry script, as the game may do at any time."""
        script_src = (MOD_CONTENT / "hermes_bridge" / "hermes_bridge.script.lua").read_text(encoding="utf-8")
        with self.lock:
            self.rt.execute(script_src)
            self.script = self.rt.globals().data()
            self.G.script = self.script

    def advance_time(self, seconds: float) -> None:
        with self.lock:
            g = self.rt.globals()
            g.FAKE_NOW = g.FAKE_NOW + seconds

    # -- driving the game --------------------------------------------------

    def frame(self) -> None:
        with self.lock:
            self.G.frame()

    def step(self) -> None:
        with self.lock:
            self.G.step()

    def run(self, frames: int, *, step_every: int = 3) -> None:
        for i in range(frames):
            self.frame()
            if i % step_every == 0:
                self.step()

    def start(self, *, frame_s: float = 0.004) -> None:
        def loop():
            i = 0
            while not self._stop.is_set():
                self.frame()
                if i % 3 == 0:
                    self.step()
                i += 1
                time.sleep(frame_s)

        self._thread = threading.Thread(target=loop, name="fake-tf3", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    # -- inspection --------------------------------------------------------

    def log_kinds(self) -> list:
        with self.lock:
            return [self.G.log[i].kind for i in range(1, len(self.G.log) + 1)]

    def find_logged(self, kind: str) -> Any:
        with self.lock:
            for i in range(len(self.G.log), 0, -1):
                if self.G.log[i].kind == kind:
                    return self.G.log[i]
        return None


def lua_available() -> bool:
    try:
        import lupa.lua52  # noqa: F401
    except ImportError:
        return False
    return True
