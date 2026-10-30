# Hermes Bridge for Transport Fever 3

This is a Transport Fever 3 mod that lets a Hermes agent running on a VPS observe, control and autonomously play the game on your Mac.

```
 VPS                                              Mac (Apple Silicon, TF3 running)
 ┌──────────────────────────┐   SSH reverse    ┌───────────────────────────────────────────┐
 │ Hermes                   │   tunnel (Mac    │ tf3_relay.py (LaunchAgent, 127.0.0.1)     │
 │  └ tf3 plugin (tools) ───┼─▶ dials out) ───▶│  └ policy · kill switch · screenshots     │
 │     127.0.0.1:47800      │                  │        │ <userdata>/heightmaps/*.lua      │
 └──────────────────────────┘                  │        ▼                                  │
                                               │ hermes_bridge mod (Lua, in the game)      │
                                               │  GUI state: reads, camera, screenshots    │
                                               │  engine state: lines, vehicles, building  │
                                               └───────────────────────────────────────────┘
```

| Part | Path | Runs on |
|---|---|---|
| The TF3 mod | `tf3-mod/hermes_bridge/` | Inside Transport Fever 3 |
| Relay (stdlib Python 3.9+, about 1 kLOC, no game logic) | `tf3-mod/relay/tf3_relay.py` | Mac, as a LaunchAgent |
| Mac installer (LaunchAgents and the tunnel key) | `tf3-mod/install_mac.sh` | Mac |
| Hermes plugin (tools, guardrails, CLI, autopilot skill) | `hermes-plugin/tf3/` | VPS |
| Wire contract | `PROTOCOL.md` | — |

**Why there is a relay.**
- TF3 mods have no network access. The GUI Lua has no file library either.
- The only way out of the game is the official `app.saveUserdata` / `app.loadUserdata` API, which works on a few userdata folders.
- The relay is the smallest piece that turns those files into an authenticated, loopback-only HTTP API.
- TF3's mod guidelines forbid executables inside a mod folder, so the relay is installed next to the mod, not inside it.

**Why this lives here and not in `plugins/`.** AGENTS.md (June 2026) keeps third-party-product integrations out of the bundled `plugins/` tree. This directory is self-contained. You install the plugin into `~/.hermes/plugins/`, and the whole folder can move to its own repository unchanged.

## What the agent gets

| Tool | Purpose |
|---|---|
| `tf3_observe` | Structured game state. Sections: game (date, speed, paused), finance, lines (stops, vehicles, load, last-year balance), vehicles, stations, depots, towns, industries, problems. Can also return one entity, a vehicle-model search, or a `selftest` of the game's API surface. |
| `tf3_screenshot` | The game renders a screenshot itself, optionally after pointing the camera at an entity or position. It is returned to vision-capable models as an image. |
| `tf3_act` | Typed actions: `set_speed` (0 pauses), `line_create`, `line_update`, `line_delete`, `vehicle_buy`, `vehicle_set_line`, `vehicle_sell`, `vehicle_to_depot`, `vehicle_stop`, `vehicle_start`, `build_construction`, `bulldoze` and `save_game`. Every action supports `dry_run`; construction dry runs return the game's own validation and cost. |
| `tf3_lua` | An escape hatch that runs Lua inside the game. It is off unless you enable it on both sides. |
| `tf3_journal` | A persistent plan, notes and action log that carry over between autopilot runs. |

The skill `tf3:autopilot` describes one bounded turn: read the journal, observe, triage problems, grow from a cash buffer, act, verify, and record.

## Install

You need:
- an Apple Silicon Mac with Transport Fever 3 (Steam, GOG or Epic);
- Python 3.9+ on the Mac (`xcode-select --install` provides `/usr/bin/python3`);
- a VPS running Hermes, reachable by SSH from the Mac.

### 1. Mac

```bash
git clone https://github.com/docsmrh/hermes-agent && cd hermes-agent/integrations/transport-fever-3/tf3-mod
./install_mac.sh --vps tunnel@your-vps.example.com --dry-run   # preview every step
./install_mac.sh --vps tunnel@your-vps.example.com
```

