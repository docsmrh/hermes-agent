"""tf3 — let Hermes observe, control and play Transport Fever 3 on the user's Mac.

Topology (see ../../README.md):

    Hermes (VPS) -> this plugin -> http://127.0.0.1:47800 (VPS end of an SSH
    reverse tunnel) -> tf3_relay.py on the Mac -> files -> the in-game
    hermes_bridge mod -> api.engine / api.cmd

The plugin is install-and-enable:

    ln -s <repo>/integrations/transport-fever-3/hermes-plugin/tf3 ~/.hermes/plugins/tf3
    hermes plugins enable tf3

Tools are offered once ``TF3_BRIDGE_TOKEN`` is set; ``tf3_lua`` additionally
needs ``plugins.entries.tf3.settings.enable_lua_tool: true``. ``register()``
makes no network calls (``hermes plugins doctor`` blocks sockets there).
"""

from __future__ import annotations

from pathlib import Path

from . import tools as _tools
from .cli import register_cli as _register_cli
from .cli import tf3_command as _tf3_command


def register(ctx) -> None:
    _tools.bind(ctx)
    for name, schema, handler, check_fn, emoji in _tools.TOOLS:
        ctx.register_tool(
            name=name,
            toolset="tf3",
            schema=schema,
            handler=handler,
            check_fn=check_fn,
            emoji=emoji,
        )

    ctx.register_cli_command(
        name="tf3",
        help="Transport Fever 3 bridge (status, screenshot, journal, autopilot)",
        setup_fn=_register_cli,
        handler_fn=_tf3_command,
        description="Inspect the hermes_bridge TF3 mod connection and set up the autonomous play loop.",
    )

    skills_dir = Path(__file__).parent / "skills"
    for child in sorted(skills_dir.iterdir()):
        skill_md = child / "SKILL.md"
        if child.is_dir() and skill_md.exists():
            ctx.register_skill(child.name, skill_md)
