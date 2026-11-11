"""
connection.py - SetupScreen (no server at launch) and DisconnectedScreen (server lost mid-session).

Both dismiss with the server's n_ctx on success. The check runs in a worker thread so the
UI never freezes while it waits for the server.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Input, Static

from danica.config import set_server
from danica.server import ServerError, normalize_url, probe

INTRO = (
    "Danica is a local coding and research assistant. The model runs on your own computer "
    "through llama-server (part of llama.cpp), and nothing leaves your machine.\n\n"
    "Start llama-server with a model first, for example:\n"
    "  llama-server -m your-model.gguf -c 65536 --jinja --port 8080\n"
    "then press Start."
)


class _ServerForm:
    """Shared by both screens: validate the URL box, check the server off the UI thread."""

    def _status(self, text: str, style: str = "") -> None:
        self.query_one("#status", Static).update(Text(text, style=style))

    def _busy(self, busy: bool) -> None:
        self.query_one("#go", Button).disabled = busy
        self.query_one("#url", Input).disabled = busy

    def _start_check(self) -> None:
        try:
            url = normalize_url(self.query_one("#url", Input).value)
        except ServerError as exc:
            self._status(str(exc), "red")
            return
        self._busy(True)
        self._status("Checking...", "yellow")
        self.run_worker(lambda: self._check(url), thread=True, exclusive=True, group="check")

    def _check(self, url: str) -> None:                  # worker thread
        try:
            n_ctx = probe(url, timeout=3.0)
        except ServerError as exc:
            self.app.call_from_thread(self._failed, str(exc))
        else:
            self.app.call_from_thread(self._succeeded, url, n_ctx)

    def _failed(self, message: str) -> None:
        self._busy(False)
        self._status(message, "red")
        self.query_one("#url", Input).focus()

    def _succeeded(self, url: str, n_ctx: int) -> None:
        set_server(url, n_ctx)                           # updates CONFIG and saves a changed URL
        self._status(f"Connected. Context window: {n_ctx:,} tokens.", "green")
        if self in self.app.screen_stack:                # the monitor may have closed us already
            self.dismiss(n_ctx)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "go":
            self._start_check()
        elif event.button.id == "quit":
            self.app.exit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._start_check()


class SetupScreen(_ServerForm, Screen[int]):
    DEFAULT_CSS = """
    SetupScreen { align: center middle; }
    SetupScreen #setup { width: 80%; min-width: 60; max-width: 100; height: auto;
                         padding: 1 3; background: ansi_default; }
    SetupScreen .heading { text-style: bold; margin-top: 1; }
    SetupScreen #status { height: auto; margin: 1 0; }
    SetupScreen .buttons { height: auto; }
    SetupScreen .buttons Button {
        width: 1fr;                  /* equal shares of the row, so every button fits */
        min-width: 0;
        margin-right: 2;
        color: #c9c9c9 !important;
        background: transparent;      /* slightly lighter than the dialog so buttons are visible */
        border: solid #c9c9c9 !important;     /* Forces the solid white border override */
    }
    SetupScreen .buttons Button:hover {
        background: transparent !important;
        text-style: bold !important;
        border: solid #ffffff !important;
        color: #ffffff !important;
    }
    SetupScreen .input{ height: auto; border: solid #c9c9c9 !important; }
    """

    def __init__(self, url: str) -> None:
        super().__init__()
        self.url = url

    def compose(self) -> ComposeResult:
        with Vertical(id="setup"):
            yield Static("Welcome to Danica\n", classes="heading")
            yield Static(INTRO)
            yield Static("Connectivity\n", classes="heading")
            yield Static("llama-server address")
            yield Input(self.url, classes="input", id="url")
            yield Static("", id="status")
            with Horizontal(classes="buttons"):
                yield Button("Start", id="go", variant="primary")
                yield Button("Quit", id="quit")

    def on_mount(self) -> None:
        self.query_one("#url", Input).focus()


class DisconnectedScreen(_ServerForm, ModalScreen[int]):
    DEFAULT_CSS = """
    DisconnectedScreen { align: center middle; }
    DisconnectedScreen #box { width: 80%; min-width: 60; max-width: 100; height: auto;
                         padding: 1 3; background: ansi_default; }
    DisconnectedScreen .heading { text-style: bold; margin-top: 1; }
    DisconnectedScreen #status { height: auto; margin: 1 0; }
    DisconnectedScreen .buttons { height: auto; }
        DisconnectedScreen .buttons Button {
            width: 1fr;                  /* equal shares of the row, so every button fits */
            min-width: 0;
            margin-right: 2;
            color: #c9c9c9 !important;
            background: transparent;      /* slightly lighter than the dialog so buttons are visible */
            border: solid #c9c9c9 !important;     /* Forces the solid white border override */
        }
        DisconnectedScreen .buttons Button:hover {
            background: transparent !important;
            text-style: bold !important;
            border: solid #ffffff !important;
            color: #ffffff !important;
        }
        DisconnectedScreen .input{ height: auto; border: solid #c9c9c9 !important; }
    """

    def __init__(self, url: str, reason: str = "") -> None:
        super().__init__()
        self.url = url
        self.reason = reason

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Static("Disconnected from llama-server", classes="heading")
            yield Static("Danica lost the connection to the model server. It reconnects "
                         "automatically when the server is back, or press Reconnect.")
            yield Static("llama-server address", classes="heading")
            yield Input(self.url, classes="input", id="url")
            yield Static(self.reason, id="status")
            with Horizontal(classes="buttons"):
                yield Button("Reconnect", id="go", variant="warning")
                yield Button("Quit", id="quit")