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

import random
import threading
from collections import deque
from datetime import datetime
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Static
from rich.console import Console
from rich.markup import escape

from danica.app_core import build_session, shutdown, short_path
from danica.artifacts import collect
from danica.config import CONFIG
from danica.imageview import ImageError, check_image
from danica.plan_flow import run_plan_mode
from danica.present import PresentFilter
from danica.tui.bridge import AgentEvent, EventBridge, RequestDone, TextualUI
from danica.tui.commands import command_names, dispatch
from danica.tui.engine import Engine
from danica.tui.widgets import (ACCENT, ArtifactsList, AssistantMessage, BusyLine, Citations,
                                CommandPopup, Notice, PromptInput, Say, StatusBar, ThinkingBlock, ToolCall,
                                UserMessage, image_widget, welcome_panel)

IDLE_PLACEHOLDER = f"Ask Danica…"
BUSY_PLACEHOLDER = "Working…  Esc stops the task"

WORKING_LABELS = [
    # Original labels
    "Working",
    "I Hate Work",
    "My Name is Dan btw",
    "badda bing, badda boom",
    "Existential crisis",
    "Hi Boss!!!",
    "Shush, I'm Working",
    " ",
    "Thinking harder than Claude himself",
    "I Think Hard, ChatGPT Think Smart",
    "Why Not Give this Task to Claude? I mean he's better.",
    "I'm Doing Rocket Science",
    "...Code goes KABOOM!",
    "Danica'ing"
    # AI & Bot Banter
    "Consulting my single remaining brain cell",
    "Beep boop, pretend I'm working",
    "Overthinking this so you don't have to",
    "Gemini is typing... aggressively",
    "Whispering to the GPUs",
    # Coding & Tech Humor
    "Asking Stack Overflow for emotional support",
    "It worked on my machine",
    "Turning coffee into broken code",
    "Ctrl+C, Ctrl+V, call it a day",
    "Shh... if I don't move, the bugs can't see me",
    # General Productivity Memes
    "1% actual work, 99% staring at the screen",
    "Error 404: Motivation Not Found",
    "Wait, you guys are getting paid?",
    "I promise I know what I'm doing (I don't)",
    "Applying aggressive keyboard tapping for effect",
    "Simulating productivity",
    "If it works, don't touch it",
    "Fueled by panic and cold coffee",
    "Brb, questioning all my life choices",
]

