"""
bridge.py - Glue between the synchronous agent (worker thread) and Textual (UI thread).

  EventBridge  the agent's on_event callback. It only posts a message, which is thread-safe and
               never blocks the agent, so streaming stays fast and ordered.
  TextualUI    implements danica.ui.UI. log/say hop to the UI thread; confirm/permission open a
               modal and BLOCK the calling worker thread until the user answers.
"""

from __future__ import annotations

import queue
import threading

from textual.message import Message
from textual.widgets import RichLog

class AgentEvent(Message):
    """One on_event(kind, data) call from the agent. Handled by DanicaApp.on_agent_event."""

    def __init__(self, kind: str, data) -> None:
        super().__init__()
        self.kind = kind
        self.data = data

class SubagentEvent(Message):
    """One progress event from a running subagent. Handled by DanicaApp.on_subagent_event."""

    def __init__(self, child_id: str, kind: str, data) -> None:
        super().__init__()
        self.child_id = child_id
        self.kind = kind
        self.data = data

class RequestDone(Message):
    """The agent.run() worker finished. `found` is the artifact list (or None)."""

    def __init__(self, result: str | None, error: str | None, found) -> None:
        super().__init__()
        self.result = result
        self.error = error
        self.found = found


class EventBridge:
    def __init__(self, app) -> None:
        self.app = app

    def __call__(self, kind: str, data) -> None:
        self.app.post_message(AgentEvent(kind, data))

class SubagentBridge:
    """The supervisor's on_event callback. Runs on worker threads; post_message is thread-safe."""

    def __init__(self, app) -> None:
        self.app = app

    def __call__(self, child_id: str, kind: str, data) -> None:
        self.app.post_message(SubagentEvent(child_id, kind, data))

class TextualUI:
    def __init__(self, app) -> None:
        self.app = app

    def _on_ui_thread(self) -> bool:
        return threading.get_ident() == self.app.ui_thread

    def _call(self, fn, *args):
        """Run fn on the UI thread (directly if we are already there) and return its result."""
        if self._on_ui_thread():
            return fn(*args)
        try:
            return self.app.call_from_thread(fn, *args)
        except RuntimeError:
            return None                      # the app is shutting down: drop the output

    def _ask(self, screen):
        """Open a modal screen and wait for its result. Worker threads only."""
        if self._on_ui_thread():
            raise RuntimeError("a blocking dialog was opened from the UI thread; "
                               "run that code in a worker thread")
        answer: queue.Queue = queue.Queue(maxsize=1)
        try:
            self.app.call_from_thread(self.app.push_screen, screen, answer.put)
        except RuntimeError:
            return None
        while True:
            try:
                return answer.get(timeout=0.25)
            except queue.Empty:
                if not self.app.is_running:   # app closed while a dialog was open: treat as "no"
                    return None

    # --- danica.ui.UI -------------------------------------------------------------------

    def log(self, level: str, text: str) -> None:
        self._call(self.app.add_notice, level, str(text))

    def say(self, text: str, style: str | None = None) -> None:
        self._call(self.app.add_say, str(text), style)

    def show(self, renderable) -> None:
        self._call(self.app.add_renderable, renderable)

    def _inline(self) -> bool:
        """Approvals as an inline menu above the prompt (approval_menu.py); [ui] inline_approvals = false = old dialogs."""
        from danica.config import CONFIG
        return bool(CONFIG.get("ui", {}).get("inline_approvals", True))

    def confirm(self, title: str, body: str = "", detail: str = "",
                yes: str = "Yes", no: str = "No") -> bool:
        if self._inline():
            from danica.tui import approval_menu
            answer = approval_menu.ask(self, {
                "title": title, "reason": detail, "body": body, "default": 1, "cancel": False,     # starts on "No"
                "options": [{"value": True, "label": yes, "keys": ("1", "y"), "shown_key": "y"},
                            {"value": False, "label": no, "keys": ("2", "n"), "shown_key": "n"}]})
            return answer is True
        from danica.tui.modals import ConfirmScreen
        return self._ask(ConfirmScreen(title, body, detail, yes, no)) is True

    def permission(self, tool: str, summary: str, tier: str, reason: str,
                   can_remember: bool) -> str:
        if self._inline():
            from danica.tui import approval_menu
            options = [{"value": "once", "label": "Yes, once", "keys": ("1", "y")}]
            if can_remember:
                options.append({"value": "session", "label": "Yes, for this session", "keys": ("2", "s")})
            options.append({"value": "deny", "label": "No", "keys": (str(len(options) + 1), "n")})
            answer = approval_menu.ask(self, {
                "title": f"Permission needed ({tier})", "reason": reason, "body": summary,
                "options": options, "default": len(options) - 1, "cancel": "deny",             # starts on "No"
                "danger": str(tier) == "destructive"})
            return answer if answer in ("once", "session") else "deny"
        from danica.tui.modals import PermissionScreen
        answer = self._ask(PermissionScreen(tool, summary, tier, reason, can_remember))
        return answer if answer in ("once", "session") else "deny"

    def choose(self, title: str, body: str, options: list[tuple[str, str]],
               detail: str = "") -> str | None:
        if self._inline():
            from danica.tui import approval_menu
            answer = approval_menu.ask(self, {
                "title": title, "reason": detail, "body": body, "default": 0, "cancel": None,
                "esc_label": "= cancel",
                "options": [{"value": key, "label": label, "keys": (key,), "shown_key": key} for key, label in options]})
            return answer if isinstance(answer, str) else None
        from danica.tui.modals import ChoiceScreen
        answer = self._ask(ChoiceScreen(title, body, options, detail))
        return answer if isinstance(answer, str) else None

    def ask_text(self, title: str, default: str = "", secret: bool = False) -> str | None:
        from danica.tui.modals import TextScreen
        answer = self._ask(TextScreen(title, default, secret))
        return answer if isinstance(answer, str) else None

    def ask_screen(self, screen):
        """Show a screen that dismisses with a value; block the worker thread until it does."""
        return self._ask(screen)

class ServerEvent(Message):
    """The server monitor saw a change: kind is "offline", "online", or "ctx_changed"."""
    def __init__(self, kind: str, data) -> None:
        super().__init__()
        self.kind = kind
        self.data = data