"""
ui.py - What the logic layer needs from a frontend.

app_core.py and the slash-command handlers never print or prompt directly. They call these
four methods instead, so the same code can run under the classic Rich/prompt_toolkit CLI
(RichUI below) or under the Textual TUI (danica/tui/bridge.py: TextualUI).

Rule for implementers: confirm() and permission() BLOCK until the user answers. A frontend
that owns the terminal (Textual) must therefore only call them from a worker thread.

██████   █████  ███    ██ ██  ██████  █████ 
██   ██ ██   ██ ████   ██ ██ ██      ██   ██
██   ██ ███████ ██ ██  ██ ██ ██      ███████
██   ██ ██   ██ ██  ██ ██ ██ ██      ██   ██
██████  ██   ██ ██   ████ ██  ██████ ██   ██


"""

from __future__ import annotations

from typing import Any, Protocol

from rich.text import Text

TAG_COLORS = {
    "ERROR": {"color": "#f7768e", "label": "ERROR"},
    "WARNING": {"color": "#e0af68", "label": "WARN"},
    "OK": {"color": "#9ece6a", "label": "OK"},
    "FAIL": {"color": "#f7768e", "label": "FAIL"},
    "INFO": {"color": "#7aa2f7", "label": "INFO"},
}
TAG_WIDTH = max(len(tag["label"]) for tag in TAG_COLORS.values())

_LEVEL_ALIASES = {"WARN": "WARNING", "SUCCESS": "OK"}


def format_log(level: str, message: str) -> Text:
    """Format a log entry with a colored level badge and aligned continuation lines."""
    key = level.strip().upper()
    key = _LEVEL_ALIASES.get(key, key)
    tag = TAG_COLORS.get(
        key, {"color": "#a9b4c8", "label": (key[:TAG_WIDTH] or "LOG")}
    )
    lines = str(message).strip().splitlines() or [""]
    first = lines[0].strip()

    aliases = [name for name, target in _LEVEL_ALIASES.items() if target == key]
    for prefix in (key, *aliases):
        if first.upper().startswith(prefix + ":"):
            first = first[len(prefix) + 1 :].lstrip()
            break

    output = Text()
    output.append(
        f" {tag['label']:^{TAG_WIDTH}} ",
        style=f"bold #101218 on {tag['color']}",
    )
    output.append("  ", style="#566078")
    output.append(first, style="#d8deeb")
    continuation_indent = " " * (TAG_WIDTH + 2)
    for line in lines[1:]:
        output.append("\n" + continuation_indent + "| ", style="#566078")
        output.append(line, style="#d8deeb")
    return output


class UI(Protocol):
    """Define the callbacks used by the agent to communicate with a user."""

    def log(self, level: str, text: str) -> None:
        """Display a status entry with a level badge and plain-text details."""

    def say(self, text: str, style: str | None = None) -> None:
        """Plain text output (command results). Never interpreted as markup."""

    def confirm(self, title: str, body: str = "", detail: str = "",
                yes: str = "Yes", no: str = "No") -> bool:
        """Blocking yes/no. Anything but an explicit yes (Esc, Ctrl+C, EOF) is False."""

    def permission(self, tool: str, summary: str, tier: str, reason: str,
                   can_remember: bool) -> str:
        """Blocking tool-permission question. Returns "once", "session" or "deny"."""

    def choose(self, title: str, body: str, options: list[tuple[str, str]],
               detail: str = "") -> str | None:
        """Blocking multiple choice. options = [(key, label), ...] with single-letter keys.
        Returns the chosen key, or None if the user dismissed it (Esc, Ctrl+C, EOF)."""

    def ask_text(self, title: str, default: str = "") -> str | None:
        """Blocking one-line text question. None if dismissed."""

    def show(self, renderable: Any) -> None:
        """Display a rich/structured renderable (frontends may degrade it to text)."""


class RichUI:
    """The classic terminal frontend (Rich output, prompt_toolkit input)."""

    def __init__(self, console=None):
        from rich.console import Console
        self.console = console or Console()

    def log(self, level: str, text: str) -> None:
        """Display a status message in the terminal."""
        self.console.print(format_log(level, text), highlight=False)

    def say(self, text: str, style: str | None = None) -> None:
        """Display a short user facing message."""
        self.console.print(text, style=style, markup=False, highlight=False)

    def confirm(self, title: str, body: str = "", detail: str = "",
                yes: str = "Yes", no: str = "No") -> bool:
        """Ask the user to confirm an action."""
        from prompt_toolkit import prompt
        self.log("warning", title)
        if body:
            self.say(body)
        if detail:
            self.say(detail, style="dim")
        try:
            return prompt(f"[y] {yes}  [n] {no} > ").strip().lower() in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            return False

    def permission(self, tool: str, summary: str, tier: str, reason: str,
                   can_remember: bool) -> str:
        """Ask the user to approve a permission request."""
        from prompt_toolkit import prompt
        self.say(f"Permission needed ({tier}): {reason}", style="bold yellow")
        self.log("warning", summary)
        options = "[y] once  [s] this session  [n] deny" if can_remember else "[y] once [n] deny"
        while True:
            try:
                answer = prompt(f"{options} > ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                return "deny"
            if answer in ("y", "yes"):
                return "once"
            if answer in ("s", "session") and can_remember:
                return "session"
            if answer in ("n", "no", ""):
                return "deny"

    def choose(self, title: str, body: str, options: list[tuple[str, str]],
               detail: str = "") -> str | None:
        """Ask the user to choose from labeled options."""
        from prompt_toolkit import prompt
        self.log("warning", title)
        if body:
            self.say(body)
        if detail:
            self.say(detail, style="dim")
        keys = {k for k, _ in options}
        line = "  ".join(f"[{k}] {label}" for k, label in options)
        while True:
            try:
                answer = prompt(f"{line} > ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                return None
            if answer in keys:
                return answer

    def ask_text(self, title: str, default: str = "") -> str | None:
        """Prompt the user for a text response."""
        from prompt_toolkit import prompt
        try:
            return prompt(f"{title} > ", default=default).strip()
        except (EOFError, KeyboardInterrupt):
            return None

    def show(self, renderable: Any) -> None:
        """Render a Rich object in the terminal."""
        self.console.print(renderable)
