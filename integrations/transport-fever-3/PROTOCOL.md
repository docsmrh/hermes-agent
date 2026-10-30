# Hermes Bridge wire protocol (v1)

There are three hops. Each has one owner on each side:

```
Hermes tf3 plugin ──A: HTTP──▶ tf3_relay.py ──B: userdata files──▶ hermes_bridge mod (GUI state)
     (VPS)        127.0.0.1:47800 on the VPS,       (Mac)       <userdata>/heightmaps/          │
                  carried by the SSH reverse tunnel                                    C: script event
                                                                                                ▼
                                                                                  mod (engine state)
```

## A. Plugin ↔ relay (HTTP/1.1, JSON)

- **Transport.** Plain HTTP on loopback. The SSH tunnel provides encryption and host authentication. The relay binds `127.0.0.1` only. The plugin refuses to send its token to a non-loopback URL unless `allow_remote_bridge: true` is set and the URL uses https.
- **Auth.** Every request carries `Authorization: Bearer <token>`. The relay compares tokens with `hmac.compare_digest`.
  - The token lives in the Mac relay config (mode 0600) and in the VPS Hermes `.env` as `TF3_BRIDGE_TOKEN`.
  - A missing or wrong token gets 401.
- **Host header.** The host part must be `127.0.0.1`, `localhost` or `[::1]`; anything else gets 421. The port may differ, because the tunnel's VPS-side port can be remapped.

### Error envelope

Every non-2xx JSON response, and a 2xx with `ok: false`, has this shape:

```json
{"ok": false, "error": {"code": "mod_not_connected", "message": "...", "retryable": true}}
```

| HTTP | code | Meaning |
|---|---|---|
| 400 | `invalid_request`, `unknown_op` | Malformed body, or an op that is not on the relay allowlist |
| 401 | `unauthorized` | Bad or missing token |
| 403 | `paused`, `policy_denied` | Mac kill switch, or a policy flag in the Mac config |
| 413 | `body_too_large` | The body is larger than 256 KiB |
| 422 | mod error code (`not_found`, `invalid_argument`, `command_failed`, `engine_timeout`, `api_unavailable`, `lua_error`, …) | The mod ran the op and reported a failure |
| 429 | `rate_limited`, `too_many_pending` | Retry later |
| 503 | `mod_not_connected` | No fresh heartbeat from the mod (the game is closed, in the menu, or the mod is not enabled) |
| 504 | `timeout` | The relay stopped waiting. The request stays queued for 120 s, so a retry with the same `request_id` gets the late result instead of running the op again |

### Endpoints

- **`GET /v1/health`** returns the relay version, `policy` (`paused`, `allow_actions`, `allow_save`, `allow_lua`, `screenshot_enabled`) and `mod` (`connected`, `heartbeat_age_s`, `stamp`, `mod_version`, `status`, `pending`).
- **`POST /v1/call`** takes `{"op": str, "args": {...}, "request_id": "[A-Za-z0-9_-]{1,64}", "timeout_s": 1-60}` and returns `{"ok": true, "request_id": ..., "result": {...}, "elapsed_ms": n}`.
  - `request_id` makes the call idempotent end to end. The relay caches the last 256 results, and the mod's engine side never executes the same id twice.
- **`GET /v1/screenshot?max_width=320-3840&quality=30-95&source=auto|game|screen`** returns `image/jpeg` with the headers `X-Image-Width`, `X-Image-Height` and `X-Image-Source`.
  - `game` asks the mod to call `api.gui.camera.takeScreenshot` and picks up the file it writes. This needs no macOS permission.
  - `screen` uses `screencapture`, which needs the Screen Recording permission.
  - `auto` tries `game` first and falls back to `screen`.

## B. Relay ↔ mod (userdata files)

The GUI Lua has no `io`, so the only way to exchange data is the official API: `app.saveUserdata(dir, name, table)`, `app.loadUserdata(dir, name)` and `app.removeUserdata(dir, name)`.

- **Folder.** Since build 40420, `saveUserdata` only accepts `heightmaps`, `mod_presets` and `towns_industries`. We use `heightmaps`: no in-game list shows Lua files there, whereas `mod_presets` files show up in the preset window.
- **Format.** Each file is `function data() return {...} end`. The game writes it, and the relay writes it with an atomic rename.
- **Payload.** Every payload is the table `{ v = 1, parts = { "<json>", ... } }`. The JSON text is split into strings of at most 60 000 characters and concatenated by the reader.

| File | Writer | Content |
|---|---|---|
| `hermes_bridge__in_<stamp>.lua` | Created empty by the mod; then rewritten only by the relay | `{"v":1, "session":"<relay session>", "relay_t":unix, "requests":[{"id","seq","op","args"}...]}` |
| `hermes_bridge__out_<stamp>_<k>.lua` | Only the mod, with a fresh name per write (`k` increases) | `{"v":1, "stamp", "session", "mod_version", "transport":"userdata", "frame", "status", "results":[{"id","seq","status":"pending"\|"done","ok","result"\|"error"}...]}` |

