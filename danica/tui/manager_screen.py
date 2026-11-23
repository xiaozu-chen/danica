"""
manager_screen.py - the shared manager pane: a list on the left, the selected entry on the right, buttons underneath.

One screen class serves Memories, Knowledge, Skills, Reminders and Web cache; managers.py says what each one lists and
what its buttons do. Everything that touches notes, the index or files runs on the ENGINE thread (managers.py explains
why) and the answer comes back to this screen on the UI thread. If Danica is busy with a request, the pane says so and
waits its turn.

Keys: Esc closes (or cancels a half-typed button), arrow keys move in the list, Tab moves between the controls.
"""

from __future__ import annotations

import re

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, Static
from textual.widgets.option_list import Option

from danica.managers import Action, Manager, ManagerError, Row
from danica.tui.modals import ConfirmScreen


class ManagerScreen(ModalScreen[None]):
    """A manager pane for one Manager (see managers.py)."""

    BINDINGS = [Binding("escape", "close", "Close")]

    DEFAULT_CSS = """
    ManagerScreen { align: center middle; background: black 60%; }
    ManagerScreen .mgr-box { width: 96%; max-width: 132; height: 92%; padding: 1 2; background: transparent; border: round #4a4a4a; }
    ManagerScreen #mgr-summary { height: auto; margin-bottom: 1; }
    ManagerScreen .mgr-body { height: 1fr; }
    ManagerScreen #mgr-list { width: 40%; height: 100%; background: transparent; border: round #4a4a4a; scrollbar-size-vertical: 1; }
    ManagerScreen #mgr-detail-scroll { width: 1fr; height: 100%; margin-left: 1; padding: 0 1; border: round #4a4a4a; scrollbar-size-vertical: 1; }
    ManagerScreen #mgr-input { margin-top: 1; background: transparent; }
    ManagerScreen .mgr-actions { height: auto; margin-top: 1; }
    ManagerScreen #mgr-status { height: auto; min-height: 1; margin-top: 1; }
    """

    def __init__(self, manager: Manager) -> None:
        super().__init__()
        self.manager = manager
        self.rows: list[Row] = []
        self.pending: Action | None = None       # a button waiting for the text typed in the box
        self.busy = False
        self._note = ""                            # message from the last action, shown again after the list reloads

    # ---------------------------------------------------------------- layout

    def compose(self) -> ComposeResult:
        """Compose the title, summary, list and detail, input box, buttons and status line."""
        with Vertical(classes="mgr-box"):
            yield Static(Text(self.manager.title, style="bold #ffffff"), classes="dialog-title")
            yield Static("", id="mgr-summary")
            with Horizontal(classes="mgr-body"):
                yield OptionList(id="mgr-list")
                with VerticalScroll(id="mgr-detail-scroll"):
                    yield Static("", id="mgr-detail")
            yield Input(id="mgr-input")
            with Horizontal(classes="mgr-actions"):
                for action in self.manager.actions:
                    yield Button(action.label, id=f"act_{action.id}")
                yield Button("Close  (Esc)", id="mgr_close")
            yield Static("", id="mgr-status")

    def on_mount(self) -> None:
        """Hide the input box and load the list."""
        self.query_one("#mgr-input", Input).display = False
        self._detail(Text(self.manager.empty, style="dim"))
        self._status("loading…", "warn")
        self._buttons()
        self._start(self._load_job)

    # ---------------------------------------------------------------- small helpers

    def _status(self, message: str, kind: str = "ok") -> None:
        style = {"ok": "#7ddc7d", "error": "#ff7b7b", "warn": "#e6c15a"}[kind]
        self.query_one("#mgr-status", Static).update(Text(message, style=style))

    def _detail(self, text: Text) -> None:
        self.query_one("#mgr-detail", Static).update(text)
        self.query_one("#mgr-detail-scroll", VerticalScroll).scroll_home(animate=False)

    def _row(self) -> Row | None:
        index = self.query_one("#mgr-list", OptionList).highlighted
        return self.rows[index] if index is not None and 0 <= index < len(self.rows) else None

    def _buttons(self) -> None:
        """Grey out what can't be used right now (no row selected, or a job is running)."""
        has_row = self._row() is not None
        for action in self.manager.actions:
            self.query_one(f"#act_{action.id}", Button).disabled = self.busy or (action.needs_row and not has_row)

    def _label(self, row: Row) -> Text:
        text = Text(row.title.replace("\n", " ")[:46], overflow="ellipsis", no_wrap=True)
        if row.hint:
            text.append("  " + row.hint, style="dim")
        return text

    def _start(self, job, *args) -> None:
        """Run `job` on the engine thread (it may have to wait for a running request)."""
        self.busy = True
        if self.app.engine.busy:
            self._status("waiting for Danica to finish what it is doing…", "warn")
        self.app.engine.submit(job, *args)

    # ---------------------------------------------------------------- jobs (engine thread) and their answers (UI thread)

    def _load_job(self) -> None:
        rows, summary, error = [], "", ""
        try:
            rows, summary = self.manager.load(self.app)
        except ManagerError as e:
            error = str(e)
        except Exception as e:                     # a broken store must not take the pane down
            error = f"could not load: {type(e).__name__}: {e}"
        self.app._call(self._loaded, rows, summary, error)

    def _loaded(self, rows: list[Row], summary: str, error: str) -> None:
        if not self.is_attached:
            return
        self.busy = False
        kept = self._row().key if self._row() else None
        self.rows = rows
        options = self.query_one("#mgr-list", OptionList)
        options.clear_options()
        options.add_options([Option(self._label(r), id=str(i)) for i, r in enumerate(rows)])
        self.query_one("#mgr-summary", Static).update(Text(summary, style="dim"))
        if rows:
            options.highlighted = next((i for i, r in enumerate(rows) if r.key == kept), 0)
        else:
            self._detail(Text(error or self.manager.empty, style="#ff7b7b" if error else "dim"))
        # Show the load error, or else the note the last action left ("Saved to global"), which the reload must not wipe
        self._status(error, "error") if error else self._status(self._note)
        self._note = ""
        self._buttons()

    def _action_job(self, action: Action, row: Row | None, text: str) -> None:
        message, detail, error = "", None, ""
        try:
            result = action.run(self.app, row, text)
            message, detail = result if isinstance(result, tuple) else (result, None)
        except ManagerError as e:
            error = str(e)
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
        self.app._call(self._acted, message, detail, error)

    def _acted(self, message: str, detail: str | None, error: str) -> None:
        if not self.is_attached:
            return
        self.busy = False
        if error:
            self._status(error, "error")
            self._buttons()
            return
        self._status(message, "ok")
        self._note = message                       # kept on screen across the reload that follows
        if detail is not None:                     # a search: show the answer, keep the list
            self._detail(Text(detail))
            self._buttons()
        else:
            self._start(self._load_job)            # something changed: read the list again

    # ---------------------------------------------------------------- events

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        """Show the highlighted entry."""
        event.stop()
        row = self._row()
        if row is not None:
            self._detail(Text(row.detail))
        self._buttons()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Close, or start the pressed action."""
        event.stop()
        if event.button.id == "mgr_close":
            self.dismiss(None)
            return
        action = next((a for a in self.manager.actions if f"act_{a.id}" == event.button.id), None)
        if action is None or self.busy:
            return
        row = self._row()
        if action.needs_row and row is None:
            self._status("select an entry first", "warn")
            return
        if action.command:                         # runs in the chat (it asks its own questions)
            command = action.command
            if row is not None and "{key}" in command:         # "/resume {key}" becomes "/resume <the selected row's key>"
                if not re.fullmatch(r"[\w.:-]+", row.key):      # the key goes into a slash command: plain characters only
                    self._status("that entry cannot be used here", "error")
                    return
                command = command.replace("{key}", row.key)
            self.dismiss(None)
            self.app._start_toolbar_command(self.app.run_command, command)
        elif action.input_label:
            box = self.query_one("#mgr-input", Input)
            self.pending, box.display, box.value, box.placeholder = action, True, "", action.input_label
            box.focus()
            self._status("type it and press Enter  (Esc cancels)", "warn")
        elif action.confirm:
            self.app.push_screen(ConfirmScreen(action.confirm, body=row.title if row else "", yes="Yes", no="No"),
                                 lambda yes: self._confirmed(yes, action, row))
        else:
            self._run(action, row, "")

    def _confirmed(self, yes: bool | None, action: Action, row: Row | None) -> None:
        if yes:
            self._run(action, row, "")

    def _run(self, action: Action, row: Row | None, text: str) -> None:
        self._status("working…", "warn")
        self._start(self._action_job, action, row, text)
        self._buttons()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter in the box: run the action that asked for the text."""
        event.stop()
        action, text = self.pending, event.value.strip()
        if action is None:
            return
        if not text:
            self._status("type something first  (Esc cancels)", "warn")
            return
        self.pending = None
        box = self.query_one("#mgr-input", Input)
        box.display = False
        self.query_one("#mgr-list", OptionList).focus()
        self._run(action, self._row(), text)

    def action_close(self) -> None:
        """Esc: cancel a half-typed button first, otherwise close the pane."""
        box = self.query_one("#mgr-input", Input)
        if box.display:
            self.pending, box.display = None, False
            self.query_one("#mgr-list", OptionList).focus()
            self._status("")
            return
        self.dismiss(None)
