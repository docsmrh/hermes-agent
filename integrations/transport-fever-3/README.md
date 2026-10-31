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
| VPS installer (tunnel account, sshd lockdown, plugin, token) | `hermes-plugin/install_vps.sh` | VPS |
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
| `tf3_screenshot` | The game renders a screenshot itself, optionally after pointing the camera at an entity or position. It is returned to vision-capable models as an image. By default nothing else on your screen can be captured. |
| `tf3_act` | Typed actions: `set_speed` (0 pauses), `line_create`, `line_update`, `line_delete`, `vehicle_buy`, `vehicle_set_line`, `vehicle_sell`, `vehicle_to_depot`, `vehicle_stop`, `vehicle_start`, `build_construction`, `bulldoze` and `save_game` (always saved as `hermes-<name>`, so your own saves are never overwritten). Every action supports `dry_run`; dry runs of `vehicle_buy` and `build_construction` report the cost. A result is either done, `outcome: not_done`, or `outcome: unknown` (re-observe before retrying). |
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

`hermes-plugin/install_vps.sh` does the VPS side in one command. Run it as an account that can `sudo`. The tunnel gets its own account (`tunnel` by default), created with no shell and no password. Never use an account you or Hermes log in with: the `Match` block turns off shell logins for it, and the script refuses root, the Hermes account, and any existing account that has a login shell.

```bash
# On the Mac: copy the installer, the plugin and the tunnel key to the VPS.
ssh you@your-vps.example.com 'mkdir -p hermes-tf3'
scp -r hermes-agent/integrations/transport-fever-3/hermes-plugin ~/.ssh/hermes_tf3_tunnel.pub you@your-vps.example.com:hermes-tf3/

# On the VPS. It asks for the token: print it on the Mac with tf3_relay.py token.
sudo bash hermes-tf3/hermes-plugin/install_vps.sh --pubkey-file hermes-tf3/hermes_tf3_tunnel.pub --dry-run   # preview
sudo bash hermes-tf3/hermes-plugin/install_vps.sh --pubkey-file hermes-tf3/hermes_tf3_tunnel.pub
hermes gateway restart
```

Add `--hermes-user NAME` if Hermes runs under an account other than the one that ran `sudo`, and `--port` if you changed it on the Mac. The script:

1. creates the tunnel account;
2. appends this marked block to `/etc/ssh/sshd_config`, after a backup. It checks the result with `sshd -t` and the tunnel account's effective policy with `sshd -T`, then reloads sshd. If any step fails, sshd keeps the old configuration. The closing `Match all` keeps settings appended to the file later from applying only to the tunnel account:

   ```
   Match User tunnel
       AllowStreamLocalForwarding no
       AllowTcpForwarding remote
       PermitListen 127.0.0.1:47800
       PermitOpen none
       AllowAgentForwarding no
       X11Forwarding no
       PermitTTY no
       ForceCommand /bin/false
   Match all
   ```

3. adds the Mac's key to the tunnel account's `~/.ssh/authorized_keys`, after the block is in place:

   ```
   restrict,port-forwarding,permitlisten="127.0.0.1:47800",permitopen="127.0.0.1:1",command="/bin/false" ssh-ed25519 AAAA… hermes-tf3-tunnel
   ```

4. copies the plugin into `~/.hermes/plugins/tf3` and runs `hermes plugins enable tf3`;
5. stores `TF3_BRIDGE_TOKEN` in `~/.hermes/.env` (mode 0600). It reads the token from a hidden prompt or `--token-file`, never from the command line. The token is an HMAC key and is never sent over the tunnel.

The key line and the block together were tested against OpenSSH 9.9 `sshd`. They allow only the reverse tunnel onto `127.0.0.1:47800`: other ports, wildcard binds, local forwards, unix-socket forwards and shells are refused. The key line alone does **not** stop unix-socket forwards, so the `Match` block is required. `--uninstall` reverses steps 2–5 and leaves the account (`sudo userdel -r tunnel` removes it).

Then, on the Mac, accept the VPS host key once and start the tunnel. The Mac installer prints both commands.

### 3. In the game

Restart Transport Fever 3 after installing or updating the mod; the game only picks up new mod files on a full restart. Then enable **Hermes Bridge** in the mod list when you start or load a game; mods are enabled per savegame. Then, on the VPS:

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

- Each run is one Hermes cron session, capped by the cron hard time limit and by `max_actions_per_session`. The budget counts the whole run, including compressed continuations and delegated subagents, and survives a gateway restart.
- Continuity comes from `tf3_journal`, because cron sessions skip memory.
- Keep the Mac awake, with the game running and unpaused. While the game is paused, actions are refused instead of queued. The agent does not resume a game you paused unless you set `allow_unpause: true`; it may resume a pause it made itself if the game has not moved since.

## Configuration

**VPS** (`config.yaml` → `plugins.entries.tf3.settings`):

