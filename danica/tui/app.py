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

import argparse
import pyfiglet
from importlib.metadata import PackageNotFoundError, version
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.text import Text
from rich.style import Style
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll, Vertical
from textual.widgets import Static
from textual.theme import Theme

from danica.app_core import build_session, shutdown, short_path
from danica.artifacts import collect, load_log, merge, save_log
from danica.config import CONFIG
from danica.imageview import ImageError, check_image
from danica.plan_flow import run_plan_mode
from danica.present import PresentFilter
from danica.tui.bridge import (
    AgentEvent,
    EventBridge,
    RequestDone,
    ServerEvent,
    SubagentBridge,
    SubagentEvent,
    TextualUI,
)
from danica.tui.commands import command_names, dispatch
from danica.tui.engine import Engine
from danica.tui.chrome import ArtifactsPane, TopBar, chip_style
from danica.tui.widgets import (
    ArtifactsList,
    AssistantMessage,
    BusyLine,
    Citations,
    CommandPopup,
    Notice,
    PromptInput,
    Say,
    StatusBar,
    SubagentPane,
    ThinkingBlock,
    ToolCall,
    UserMessage,
    welcome_panel,
)
from danica.server import ServerError, ServerMonitor, probe
from danica.tui.connection import DisconnectedScreen, SetupScreen
from danica.tui.viewer_widgets import ArtifactPane, PresentLink
from danica.tui.session_pane import SessionPane
from danica.htmlview import ViewerError, build_viewer
from danica.workspace import PathJailError
from danica.seal import is_sealed_html
from danica import perf, titles, vision
from danica.interactive import page_kind

IDLE_PLACEHOLDER = "Ask Danica…  / commands · Ctrl+J new line"
BUSY_PLACEHOLDER = "Working…"

class _PlanCancelled(Exception):
    pass


