"""
enable_tools - ask for tools that are not loaded right now (see toolgroups.py).

Rarely used tools are left out of each request to keep it fast. This one is always there: name the groups you need and
their tools appear on your next step.
"""

from danica import toolgroups
from danica.tools.registry import TOOLS, tool
from danica.workspace import Workspace


@tool("enable_tools",
      "Load extra tools. Not every tool is sent with every request (it would slow you down). Groups: "
      + toolgroups.describe() + ". Call this with the groups you need; they are available from your next step and stay on.",
      {"groups": {"type": "array", "items": {"type": "string"}, "description": "Group names, for example [\"docs\", \"viz\"]"}})
def enable_tools(ws: Workspace, groups: list) -> str:
    if not isinstance(groups, list) or not groups:
        return "ERROR: groups must be a list such as [\"docs\"]. Groups: " + ", ".join(toolgroups.GROUPS)
    unknown = [g for g in groups if g not in toolgroups.GROUPS]
    new = toolgroups.activate(ws, [g for g in groups if g in toolgroups.GROUPS])
    names = [n for g in groups if g in toolgroups.GROUPS for n in toolgroups.GROUPS[g]["tools"] if n in TOOLS]
    out = f"OUTPUT: enabled {', '.join(new) or 'nothing new'}. Tools you can now call: {', '.join(names) or 'none'}."
    if unknown:
        out += f" Unknown groups ignored: {', '.join(map(str, unknown))} (valid: {', '.join(toolgroups.GROUPS)})."
    return out
