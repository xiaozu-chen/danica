"""
settings_screen.py - the /config pane: every setting the user may change, grouped in tabs.

A switch, a drop-down or a box per setting. A change takes effect at once and is saved to config.toml (see
settings.py, which holds the list and all the checking). Typed values (numbers, text) are saved when you press Enter.
The little "reset" button next to a setting puts the built-in default back.
Esc closes the pane.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Select, Static, Switch, TabbedContent, TabPane

from danica import settings
from danica.config import CONFIG_PATH
from danica.settings import Setting, SettingError


def _wid(prefix: str, s: Setting) -> str:
    return f"{prefix}_{s.section}_{s.key}"


class SettingsScreen(ModalScreen[None]):
    """Edit Danica's settings. Opened by /config."""

    BINDINGS = [Binding("escape", "close", "Close")]

    DEFAULT_CSS = """
    SettingsScreen { align: center middle; background: black 60%; }
    SettingsScreen .cfg-box { width: 94%; max-width: 112; height: 92%; padding: 1 2; background: transparent; border: round #4a4a4a; }
    SettingsScreen TabbedContent { height: 1fr; }
    SettingsScreen TabPane { padding: 1 0 0 0; }
    SettingsScreen .cfg-scroll { height: 1fr; scrollbar-size-vertical: 1; }
    SettingsScreen .cfg-row { height: auto; margin-bottom: 1; }
    SettingsScreen .cfg-label { width: 1fr; height: auto; padding-right: 2; }
    SettingsScreen .cfg-ctl { width: 26; }
    SettingsScreen Switch.cfg-ctl { width: auto; }
    SettingsScreen .cfg-intro { height: auto; margin-bottom: 1; }
    SettingsScreen .cfg-reset { width: 9; min-width: 9; margin-left: 1; background: transparent; }
    SettingsScreen #cfg-status { height: auto; min-height: 1; margin-top: 1; }
    SettingsScreen .cfg-buttons { height: auto; margin-top: 1; }
    SettingsScreen .cfg-buttons Button { width: 1fr; background: transparent; }
    """

    def __init__(self) -> None:
        super().__init__()
        self.by_id = {_wid("cfg", s): s for s in settings.SETTINGS}       # widget id -> its setting
        self.reset_id = {_wid("rst", s): s for s in settings.SETTINGS}

    # ---------------------------------------------------------------- layout

    def compose(self) -> ComposeResult:
        """Compose the title, one tab per group, a status line and the close button."""
        with Vertical(classes="cfg-box"):
            yield Static(Text("Settings", style="bold #ffffff"), classes="dialog-title")
            yield Static(Text(f"{CONFIG_PATH}\nChanges apply now and are saved to this file. Enter saves a typed value.",
                              style="dim"), classes="cfg-intro")
            with TabbedContent():
                for n, group in enumerate(settings.groups()):
                    with TabPane(group, id=f"cfg_tab{n}"):
                        with VerticalScroll(classes="cfg-scroll"):
                            for s in settings.in_group(group):
                                yield self._row(s)
            yield Static("", id="cfg-status")
            with Horizontal(classes="cfg-buttons"):
                yield Button("Close  (Esc)", id="cfg_close")

    def _label(self, s: Setting) -> Text:
        text = Text()
        text.append(s.label, style="bold #ffffff")
        text.append(f"   {s.path}", style="dim")
        text.append("\n" + s.help, style="#b0b0b0")
        range_note = f" (allowed: {s.lo:g} to {s.hi:g})" if s.kind in ("int", "float") else ""
        text.append(f"\ndefault: {settings.show(s, settings.default(s))}{range_note}", style="dim")
        return text

    def _control(self, s: Setting):
        value = settings.current(s)
        if s.kind == "bool":
            return Switch(value=bool(value), id=_wid("cfg", s), classes="cfg-ctl")
        if s.kind == "choice":
            options = [(label, label) for label, _ in s.choices]
            shown = settings.show(s, value)
            if shown not in [label for label, _ in options]:        # a value written by hand that isn't in the list
                options.append((shown, shown))
            return Select(options, value=shown, allow_blank=False, id=_wid("cfg", s), classes="cfg-ctl")
        return Input(value=str(value), id=_wid("cfg", s), classes="cfg-ctl")

    def _row(self, s: Setting) -> Horizontal:
        return Horizontal(Static(self._label(s), classes="cfg-label"), self._control(s),
                          Button("reset", id=_wid("rst", s), classes="cfg-reset"), classes="cfg-row")

    # ---------------------------------------------------------------- helpers

    def _status(self, message: str, kind: str = "ok") -> None:
        style = {"ok": "#7ddc7d", "error": "#ff7b7b", "warn": "#e6c15a"}[kind]
        self.query_one("#cfg-status", Static).update(Text(message, style=style))

    def _show(self, s: Setting) -> None:
        """Make the control show the value that is really in use (after a refused entry or a reset)."""
        widget = self.query_one("#" + _wid("cfg", s))
        value = settings.current(s)
        if isinstance(widget, Switch):
            widget.value = bool(value)
        elif isinstance(widget, Select):
            shown = settings.show(s, value)
            if shown in [label for label, _ in s.choices] or shown in [o[0] for o in widget._options if o[0] is not None]:
                widget.value = shown
        elif isinstance(widget, Input):
            widget.value = str(value)

    def _commit(self, s: Setting, raw) -> None:
        """A control was changed: check, apply, save, and say what happened."""
        try:
            value = settings.parse(s, raw)
        except SettingError as e:
            self._status(f"{s.label}: {e}. Nothing changed.", "error")
            self._show(s)
            return
        if value == settings.current(s):
            return                                           # nothing to do (also swallows the event of a control being filled in)
        problem = settings.change(s, value, self.app)
        if problem:
            self._status(f"{s.label} = {settings.show(s, value)} for this session only: it could not be saved ({problem})", "warn")
        else:
            self._status(f"saved: {s.label} = {settings.show(s, value)}" + (f"  ({s.note})" if s.note else ""), "ok")
        if s.kind in ("int", "float"):
            self._show(s)                                    # tidy what was typed ("07" -> 7)

    # ---------------------------------------------------------------- events

    def on_switch_changed(self, event: Switch.Changed) -> None:
        """A switch was flipped."""
        event.stop()
        s = self.by_id.get(event.switch.id or "")
        if s:
            self._commit(s, event.value)

    def on_select_changed(self, event: Select.Changed) -> None:
        """A drop-down choice was made."""
        event.stop()
        s = self.by_id.get(event.select.id or "")
        if s and isinstance(event.value, str):
            self._commit(s, event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter in a number or text box."""
        event.stop()
        s = self.by_id.get(event.input.id or "")
        if s:
            self._commit(s, event.value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Close, or reset one setting to its default."""
        event.stop()
        if event.button.id == "cfg_close":
            self.dismiss(None)
            return
        s = self.reset_id.get(event.button.id or "")
        if s:
            problem = settings.reset(s, self.app)
            self._show(s)
            self._status(f"{s.label} is back to its default ({settings.show(s, settings.default(s))})"
                         + (f". Not removed from the file: {problem}" if problem else ""), "warn" if problem else "ok")

    def action_close(self) -> None:
        """Esc."""
        self.dismiss(None)