class _PlanCancelled(Exception):
    pass


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
        self.theme = "ansi-dark"
        self.ui_thread = 0
        self.ui = TextualUI(self)
        self.engine = Engine(self._engine_error)
        self._poll_queued = False
        self.bridge = EventBridge(self)

        self.is_ready = False            # a session exists
        self.working = False             # a request or command is running
        self.work_kind = ""              # "request" | "command" | "starting"

        # The 5 session objects (replaced by /new, later /project, /resume)
        self.ws = self.audit = self.checkpoints = self.policy = self.agent = None
        self.session_id = ""
        self.project: dict | None = None
        self.plan_next = False                       # set by a bare /plan: the NEXT request gets a plan
        self.plan_auto = CONFIG.get("planning", {}).get("auto", "ask")   # "ask" | "off"
        self.artifacts: list = []                    # files the LAST request made (for /open, /save)
        self.launch_dir = Path.cwd()                 # /project with no argument means THIS folder
        self.current_command = ""

        # Per-request streaming state (UI thread only)
        self.filter = PresentFilter()
        self.presented: set[str] = set()
        self._assistant: AssistantMessage | None = None
        self._thinking: ThinkingBlock | None = None
        self._pending: deque[ToolCall] = deque()
        self._stream_widgets: dict[int, ToolCall] = {}   # tool calls still being streamed, by index
        self._stream_queue: deque[ToolCall] = deque()    # same, in order, waiting for their final args

    # ------------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="transcript")
        yield BusyLine(id="busy")
        yield CommandPopup(id="popup")
        with Horizontal(id="prompt-box"):
            yield Static(">", id="caret")
            yield PromptInput(placeholder=IDLE_PLACEHOLDER, id="prompt")
        yield StatusBar(id="status")

    def on_mount(self) -> None:
        self.ui_thread = threading.get_ident()
        transcript = self.query_one("#transcript", VerticalScroll)
        if hasattr(transcript, "anchor"):        # stay pinned to the bottom until the user scrolls up
            transcript.anchor()
        self.query_one(CommandPopup).hide()
        self.query_one(PromptInput).focus()
        self.set_working(True, "starting")
        self.engine.start()
        self.engine.submit(self.boot)
        self.set_interval(1.0, self._tick_reminders)

    def on_unmount(self) -> None:
        # Stop a running request, then close sqlite-backed objects ON the engine thread
        if self.agent is not None:
            self.agent.cancel()
        ws = self.ws
        self.engine.submit(shutdown, ws)
        self.engine.stop()

    def _engine_error(self, exc: BaseException) -> None:
        if isinstance(exc, KeyboardInterrupt):
            return
        self._call(self.add_notice, "error", f"internal error: {type(exc).__name__}: {exc}")
        self._call(self.set_working, False)

    # ------------------------------------------------------------------ sessions (worker thread)

    def boot(self) -> None:
        try:
            self.swap_session(None, boot=True)
        except Exception as exc:
            self.ui.log("error", f"could not start a session: {type(exc).__name__}: {exc}")
            self._call(self.set_working, False)

    def swap_session(self, project: dict | None, note: str | None = None, boot: bool = False) -> None:
        """Build a new session (worker thread: it may open dialogs) and show it.
        `note` may contain {id}; the default depends on the mode."""
        new_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        parts = build_session(new_id, self.ui, self.bridge, project)
        default = f"New session {new_id}."
        if project:
            default = (f"Project mode: {project['name']}\n  folder: {parts[0].root}\n"
                       f"{default} The previous session stays available via /sessions and /resume.")
        text = "" if boot else (note.format(id=new_id) if note else default)
        self.adopt_session(parts, new_id, project, text, start_mode="project" if project else "chat")

    def adopt_session(self, parts, session_id: str, project: dict | None, note: str,
                      start_mode: str | None = None) -> None:
        """Make `parts` (from build_session / open_resumed) the current session. Worker thread."""
        ws, audit, checkpoints, policy, agent = parts
        self.ws, self.audit, self.checkpoints, self.policy, self.agent = ws, audit, checkpoints, policy, agent
        self.session_id, self.project = session_id, project
        self.artifacts = []
        if start_mode:
            extra = {"project": project["id"]} if project else {}
            audit.append("session_start", workspace=str(ws.root), mode=start_mode, **extra)
        self._call(self.after_session, note, list(agent.messages[1:]))

    def after_session(self, note: str, history: list | None = None) -> None:
        transcript = self.query_one("#transcript", VerticalScroll)
        transcript.remove_children()
        transcript.mount(Static(welcome_panel(self.project["name"] if self.project else None,
                                              short_path(self.ws.root)), classes="welcome"))
        if history:                                   # a resumed session: replay the last exchanges
            from danica.compaction import is_summary
            entries = []
            for m in history:
                content = m.get("content")
                if not isinstance(content, str) or not content.strip():
                    continue
                if m.get("role") == "user" and not content.startswith("[harness reminder") and not is_summary(m):
                    entries.append(UserMessage(content))
                elif m.get("role") == "assistant" and not m.get("tool_calls"):
                    reply = AssistantMessage()
                    reply.append(content)
                    entries.append(reply)
            if entries:
                transcript.mount(Say("— earlier in this session —", "dim"), *entries[-12:])
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

    def on_prompt_input_submitted(self, event: PromptInput.Submitted) -> None:
        prompt = event.prompt
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
        prompt.remember(text)
        if text.startswith("/"):
            self.set_working(True, "command", kind="command")
            self.engine.submit(self.run_command, text)
        else:
            self.start_request(text)

    def run_command(self, text: str) -> None:
        follow = None
        try:
            follow = dispatch(self, text)
        except Exception as exc:
            self.ui.log("error", f"{type(exc).__name__}: {exc}")
        self._call(self._finish_command, follow)

    def _finish_command(self, follow) -> None:
        self.set_working(False)
        if follow and follow[0] == "request":          # e.g. /plan <request>
            self.start_request(follow[1], force_plan=True)

    # ------------------------------------------------------------------ slash-command popup

    def on_text_area_changed(self, event) -> None:
        prompt = self.query_one(PromptInput)
        popup = self.query_one(CommandPopup)
        text = prompt.text
        if "\n" not in text and text.startswith("/") and " " not in text:
            matches = [(n, d) for n, d in command_names() if n.startswith(text)]
            if matches and not (len(matches) == 1 and matches[0][0] == text):
                popup.show_matches(matches)
                prompt.popup_open = True
                return
        popup.hide()
        prompt.popup_open = False

    def on_prompt_input_popup_key(self, event: PromptInput.PopupKey) -> None:
        prompt = self.query_one(PromptInput)
        popup = self.query_one(CommandPopup)
        if event.action == "up":
            popup.action_cursor_up()
        elif event.action == "down":
            popup.action_cursor_down()
        elif event.action == "close":
            popup.hide()
            prompt.popup_open = False
        elif event.action == "accept":
            choice = popup.current()
            if choice is None or choice == prompt.text:
                popup.hide()                          # already exact: Enter just sends it
                prompt.popup_open = False
                prompt.post_message(PromptInput.Submitted(prompt, prompt.text))
            else:
                prompt.value = choice + " "
                popup.hide()
                prompt.popup_open = False

    def on_option_list_option_selected(self, event) -> None:
        """A click on a suggestion completes it."""
        prompt = self.query_one(PromptInput)
        prompt.value = str(event.option.id) + " "
        self.query_one(CommandPopup).hide()
        prompt.popup_open = False
        prompt.focus()

    # ------------------------------------------------------------------ one request

    def start_request(self, text: str, force_plan: bool = False) -> None:
        self.mount_block(UserMessage(text))
        self._reset_stream_state()
        forced = force_plan or self.plan_next
        self.plan_next = False
        self.set_working(True, random.choice(WORKING_LABELS), kind="request")
        self.engine.submit(self._request_worker, text, forced)

    def _reset_stream_state(self) -> None:
        self.filter = PresentFilter()
        self.presented.clear()
        self._assistant = None
        self._thinking = None
        self._pending.clear()
        self._stream_widgets.clear()
        self._stream_queue.clear()

    def _request_worker(self, text: str, forced: bool = False) -> None:
        agent, ws, audit = self.agent, self.ws, self.audit
        result = error = found = None
        try:
            # May open dialogs (plan offer, plan review); Esc during drafting cancels it
            decision, plan, self.plan_auto = run_plan_mode(text, forced, self.plan_auto, agent, audit, self.ui)
            if decision == "cancel":
                raise _PlanCancelled()
            result = agent.run(text, plan=plan)
            try:
                found = collect(ws, agent.checkpoints, agent.request_snap, agent.touched, set(self.presented))
            except Exception:
                found = None                      # a failed file listing must not hide the answer
        except _PlanCancelled:
            error = "plan-cancelled"
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

        if kind == "tool_start":                      # the model just named a tool: show it NOW
            widget = ToolCall(data["name"], {}, streaming=True)
            self._stream_widgets[data["index"]] = widget
            self._stream_queue.append(widget)
            self.mount_block(widget)
            return
        if kind == "tool_delta":                      # one more piece of its arguments
            widget = self._stream_widgets.get(data["index"])
            if widget is not None:
                widget.feed(data["text"])
            return
        if kind == "tool_call":                       # arguments complete: reuse the live widget
            name, args = data
            widget = self._stream_queue.popleft() if self._stream_queue else None
            if widget is None:                        # planning turns don't stream tool calls
                widget = ToolCall(name, args)
                self.mount_block(widget)
            else:
                widget.finalize(args)
            self._pending.append(widget)
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
        for w in list(self._stream_queue):       # cancelled mid-stream: stop their spinners
            w.abort()
        self._stream_queue.clear()
        self._stream_widgets.clear()
        for k, v in self.filter.flush():
            self._emit(k, v)
        self._close_assistant()
        if self._thinking is not None:
            self._thinking.finish()
            self._thinking = None

        if msg.error == "plan-cancelled":
            self.add_notice("warning", "nothing was run")
        elif msg.error == "interrupted":
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
            if msg.found is not None:
                self.artifacts = list(msg.found.items)
                if msg.found.items or msg.found.deleted:
                    self.mount_block(ArtifactsList(msg.found))
        self.set_working(False)

    # ------------------------------------------------------------------ reminders

    _reminders_ok = True

    def _tick_reminders(self) -> None:
        """UI thread, once a second. Queue a poll only when the engine is idle."""
        if (not self._reminders_ok or not self.is_ready or self.working
                or self.engine.busy or self._poll_queued):
            return
        self._poll_queued = True
        self.engine.submit(self._poll_reminders)

    def _poll_reminders(self) -> None:
        """Engine thread."""
        try:
            if self.working:
                return
            from danica.app_core import knowledge_index
            due = knowledge_index().pop_due_reminders()
            for reminder_id, message in due:
                self._call(self.show_reminder, str(message))
                self.audit.append("reminder_due", id=reminder_id)
        except Exception as exc:                  # KnowledgeError or anything else: say so once, stop polling
            self._reminders_ok = False
            self.ui.log("error", f"checking reminders: {exc}")
        finally:
            self._poll_queued = False

    def show_reminder(self, message: str) -> None:
        self.mount_block(Say(f"REMINDER: {message}", "bold yellow"))
        self.notify(message, title="Reminder", severity="warning", timeout=10)

    # ------------------------------------------------------------------ output helpers (UI thread)

    def mount_block(self, widget) -> None:
        self.query_one("#transcript", VerticalScroll).mount(widget)
        if not hasattr(VerticalScroll, "anchor"):
            self.call_after_refresh(self.query_one("#transcript", VerticalScroll).scroll_end, animate=False)

    def show_artifacts_list(self, items: list) -> None:
        from danica.artifacts import ArtifactList
        self.mount_block(ArtifactsList(ArtifactList(items=items)))

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

    # the toolbar where context, mode, and workspace is displayed
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
        # Ctrl+D quits only on an empty prompt; otherwise it falls through to the prompt
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
    # try:
    #     Console.print("[bold #000000 on cyan] STATUS [/] Launching Danica...")
    #     main()
    # except KeyboardInterrupt:
    #     Console.print("[bold #000000 on red] ERROR [/] Aborted")
    # except EOFError:
    #     Console.print("[bold red on #555555] CRITICAL [/] Hard Stop")
    # except Exception as e:
    #     Console.print(f"[bold #000000 on red] ERROR [/] {escape(str(e))}")