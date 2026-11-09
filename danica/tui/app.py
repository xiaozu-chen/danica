"""
app.py - Danica as a Textual app (Claude Code style).

Run from the project root:   python -m danica.tui
Live CSS reload while styling:   textual run --dev danica.tui.app:DanicaApp

Threading model (the important part):
  UI thread     Textual's event loop. Only this thread touches widgets.
  worker thread agent.run(), slash commands and session building. They may block (model calls,
                tools, permission dialogs). They talk to the UI only through
                  - EventBridge: agent events -> post_message(AgentEvent)
                  - TextualUI:   ui.log/say (hop to the UI thread), ui.confirm/permission (modal + wait)
"""

from __future__ import annotations

import threading
from collections import deque
from datetime import datetime

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Input, Static

from danica.app_core import build_session, shutdown, short_path
from danica.artifacts import collect
from danica.imageview import ImageError, check_image
from danica.present import PresentFilter
from danica.tui.bridge import AgentEvent, EventBridge, RequestDone, TextualUI
from danica.tui.commands import dispatch
from danica.tui.widgets import (ACCENT, ArtifactsList, AssistantMessage, BusyLine, Citations,
                                Notice, PromptInput, Say, StatusBar, ThinkingBlock, ToolCall,
                                UserMessage, image_widget, welcome_panel)

IDLE_PLACEHOLDER = "Ask Danica…  (/help for commands)"
BUSY_PLACEHOLDER = "Working…  Esc stops the task"


