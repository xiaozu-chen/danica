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

from typing import Protocol

TAG_COLORS = {
    "ERROR":   {"color": "#E03E3E", "symbol": "[ ✗ ]"},   # U+2717
    "WARNING": {"color": "#E5C158", "symbol": "[ ▲ ]"},   # U+25B2
    "OK":      {"color": "#4CAF50", "symbol": "[ ✓ ]"},   # U+2713
    "FAIL":    {"color": "#E67E22", "symbol": "[ — ]"},   # U+2014
    "INFO":    {"color": "#F5F6FA", "symbol": "[ i ]"},   # plain ASCII
}
TAG_WIDTH = max(map(len, TAG_COLORS))   # updates itself if you add a longer label


class UI(Protocol):
    def log(self, level: str, text: str) -> None:
        """A one-line status badge: level is error / warning / ok / fail / info."""

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


class RichUI:
    """The classic terminal frontend (Rich output, prompt_toolkit input)."""

    def __init__(self, console=None):
        from rich.console import Console
        self.console = console or Console()

    def log(self, level: str, text: str) -> None:
        from rich.markup import escape
        key = level.upper()
    
        # Retrieve the dictionary for the level, fallback to white/empty if not found
        tag_info = TAG_COLORS.get(key, {"color": "white", "symbol": ""})
    
        # Extract the individual string values correctly
        color = tag_info["color"]
        label = tag_info["symbol"]
    
        self.console.print(f"[bold #000000 on {color}] {label} [/]::{escape(str(text))}", highlight=False)

    def say(self, text: str, style: str | None = None) -> None:
        self.console.print(text, style=style, markup=False, highlight=False)

    def confirm(self, title: str, body: str = "", detail: str = "",
                yes: str = "Yes", no: str = "No") -> bool:
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
        from prompt_toolkit import prompt
        try:
            return prompt(f"{title} > ", default=default).strip()
        except (EOFError, KeyboardInterrupt):
            return None