The installer:
- copies the mod into `<TF3 userdata>/mods/hermes_bridge`;
- installs the relay into `~/Library/Application Support/hermes-tf3-bridge/` and generates its token;
- creates a tunnel-only SSH key;
- installs the `com.hermes.tf3-relay` and `com.hermes.tf3-tunnel` LaunchAgents;
- prints the remaining VPS steps.

It looks for the userdata folder automatically. If it finds none, or more than one, pass `--userdata-dir`; the in-game menu Settings → Advanced → Open User Data Folder shows the right one.

### 2. VPS

1. Use a dedicated low-privilege user for the tunnel. Add the line the installer printed to that user's `~/.ssh/authorized_keys`:

   ```
   restrict,port-forwarding,permitlisten="127.0.0.1:47800",permitopen="127.0.0.1:1",command="/bin/false" ssh-ed25519 AAAA… hermes-tf3-tunnel
   ```

   This line was tested against OpenSSH 9.9 `sshd`. It allows only the reverse tunnel onto `127.0.0.1:47800`. It refuses other ports, wildcard binds, local forwards and shells.

2. Install and enable the plugin:

   ```bash
   ln -s /path/to/hermes-agent/integrations/transport-fever-3/hermes-plugin/tf3 ~/.hermes/plugins/tf3
   # or: hermes plugins install docsmrh/hermes-agent/integrations/transport-fever-3/hermes-plugin/tf3 --ref <commit-sha>
   hermes plugins enable tf3
   ```

3. Put the token in the Hermes `.env`: `TF3_BRIDGE_TOKEN=<value of "tf3_relay.py token" on the Mac>`.

4. On the Mac, accept the VPS host key once, then start the tunnel. The installer prints both commands.

### 3. In the game

Enable **Hermes Bridge** in the mod list when you start or load a game; mods are enabled per savegame. Then, on the VPS:

```bash
hermes tf3 status          # relay, tunnel and mod heartbeat
```

Ask the agent to run `tf3_observe` with `selftest: true` once. It lists the game API entry points your TF3 build provides.

## Autonomous play

```bash
hermes tf3 autopilot       # prints the exact command, e.g.:
hermes cron create "every 15m" "Play one Transport Fever 3 autopilot turn: ..." \
    --name tf3-autopilot --skill tf3:autopilot --deliver local
hermes cron pause tf3-autopilot   # stop the loop
```

- Each run is one Hermes cron session, capped by the cron hard time limit and by `max_actions_per_session`.
- Continuity comes from `tf3_journal`, because cron sessions skip memory.
- Keep the Mac awake, with the game running and unpaused. The simulation does not run commands while the game is paused, but `set_speed` can unpause it.

## Configuration

**VPS** (`config.yaml` → `plugins.entries.tf3.settings`):

| Key | Default | Meaning |
|---|---|---|
| `bridge_url` | `http://127.0.0.1:47800` | The VPS end of the tunnel |
| `request_timeout_s` | 30 | How long to wait per call |
| `readonly` | false | Refuse every action |
| `max_actions_per_session` | 40 | Cap on mutating calls per session or cron run (dry runs are free) |
| `min_cash_reserve` | 0 (off) | Refuse spending actions while the balance is below this |
| `blocked_actions` | `[]` | For example `["bulldoze", "vehicle_sell", "line_delete"]` |
| `enable_lua_tool` | false | Expose `tf3_lua`. The Mac must also allow it. |
| `screenshot_max_width` | 1600 | Downscale target |
| `allow_remote_bridge` | false | Allow a non-loopback `bridge_url` (https only) |

**Mac** (`~/Library/Application Support/hermes-tf3-bridge/config.json`, mode 0600):
- `allow_actions` (true), `allow_save` (true), `allow_lua` (false);
- `screenshot_enabled` (true), `screenshot_source` (`auto` | `game` | `screen`), `keep_game_screenshots` (false);
- `rate_limit_per_s` (10), `port` (47800), `userdata_dir`.

