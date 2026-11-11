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
    "A local-first, CLI-only agentic assistant for coding, research, presentations, notes and productivity.\n\n"
    "Start llama-server with a model first, for example:\n"
    "  [yellow]llama-server -m your-model.gguf -c 65536 --jinja --port 8080[/yellow]\n\n"
    "Having a trouble? Visit llama.cpp / llama-server documentation:\n"
    "  [cyan]https://llama.app/docs/serve[/cyan]\n\n"
    "Recommended model: Qwen3.5 family\n\n"
    "then press Start.\n"
    ""
)

_FORM_CSS = """
{S} { align: center middle; }
{S} {BOX} { width: 80%; min-width: 60; max-width: 100; height: auto;
            padding: 1 3; background: ansi_default; }
{S} .heading { text-style: bold; margin-top: 1; }
{S} #status { height: auto; margin: 1 0; text-wrap: wrap; }
{S} .buttons { height: auto; }
{S} .input { height: auto; border: solid #c9c9c9 !important; }

/* resting */
{S} .buttons Button {
    width: 1fr;
    min-width: 0;
    margin-right: 2;
    color: #c9c9c9 !important;
    background: transparent !important;
    background-tint: transparent !important;
    tint: transparent !important;
    border: solid #c9c9c9 !important;
    text-style: none !important;
}
/* hover */
{S} .buttons Button:hover {
    color: #ffffff !important;
    border: solid #ffffff !important;
    text-style: bold !important;
}
/* focus (what you get after clicking) */
{S} .buttons Button:focus {
    color: #ffffff !important;
    border: solid #ffffff !important;
    text-style: bold !important;      /* kills the default "bold reverse" */
}
/* the press flash */
{S} .buttons Button.-active {
    color: #ffffff !important;
    border: solid #ffffff !important;
    tint: transparent !important;
}
/* disabled while checking */
{S} .buttons Button:disabled {
    color: #6b6b6b !important;
    border: solid #6b6b6b !important;
    opacity: 1 !important;
    text-style: none !important;
}
{S} .buttons Button.-busy {
    color: #ffffff !important;
    border: solid #ffffff !important;
    text-style: bold !important;
}
"""


def _css(selector: str, box: str) -> str:
    return _FORM_CSS.replace("{S}", selector).replace("{BOX}", box)


class _ServerForm:
    """Shared by both screens: validate the URL box, check the server off the UI thread."""
    _checking = False

    def _status(self, text: str, style: str = "") -> None:
        self.query_one("#status", Static).update(Text(text, style=style))

    def _busy(self, busy: bool) -> None:
        self._checking = busy
        self.query_one("#go", Button).set_class(busy, "-busy")
        self.query_one("#url", Input).read_only = busy   # blocks typing, keeps normal look

    def _start_check(self) -> None:
        if self._checking:                               # ignore clicks/Enter while checking
            return
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
    DEFAULT_CSS = _css("SetupScreen", "#setup")

    BODY = (
        ""
    )

    def __init__(self, url: str) -> None:
        super().__init__()
        self.url = url

    def compose(self) -> ComposeResult:
        with Vertical(id="setup"):
            yield Static("""

██████   █████  ███    ██ ██  ██████  █████ 
██   ██ ██   ██ ████   ██ ██ ██      ██   ██
██   ██ ███████ ██ ██  ██ ██ ██      ███████
██   ██ ██   ██ ██  ██ ██ ██ ██      ██   ██
██████  ██   ██ ██   ████ ██  ██████ ██   ██   ✦ v1.4.0

""", classes="heading")
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
    DEFAULT_CSS = _css("DisconnectedScreen", "#box")

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