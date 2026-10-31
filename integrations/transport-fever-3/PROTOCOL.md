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

- **Transport.** Plain HTTP on loopback. The SSH tunnel provides encryption and host authentication. The relay binds `127.0.0.1` only. The plugin ignores proxy environment variables and refuses a non-loopback URL unless `allow_remote_bridge: true` is set and the URL uses https.
- **Host header.** The host part must be `127.0.0.1`, `localhost` or `[::1]`; anything else gets 421. The port may differ, because the tunnel's VPS-side port can be remapped.

### Authentication

The token (`TF3_BRIDGE_TOKEN`) is an HMAC-SHA256 key shared by the Mac relay config (mode 0600) and the VPS Hermes `.env`. It is never sent.

- **Request headers.** Every request carries:
  - `X-TF3-Timestamp`: unix seconds;
  - `X-TF3-Nonce`: 32 to 64 lowercase hex characters, fresh per request;
  - `X-TF3-Signature`: `hex(HMAC(token, "tf3-v1\n" + METHOD + "\n" + TARGET + "\n" + TIMESTAMP + "\n" + NONCE + "\n" + hex(sha256(BODY))))`. TARGET is the path plus query exactly as sent, e.g. `/v1/screenshot?max_width=800&quality=70`. BODY is the raw request bytes, empty for GET.
- **Relay checks.** The relay verifies the signature with `hmac.compare_digest`. It then refuses:
  - a timestamp more than 60 s away from its clock;
  - a nonce already seen in the last 120 s.

  A failure gets 401, with code `unauthorized`, `stale_request` or `replayed_request`.
- **Response signature.** Every response, including errors, carries `X-TF3-Response-Signature`: `hex(HMAC(token, "tf3-v1-resp\n" + NONCE + "\n" + STATUS + "\n" + hex(sha256(RESPONSE_BODY))))`.
  - The plugin rejects any response without a valid one as `relay_unauthenticated`. That happens with a wrong token, or with something other than the relay listening on the port while the tunnel is down.
  - Such an impostor learns no reusable secret. A captured request can not be replayed after 60 s, nor twice.
- **Config changes.** The relay re-reads `config.json` when it changes. A rotated token or a policy edit applies within a second.

### Error envelope

Every non-2xx JSON response, and a 2xx with `ok: false`, has this shape:

```json
{"ok": false, "error": {"code": "mod_not_connected", "message": "...", "retryable": true}}
```

| HTTP | code | Meaning |
|---|---|---|
| 400 | `invalid_request`, `unknown_op` | Malformed body (including NaN/Infinity, or nesting deeper than 32 levels), or an op that is not on the relay allowlist |
| 401 | `unauthorized`, `stale_request`, `replayed_request` | Request signature failed (see Authentication) |
| 403 | `paused`, `policy_denied` | Mac kill switch, or a policy flag in the Mac config |
| 413 | `body_too_large`, `args_too_large` | The body is larger than 256 KiB, or one request's args serialize to more than 32 KiB |
| 422 | mod error code (`not_found`, `invalid_argument`, `command_failed`, `engine_timeout`, `game_paused`, `game_reloaded`, `stale_stamp`, `api_unavailable`, `lua_error`, …) | The mod (or relay channel) reported a failure for this op |
| 429 | `rate_limited`, `too_many_pending`, `too_much_pending` | Retry later. The limits are 10 req/s, 1 screenshot per 2 s, 32 pending requests and 512 KiB of pending args |
| 503 | `mod_not_connected`, `relay_io_error` | No fresh heartbeat from the mod, or the exchange file could not be written. Either way the request was **not** queued |
| 504 | `timeout` | The relay stopped waiting. The request stays queued for 120 s, so a retry with the same `request_id` gets the late result instead of running the op again |

The plugin reports what happened to each action:

| Outcome | Errors | Meaning |
|---|---|---|
| (none) | — | Success |
| `outcome: not_done` | Refusals, validation errors, `engine_timeout`, `game_paused` | Nothing changed in the game |
| `outcome: unknown` | `timeout`, `transport_error`, `game_reloaded` | The action may have happened, so re-observe before retrying |

Before reporting `unknown`, the plugin re-asks with the same `request_id` for up to 110 s, which settles almost every timeout.

### Endpoints

- **`GET /v1/health`** returns the relay version, `policy` (`paused`, `allow_actions`, `allow_save`, `allow_lua`, `screenshot_enabled`, `screenshot_source`) and `mod` (`connected`, `heartbeat_age_s`, `stamp`, `mod_version`, `status`, `pending`).
- **`POST /v1/call`** takes `{"op": str, "args": {...}, "request_id": "[A-Za-z0-9_-]{1,64}", "timeout_s": 1-60}` and returns `{"ok": true, "request_id": ..., "result": {...}, "elapsed_ms": n}`.
  - `request_id` makes the call idempotent end to end. The relay caches the last 256 results, and the mod's engine side never executes the same id twice.
