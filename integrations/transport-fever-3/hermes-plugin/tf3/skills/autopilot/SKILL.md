---
name: autopilot
description: Play Transport Fever 3 autonomously via the tf3 tools.
version: 0.1.0
author: docsmrh
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [gaming, transport-fever-3, autonomous]
    category: gaming
---

# Transport Fever 3 Autopilot Skill

Runs one bounded "turn" of autonomous Transport Fever 3 play on the user's Mac:
read the standing plan, observe the game, make a few high-value moves, verify
them, and write down what happened for the next turn. It does not install or
configure the bridge — see the plugin README for that.

## When to Use

- A scheduled job (`hermes cron`) fires with this skill attached.
- The user asks you to "play", "run the company" or "keep the network growing" in TF3.
- Not for one-off questions about the game state — call `tf3_observe` directly.

## Prerequisites

- The `tf3` plugin is enabled (`hermes plugins enable tf3`) and `TF3_BRIDGE_TOKEN` is set.
- On the Mac: Transport Fever 3 is running with a save loaded that has the
  **Hermes Bridge** mod enabled, the relay LaunchAgent is running, and the SSH
  reverse tunnel is up. `hermes tf3 status` checks all three.

## How to Run

Scheduled (recommended): `hermes tf3 autopilot` prints the exact
`hermes cron create ... --skill tf3:autopilot` command. Interactive: load this
skill and follow the Procedure once per user request.

## Quick Reference

| Tool | Use |
|------|-----|
| `tf3_journal` | `read` first, `append` last, `set_plan` when strategy changes |
| `tf3_observe` | Numbers and ids. `sections`: game, finance, lines, vehicles, stations, towns, industries, problems |
| `tf3_screenshot` | Spatial questions only (where is free land, what is the dialog on screen) |
| `tf3_act` | All changes. Always give `reason`. Use `dry_run: true` when unsure of params |
| `tf3_lua` | Only if enabled and `tf3_act` lacks a command; keep chunks small |

Budget per turn: `max_actions_per_session` (shown in `tf3_observe` →
`guardrails`). A cron turn also has a hard time cap of a few minutes, so plan
for 3–8 actions, not 30.

## Procedure

1. **Orient.** `tf3_journal action=read`. Note the plan, the last entries, and
   any failed actions in `recent_actions`.
2. **Observe.** `tf3_observe` with `sections=["game","finance","lines","problems"]`.
   If the bridge reports the mod is not connected or the game is paused in a
   menu, append a journal entry saying so and stop — do not retry in a loop.
3. **Triage problems first** (cheapest wins):
   - Lines flagged in `problems` (no path, unconnected stops) — fix or delete.
   - Vehicles stuck or without a path — send to depot or re-assign.
   - Lines losing money for several observations — reduce vehicles or delete.
4. **Grow only from a cash buffer.** Keep at least a few months of running
   costs in hand (check `finance`). Prefer, in order:
   - Add vehicles to profitable lines with high load/waiting cargo.
   - Connect two large nearby towns with a bus or tram line.
   - Complete a cargo chain (raw industry → processing industry → town).
   Use `tf3_observe sections=["towns","industries","stations"]` to choose;
   take a `tf3_screenshot` only when placement depends on terrain.
5. **Act.** One `tf3_act` per change, each with a one-sentence `reason`.
   For unfamiliar params run once with `dry_run: true` first.
6. **Verify.** Re-run `tf3_observe` for the sections you touched. A command
   the game accepted can still be wrong (vehicle bought but not on the line,
   line created with a missing stop). Fix or record it.
7. **Record.** `tf3_journal action=append` with: date in game, balance, what
   you changed, what you verified, and the next intended move. Call
   `set_plan` only when the medium-term strategy actually changes.

## Pitfalls

- While the game is paused, actions fail with `game_paused` and nothing is
  done: `set_speed {speedup: 1}` first. An action that fails with
  `outcome: unknown` (a timeout) may still happen — re-observe before retrying.
- Entity ids are stable only within one loaded save. After the user loads a
  different save, the journal's ids are stale; re-observe before acting.
- Do not bulldoze or sell to "clean up" without evidence from `tf3_observe`
  that the asset loses money or is broken.
- `save_game` overwrites by name; use a dedicated name like `hermes-autosave`.
- If an action fails with `paused`, `policy_denied` or `readonly`, the user has
  stopped automation on purpose. Journal it and end the turn.

## Verification

A good turn leaves: (a) a journal entry for this turn, (b) every `tf3_act`
followed by an observation that confirms it, and (c) no repeated failed
action with the same params.