| Key | Default | Meaning |
|---|---|---|
| `bridge_url` | `http://127.0.0.1:47800` | The VPS end of the tunnel |
| `request_timeout_s` | 30 | How long to wait per call |
| `readonly` | false | Refuse every action |
| `max_actions_per_session` | 40 | Cap on mutating calls per run: one conversation or cron run, including its compressed continuations and subagents (dry runs are free) |
| `min_cash_reserve` | 0 (off) | Refuse a spending action if its cost (from a dry run) would take the balance below this, or if the cost is unknown |
| `blocked_actions` | `[]` | For example `["bulldoze", "vehicle_sell", "line_delete"]`. Blocking `vehicle_sell` also blocks `vehicle_to_depot` with `sell_on_arrival`. |
| `allow_unpause` | false | Let the agent resume a game the player paused |
| `enable_lua_tool` | false | Expose `tf3_lua`. The Mac must also allow it. |
| `screenshot_max_width` | 1600 | Downscale target |
| `screenshot_source` | unset | Optionally narrow the screenshot source (`game`). It can never exceed the Mac's setting. |
| `allow_remote_bridge` | false | Allow a non-loopback `bridge_url` (https only) |

**Mac** (`~/Library/Application Support/hermes-tf3-bridge/config.json`, mode 0600). Edits take effect within a second; there is no need to restart the relay. Changing `port` or `userdata_dir` is the exception and does need a restart.
- `allow_actions` (true), `allow_save` (true), `allow_lua` (false);
- `screenshot_enabled` (true), `keep_game_screenshots` (false);
- `screenshot_source`:
  - `game` (default): only the game's own renderer, so nothing else on your screen is ever captured;
  - `auto`: the game, else a capture of the whole display;
  - `screen`: always the whole display.

  The VPS can only narrow this, never widen it.
- `rate_limit_per_s` (10), `port` (47800), `userdata_dir`.

**Kill switch.** `tf3_relay.py pause` makes the relay refuse every state-changing op, and the mod refuses any already queued. Reads and screenshots keep working. Undo it with `tf3_relay.py resume`.

## Security model

- **Trust boundary.** The Mac is the authority. The relay enforces the policy flags and the kill switch whatever the VPS asks. A compromised VPS can do at most what the relay allows, and by default that excludes running Lua.
- **Network exposure.** No listening port on the Mac's network interfaces. The relay binds `127.0.0.1`, and the Mac dials out to the VPS.
- **Tunnel key.** With the `Match` block, the tunnel account can only create that one loopback forward on the VPS.
- **Auth.** The shared token is an HMAC-SHA256 key and is never sent over the wire.
  - Every request is signed over method, path, timestamp, nonce and body hash. Requests outside a 60 s window and replayed nonces are refused.
  - Every response is signed back, except the two refusals sent before a request can be authenticated (wrong Host header, body over 256 KiB). If the tunnel is down and another local user on the VPS listens on `127.0.0.1:47800`, that user learns nothing reusable, and the plugin reports `relay_unauthenticated` instead of trusting the answers.
  - The plugin ignores `HTTP(S)_PROXY`.
  - The relay pins the Host header (which defeats DNS rebinding). It limits:
    - bodies (256 KiB), args per request (32 KiB) and total pending args (512 KiB);
    - JSON nesting (32 levels) and numbers (NaN, Infinity and values that overflow a double are refused);
    - request ids, request rate, screenshot rate and pending requests;
    - concurrent connections, with a 15 s socket timeout.
  - The token is never printed by `init` or written to logs. To rotate it, run `tf3_relay.py init --rotate-token` (it takes effect immediately) and update the VPS `.env`.
- **Op allowlist and policy.** Ops not on the allowlist never reach the game. The relay publishes its policy to the mod, which re-checks every request and refuses anything without a policy.
- **No cross-game replay.** Each request carries the stamp of the game load it was queued for. On a reload, pending actions fail with `game_reloaded` instead of running against the new save.
- **No late surprises.** A forwarded command has a 75 s deadline. If the simulation reaches it later, the command is refused, so `engine_timeout` means nothing was done.
- **`lua_eval` is code execution inside the game.** Game Lua can call `app.*`, which can save, load or delete games and quit TF3. Keep it off unless you need it. `tf3_act` covers normal play.
- **Exchange-folder trust.** The game executes the exchange files as Lua when it loads them. Anything that can write to your TF3 userdata folder can therefore already run Lua in the game. The relay only ever writes a table of plain strings.
- **Engine-crash guard.** `getComponent` on a deleted entity crashes the game (a fatal engine assertion, not a Lua error), and entity ids are reused. The mod checks `entityExists` before every read.

## Verification

**Automated** (`scripts/run_tests.sh tests/plugins/tf3/`): 158 tests.
- The real mod Lua runs under Lua 5.2 (`lupa`, optional; those tests are skipped without it). It runs against `tests/plugins/tf3/fake_game.lua`, a stand-in for the TF3 script runtime with real userdata files and a crash-strict `getComponent`.
- The real relay and the real plugin run unchanged.
- The end-to-end tests drive Hermes tools → HTTP → relay → files → mod → simulated engine and back.
- The plugin is also loaded through the real `PluginManager`.
- `install_vps.sh` is checked against a real OpenSSH 9.9 `sshd` in test mode (`sshd -t` and `sshd -T`) on throwaway configs. The tests cover:
  - the lockdown the tunnel account actually gets, and that nobody else is affected;
  - re-installing and uninstalling;
  - refusing a config in which an earlier `Match` block would win;
  - the key line matching `install_mac.sh`;
  - the token file stays private.
  
  Its root-only steps (`useradd`, `runuser`, reloading sshd) only run on a real VPS. Preview them there with `--dry-run`.

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