- **`GET /v1/screenshot?max_width=320-3840&quality=30-95&source=game|auto|screen`** returns `image/jpeg` with the headers `X-Image-Width`, `X-Image-Height` and `X-Image-Source`.
  - The Mac config's `screenshot_source` is a ceiling; a request may only narrow it.
  - `game` (the default) asks the mod to call `api.gui.camera.takeScreenshot` and picks up the file it writes. It needs no macOS permission and shows nothing but the game.
  - `auto` tries `game` and falls back to a capture of the whole display.
  - `screen` always captures the display, which needs the Screen Recording permission.
  - `take_screenshot` is an internal op; `/v1/call` refuses it.

## B. Relay ↔ mod (userdata files)

The GUI Lua has no `io`, so the only way to exchange data is the official API: `app.saveUserdata(dir, name, table)`, `app.loadUserdata(dir, name)` and `app.removeUserdata(dir, name)`.

- **Folder.** Since build 40420, `saveUserdata` only accepts `heightmaps`, `mod_presets` and `towns_industries`. We use `heightmaps`: no in-game list shows Lua files there, whereas `mod_presets` files show up in the preset window.
- **Format.** Each file is `function data() return {...} end`. The game writes it, and the relay writes it with an atomic rename.
- **Payload.** Every payload is the table `{ v = 1, parts = { "<json>", ... } }`. The JSON text is split into strings of at most 60 000 characters and concatenated by the reader. The relay decodes it as UTF-8 and replaces bad bytes rather than failing.

| File | Writer | Content |
|---|---|---|
| `hermes_bridge__in_<stamp>.lua` | Created empty by the mod; then rewritten only by the relay | `{"v":1, "session":"<relay session>", "relay_t":unix, "policy":{"paused","allow_actions","allow_save","allow_lua"}, "requests":[{"id","seq","op","args","stamp"}...]}` |
| `hermes_bridge__out_<stamp>_<k>.lua` | Only the mod, with a fresh name per write (`k` increases) | `{"v":1, "stamp", "session", "mod_version", "transport":"userdata", "frame", "status", "results":[{"id","seq","status":"pending"\|"done","ok","result"\|"error"}...]}` |

- **Stamp.** The mod picks a fresh `<stamp>` (digits) every time its GUI Lua state initialises, which in practice means every game load. A re-evaluated script keeps its state, and so its stamp, through `package.loaded`.
- **Stamp change.** A new stamp means a new game load. The relay then:
  - clears the old inbox;
  - fails pending state-changing requests with `game_reloaded` (outcome unknown) rather than running them against the new save, where entity ids mean something else;
  - re-sends pending reads.
- **Per-request stamp.** Each request names the stamp it was queued for, and the mod refuses others (`stale_stamp`).
- **Policy.** The relay publishes the policy in every inbox write, and immediately when it changes, for example on `tf3_relay.py pause`.
  - The mod refuses state-changing requests the policy does not allow (`paused` / `policy_denied`), so requests queued before a pause do not run.
  - Without a policy the mod allows reads only.
- **Why the mod never rewrites a file.** It only ever writes fresh file names. That sidesteps two quirks reported on this build: `saveUserdata` not overwriting, and `loadUserdata` caching per name.
- **Why the mod creates its own empty inbox.** Loading a missing file logs a warning, and creating the inbox first means that never happens.
- **Relay duties:**
  - follow the newest outbox (by mtime, then `k`);
  - delete consumed outboxes, other stamps' outboxes after 120 s, and stale inboxes after one day;
  - rewrite the inbox whenever the pending set or the policy changes, and every 5 s as a heartbeat.
- **Liveness.**
  - The mod writes a heartbeat outbox every 3 s while the inbox has changed within the last 30 s. Otherwise it stays silent: it writes no files while the relay is down.
  - The relay treats the mod as connected while the newest outbox is younger than 12 s. An oversized outbox (> 8 MiB) still counts as a sign of life.
- **Ordering and budget.**
  - The mod dispatches requests in `seq` order, at most 4 per poll.
  - It holds new work while more than 2 MiB of results wait for the relay.
  - It polls every 6 frames while busy and every 15 frames when idle.
  - It decodes the inbox only when the inbox changes, and ignores an inbox over 1 MiB.
- **Pruning.** The mod drops results whose request is no longer in the inbox.
- **Arguments.** JSON `null` object members are treated as absent.

## C. Mod GUI state ↔ mod engine state

