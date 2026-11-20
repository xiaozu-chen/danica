"""Build the terminal help menu from registered command descriptions."""

from typing import Any

from rich import box
from rich.columns import Columns
from rich.panel import Panel
from rich.text import Text

HELP = [
    {"name": "Chat", "commands": ["/think", "/think auto", "/new", "/exit"]},
    {"name": "Plans", "commands": ["/plan [<request>]", "/plan show", "/plan auto ask|off"]},
    {"name": "Project", "commands": ["/mode", "/project [path]", "/openmode [folder]", "/chat", "/instructions"]},
    {"name": "Context", "commands": ["/context", "/prune", "/compact [tokens]", "/summary", "/todo [clear]"]},
    {"name": "Sessions", "commands": ["/sessions [delete n]", "/resume [number|id]", "/export"]},
    {"name": "Files", "commands": ["/artifacts", "/open <n>", "/close", "/copy <n>", "/save <n> [folder]", "/upload [path]", "/attach <picture>", "/vision"]},
    {"name": "Productivity", "commands": ["/mail [login | test | logout]", "/play [file.py | log | stop]", "/computer [on | off | allow | deny | smooth | log]"]},
    {"name": "Safety", "commands": ["/fullaccess", "/sandbox", "/audit", "/undo", "/rewind [id]", "/taint", "/untaint"]},
    {"name": "Web", "commands": ["/sources", "/webcache [status | list | search <words> | reindex | clear]"]},
    {"name": "Memory", "commands": ["/memory [list | add [scope] <text> | review | capture | search <words> | merge | forget <id> | show <id> | status | reindex | path]"]},
    {"name": "Knowledge", "commands": ["/knowledge [index|search|watch|stop|status|digest|graph|commit <preview-id>]"]},
    {"name": "Reminder", "commands": ["/reminder add <ISO datetime> <text>", "/reminder list"]},
    {"name": "Other", "commands": ["/ws", "/skills", "/agents [run <task> | peek|wait|kill <id>]", "/config", "/perf [tools]", "/classifier", "/dialogs", "/diagrams", "/help"]},
]

def render_help_menu() -> Any:
    """Build the command reference as a Rich renderable (does not print)."""
    category_panels = []
    for section in HELP:
        command_lines = []
        for cmd in section["commands"]:
            parts = cmd.split(" ", 1)
            base_cmd = parts[0]
            args = f" {parts[1]}" if len(parts) > 1 else ""

            t = Text()
            t.append(base_cmd, style="bold bright_white")
            if args:
                t.append(args, style="dim white")
            command_lines.append(t)

        body = Text("\n").join(command_lines)

        category_panels.append(
            Panel(
                body,
                title=Text(section["name"], style="white"),
                title_align="left",
                box=box.ROUNDED,
                border_style="bright_black",
                padding=(1, 2),
                expand=True,
            )
        )

    return Panel(
        Columns(category_panels, equal=True, expand=True),
        title=Text("Command Reference Help", style="#ffffff"),
        subtitle=Text("Type a command to execute", style="dim"),
        subtitle_align="center",
        box=box.ASCII3,
        border_style="#3B3B3B",
        padding=(1, 2),
        expand=True,
    )