**Kill switch.** `tf3_relay.py pause` makes the relay refuse every state-changing op; reads and screenshots keep working. Undo it with `tf3_relay.py resume`.

## Security model

- **Trust boundary.** The Mac is the authority. The relay enforces the policy flags and the kill switch whatever the VPS asks. A compromised VPS can do at most what the relay allows, and by default that excludes running Lua.
- **Network exposure.** No listening port on the Mac's network interfaces. The relay binds `127.0.0.1`, and the Mac dials out to the VPS.
- **Tunnel key.** The tunnel key can only create that one loopback forward on the VPS.
- **Auth.** A bearer token checked in constant time. The relay pins the Host header (which defeats DNS rebinding) and limits bodies (256 KiB), request ids, request rate and pending requests.
  - The plugin never sends the token to a non-loopback host unless you explicitly allow it, and then only over https.
  - Tokens are never printed by `init` or written to logs.
  - To rotate the token, run `tf3_relay.py init --rotate-token` and update the VPS `.env`.
- **Op allowlist.** Ops not on the allowlist never reach the game.
- **`lua_eval` is code execution inside the game.** Game Lua can call `app.*`, which can save, load or delete games and quit TF3. Keep it off unless you need it. `tf3_act` covers normal play.
- **Exchange-folder trust.** The game executes the exchange files as Lua when it loads them. Anything that can write to your TF3 userdata folder can therefore already run Lua in the game. The relay only ever writes a table of plain strings.
- **Engine-crash guard.** `getComponent` on a deleted entity crashes the game (a fatal engine assertion, not a Lua error), and entity ids are reused. The mod checks `entityExists` before every read.

## Verification

**Automated** (`scripts/run_tests.sh tests/plugins/tf3/`): 79 tests.
- The real mod Lua runs under Lua 5.2 (`lupa`, optional; those tests are skipped without it). It runs against `tests/plugins/tf3/fake_game.lua`, a stand-in for the TF3 script runtime with real userdata files and a crash-strict `getComponent`.
- The real relay and the real plugin run unchanged.
- The end-to-end tests drive Hermes tools → HTTP → relay → files → mod → simulated engine and back.
- The plugin is also loaded through the real `PluginManager`.

**Not verifiable without the game.**
- Live TF3 behaviour is untested. The API calls follow the official script reference and the working community mods listed below.
- Ops marked EXPERIMENTAL in `ops.lua` (`vehicle_buy` config assembly, `build_construction`, `bulldoze`) should be tried with `dry_run` first.

**First run on your Mac:**
1. `hermes tf3 status` shows `mod.connected: true`.
2. `tf3_observe` with `selftest: true` shows no important APIs under `missing`, and `line_stop_index_base: 0`.
3. Run `tf3_act set_speed {speedup: 0}`, then `1`: the game pauses and resumes.
4. Do a `line_create` `dry_run`, then a real `line_create`, between two existing stations; confirm it in the game's line manager.
5. `tf3_screenshot` returns an image with `source: game`.

## Known limits

- **No typed track or road laying.** TF3 exposes it only through low-level street proposals that are not documented end to end. Stations and depots work through `build_construction`. Use `tf3_lua` (opt-in) for edges if you need them.
- **Loans have no command API.** The base game drives them through script events whose names are undocumented.
- **Large maps.** Snapshots run in the GUI frame, so keep `limit` modest to avoid hitches.
- **macOS specifics.** `screencapture` fallback screenshots need the Screen Recording permission for `/usr/bin/python3`. In-game screenshots do not.

## Credits

This is a clean-room implementation. API facts were cross-checked against the official TF3 script reference (wiki.transportfever3.com), the fan reference tf3-docs.transportfever.net, and these community projects:

| Project | License | How it was used |
|---|---|---|
| bsaurusrex/tf3-network-data-bridge | MIT | Facts only; no code copied |
| nicanthomas/TF3-capocantiere | MIT | Facts only; no code copied |
| M1r077/tf3-dashboard | GPL-3.0 | Facts only; no code copied |
| sweetgiorni/tf3-lua-bridge | GPL-3.0 | Facts only; no code copied |