Reads, camera, screenshots, `save_game`, `lua_eval` and `set_speed` run in the GUI state. `set_speed` runs there because pausing must not wait for a simulation step.

Every other state-changing op is forwarded to the simulation:

```lua
api.cmd.sendCommand(api.cmd.makeScriptingSendEventCmd("", "hermes_bridge", "hermes_bridge_op",
                                                      { key = <request id>, op = <op>, args = <args>,
                                                        expires_at = os.time() + 75 }))
```

- **Engine side.** The engine `handleEvent` executes the op unless `key` is already in its results or `expires_at` has passed, and records `{done, ok, result|error}` in the game-script state (`state:set`, bounded to 40 entries). The engine `update` subscribes to the event name.
- **GUI side.** The GUI reads the result back through `stateReadOnly:get()`, falling back to the `GAME_SCRIPT` component of `hermes_bridge::/hermes_bridge/hermes_bridge.gs`.
- **Paused game.** The simulation does not step while paused. The mod therefore refuses engine ops with `game_paused` while `GAME_SPEED.speedup` is 0. Resuming is the player's call; the plugin refuses `set_speed > 0` on a player-paused game unless `allow_unpause` is set.
- **Deadline.**
  - A forwarded op whose deadline passes without a result fails with `engine_timeout`, 80 s after it was sent.
  - This is definitive. If the queued command reaches the simulation later, the engine refuses it as `expired`, so nothing is ever done after the agent was told it failed.

## Op table

| Op | Class | Args | Result |
|---|---|---|---|
| `hello`, `ping` | read | — | version / pong |
| `snapshot` | read | `sections` (`game`, `finance`, `lines`, `vehicles`, `stations`, `depots`, `towns`, `industries`, `problems`), `limit` 1-200 | One object per section |
| `get_entity` | read | `id` | `{exists, name, components, position?}` (never reads a dead entity) |
| `list_vehicle_models` | read | `contains?`, `available_only?`, `metadata?`, `limit?` | `{models:[{file, id, year_from?, year_to?, price?, available?}], total, year}` |
| `selftest` | read | — | `{available, missing, line_stop_index_base}` |
| `camera_focus` | view | `entity_id` (must exist) or `position:[x,y,z]` + `distance?` | `{focused}` |
| `take_screenshot` | internal | `scale?` (1-2) | `{requested}` (issued by `/v1/screenshot` only) |
| `set_speed` | action (GUI) | `speedup` 0-4 (0 = pause) | `{accepted, speedup}` |
| `line_create` | action | `name`, `stops:[station_id \| {station_id, terminal?} \| {station_group_id, station_index, terminal?}]`, `color?` | `{accepted, line_id}` |
| `line_update` | action | `line_id`, `stops?`, `name?` | `{accepted}` |
| `line_delete` | destructive | `line_id` | `{accepted}` |
| `vehicle_buy` | spend | `depot_id` (a depot, or a construction that contains one), `model` (file or id) or `models:[...]`, `line_id?` | `{accepted, vehicle_id, line_assigned?}`; with `dry_run`, `{valid, cost}` |
| `vehicle_set_line` | action | `vehicle_id`, `line_id`, `stop_index?` | `{accepted}` |
| `vehicle_sell` | destructive | `vehicle_id` | `{accepted}` |
| `vehicle_to_depot` | action (`destructive` with `sell_on_arrival`) | `vehicle_id`, `sell_on_arrival?` | `{accepted}` |
| `vehicle_stop`, `vehicle_start` | action | `vehicle_id` | `{accepted}` |
| `build_construction` | spend | `file`, `position`, `rotation?`, `params?`, `name?`, `ignore_errors?` | `{accepted, entity_ids, depot_ids, station_ids}`; with `dry_run`, `{valid, errors, cost, costs}` |
| `bulldoze` | destructive | `entity_id` (a construction) | `{accepted, entity_ids}` |
| `save_game` | save | `name` (`[A-Za-z0-9_-]`, ≤48; saved as `hermes-<name>`) | `{saved}` |
| `lua_eval` | lua | `code` | `{value, printed}` |

- **Dry runs.** Every action op accepts `dry_run: true`. The mod then validates the arguments and builds the command without sending it; proposals are priced through `makeProposalData`, vehicles from their model metadata.
- **Validation.** Stops are checked against live entities: the group exists, `station_index` and `terminal` are in range, and `stop_index` falls within the line. Stale indices are a reported native crash in the game's line code.
- **Native engine lists** are read as `v:at(1..v:size())` and are never iterated with `ipairs` or `pairs`, which can hang the game.
- **Engine maps**, such as per-cargo line usage, are read with `pairs`.
- **Enum values** are mapped back to names by indexing known member names into `api.type.enum.<Type>`.
