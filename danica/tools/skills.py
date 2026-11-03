"""The on-demand skill loader tool."""

from danica.skills import SkillError, load_skill
from danica.tools.registry import tool
from danica.workspace import Workspace


@tool(
    "load_skill",
    "Load one local skill's SKILL.md guidance by name. Only names from the skill catalog are valid.",
    {"name": {"type": "string", "description": "Skill name from the available skill catalog"}},
)
def load_skill_tool(ws: Workspace, name: str) -> str:
    try:
        return load_skill(ws.skill_dir, name)
    except (OSError, UnicodeError, SkillError) as exc:
        return f"ERROR: could not load skill '{name}': {exc}"
