"""
chrome.py - the frame around the chat: the top bar, the sidebar menu and the artifacts pane.

TopBar      "[ sidebar ]"   the chat title (middle)   "[ subagents ]  [ file view ]  [ artifacts ]"
            Each button toggles its pane; a button is drawn bright while its pane is open.
SIDE_MENU   The menu at the top of the left-hand session pane (New chat, Project mode, the managers...).
ArtifactsPane   The files the last request made; click one to open it (the same as /open <n>).
"""

from __future__ import annotations

from typing import Any

from rich.style import Style
from rich.text import Text
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Static

from danica.artifacts import human_size

OPEN = "bold #ffffff on #4a4a4a"       # a button whose pane is open
SHUT = "#c8c8c8"                       # a button whose pane is closed

LEFT = "[ sidebar ]"
RIGHT = (("[ subagents ]", "agents-pane", "toggle_agents"),
         ("[ file view ]", "viewer-pane", "toggle_viewer"),
         ("[ artifacts ]", "artifacts-pane", "toggle_artifacts"))


def button(label: str, action: str, active: bool) -> Text:
    """A text button: clicking runs the app action."""
    return Text(label, style=Style.parse(OPEN if active else SHUT) + Style(meta={"@click": f"app.{action}"}))


class TopBar(Horizontal):
    """One line: sidebar button | chat title | pane buttons. `refresh_state` redraws it from the app."""

    def compose(self) -> Any:
        """Compose the three parts of the bar."""
        yield Static("", id="tb-left")
        yield Static("", id="tb-title")
        yield Static("", id="tb-right")

    def refresh_state(self, sidebar_open: bool, open_panes: set[str], title: str, width: int) -> None:
        """Redraw the buttons (bright when their pane is open) and the title (cut to the room that is left)."""
        self.query_one("#tb-left", Static).update(button(LEFT, "toggle_sessions", sidebar_open))
        right = Text()
        for n, (label, pane, action) in enumerate(RIGHT):
            if n:
                right.append("  ")
            right.append_text(button(label, action, pane in open_panes))
        self.query_one("#tb-right", Static).update(right)
        room = max(8, width - len(LEFT) - right.cell_len - 6)
        shown = (title or "New chat").strip()
        if len(shown) > room:
            shown = shown[:room - 1].rstrip() + "…"
        self.query_one("#tb-title", Static).update(Text(shown, style="bold #ffffff", justify="center"))


# ---- the sidebar menu: (label, what happens). Names starting with "manage:" open that manager pane.
MENU = (
    ("[ New chat ]", "new"),
    ("[ Project mode ]", "project"),
    None,
    ("[ Memories ]", "manage:memories"),
    ("[ Knowledge ]", "manage:knowledge"),
    ("[ Skills ]", "manage:skills"),
    ("[ Reminders ]", "manage:reminders"),
    ("[ Web cache ]", "manage:webcache"),
    None,
    ("[ Settings ]", "settings"),
    ("[ Help ]", "help"),
)


def side_menu_text() -> Text:
    """The menu as clickable text (each line runs app.menu(<name>))."""
    text = Text()
    for item in MENU:
        if item is None:
            text.append("\n")
            continue
        label, name = item
        text.append(label + "\n", style=Style(meta={"@click": f"app.menu('{name}')"}))
    text.rstrip()
    return text


class ArtifactsPane(Vertical):
    """The files this session has made. A click opens one (text files go to the file view)."""

    def compose(self) -> Any:
        """Compose the file list."""
        yield Static("", id="art-head")
        with VerticalScroll(id="art-scroll"):
            yield Static("", id="art-body", classes="session-list")

    def on_mount(self) -> None:
        """Set the title."""
        self.border_title = "Artifacts"

    def show(self, items: list) -> None:
        """Draw the list. `items` are the artifacts of the last request (artifacts.Artifact)."""
        head = self.query_one("#art-head", Static)
        body = self.query_one("#art-body", Static)
        if not items:
            head.update(Text("nothing yet", style="dim"))
            body.update(Text("Files Danica makes or changes appear here and stay with the session.", style="dim"))
            return
        head.update(Text(f"{len(items)} in this session", style="dim"))
        text = Text()
        for n, a in enumerate(items, start=1):
            size = human_size(a.size)
            text.append(f"{n:>2}. ", style="dim")
            text.append(f"{a.rel}", style=Style(meta={"@click": f"app.open_artifact({n})"}))
            text.append(f"  {a.status}, {size}\n", style="dim")
        text.rstrip()
        body.update(text)
