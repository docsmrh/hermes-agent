"""``hermes tf3 ...`` — operator commands for the Transport Fever 3 bridge."""

from __future__ import annotations

import argparse
import json
import shlex
from pathlib import Path

from . import tools
from .client import BridgeError

AUTOPILOT_SKILL = "tf3:autopilot"
AUTOPILOT_PROMPT = (
    "Play one Transport Fever 3 autopilot turn: follow the tf3:autopilot skill "
    "(journal read -> observe -> decide -> act -> verify -> journal append)."
)


def register_cli(subparser: argparse.ArgumentParser) -> None:
    subs = subparser.add_subparsers(dest="tf3_command")
    subs.add_parser("status", help="Check the relay, SSH tunnel and in-game mod")
    shot = subs.add_parser("screenshot", help="Save a screenshot from the Mac to a file")
    shot.add_argument("--out", default="tf3-screenshot.jpg", help="Output path (default: ./tf3-screenshot.jpg)")
    shot.add_argument("--max-width", type=int, default=1600)
    journal = subs.add_parser("journal", help="Print the autopilot plan and recent journal entries")
    journal.add_argument("--limit", type=int, default=20)
    auto = subs.add_parser("autopilot", help="Print the cron command that runs the autonomous loop")
    auto.add_argument("--every", default="every 15m", help="Schedule (default: 'every 15m')")
    auto.add_argument("--deliver", default="local", help="Where run summaries go (default: local)")
    subparser.set_defaults(func=tf3_command)


def _print_json(payload) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


def tf3_command(args: argparse.Namespace) -> int:
    cmd = getattr(args, "tf3_command", None) or "status"
    if cmd == "autopilot":
        command = (
            f"hermes cron create {shlex.quote(args.every)} {shlex.quote(AUTOPILOT_PROMPT)} "
            f"--name tf3-autopilot --skill {AUTOPILOT_SKILL} --deliver {shlex.quote(args.deliver)}"
        )
        print("Run this to start the autonomous loop (pause it with `hermes cron pause tf3-autopilot`):\n")
        print(f"  {command}\n")
        print("Each run is capped at the cron hard limit and by "
              "plugins.entries.tf3.settings.max_actions_per_session.")
        return 0
    if cmd == "journal":
        raw = tools.handle_tf3_journal({"action": "read", "limit": args.limit})
        _print_json(json.loads(raw))
        return 0
    if not tools.check_tf3_available():
        print(f"{tools.TOKEN_ENV} is not set. Add it to your Hermes .env (value printed by "
              "`tf3_relay.py init` on the Mac).")
        return 1
    try:
        client = tools._client()
        if cmd == "screenshot":
            shot = client.screenshot(max_width=args.max_width)
            out = Path(args.out).expanduser()
            out.write_bytes(shot.data)
            print(f"wrote {out} ({len(shot.data):,} bytes, {shot.mime}, {shot.width}x{shot.height})")
            return 0
        health = client.health()
    except BridgeError as exc:
        print(f"tf3 bridge error [{exc.code}]: {exc.message}")
        return 2
    _print_json(health)
    mod = health.get("mod") or {}
    if not mod.get("connected"):
        print("\nRelay is up but the game mod is not answering: start Transport Fever 3, load a save "
              "with the 'Hermes Bridge' mod enabled, and unpause the game menu.")
        return 3
    return 0
