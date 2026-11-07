"""visual-gate: visual regression and WCAG accessibility gate for Hermes.

The agent calls these as tools; they are not CLI subcommands:

  vg_gate      name + url  full PASS/FAIL: visual diff plus accessibility audit
  vg_a11y      url         accessibility audit only
  vg_capture   name + url  store a baseline, or compare against it
  vg_diff      two paths   compare two images on disk
  vg_baselines             list or delete baselines
  vg_doctor                self-check: browser, CDP, audit probe
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import schemas, tools

if TYPE_CHECKING:
    from hermes_cli.plugins import PluginContext


def register(ctx: "PluginContext") -> None:
    tools.configure(ctx)
    for schema in schemas.ALL:
        ctx.register_tool(
            name=schema["name"],
            toolset="visual_gate",
            schema=schema,
            handler=getattr(tools, schema["name"]),
            description=schema["description"],
        )
