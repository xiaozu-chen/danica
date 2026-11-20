"""
approval_menu.py - Approval questions as an INLINE MENU, styled like the slash-command popup.

Instead of a dialog that covers the screen, the question appears just above the prompt:

    ╭────────────────────────────────────────────────────────────╮
    │ Permission needed (execute)                                │
    │ runs a program; model: Installs a package into the project │
    │ npm install --save-dev vite                                │
    │ ▌1  Yes, once                                              │
    │  2  Yes, for this session                                  │
    │  3  No                                                     │
    ╰────────────────────────────────────────────────────────────╯

  Up / Down move, Enter picks, or press the number / letter shown. Esc = the safe answer (No).
  It starts on the SAFE choice, so a stray Enter never allows anything.
  The menu keeps the keyboard until it is answered (the prompt can't be typed into meanwhile).

Used by TextualUI.permission / confirm / choose (bridge.py). The old modal screens (modals.py) remain as the
fallback when  [ui] inline_approvals = false.
"""

from __future__ import annotations

import queue
import threading
from typing import Any, Callable

from rich.text import Text
from textual.containers import Vertical, VerticalScroll
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

_one_at_a_time = threading.Lock()          # subagents can ask too: one menu on screen at a time


class _Choices(OptionList):
    """The options. Number / letter keys pick directly; everything else is the normal list behaviour."""

    def on_key(self, event: Any) -> None:
        """Handle direct key selection before default list navigation."""
        menu = self.parent
        if isinstance(menu, ApprovalMenu):
            value = menu.value_for_key(event.key)
            if value is not menu.NO_MATCH:
                event.stop()
                event.prevent_default()
                menu.finish(value)


class ApprovalMenu(Vertical):
    """spec = {"title", "reason", "body", "options": [{"value", "label", "keys": [...]}],
              "default": index to start on, "cancel": value for Esc, "danger": bool}"""

    NO_MATCH = object()

    DEFAULT_CSS = """
    ApprovalMenu { height: auto; max-height: 22; margin: 0 1; padding: 0 1; background: transparent; border: round #d0d0d0; }
    ApprovalMenu.-danger { border: round #b0b0b0; }
    ApprovalMenu .am-title { height: auto; text-style: bold; color: #d0d0d0; }
    ApprovalMenu.-danger .am-title { color: #d0d0d0; }
    ApprovalMenu .am-reason { height: auto; color: #c0c0c0; }
    ApprovalMenu .am-body { height: auto; max-height: 8; margin: 0; background: transparent; }
    ApprovalMenu .am-body Static { color: #ffffff; }
    ApprovalMenu .am-choices { height: auto; max-height: 8; background: transparent; border: none; padding: 0; }
    ApprovalMenu .am-choices > .option-list--option-highlighted { background: #333333; }
    ApprovalMenu .am-hint { height: 1; color: #999999; }
    """

    def __init__(self, spec: dict, on_done) -> None:
        super().__init__()
        self.spec, self.on_done, self._finished = spec, on_done, False
        if spec.get("danger"):
            self.add_class("-danger")

    def compose(self) -> Any:
        """Compose the permission approval dialog."""
        yield Static(Text(self.spec["title"]), classes="am-title")
        if self.spec.get("reason"):
            yield Static(Text(self.spec["reason"]), classes="am-reason")
        if self.spec.get("body"):
            with VerticalScroll(classes="am-body"):
                yield Static(Text(self.spec["body"]))
        options = self.spec["options"]
        yield _Choices(*[Option(self._label(i, o), id=str(i)) for i, o in enumerate(options)], classes="am-choices")
        yield Static(Text("↑↓ choose   Enter confirm   Esc " + self.spec.get("esc_label", "= no")), classes="am-hint")

    @staticmethod
    def _label(index: int, option: dict) -> Text:
        label = Text()
        label.append(f"{option.get('shown_key', index + 1)}  ", style="bold #ffffff")
        label.append(option["label"])
        return label

    def on_mount(self) -> None:
        """Focus the permission choices after mounting."""
        choices = self.query_one(_Choices)
        choices.highlighted = int(self.spec.get("default", 0))
        choices.focus()

    # -- answering ---------------------------------------------------------------------

    def value_for_key(self, key: str) -> Any:
        """Return the permission value for a pressed key."""
        if key == "escape":
            return self.spec["cancel"]
        for option in self.spec["options"]:
            if key in option.get("keys", ()):
                return option["value"]
        return self.NO_MATCH

    def finish(self, value: Any) -> None:
        """Close the dialog with the selected permission value."""
        if self._finished:
            return
        self._finished = True
        app = self.app
        self.remove()
        try:
            app.query_one("#prompt").focus()                  # give the keyboard back to the prompt
        except Exception:
            pass
        self.on_done(value)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Resolve the dialog when an option is selected."""
        event.stop()
        self.finish(self.spec["options"][int(event.option_id)]["value"])

    def on_descendant_blur(self, event: Any) -> None:
        """Restore focus when a child control loses focus."""
        if not self._finished:                                # keep the keyboard until answered
            self.call_after_refresh(
                lambda: None
                if self._finished
                else self.query_one(_Choices).focus()
            )


def open_menu(app: Any, spec: dict, on_done: Callable[[Any], None]) -> None:
    """UI thread: show the menu just above the prompt."""
    app.mount(ApprovalMenu(spec, on_done), before=app.query_one("#prompt-box"))


def ask(ui: Any, spec: dict) -> Any:
    """Worker thread: show the menu and wait for the answer (None if the app closes meanwhile)."""
    if ui._on_ui_thread():
        raise RuntimeError("a blocking menu was opened from the UI thread; run that code in a worker thread")
    with _one_at_a_time:
        answer: queue.Queue = queue.Queue(maxsize=1)
        try:
            ui.app.call_from_thread(open_menu, ui.app, spec, answer.put)
        except RuntimeError:
            return None
        while True:
            try:
                return answer.get(timeout=0.25)
            except queue.Empty:
                if not ui.app.is_running:
                    return None
