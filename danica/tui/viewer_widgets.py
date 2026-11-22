"""
viewer_widgets.py - Widgets for the split-screen file viewer and the clickable <present> links.
(Kept in their own file so widgets.py is untouched.)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rich.style import Style
from rich.syntax import Syntax
from rich.text import Text
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Static

from danica.artifacts import file_kind, human_size
from danica.tui.chrome import button
from danica.tui.widgets import ACCENT, highlighted


class PresentLink(Static):
    """What <present file="..."/> shows now: ONE clickable line instead of a picture. Ctrl+click
    (Windows Terminal) opens the interactive HTML viewer in the browser."""

    def __init__(
        self,
        label: str,
        target: Path,
        what: str,
        save_index: int | None = None,
    ) -> None:
        t = Text("  \u25a3 ", style=ACCENT)
        t.append(f"{what}: {label}", style="bold")
        try:
            t.append("   open interactive view \u2197", style=Style(color=ACCENT, underline=True, link=target.as_uri()))
        except Exception:
            t.append(f"   ({target})", style="dim")
        t.append("   (Ctrl+click)", style="dim")
        if save_index is not None:                       # a second link that works without any browser: click = copy to Downloads
            t.append("   ")
            t.append("save picture \u2193", style=Style(color=ACCENT, underline=True, meta={"@click": f"app.save_picture({save_index})"}))
        super().__init__(t, classes="say")


class ArtifactPane(Vertical):
    """Split-screen file viewer: text only, syntax highlighted, with line numbers.

    top row = the file (name, type, size, lines)  +  "[ copy ]  [ save ]" buttons
    body    = the code (scroll with the mouse or PgUp/PgDn)
    Shown by /open <n> (text and code files), hidden by /close or Ctrl+E. Only ever DISPLAYS text:
    nothing is run, and pictures/office files are not rendered (/open sends those to their app).
    [ copy ] puts the whole file on the clipboard, [ save ] copies it to Downloads (app.viewer_copy / viewer_save)."""

    current: tuple[str, Path] | None = None          # (name shown, real path) of the file on screen

    def compose(self) -> Any:
        """Compose the preview pane: the top row (file info + buttons) over the scrolling body."""
        with Horizontal(id="view-bar"):
            yield Static("", id="view-head")
            actions = Text()                          # start empty: a button's own style must not colour the gap between them
            actions.append_text(button("[ copy ]", "viewer_copy", False))
            actions.append("  ")
            actions.append_text(button("[ save ]", "viewer_save", False))
            yield Static(actions, id="view-actions")
        with VerticalScroll(id="view-scroll"):
            yield Static("", id="view-body")

    def on_mount(self) -> None:
        """Set the pane's title after mounting."""
        self.border_title = "Viewer"

    def show_file(
        self, rel: str, path: Path, text: str, cut: bool, left_out: int
    ) -> None:
        """Fill the pane. `text` is already tidied (artifacts.tidy_for_view)."""
        self.current = (rel, path)                    # what the buttons act on
        try:
            lexer = Syntax.guess_lexer(str(path), text) if text else "text"
        except Exception:
            lexer = "text"
        try:
            size = human_size(path.stat().st_size)
        except OSError:
            size = "?"
        head = Text(rel, style="bold", no_wrap=True, overflow="ellipsis")      # cut with "..." so the buttons stay visible
        head.append(f"   {file_kind(rel)}   {size}", style="dim")                  # short, so it fits beside the buttons
        n_lines = len(text.splitlines())
        self.border_subtitle = f" {n_lines:,} line{'' if n_lines == 1 else 's'} "      # the line count lives on the bottom border
        self.query_one("#view-head", Static).update(head)
        body = highlighted(text, lexer)
        if cut or left_out:
            body.append("\n\n  \u2026 the rest is not shown here ([ copy ] and [ save ] take the whole file)", style="dim")
        self.query_one("#view-body", Static).update(body)
        self.query_one("#view-scroll", VerticalScroll).scroll_home(animate=False)
        self.border_title = f"Viewer  (/close or Ctrl+E to hide)"
