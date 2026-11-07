"""visual-gate — visual regression + WCAG accessibility gate for Hermes.

Usage:
  hermes vg_gate name=https://example.com              → full PASS/FAIL gate
  hermes vg_a11y url=https://example.com               → accessibility only
  hermes vg_capture name=home-desktop url=https://...  → new baseline
  hermes vg_baselines                                → list them
  hermes vg_doctor                                   → self-check
"""

from __future__ import annotations

from hermes_cli.plugins import PluginContext

from . import schemas, tools

# Hermes does not pass ``ctx`` to tool handlers, so the plugin holds it here and tools read
# their config from it at call time. This keeps the handler surface stable without losing
# access to settings and the durable state facade.
_CTX = type("_Ctx", (), {})()


def configure(ctx: "_Ctx") -> None:
    globals()["_CTX"] = ctx


def register(ctx: PluginContext) -> None:
    tools.configure(ctx)

    for tool in ("vg_capture", "vg_diff", "vg_a11y", "vg_gate",
                 "vg_baselines", "vg_doctor"):
        fn = getattr(tools, tool)
        schema = getattr(schemas, tool.upper(), None)
        ctx.register_tool(
            name=tool,
            toolset="visual_gate",
            schema=schema,
            handler=fn,
            description=getattr(schema, "description", ""),
        )
