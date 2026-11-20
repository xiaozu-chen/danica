"""
session_pane.py - Left-hand side panel that lists the saved sessions (click one to resume it).
Same idea as the subagent / viewer panes on the right, but docked to the LEFT of the chat.
Toggle: Ctrl+B or /sidebar.
"""

from __future__ import annotations

from typing import Any

from rich.style import Style
from rich.text import Text
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static


class SessionPane(Vertical):
    """head = how many sessions        list = one clickable entry per saved session (newest first).
    The list is read on a worker thread (it touches the disk); the widgets are only touched on the UI thread."""

    def compose(self) -> Any:
        """Compose the saved-session list pane."""
        yield Static("", id="sess-head")
        with VerticalScroll(id="sess-scroll"):
            yield Static("", id="sess-body", classes="session-list")

    def on_mount(self) -> None:
        """Set the pane title."""
        self.border_title = "Sessions"
        self._loading = False
        self._again = False

    def reload(self) -> None:
        """UI thread. Re-read the saved sessions in the background and redraw. Cheap to call often:
        a reload that is already running is simply repeated once when it finishes."""
        if not self.display:                  # hidden pane: nothing to draw, it reloads when it is shown
            return
        if self._loading:
            self._again = True
            return
        self._loading = True
        self.run_worker(self._read, thread=True, exclusive=False)

    def _read(self) -> None:
        """Worker thread."""
        try:
            from danica.sessions import list_sessions
            infos = list(list_sessions())
        except Exception:                     # a broken session folder must never break the UI
            infos = None
        try:
            self.app.call_from_thread(self._done, infos)
        except RuntimeError:                  # the app is shutting down
            pass

    def _done(self, infos: list[dict] | None) -> None:
        """UI thread."""
        self._loading = False
        self._draw(infos)
        if self._again:
            self._again = False
            self.reload()

    def _draw(self, infos: list[dict] | None) -> None:
        head = self.query_one("#sess-head", Static)
        body = self.query_one("#sess-body", Static)
        if infos is None:
            head.update(Text("could not read the sessions", style="dim"))
            body.update(Text(""))
            return
        head.update(Text(f"{len(infos)} saved", style="dim"))
        if not infos:
            body.update(Text("(no saved sessions yet)", style="dim"))
            return
        from rich import box
        from rich.table import Table
        current = getattr(self.app, "session_id", "")
        # same look as /sessions (commands.py), but only the "#" and title columns
        table = Table(box=box.SIMPLE, show_header=True, header_style="bold #ffffff", expand=True, pad_edge=False)
        table.add_column("#", justify="right", style="bold #ffffff", width=3, no_wrap=True)
        table.add_column("Session", ratio=1, overflow="fold")
        for n, info in enumerate(infos, start=1):                  # numbers match /sessions and /resume <n>
            title = (info.get("title") or info.get("preview") or "(untitled)").strip().replace("\n", " ")
            if info["id"] == current:
                label = Text(f"\u2022 {title}", style="bold #CCCCFF")
            else:
                label = Text(title, style=Style(meta={"@click": f"app.resume_session_id('{info['id']}')"}, underline=False))
            table.add_row(str(n), label)
        body.update(table)