class DanicaApp(App):
    """Run Danica's interactive terminal user interface."""

    CSS_PATH = ["app.tcss", "viewer.tcss"]
    TITLE = "Danica"

    # Input itself binds ctrl+c (copy) and ctrl+d (delete), so ours are priority bindings.
    BINDINGS = [
        Binding("ctrl+c", "interrupt", "Stop", priority=True, show=False),
        Binding("escape", "interrupt", "Stop", show=False),
        Binding("ctrl+d", "eof", "Quit", priority=True, show=False),
        Binding("ctrl+o", "toggle_tools", "Expand tool output", show=False),
        Binding("ctrl+g", "toggle_agents", "Show/hide subagent pane", show=False),
        Binding("ctrl+e", "toggle_viewer", "Show/hide file viewer", show=False),
        Binding("ctrl+b", "toggle_sessions", "Show/hide session list", show=False),
    ]


    def __init__(self, bypass: bool = False) -> None:
        super().__init__()
        self.register_theme(Theme(
            name="monochrome",
            ansi=True,
            primary="ansi_white",
            secondary="ansi_bright_black",
            warning="ansi_bright_white",
            error="ansi_white",
            success="ansi_bright_white",
            accent="ansi_white",
            foreground="ansi_default",
            background="ansi_default",
            surface="ansi_default",
            panel="ansi_default",
            boost="ansi_default",
            dark=True,
            variables={
                "ansi-background": "ansi_black",
                "ansi-foreground": "ansi_white",
                "border-blurred": "ansi_bright_black",
                "block-cursor-foreground": "ansi_black",
                "block-cursor-background": "ansi_white",
                "input-cursor-background": "ansi_black",
                "input-cursor-foreground": "ansi_bright_white",
                "input-cursor-text-style": "none",
                "input-selection-background": "ansi_bright_white",
                "input-selection-foreground": "ansi_black",
                "screen-selection-background": "ansi_bright_white",
                "screen-selection-foreground": "ansi_black",
                "link-color": "ansi_white",
                "link-color-hover": "ansi_bright_white",
            },
        ))
        self.theme = "monochrome"
        self.ui_thread = 0
        self.ui = TextualUI(self)
        self.engine = Engine(self._engine_error)
        self._poll_queued = False
        self.bridge = EventBridge(self)
        self.subagent_bridge = SubagentBridge(self) # subagent progress -> split pane

        self.is_ready = False            # a session exists
        self.working = False             # a request or command is running
        self.work_kind = ""              # "request" | "command" | "starting"

        # The 5 session objects (replaced by /new, later /project, /resume)
        self.ws = self.audit = self.checkpoints = self.policy = self.agent = None
        self.session_id = ""
        self.project: dict | None = None
        self.open_root: Path | None = None           # Open Mode: the START folder (None = not in Open Mode)
        self.session_title = ""                       # generated by titles.py after the first answer
        self.plan_next = False                       # set by a bare /plan: the NEXT request gets a plan
        self.plan_auto = CONFIG.get("planning", {}).get("auto", "ask")   # "ask" | "off"
        self.artifacts: list = []                    # files the LAST request made (for /open, /save)
        self.launch_dir = Path.cwd()                 # /project with no argument means THIS folder
        self.current_command = ""

        # Per-request streaming state (UI thread only)
        self.filter = PresentFilter()
        self.presented: set[str] = set()
        self.save_targets: list[Path] = []                 # pictures behind the 'save picture' links (by index)
        self._assistant: AssistantMessage | None = None
        self._thinking: ThinkingBlock | None = None
        self._pending: deque[ToolCall] = deque()
        self._stream_widgets: dict[int, ToolCall] = {}   # tool calls still being streamed, by index
        self._stream_queue: deque[ToolCall] = deque()    # same, in order, waiting for their final args
        self.bypass = bypass
        self.monitor: ServerMonitor | None = None

    # ------------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        # Split screen: chat on the left, subagent pane on the right (hidden until one runs)
        """Compose the main application layout."""
        yield TopBar(id="topbar")                          # [ sidebar ] | chat title | [ subagents ] [ file view ] [ artifacts ]
        with Horizontal(id="main"):
            yield SessionPane(id="sessions-pane")          # left-hand column, hidden until toggled
            yield VerticalScroll(id="transcript")
            with Vertical(id="side"):
                yield SubagentPane(id="agents-pane")
                yield ArtifactPane(id="viewer-pane")
                yield ArtifactsPane(id="artifacts-pane")
        yield BusyLine(id="busy")
        yield CommandPopup(id="popup")
        with Horizontal(id="prompt-box"):
            yield Static(">", id="caret")
            yield PromptInput(placeholder=IDLE_PLACEHOLDER, id="prompt")
            yield Static(Text("[···]", style=Style(color="#ffffff", meta={"@click": "app.open_add_menu"})),
                         id="upload-button")
        yield StatusBar(id="status")

    def on_mount(self) -> None:
        """Initialize the application after it is mounted."""
        self.ui_thread = threading.get_ident()
        transcript = self.query_one("#transcript", VerticalScroll)
        if hasattr(transcript, "anchor"):        # stay pinned to the bottom until the user scrolls up
            transcript.anchor()
        self.query_one(CommandPopup).hide()
        self.query_one("#sessions-pane", SessionPane).display = False
        self.query_one("#agents-pane", SubagentPane).display = False
        self.query_one("#viewer-pane", ArtifactPane).display = False
        self.query_one("#artifacts-pane", ArtifactsPane).display = False
        self.sync_side()                                 # also draws the top bar
        self.query_one(PromptInput).focus()
        perf.mark("window drawn")
        self.set_working(True, "starting")
        self.engine.start()
        self.engine.submit(self.boot)
        self.set_interval(1.0, self._tick_reminders)

    def on_unmount(self) -> None:
        # Stop a running request, then close sqlite-backed objects ON the engine thread
        """Release application resources during shutdown."""
        if self.monitor:
            self.monitor.stop()
        if self.agent is not None:
            self.agent.cancel()
        ws = self.ws
        if ws is not None:
            self.engine.submit(shutdown, ws)
        self.engine.stop()

    def _engine_error(self, exc: BaseException) -> None:
        if isinstance(exc, KeyboardInterrupt):
            return
        self._call(self.add_notice, "error", f"internal error: {type(exc).__name__}: {exc}")
        self._call(self.set_working, False)

    # ------------------------------------------------------------------ sessions (worker thread)

    def boot(self) -> None:
        """Initialize the session and connect to the model server."""
        try:
            if not self._ensure_server():
                return                              # the user quit from the set-up screen
            perf.mark("server answered")
            self.swap_session(None, boot=True)
        except Exception as exc:
            self.ui.log("error", f"could not start a session: {type(exc).__name__}: {exc}")
            self._call(self.set_working, False)

    def _ensure_server(self) -> bool:
        """Worker thread. Make sure llama-server answers before any session is built, because
        Agent reads the context window when it is created."""
        if self.bypass:
            return True
        url = CONFIG["server"]["base_url"]
        try:
            n_ctx = probe(url)
        except ServerError:
            n_ctx = self.ui.ask_screen(SetupScreen(url))     # blocks until Start works or Quit
            if n_ctx is None:
                return False
        CONFIG["model"]["context"] = n_ctx
        self.monitor = ServerMonitor(
            lambda: CONFIG["server"]["base_url"],
            lambda kind, data: self.post_message(ServerEvent(kind, data)),
        )
        self.monitor.n_ctx = n_ctx
        self.monitor.start()
        return True

    def swap_session(
        self,
        project: dict | None,
        note: str | None = None,
        boot: bool = False,
        open_root: Path | None = None,
    ) -> None:
        """Build a new session (worker thread: it may open dialogs) and show it.
        open_root: start Open Mode in this folder (the caller already showed the warning)."""
        new_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        parts = build_session(
            new_id, self.ui, self.bridge, project, open_root=open_root
        )
        perf.mark("session built")
        default = f"New session {new_id}."
        if open_root is not None:
            default = (f"OPEN MODE: full disk access (protected areas stay blocked)\n"
                   f"  start folder: {parts[0].root}\n{default} /chat, /project or /new leave Open Mode.")
        elif project:
            default = (f"Project mode: {project['name']}\n  folder: {parts[0].root}\n"
                   f"{default} The previous session stays available via /sessions and /resume.")
        text = "" if boot else (note.format(id=new_id) if note else default)
        self.adopt_session(parts, new_id, project, text,
                       start_mode="open" if open_root is not None else "project" if project else "chat")

    def adopt_session(
        self,
        parts: Any,
        session_id: str,
        project: dict | None,
        note: str,
        start_mode: str | None = None,
    ) -> None:
        """Make `parts` (from build_session / open_resumed) the current session. Worker thread."""
        ws, audit, checkpoints, policy, agent = parts
        self.ws, self.audit, self.checkpoints, self.policy, self.agent = (
            ws,
            audit,
            checkpoints,
            policy,
            agent,
        )
        self.session_id, self.project = session_id, project
        self.open_root = ws.root if ws.open_mode else None     # also right after /resume of an Open Mode session
        self._call(self.set_session_title, titles.read_title(session_id))   # a resumed session keeps its title
        vision.refresh()                                  # does this server have --mmproj? (/vision shows the answer)
        # The session's files come back with it (saved by save_log after each request); a new session starts empty
        self.artifacts = load_log(ws, titles.SESSIONS / str(session_id))
        self._call(self.refresh_artifacts)
        # The TUI draws subagents itself, so the supervisor must not open a console window
        if ws.subagent_supervisor is not None:
            ws.subagent_supervisor.on_event = self.subagent_bridge
        if start_mode:
            extra = {"project": project["id"]} if project else {}
            audit.append("session_start", workspace=str(ws.root), mode=start_mode, **extra)
        self._call(self.after_session, note, list(agent.messages[1:]))

    def after_session(self, note: str, history: list | None = None) -> None:
        """Reset visible request state after a session change."""
        self.refresh_sessions()
        transcript = self.query_one("#transcript", VerticalScroll)
        transcript.remove_children()
        transcript.mount(Static(welcome_panel(self.project["name"] if self.project else None,
                                      short_path(self.ws.root),
                                      open_mode=self.open_root is not None,
                                      current_session_id=self.session_id),
                         classes="welcome session-list"))
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
        perf.mark("ready for input")

    def _call(self, fn, *args):
        """Worker thread -> UI thread, tolerant of shutdown."""
        try:
            return self.call_from_thread(fn, *args)
        except RuntimeError:
            return None

    def add_renderable(self, renderable: Any) -> None:
        """Add a Rich renderable to the conversation view."""
        classes = "session-list" if getattr(renderable, "session_list", False) else ""
        self.mount_block(Static(renderable, classes=classes))

    # ------------------------------------------------------------------ input

    def on_prompt_input_submitted(self, event: PromptInput.Submitted) -> None:
        """Handle submitted prompt text."""
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
        """Execute a slash command on the session worker."""
        follow = None
        try:
            follow = dispatch(self, text)
        except Exception as exc:
            self.ui.log("error", f"{type(exc).__name__}: {exc}")
        self._call(self._finish_command, follow)

    def action_resume_session(self, number: int) -> None:
        """Resume a saved session selected from the clickable /sessions table."""
        if self.working or self.engine.busy:
            self.add_notice("warning", "Danica is busy. Wait for the current task to finish first.")
            return
        self.set_working(True, "command", kind="command")
        self.engine.submit(self.run_command, f"/resume {int(number)}")

    def action_show_sessions(self) -> None:
        """Show the full saved-session list from the welcome panel."""
        if self.working or self.engine.busy:
            self.add_notice("warning", "Danica is busy. Wait for the current task to finish first.")
            return
        self.set_working(True, "command", kind="command")
        self.engine.submit(self.run_command, "/sessions")

    def action_resume_session_id(self, session_id: str) -> None:
        """Resume the saved session clicked in the left-hand session pane."""
        if session_id == self.session_id:
            return
        if self.working or self.engine.busy:
            self.add_notice("warning", "Danica is busy. Wait for the current task to finish first.")
            return
        self.set_working(True, "command", kind="command")
        self.engine.submit(self.run_command, f"/resume {session_id}")

    def action_open_artifact(self, number: int) -> None:
        """Open a file selected from the clickable artifact list."""
        if self.working or self.engine.busy:
            self.add_notice("warning", "Danica is busy. Wait for the current task to finish first.")
            return
        self.set_working(True, "command", kind="command")
        self.engine.submit(self.run_command, f"/open {int(number)}")

    def action_open_add_menu(self) -> None:
        """Open a compact menu for files and session modes."""
        self._start_toolbar_command(self.choose_add_action)

    def _start_toolbar_command(self, fn, *args) -> bool:
        if self.working or self.engine.busy:
            self.add_notice("warning", "Danica is busy. Wait for the current task to finish first.")
            return False
        self.set_working(True, "command", kind="command")
        self.engine.submit(fn, *args)
        return True

    def choose_add_action(self) -> None:
        """Worker-thread menu for attach, upload, and mode commands."""
        override = self.agent.thinking_override if self.agent else None
        mode = "auto" if override is None else ("think" if override else "fast")
        full_access = bool(self.policy and self.policy.full_access)
        choice = self.ui.choose(
            "Quick actions",
            "Choose what to add. Images are attached to your next message; other files go into the workspace.",
            [
                ("i", "Attach image"),
                ("f", "Upload file"),
                ("t", f"Thinking mode: {mode} (cycle)"),
                ("p", f"Permission mode: {'Full Access' if full_access else 'Ask when needed'} (toggle)"),
            ],
        )
        if choice == "i":
            self.run_command("/attach")
        elif choice == "f":
            self.run_command("/upload")
        elif choice == "t":
            self.run_command("/think")
        elif choice == "p":
            self.run_command("/fullaccess")
        else:
            self._call(self._finish_command, None)

    def _finish_command(self, follow) -> None:
        self.set_working(False)
        if follow and follow[0] == "request":          # e.g. /plan <request>
            self.start_request(follow[1], force_plan=True)

    # ------------------------------------------------------------------ slash-command popup

    def on_text_area_changed(self, event: Any) -> None:
        """Update prompt state when the text area changes."""
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
        """Route key presses while the command popup is open."""
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

    def on_option_list_option_selected(self, event: Any) -> None:
        """A click on a suggestion completes it."""
        prompt = self.query_one(PromptInput)
        prompt.value = str(event.option.id) + " "
        self.query_one(CommandPopup).hide()
        prompt.popup_open = False
        prompt.focus()

    # ------------------------------------------------------------------ one request

    def start_request(self, text: str, force_plan: bool = False) -> None:
        """Schedule a user request on the session engine."""
        self.mount_block(UserMessage(text))
        self._reset_stream_state()
        forced = force_plan or self.plan_next
        self.plan_next = False
        self.set_working(True, "Working", kind="request")
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
            decision, plan, self.plan_auto = run_plan_mode(
                text, forced, self.plan_auto, agent, audit, self.ui
            )
            if decision == "cancel":
                raise _PlanCancelled()
            result = agent.run(text, plan=plan)
            if result:                            # first finished answer: name the session in the background
                titles.start(
                    agent,
                    self.session_id,
                    lambda title: self._call(self.set_session_title, title),
                )
            try:
                found = collect(
                    ws,
                    agent.checkpoints,
                    agent.request_snap,
                    agent.touched,
                    set(self.presented),
                )
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

    def on_subagent_event(self, msg: SubagentEvent) -> None:
        """UI thread. A subagent reported progress: open the split pane on "start" and feed it."""
        pane = self.query_one("#agents-pane", SubagentPane)
        if msg.kind == "start":
            pane.display = True                        # the chat shrinks to the left half
            self.sync_side()
        pane.handle(msg.child_id, msg.kind, msg.data)

    def action_toggle_agents(self) -> None:
        """Show or hide the delegated worker pane."""
        pane = self.query_one("#agents-pane", SubagentPane)
        pane.display = not pane.display
        self.sync_side()

    def action_toggle_viewer(self) -> None:
        """Ctrl+E: show or hide the file viewer."""
        pane = self.query_one("#viewer-pane", ArtifactPane)
        pane.display = not pane.display
        self.sync_side()

    def action_toggle_sessions(self) -> None:
        """Ctrl+B / /sidebar: show or hide the saved-session list on the left."""
        pane = self.query_one("#sessions-pane", SessionPane)
        pane.display = not pane.display
        self.refresh_sessions()
        self.refresh_topbar()

    def action_toggle_artifacts(self) -> None:
        """Top bar [ artifacts ]: show or hide the list of files the last request made."""
        pane = self.query_one("#artifacts-pane", ArtifactsPane)
        pane.display = not pane.display
        self.refresh_artifacts()
        self.sync_side()

    def refresh_artifacts(self) -> None:
        """UI thread. Redraw the artifacts pane from self.artifacts (no-op before it exists)."""
        try:
            self.query_one("#artifacts-pane", ArtifactsPane).show(self.artifacts)
        except Exception:
            pass

    def refresh_topbar(self) -> None:
        """UI thread. Redraw the top bar: which panes are open, and the chat title."""
        try:
            panes = {p for p in ("agents-pane", "viewer-pane", "artifacts-pane") if self.query_one("#" + p).display}
            self.query_one("#topbar", TopBar).refresh_state(
                self.query_one("#sessions-pane").display, panes, self.session_title, self.size.width)
        except Exception:                                 # not mounted yet / shutting down
            pass

    # ------------------------------------------------------------------ sidebar menu and manager panes

    def action_menu(self, name: str) -> None:
        """A click in the sidebar menu (chrome.MENU)."""
        if name.startswith("manage:"):
            self.open_manager(name[len("manage:"):])
        elif name == "settings":
            from danica.tui.settings_screen import SettingsScreen
            self.push_screen(SettingsScreen())
        elif name == "new":
            self._start_toolbar_command(self.run_command, "/new")
        elif name == "help":
            self._start_toolbar_command(self.run_command, "/help")
        elif name == "project":
            self._start_toolbar_command(self.menu_project)

    def action_chip(self, name: str) -> None:
        """A click on a piece of the status bar (made with chrome.chip_style in status_text)."""
        if name in ("context", "safety"):
            self.open_manager(name)
        elif name == "think":
            self._start_toolbar_command(self.run_command, "/think")        # the same toggle as the [···] menu
        elif name == "plan_off":
            self.plan_next = False
            self.refresh_status()
            self.notify("Plan mode cancelled: the next request runs normally.", timeout=3)

    def open_manager(self, key: str) -> None:
        """UI thread. Open the manager pane for `key` (managers.MANAGERS)."""
        from danica.managers import find_manager
        from danica.tui.manager_screen import ManagerScreen
        manager = find_manager(key)
        if manager is None:
            self.add_notice("warning", f"no manager called {key}")
        elif not isinstance(self.screen, ManagerScreen):
            self.push_screen(ManagerScreen(manager))

    def menu_project(self) -> None:
        """Worker thread. Sidebar [ Project mode ]: ask for a folder and open it as a project (or leave project mode)."""
        if self.project is not None or self.open_root is not None:
            where = self.project["name"] if self.project else "Open Mode"
            choice = self.ui.choose("Project mode", f"Now in: {where}",
                                    [("o", "Open another folder"), ("c", "Back to a normal chat")])
            if choice == "c":
                self.run_command("/chat")
                return
            if choice != "o":
                self._call(self._finish_command, None)
                return
        raw = self.ui.ask_text("Project folder (it becomes the workspace)", default=str(self.launch_dir))
        folder = (raw or "").strip().strip('"')
        if folder:
            self.run_command(f'/project "{folder}"')
        else:
            self._call(self._finish_command, None)

    def refresh_sessions(self) -> None:
        """UI thread. Redraw the left-hand session list (no-op while it is hidden)."""
        try:
            self.query_one("#sessions-pane", SessionPane).reload()
        except Exception:                         # not mounted yet / shutting down
            pass

    def sync_side(self) -> None:
        """Show the right-hand column only while one of its panes is visible."""
        side = self.query_one("#side", Vertical)
        side.display = any(child.display for child in side.children)
        self.refresh_topbar()                             # its buttons light up while their pane is open

    def view_artifact(
        self, rel: str, path: Path, text: str, cut: bool, left_out: int, body=None
    ) -> None:
        """UI thread. /open <n>: show a text file in the split-screen viewer. `body` = the finished text, built off
        the UI thread (viewer_widgets.view_body); None = build it here."""
        pane = self.query_one("#viewer-pane", ArtifactPane)
        pane.show_file(rel, path, text, cut, left_out, body)
        pane.display = True
        self.sync_side()

    def action_viewer_copy(self) -> None:
        """Viewer [ copy ]: put the whole file on the clipboard."""
        self._viewer_job("copy")

    def action_viewer_save(self) -> None:
        """Viewer [ save ]: copy the file into the Downloads folder (never overwrites)."""
        self._viewer_job("save")

    def _viewer_job(self, what: str) -> None:
        """UI thread. Run copy/save for the file in the viewer on a worker thread, so a big file never freezes the screen
        and the buttons work while Danica is busy with a request (the /copy and /save commands would have to wait)."""
        current = self.query_one("#viewer-pane", ArtifactPane).current
        if current is None:
            self.notify("No file is open in the viewer.", severity="warning")
            return
        rel, path = current
        self.run_worker(lambda: self._viewer_work(what, rel, path), thread=True, group="viewer-file")

    def _viewer_work(self, what: str, rel: str, path: Path) -> None:
        """Worker thread. Do the copy / save, then tell the user with a toast (always via call_from_thread)."""
        from danica.artifacts import Artifact, ArtifactError, copy_contents, downloads_dir, save_copy
        try:
            if what == "copy":
                chars = copy_contents(Artifact(rel, path, "", 0))
                note = f"Copied {rel} ({chars:,} characters) to the clipboard"
                event = ("artifact_copy", {"path": rel, "chars": chars})
            else:
                saved = save_copy(path, downloads_dir())
                note = f"Saved a copy to {saved}"
                event = ("artifact_save", {"path": rel, "to": str(saved.parent)})
        except ArtifactError as e:
            self.call_from_thread(self.notify, str(e), title="Viewer", severity="error", timeout=6)
            return
        self.call_from_thread(self.notify, note, title="Viewer", timeout=4)
        # The audit log is a hash chain that only the engine thread may write, so queue the entry there
        self.engine.submit(lambda: self.audit.append(event[0], **event[1]))

    def close_viewer(self) -> None:
        """/close: hide the viewer."""
        self.query_one("#viewer-pane", ArtifactPane).display = False
        self.sync_side()

    def on_agent_event(self, ev: AgentEvent) -> None:
        """Handle an event emitted by the agent."""
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
                call = self._pending.popleft()
                call.set_result(data)
                self._show_chart_preview(call, data)
            else:
                self.mount_block(Say(str(data)[:400], "dim"))
            self.refresh_status()
        elif kind == "notice":
            text = str(data)
            self.add_notice("error" if text.startswith("SECURITY") else "warning", text)
        elif kind == "citations":
            self.mount_block(Citations(str(data)))

    def _show_chart_preview(self, call, result) -> None:
        """A chart tool call finished: draw the same data in the terminal under it (the PNG and the interactive
        chart are made as before). Switch off: Settings > Display & files > Charts in the terminal."""
        from danica import termchart
        from danica.tui.widgets import TermChart
        width = max(40, self.size.width - 8)
        preview = termchart.preview_for_tool(self.ws, getattr(call, "tool", ""), getattr(call, "call_args", None), result, width)
        if preview is not None:
            self.mount_block(TermChart(preview))

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

    def action_save_picture(self, index: int) -> None:
        """Click on 'save picture': copy the picture into the real Downloads folder (never overwrites). Works
        without any browser, so it is the way out when the viewer page's own download button does not."""
        from danica.artifacts import ArtifactError, downloads_dir, save_copy
        try:
            src = self.save_targets[int(index)]
            saved = save_copy(src, downloads_dir())
            self.add_notice("info", f"  saved a copy to {saved}")
            self.audit.append("artifact_save", path=self.ws.display(src), to=str(saved.parent))
        except (IndexError, ValueError, TypeError):
            self.add_notice("warning", "  that picture is no longer available: use /save")
        except ArtifactError as exc:
            self.add_notice("warning", f"  could not save: {exc}")

    def show_image(self, rel: str) -> None:
        """<present file="..."/>. Pictures are never drawn inline: a picture becomes ONE clickable link
        to an interactive HTML viewer (htmlview.py), a sealed Danica slideshow links to itself. If the
        viewer can't be built, the link opens the picture file itself."""
        try:
            if rel.lower().endswith(".html"):                      # an HTML slideshow Danica made
                path = self.ws.resolve(rel)
                if not path.is_file() or not is_sealed_html(path):
                    raise ImageError("only slideshows made by Danica can be linked")
                label = self.ws.display(path)
                self.presented.add(label)
                what = {"quiz": "quiz", "flashcards": "flash cards"}.get(page_kind(path), "slideshow")
                self.mount_block(PresentLink(label, path, what))
                return
            path = check_image(self.ws, rel)
            label = self.ws.display(path)
            self.presented.add(label)
            what = "chart" if path.with_suffix(".json").is_file() else "picture"
            self.save_targets.append(path)                          # the 'save picture' link points at this index
            index = len(self.save_targets) - 1
            try:
                self.mount_block(PresentLink(label, build_viewer(path), what, index))
            except ViewerError as exc:                              # no viewer: link the picture itself
                self.add_notice("warning", f"  No interactive view ({exc}); linking the picture file")
                self.mount_block(PresentLink(label, path, what, index))
        except (ImageError, PathJailError) as exc:
            self.add_notice("warning", f"  Could not show {rel}: {exc}")

    def on_server_event(self, msg: ServerEvent) -> None:
        """Update the interface after a server state change."""
        if msg.kind == "offline":
            if not any(isinstance(s, DisconnectedScreen) for s in self.screen_stack):
                self.push_screen(DisconnectedScreen(CONFIG["server"]["base_url"], msg.data),
                                 self._reconnected)
        elif msg.kind == "online":                        # it came back by itself
            if isinstance(self.screen, DisconnectedScreen):
                self.screen.dismiss(msg.data)             # -> _reconnected
        elif msg.kind == "ctx_changed":                   # restarted quickly with another -c
                self.engine.submit(self._reconcile_context, msg.data[1])

    def _reconnected(self, n_ctx: int | None) -> None:
        if n_ctx is not None:
            if self.monitor:
                self.monitor.mark_online(n_ctx)
            self.engine.submit(self._reconcile_context, n_ctx)

    def _reconcile_context(self, n_ctx: int) -> None:     # engine thread: may ask / call the model
        agent = self.agent
        if agent is None:
            return
        self._call(lambda: self.set_working(True, "Checking context", kind="command"))
        try:
            old = agent.tracker.window
            report = agent.adopt_window(n_ctx)
            if n_ctx != old:
                self.ui.log("warning", f"The server's context window changed: {old:,} -> {n_ctx:,} tokens.")
            if report.status == "too_small":
                self.ui.log("error", f"The context window ({n_ctx:,} tokens) is too small for Danica. "
                                     f"Restart llama-server with -c {report.recommended_window:,} or more.")
            elif report.status == "over":
                if self.ui.confirm("The conversation no longer fits",
                                   f"It is about {report.used:,} tokens, but the window is now {n_ctx:,}. "
                                   f"Summarize the older part now?", yes="Compact now", no="Later"):
                    report = agent.fit_to_window()
                    self.ui.log("info" if report.status == "ok" else "error",
                                f"Context is now about {report.used:,} of {n_ctx:,} tokens.")
        finally:
            self._call(self.set_working, False)

    def on_request_done(self, msg: RequestDone) -> None:
        """Restore the interface after a request completes."""
        self.refresh_sessions()                   # request counts / last-active time changed
        if msg.error and self.monitor:
            self.monitor.check_now()
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
            todo = [
                path
                for path in (self.agent.charts_made if self.agent else [])
                if path not in self.presented
            ]
            if todo:
                self.mount_block(Say("Charts made in this request:", "dim"))
                for p in todo:
                    self.show_image(p)
            if msg.found is not None:
                # Add this request's files to the session list (an empty request no longer empties it) and save it
                self.artifacts = merge(list(msg.found.items), self.artifacts)
                save_log(titles.SESSIONS / str(self.session_id), self.artifacts)
                self.refresh_artifacts()
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
        """Display a reminder from the background poller."""
        self.mount_block(Say(f"REMINDER: {message}", "bold white"))
        self.notify(message, title="Reminder", severity="warning", timeout=10)

    # ------------------------------------------------------------------ output helpers (UI thread)

    def mount_block(self, widget: Any) -> None:
        """Mount a transcript block for an agent event."""
        transcript = self.query_one("#transcript", VerticalScroll)
        transcript.mount(widget)
        if hasattr(transcript, "anchor"):
            # Modern Textual's anchor tracks whether the reader has scrolled away from the end.
            # Let it preserve that choice instead of forcing the transcript to the latest block.
            return
        self.call_after_refresh(transcript.scroll_end, animate=False)

    def show_artifacts_list(self, items: list) -> None:
        """Display files changed by the completed request."""
        from danica.artifacts import ArtifactList
        self.mount_block(ArtifactsList(ArtifactList(items=items), "Files in this session"))   # /artifacts: the whole session

    def add_notice(self, level: str, text: str) -> None:
        """Add a notice to the conversation view."""
        self.mount_block(Notice(level, text))

    def add_say(self, text: str, style: str | None = None) -> None:
        """Add a short message to the conversation view."""
        self.mount_block(Say(text, style))

    # ------------------------------------------------------------------ busy / status

    def set_working(self, working: bool, label: str = "", kind: str = "") -> None:
        """Update the working indicator and request status."""
        self.working = working
        self.work_kind = kind or label if working else ""
        busy = self.query_one(BusyLine)
        prompt = self.query_one(PromptInput)
        if working:
            busy.start(label, stoppable=kind == "request")
            prompt.placeholder = BUSY_PLACEHOLDER
        else:
            busy.stop()
            prompt.placeholder = IDLE_PLACEHOLDER
            prompt.focus()
        self.refresh_status()

    # the toolbar where context, mode, and workspace is displayed
    def status_text(self) -> Text:
        """Build the current session status text."""
        agent = self.agent
        if agent is None:
            return Text("starting…", style="dim")
        level = agent.tracker.level(agent.messages)
        color = {
            "ok": "#c0c0c0",
            "prune": "#d0d0d0",
            "compact": "#e0e0e0",
            "stop": "#d0d0d0",
        }.get(level, "#c0c0c0")
        width = self.size.width
        t = Text()
        if self.open_root is not None:                # first, so a narrow terminal can never cut it off
            t.append(" OPEN ", style="bold #010101 on #ff4a4a")
            t.append(" · ")
            
        if self.session_title:
            title_limit = 16 if width < 92 else 28
            title = self.session_title
            if len(title) > title_limit:
                title = title[: title_limit - 1].rstrip() + "…"
            t.append(f"{title} · ", style="bold #ffffff")
        # Pieces made with chip_style() can be clicked: ctx -> Context panel, thinking -> cycle modes,
        # the permission label and TAINTED -> Safety panel, "plan next" -> cancel it (see action_chip)
        if width < 92:
            t.append(f"ctx {agent.tracker.fraction(agent.messages):.0%}", style=chip_style(color, "context"))
        else:
            t.append(f"ctx {agent.tracker.bar(agent.messages, width=6)}", style=chip_style(color, "context"))
        t.append(" · ")
        override = agent.thinking_override
        thinking_mode = "auto" if override is None else ("think" if override else "fast")
        t.append(thinking_mode, style=chip_style("#c0c0c0", "think"))
        if self.policy is not None and self.policy.full_access:
            t.append(" · ")
            t.append("FULL ACCESS", style=chip_style("#d0d0d0", "safety", bold=True))
        elif width >= 92:
            t.append(" · ")
            t.append("ask first", style=chip_style("#999999", "safety"))        # the normal mode, so the label is always there to click
        if self.plan_next and width >= 92:
            t.append(" · ")
            t.append("plan next \u2715", style=chip_style("#d0d0d0", "plan_off"))     # click = cancel
        if agent.proposals and agent.proposals.count() and width >= 140:
            t.append(f" · {agent.proposals.count()} proposed note(s)", style="#d0d0d0")
        if self.ws is not None and self.ws.web and self.ws.web.tainted:
            t.append(" · ")
            t.append("TAINTED", style=chip_style("#d0d0d0", "safety", bold=True))
        if self.project and width >= 116:
            t.append(f" · {self.project['name']}", style="#c0c0c0")
        if self.ws is not None and width >= 140:
            t.append(f" · {short_path(self.ws.root)}", style="#999999")
        if width >= 160:
            t.append(" · Ctrl+O tools · Ctrl+G agents · Ctrl+E files", style="#999999")
        return t

    def on_resize(self, event: Any) -> None:
        """Refresh compact status labels when the terminal size changes."""
        self.refresh_status()
        self.refresh_topbar()

    def set_session_title(self, title: str) -> None:
        """UI thread. Show the session title in the status bar and as the terminal window title."""
        self.session_title = title
        self.refresh_sessions()                   # the list shows titles and the current-session marker
        self.refresh_topbar()
        self.title = f"Danica - {title}" if title else "Danica"
        try:
            self._driver.write(f"\x1b]0;{self.title}\x07")      # the terminal's window title (Windows Terminal etc.)
        except Exception:
            pass                                               # a title must never break the app
        self.refresh_status()

    def refresh_status(self) -> None:
        """Refresh the visible session status."""
        try:
            self.query_one(StatusBar).update(self.status_text())
        except Exception:
            pass                                   # the status bar must never break a request

    # ------------------------------------------------------------------ actions

    def action_interrupt(self) -> None:
        """Interrupt the current request."""
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
        """Exit when the prompt receives end of input."""
        self.exit()

    def check_action(self, action: str, parameters: tuple[Any, ...]) -> bool:
        # Ctrl+D quits only on an empty prompt; otherwise it falls through to the prompt
        """Check whether an interface action is available."""
        if action == "eof":
            try:
                return not self.query_one(PromptInput).value
            except Exception:
                return True
        return True

    def action_toggle_tools(self) -> None:
        """Toggle expanded tool output in the transcript."""
        for widget in self.query(ToolCall):
            widget.toggle()

    
def main() -> None:
    """Start the Textual application."""
    try:
        current_version = version("danica")
        banner = pyfiglet.figlet_format(current_version, font="ansi_shadow").rstrip()
    except PackageNotFoundError:
        current_version = "unknown"
        banner = "unknown"

    parser = argparse.ArgumentParser(
        prog="danica",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=banner.replace("%", "%%"),  # argparse %-formats this string
        help="show the current version and exit",
    )
    parser.add_argument(
        "--bypass",
        action="store_true",
        help="skip the llama-server check (testing and debugging)",
    )
    args = parser.parse_args()

    DanicaApp(bypass=args.bypass).run()


if __name__ == "__main__":
    main()
    # try:
    #     Console.print("[bold #000000 on cyan] STATUS [/] Launching Danica...")
    #     main()
    # except KeyboardInterrupt:
    #     Console.print("[bold #000000 on red] ERROR [/] Aborted")
    # except EOFError:
    #     Console.print("[bold white on #555555] CRITICAL [/] Hard Stop")
    # except Exception as e:
    #     Console.print(f"[bold #000000 on red] ERROR [/] {escape(str(e))}")