- **Stamp.** The mod picks `<stamp>` (digits) once per game load and keeps it in `guiState`, so a re-evaluated script reuses it. A new stamp means the game was reloaded. The relay then rewrites all pending requests into the new inbox.
- **Why the mod never rewrites a file.** It only ever writes fresh file names. That sidesteps two quirks reported on this build: `saveUserdata` not overwriting, and `loadUserdata` caching per name.
- **Why the mod creates its own empty inbox.** Loading a missing file logs a warning, and creating the inbox first means that never happens.
- **Relay duties:**
  - discover the newest stamp by mtime;
  - read the newest outbox;
  - delete consumed outboxes, other stamps' outboxes after 120 s, and stale inboxes after one day;
  - rewrite the inbox whenever the pending set changes, and every 5 s as a heartbeat.
- **Liveness.**
  - The mod writes a heartbeat outbox every 3 s while the inbox has changed within the last 30 s. Otherwise it stays silent: it writes no files while the relay is down.
  - The relay treats the mod as connected while the newest outbox is younger than 12 s.
- **Ordering.** The mod dispatches requests in `seq` order, at most 4 per poll. It polls every 6 frames while busy and every 15 frames when idle.
- **Pruning.** The mod drops results whose request is no longer in the inbox.

## C. Mod GUI state ↔ mod engine state

Reads, camera, screenshots, `save_game`, `lua_eval` and `set_speed` run in the GUI state. `set_speed` runs there so it can unpause the game, as the base game's own speed buttons do.

Every other state-changing op is forwarded to the simulation:

```lua
api.cmd.sendCommand(api.cmd.makeScriptingSendEventCmd("", "hermes_bridge", "hermes_bridge_op",
                                                      { key = <request id>, op = <op>, args = <args> }))
```

- **Engine side.** The engine `handleEvent` executes the op, unless `key` is already in its results, and records `{done, ok, result|error}` in the game-script state (`state:set`, bounded to 40 entries). The engine `update` subscribes to the event name.
- **GUI side.** The GUI reads the result back through `stateReadOnly:get()`, falling back to the `GAME_SCRIPT` component of `hermes_bridge::/hermes_bridge/hermes_bridge.gs`.
- **Paused game.** The simulation does not step while paused. A forwarded op that gets no result within 90 s fails with `engine_timeout`.

## Op table

| Op | Class | Args | Result |
|---|---|---|---|
| `hello`, `ping` | read | — | version / pong |
| `snapshot` | read | `sections` (`game`, `finance`, `lines`, `vehicles`, `stations`, `depots`, `towns`, `industries`, `problems`), `limit` 1-200 | One object per section |
| `get_entity` | read | `id` | `{exists, name, components, position?}` (never reads a dead entity) |
| `list_vehicle_models` | read | `contains?`, `metadata?`, `limit?` | `{models:[{file,id}], total}` |
| `selftest` | read | — | `{available, missing, line_stop_index_base}` |
| `camera_focus` | view | `entity_id` or `position:[x,y,z]` + `distance?` | `{focused}` |
| `take_screenshot` | view | `scale?` | `{requested}` (used by `/v1/screenshot`) |
| `set_speed` | action (GUI) | `speedup` 0-4 (0 = pause) | `{accepted, speedup}` |
| `line_create` | action | `name`, `stops:[station_id \| {station_id, terminal?} \| {station_group_id, station_index}]`, `color?` | `{accepted, line_id}` |
| `line_update` | action | `line_id`, `stops?`, `name?` | `{accepted}` |
| `line_delete` | destructive | `line_id` | `{accepted}` |
| `vehicle_buy` | spend | `depot_id`, `model` (file or id) or `models:[...]`, `line_id?` | `{accepted, vehicle_id, line_assigned?}` |
| `vehicle_set_line` | action | `vehicle_id`, `line_id`, `stop_index?` | `{accepted}` |
| `vehicle_sell` | destructive | `vehicle_id` | `{accepted}` |
| `vehicle_to_depot` | action | `vehicle_id`, `sell_on_arrival?` | `{accepted}` |
| `vehicle_stop`, `vehicle_start` | action | `vehicle_id` | `{accepted}` |
| `build_construction` | spend | `file`, `position`, `rotation?`, `params?`, `name?`, `ignore_errors?` | `{accepted, entity_ids}`; with `dry_run`, `{valid, errors, costs}` |
| `bulldoze` | destructive | `entity_id` (a construction) | `{accepted, entity_ids}` |
| `save_game` | save | `name` (`[A-Za-z0-9 _-]`, ≤64) | `{saved}` |
| `lua_eval` | lua | `code` | `{value, printed}` |

Every action op accepts `dry_run: true`. The mod then validates the arguments and builds the command without sending it; proposals are priced through `makeProposalData`.
