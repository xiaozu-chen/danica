"""
modals.py - Dialogs that replace the blocking prompt() calls of the old CLI.

Each is a ModalScreen that dismisses with a result; TextualUI._ask() hands that result back
to the waiting worker thread. Esc always means "no" / "deny" (the safe default).
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static


class PermissionScreen(ModalScreen[str]):
    """Result: "once", "session" or "deny"."""

    BINDINGS = [
        Binding("1,y", "choose('once')", "Once"),
        Binding("2,s", "choose('session')", "This session"),
        Binding("3,n,escape", "choose('deny')", "Deny"),
    ]

    def __init__(self, tool: str, summary: str, tier: str, reason: str, can_remember: bool) -> None:
        super().__init__()
        self.tool, self.summary, self.tier = tool, summary, tier
        self.reason, self.can_remember = reason, can_remember

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog warn"):
            yield Static(Text(f"Permission needed ({self.tier})", style="bold #ffff00"), classes="dialog-title")
            yield Static(Text(self.reason), classes="dialog-reason")
            with VerticalScroll(classes="dialog-body"):
                yield Static(Text(self.summary))
            with Horizontal(classes="dialog-buttons"):
                yield Button("1  Yes, once", id="once", variant="success")
                if self.can_remember:
                    yield Button("2  Yes, this session", id="session", variant="primary")
                yield Button("3  Deny", id="deny", variant="error")

    def on_mount(self) -> None:
        self.query_one("#deny", Button).focus()     # Enter alone = deny, like the old CLI

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(event.button.id or "deny")

    def action_choose(self, choice: str) -> None:
        if choice == "session" and not self.can_remember:
            return
        self.dismiss(choice)


class ConfirmScreen(ModalScreen[bool]):
    """Result: True only for an explicit yes."""

    BINDINGS = [
        Binding("y", "answer(True)", "Yes"),
        Binding("n,escape", "answer(False)", "No"),
    ]

    def __init__(self, title: str, body: str = "", detail: str = "",
                 yes: str = "Yes", no: str = "No") -> None:
        super().__init__()
        self.title_text, self.body, self.detail = title, body, detail
        self.yes, self.no = yes, no

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(Text(self.title_text, style="bold #ff9e64"), classes="dialog-title")
            if self.body:
                with VerticalScroll(classes="dialog-body"):
                    yield Static(Text(self.body))
            if self.detail:
                yield Static(Text(self.detail, style="dim"), classes="dialog-reason")
            with Horizontal(classes="dialog-buttons"):
                yield Button(f"y  {self.yes}", id="yes", variant="success")
                yield Button(f"n  {self.no}", id="no", variant="error")

    def on_mount(self) -> None:
        self.query_one("#no", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(event.button.id == "yes")

    def action_answer(self, value: bool) -> None:
        self.dismiss(value)


class ChoiceScreen(ModalScreen):
    """Generic multiple choice. Result: the chosen key, or None for Esc."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, body: str, options: list[tuple[str, str]], detail: str = "") -> None:
        super().__init__()
        self.title_text, self.body, self.options, self.detail = title, body, options, detail

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(Text(self.title_text, style="bold #ff9e64"), classes="dialog-title")
            if self.body:
                with VerticalScroll(classes="dialog-body"):
                    yield Static(Text(self.body))
            if self.detail:
                yield Static(Text(self.detail, style="dim"), classes="dialog-reason")
            with Horizontal(classes="dialog-buttons"):
                for i, (key, label) in enumerate(self.options):
                    yield Button(f"{key}  {label}", id=f"opt{i}",
                                 variant="success" if i == 0 else "default")

    def on_mount(self) -> None:
        self.query_one("#opt0", Button).focus()

    def on_key(self, event) -> None:
        for key, _ in self.options:
            if event.key == key:
                event.stop()
                self.dismiss(key)
                return

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        index = int((event.button.id or "opt0")[3:])
        self.dismiss(self.options[index][0])

    def action_cancel(self) -> None:
        self.dismiss(None)


class TextScreen(ModalScreen):
    """One-line text entry. Result: the text, or None for Esc."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, default: str = "", secret: bool = False) -> None:
        super().__init__()
        self.title_text, self.default, self.secret = title, default, secret      # secret = show dots instead of the text

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(Text(self.title_text, style="bold #ff9e64"), classes="dialog-title")
            yield Input(value=self.default, id="answer", password=self.secret)
            yield Static(Text("Enter to confirm, Esc to cancel", style="dim"), classes="dialog-reason")

    def on_mount(self) -> None:
        field = self.query_one("#answer", Input)
        field.focus()
        field.cursor_position = len(self.default)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss(None)