class DanicaApp(App):
    CSS_PATH = "app.tcss"
    TITLE = "Danica"

    # Input itself binds ctrl+c (copy) and ctrl+d (delete), so ours are priority bindings.
    BINDINGS = [
        Binding("ctrl+c", "interrupt", "Stop", priority=True, show=False),
        Binding("escape", "interrupt", "Stop", show=False),
        Binding("ctrl+d", "eof", "Quit", priority=True, show=False),
        Binding("ctrl+o", "toggle_tools", "Expand tool output", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.ui_thread = 0
        self.ui = TextualUI(self)
        self.bridge = EventBridge(self)

        self.is_ready = False            # a session exists
        self.working = False             # a request or command is running
        self.work_kind = ""              # "request" | "command" | "starting"

        # The 5 session objects (replaced by /new, later /project, /resume)
        self.ws = self.audit = self.checkpoints = self.policy = self.agent = None
        self.session_id = ""
        self.project: dict | None = None
        self.plan_next = False

        # Per-request streaming state (UI thread only)
        self.filter = PresentFilter()
        self.presented: set[str] = set()
        self._assistant: AssistantMessage | None = None
        self._thinking: ThinkingBlock | None = None
        self._pending: deque[ToolCall] = deque()

    # ------------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="transcript")
        yield BusyLine(id="busy")
        with Horizontal(id="prompt-box"):
            yield Static(">", id="caret")
            yield PromptInput(placeholder=IDLE_PLACEHOLDER, id="prompt")
        yield StatusBar(id="status")

    def on_mount(self) -> None:
        self.ui_thread = threading.get_ident()
        transcript = self.query_one("#transcript", VerticalScroll)
        if hasattr(transcript, "anchor"):        # stay pinned to the bottom until the user scrolls up
            transcript.anchor()
        self.query_one(PromptInput).focus()
        self.set_working(True, "starting")
        self.boot()

    def on_unmount(self) -> None:
        shutdown(self.ws)

    # ------------------------------------------------------------------ sessions (worker thread)

    @work(thread=True, group="session")
    def boot(self) -> None:
        try:
            self._build(None)
        except Exception as exc:
            self.ui.log("error", f"could not start a session: {type(exc).__name__}: {exc}")
            self._call(self.set_working, False)

    def swap_session(self, project: dict | None) -> None:
        """Build a new session (worker thread: it may open dialogs) and show it."""
        self._build(project)

    def _build(self, project: dict | None) -> None:
        new_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        ws, audit, checkpoints, policy, agent = build_session(new_id, self.ui, self.bridge, project)
        self.ws, self.audit, self.checkpoints, self.policy, self.agent = ws, audit, checkpoints, policy, agent
        self.session_id, self.project = new_id, project
        audit.append("session_start", workspace=str(ws.root), mode="project" if project else "chat")
        self._call(self.after_session, f"New session {new_id}." if self.is_ready else "")

    def after_session(self, note: str) -> None:
        transcript = self.query_one("#transcript", VerticalScroll)
        transcript.remove_children()
        transcript.mount(Static(welcome_panel(self.project["name"] if self.project else None,
                                              short_path(self.ws.root)), classes="welcome"))
        if note:
            transcript.mount(Notice("ok", note))
        self._reset_stream_state()
        self.is_ready = True
        self.set_working(False)

    def _call(self, fn, *args):
        """Worker thread -> UI thread, tolerant of shutdown."""
        try:
            return self.call_from_thread(fn, *args)
        except RuntimeError:
            return None

    # ------------------------------------------------------------------ input

    def on_input_submitted(self, event: Input.Submitted) -> None:
        prompt = event.input
        text = event.value.strip()
        prompt.value = ""
        if not text:
            return
        if not self.is_ready:
            self.add_notice("warning", "Still starting up…")
            return
        if self.working:
            self.add_notice("warning", "Danica is busy. Esc or Ctrl+C stops the current task.")
            return
        if isinstance(prompt, PromptInput):
            prompt.remember(text)
        if text.startswith("/"):
            self.set_working(True, "command", kind="command")
            self.run_command(text)
        else:
            self.start_request(text)

    @work(thread=True, group="command")
    def run_command(self, text: str) -> None:
        try:
            dispatch(self, text)
        except Exception as exc:
            self.ui.log("error", f"{type(exc).__name__}: {exc}")
        finally:
            self._call(self.set_working, False)

    # ------------------------------------------------------------------ one request

    def start_request(self, text: str) -> None:
        self.mount_block(UserMessage(text))
        self._reset_stream_state()
        self.set_working(True, "working", kind="request")
        self._request_worker(text)

    def _reset_stream_state(self) -> None:
        self.filter = PresentFilter()
        self.presented.clear()
        self._assistant = None
        self._thinking = None
        self._pending.clear()

    @work(thread=True, group="request")
    def _request_worker(self, text: str) -> None:
        agent, ws = self.agent, self.ws
        result = error = found = None
        try:
            result = agent.run(text)
            try:
                found = collect(ws, agent.checkpoints, agent.request_snap, agent.touched, set(self.presented))
            except Exception:
                found = None                      # a failed file listing must not hide the answer
        except KeyboardInterrupt:
            error = "interrupted"
        except Exception as exc:                  # most likely: llama-server is not running
            error = f"{type(exc).__name__}: {exc}"
        self.post_message(RequestDone(result, error, found))

    # ------------------------------------------------------------------ agent events (UI thread)

    def on_agent_event(self, ev: AgentEvent) -> None:
        kind, data = ev.kind, ev.data
        if kind == "content":
            if self._thinking is not None:
                self._thinking.finish()
                self._thinking = None
            for k, v in self.filter.feed(data):
                self._emit(k, v)
            return
        for k, v in self.filter.flush():          # anything else ends the text run
            self._emit(k, v)

        if kind == "thinking":
            self._close_assistant()
            if self._thinking is None:
                self._thinking = ThinkingBlock()
                self.mount_block(self._thinking)
            self._thinking.append(data)
            return

        self._close_assistant()
        if self._thinking is not None:
            self._thinking.finish()
            self._thinking = None

        if kind == "tool_call":
            name, args = data
            widget = ToolCall(name, args)
            self._pending.append(widget)
            self.mount_block(widget)
        elif kind == "tool_result":
            if self._pending:
                self._pending.popleft().set_result(data)
            else:
                self.mount_block(Say(str(data)[:400], "dim"))
            self.refresh_status()
        elif kind == "notice":
            text = str(data)
            self.add_notice("error" if text.startswith("SECURITY") else "warning", text)
        elif kind == "citations":
            self.mount_block(Citations(str(data)))

    def _emit(self, kind: str, value: str) -> None:
        """One piece from the PresentFilter: answer text, or a picture the answer presents."""
        if kind == "text":
            if self._assistant is None:
                self._assistant = AssistantMessage()
                self.mount_block(self._assistant)
            self._assistant.append(value)
        else:
            self._close_assistant()
            self.show_image(value)

    def _close_assistant(self) -> None:
        if self._assistant is not None:
            self._assistant.finish()
            self._assistant = None

    def show_image(self, rel: str) -> None:
        try:
            path = check_image(self.ws, rel)
            label = self.ws.display(path)
            self.presented.add(label)
            self.mount_block(image_widget(path, label))
        except ImageError as exc:
            self.add_notice("warning", f"  Could not show {rel}: {exc}")

    def on_request_done(self, msg: RequestDone) -> None:
        for k, v in self.filter.flush():
            self._emit(k, v)
        self._close_assistant()
        if self._thinking is not None:
            self._thinking.finish()
            self._thinking = None

        if msg.error == "interrupted":
            self.add_notice("warning", "[interrupted]")
        elif msg.error:
            self.add_notice("error", msg.error)
        else:
            # cli.py tested startswith("[stopped]"), which never matches "[stopped: ...]"
            if msg.result and msg.result.startswith("[stopped"):
                self.add_notice("warning", msg.result)
            todo = [p for p in (self.agent.charts_made if self.agent else []) if p not in self.presented]
            if todo:
                self.mount_block(Say("Charts made in this request:", "dim"))
                for p in todo:
                    self.show_image(p)
            if msg.found is not None and (msg.found.items or msg.found.deleted):
                self.mount_block(ArtifactsList(msg.found))
        self.set_working(False)

    # ------------------------------------------------------------------ output helpers (UI thread)

    def mount_block(self, widget) -> None:
        self.query_one("#transcript", VerticalScroll).mount(widget)
        if not hasattr(VerticalScroll, "anchor"):
            self.call_after_refresh(self.query_one("#transcript", VerticalScroll).scroll_end, animate=False)

    def add_notice(self, level: str, text: str) -> None:
        self.mount_block(Notice(level, text))

    def add_say(self, text: str, style: str | None = None) -> None:
        self.mount_block(Say(text, style))

    # ------------------------------------------------------------------ busy / status

    def set_working(self, working: bool, label: str = "", kind: str = "") -> None:
        self.working = working
        self.work_kind = kind or label if working else ""
        busy = self.query_one(BusyLine)
        prompt = self.query_one(PromptInput)
        if working:
            busy.start(label)
            prompt.placeholder = BUSY_PLACEHOLDER
        else:
            busy.stop()
            prompt.placeholder = IDLE_PLACEHOLDER
            prompt.focus()
        self.refresh_status()

    def status_text(self) -> Text:
        agent = self.agent
        if agent is None:
            return Text("starting…", style="dim")
        level = agent.tracker.level(agent.messages)
        color = {"ok": "green", "prune": "yellow", "compact": "yellow", "stop": "red"}.get(level, "white")
        t = Text()
        t.append(f"ctx {agent.tracker.bar(agent.messages)}", style=color)
        t.append(" | " + ("think" if agent.thinking else "fast"))
        if self.policy is not None and self.policy.full_access:
            t.append(" | FULL ACCESS", style="bold red")
        if self.project:
            t.append(f" | project: {self.project['name']}")
        if self.plan_next:
            t.append(" | plan next")
        if agent.proposals and agent.proposals.count():
            t.append(f" | {agent.proposals.count()} proposed note(s)")
        if self.ws is not None and self.ws.web and self.ws.web.tainted:
            t.append(" | TAINTED", style="bold red")
        if self.ws is not None:
            t.append(" | Workspace: " + short_path(self.ws.root), style="#C6C0BE")
        return t

    def refresh_status(self) -> None:
        try:
            self.query_one(StatusBar).update(self.status_text())
        except Exception:
            pass                                   # the status bar must never break a request

    # ------------------------------------------------------------------ actions

    def action_interrupt(self) -> None:
        if self.working and self.work_kind == "request" and self.agent is not None:
            self.agent.cancel()
            self.add_notice("warning", "Stopping… (a tool that is already running finishes first)")
        elif self.working:
            self.notify("A command is running; it cannot be stopped.", severity="warning")
        else:
            prompt = self.query_one(PromptInput)
            if prompt.value:
                prompt.value = ""
            else:
                self.notify("Ctrl+D or /exit to quit", timeout=2)

    def action_eof(self) -> None:
        self.exit()

    def check_action(self, action: str, parameters: tuple):
        # Ctrl+D quits only on an empty prompt; otherwise it falls through to Input's delete-right
        if action == "eof":
            try:
                return not self.query_one(PromptInput).value
            except Exception:
                return True
        return True

    def action_toggle_tools(self) -> None:
        for widget in self.query(ToolCall):
            widget.toggle()


def main() -> None:
    DanicaApp().run()


if __name__ == "__main__":
    main()