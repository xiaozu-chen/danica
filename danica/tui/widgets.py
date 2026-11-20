"""
widgets.py - The things that appear in the transcript, plus the prompt, busy line and status bar.
"""

from __future__ import annotations

import difflib
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from rich import box
from rich.console import Group
from rich.panel import Panel
from rich.style import Style
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Span, Text
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Markdown, OptionList, RichLog, Static, TextArea
from textual.widgets.option_list import Option

from danica.config import APP_DIR
from danica.partial_json import partial_fields

from danica.ui import format_log

ACCENT = "#7aa2f7"
HISTORY_PATH = APP_DIR / "history.txt"

TIPS_LABEL = [
    "Type a request to get started",
    "/help lists commands and keyboard shortcuts",
    "Use Ctrl+J to add a line without sending",
    "Use /new to start a fresh session",
]


# ----------------------------------------------------------------------------- transcript

class UserMessage(Static):
    """Render a user message in the conversation transcript."""

    def __init__(self, text: str) -> None:
        t = Text()
        t.append("> ", style=f"bold {ACCENT}")
        t.append(text)
        super().__init__(t, classes="user")


class AssistantMessage(Markdown):
    """Streamed answer. Tokens are buffered and re-rendered ~12x/second instead of per token."""

    def __init__(self) -> None:
        super().__init__("", classes="assistant")
        self._buf = ""
        self._dirty = False

    def on_mount(self) -> None:
        """Set up streaming state after the message widget mounts."""
        self.set_interval(0.08, self._flush)

    def append(self, text: str) -> None:
        """Append streamed assistant text to the message."""
        self._buf += text
        self._dirty = True

    def _flush(self) -> None:
        if self._dirty:
            self._dirty = False
            self.update(self._buf)

    def finish(self) -> None:
        """Finalize the assistant message after streaming ends."""
        if self.is_attached:
            self._dirty = True
            self._flush()


class ThinkingBlock(Static):
    """Dim streamed reasoning. Shows the last few lines while streaming, one line when done;
    click to expand."""

    def __init__(self) -> None:
        super().__init__("", classes="thinking")
        self.buf = ""
        self.done = False
        self.expanded = False

    def append(self, text: str) -> None:
        """Append streamed reasoning text to the block."""
        self.buf += text
        self._paint()

    def finish(self) -> None:
        """Finalize the reasoning block after streaming ends."""
        self.done = True
        self._paint()

    def on_click(self) -> None:
        """Toggle the expanded reasoning display."""
        self.expanded = not self.expanded
        self._paint()

    def _paint(self) -> None:
        t = Text(style="dim italic")
        if self.done and not self.expanded:
            t.append(f"✻ thought ({len(self.buf):,} chars, click to expand)")
        else:
            lines = self.buf.strip().splitlines() or [""]
            shown = lines if self.expanded else lines[-6:]
            t.append("✻ " + ("thought" if self.done else "thinking…") + "\n")
            t.append("\n".join(shown))
        self.update(t)


EDIT_PAIRS = (("old_string", "new_string"), ("old_str", "new_str"), ("old_text", "new_text"),
              ("old", "new"), ("search", "replace"), ("find", "replace"))
CONTENT_KEYS = ("content", "contents", "text")
PREVIEW_LINES = 24        # lines of a file / diff shown before the user expands it
FULL_LINES = 400


def highlighted(code: str, lexer: str) -> Text:
    """Syntax-highlighted code with line numbers. Rich's own Syntax paints a background behind
    tokens that clashes with the app's colors, so keep only the foreground styling."""
    try:
        raw = Syntax(code, lexer, theme="monokai").highlight(code)
        spans = []
        for span in raw.spans:
            style = span.style
            if isinstance(style, str):
                style = Style.parse(style)
            spans.append(
                Span(
                    span.start,
                    span.end,
                    Style(
                        color=style.color,
                        bold=style.bold,
                        italic=style.italic,
                        underline=style.underline,
                    ),
                )
            )
        plain = Text(raw.plain, spans=spans)
    except Exception:
        plain = Text(code)
    plain.rstrip()
    out = Text()
    for number, line in enumerate(plain.split("\n", allow_blank=True), start=1):
        if number > 1:
            out.append("\n")
        out.append(f"{number:>4} ", style="#4a5070")
        out.append_text(line)
    return out


def edit_pair(args: Any) -> tuple[str, str] | None:
    """Which two argument names hold the old and new text of an edit (None if not an edit)."""
    if isinstance(args, dict):
        for old, new in EDIT_PAIRS:
            if isinstance(args.get(old), str) and isinstance(args.get(new), str):
                return old, new
    return None


def content_key(args: Any) -> str | None:
    """The argument holding a whole file's text (a write), if there is one."""
    if isinstance(args, dict) and isinstance(args.get("path"), str):
        for key in CONTENT_KEYS:
            if isinstance(args.get(key), str):
                return key
    return None


def summarize_args(args: Any, hide: tuple[str, ...] = ()) -> str:
    """Format tool arguments for concise display."""
    if not isinstance(args, dict):
        return str(args)[:120]
    parts = []
    for key, value in args.items():
        if key in hide:
            continue
        shown = str(value).replace("\n", "\\n")
        parts.append(f"{key}={shown[:80] + '…' if len(shown) > 80 else shown}")
    return "  ".join(parts)


class ToolCall(Static):
    """● Tool Name  path   then, depending on the tool:
         write      the file's content, syntax highlighted, with line numbers
         edit       a red/green diff of old -> new
         run_shell  the command
         other      arg=value summary
       and the result below. Click or Ctrl+O expands everything that was cut short."""

    PREVIEW = 400
    FULL = 6000

    def __init__(self, name: str, args, streaming: bool = False) -> None:
        self.tool = name
        self.title_text = name.replace("_", " ").title()
        self.result: str | None = None
        self.expanded = False
        self.streaming = streaming          # True while the model is still writing the arguments
        self.raw = ""                       # the argument JSON received so far (streaming only)
        self._dirty = False
        self._timer = None
        self._set_args(self._padded({}) if streaming else args)
        super().__init__(self._build(), classes="toolcall")

    def _set_args(self, args) -> None:
        self.call_args = args
        self.edit = edit_pair(args)
        self.ckey = None if self.edit else content_key(args)
        self.command = args.get("command") if self.tool == "run_shell" and isinstance(args, dict) \
            and isinstance(args.get("command"), str) else None

    def _padded(self, fields: dict) -> dict:
        """Half-arrived arguments, with the file tools' fields filled in as empty strings so the
        write / edit rendering works from the very first character."""
        args = dict(fields)
        if self.tool == "write_file":
            args.setdefault("path", "")
            args.setdefault("content", "")
        elif self.tool == "edit_file":
            args.setdefault("path", "")
            args.setdefault("old_string", "")
            args.setdefault("new_string", "")
        return args

    # -- pieces -------------------------------------------------------------------------

    def _header(self) -> Text:
        t = Text()
        t.append("● ", style="bold #7aa2f7" if self.result is None else "bold #9ece6a")
        t.append(self.title_text, style="bold")
        args = self.call_args
        if self.edit or self.ckey:
            t.append("  " + str(args.get("path")), style="#7aa2f7")
        elif self.command is not None:
            hidden = summarize_args(args, hide=("command",))
            if hidden:
                t.append("  " + hidden, style="#7aa2f7")
        else:
            summary = summarize_args(args)
            if summary:
                t.append("  " + summary, style="#7aa2f7")
        return t

    def _more(self, hidden: int) -> Text | None:
        if hidden <= 0:
            return None
        return Text(f"… {hidden:,} more line(s), click or Ctrl+O to expand", style="dim italic")

    def _body(self, header: Text):
        limit = FULL_LINES if self.expanded else PREVIEW_LINES
        if self.edit:
            old, new = self.call_args[self.edit[0]], self.call_args[self.edit[1]]
            diff = [line for line in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=2)
                    if not line.startswith(("---", "+++"))]
            added = sum(1 for line in diff if line.startswith("+"))
            removed = sum(1 for line in diff if line.startswith("-"))
            header.append(f"  +{added}", style="#9ece6a")
            header.append(f" -{removed}", style="#f7768e")
            if not diff:
                return Text("(no change)", style="dim")
            body = Text(no_wrap=False)
            for line in diff[:limit]:
                if line.startswith("+"):
                    body.append(line + "\n", style="#9ece6a on #1a2b1a")
                elif line.startswith("-"):
                    body.append(line + "\n", style="#f7768e on #2e1a1f")
                elif line.startswith("@@"):
                    body.append(line + "\n", style="#6b7089")
                else:
                    body.append(line + "\n", style="dim")
            body.rstrip()
            return Group(body, *filter(None, [self._more(len(diff) - limit)]))
        if self.ckey:
            code = self.call_args[self.ckey]
            lines = code.splitlines()
            header.append(f"  +{len(lines):,}", style="#9ece6a")
            header.append(" -0", style="#f7768e")
            shown = "\n".join(lines[:limit])
            try:
                path = str(self.call_args.get("path"))
                lexer = Syntax.guess_lexer(path, code) if path else "text"
            except Exception:
                lexer = "text"
            return Group(
                highlighted(shown, lexer),
                *filter(None, [self._more(len(lines) - limit)]),
            )
        if self.command is not None:
            return Text("$ " + self.command, style="#c0caf5")
        return None

    def _result(self) -> Text | None:
        if self.result is None:
            return None
        is_error = self.result.lstrip().startswith("ERROR")
        if (self.edit or self.ckey) and not is_error and not self.expanded:
            body = self.result.strip().splitlines()[0][:200] if self.result.strip() else ""
        else:
            limit = self.FULL if self.expanded else self.PREVIEW
            body = self.result[:limit]
            more = len(self.result) - len(body)
            if more > 0:
                body += f"… ({more:,} more chars, click to expand)"
        return Text(body, style="#f7768e" if is_error else "dim")

    def _build(self):
        header = self._header()
        body = self._body(header)          # may append counts to the header
        parts = [header]
        if body is not None:
            parts.append(body)
        result = self._result()
        if result is not None:
            parts.append(result)
        return Group(*parts) if len(parts) > 1 else header

    # -- behaviour ----------------------------------------------------------------------

    def on_mount(self) -> None:
        """Initialize the tool call display after mounting."""
        if self.streaming:                  # redraw at most 10x/second, however fast tokens arrive
            self._timer = self.set_interval(0.1, self._tick)

    def feed(self, fragment: str) -> None:
        """A piece of the argument JSON arrived (called for every streamed fragment)."""
        self.raw += fragment
        self._dirty = True

    def _tick(self) -> None:
        if self.streaming and self._dirty:
            self._dirty = False
            self._set_args(self._padded(partial_fields(self.raw)))
            self.update(self._build())

    def _stop_streaming(self) -> None:
        self.streaming = False
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def finalize(self, args: Any) -> None:
        """The arguments are complete: switch to the real, parsed ones."""
        self._stop_streaming()
        self._set_args(args)
        self.update(self._build())

    def abort(self) -> None:
        """The request was cancelled before this call ever ran."""
        if self.streaming:
            self._stop_streaming()
            self.result = "ERROR: interrupted"
            self.update(self._build())

    def set_result(self, text: str) -> None:
        """Attach a tool result to the displayed call."""
        self.result = str(text)
        self.update(self._build())

    def toggle(self) -> None:
        """Expand or collapse the tool call details."""
        self.expanded = not self.expanded
        self.update(self._build())

    def on_click(self) -> None:
        """Toggle the tool call details when clicked."""
        self.toggle()


class Notice(Static):
    """Render a consistently formatted status log entry."""

    def __init__(self, level: str, text: str) -> None:
        super().__init__(format_log(level, text), classes="notice")


class Say(Static):
    """Plain command output."""

    def __init__(self, text: str, style: str | None = None) -> None:
        super().__init__(Text(text, style=style or ""), classes="say")


class Citations(Static):
    """Render citation links and validation details."""

    def __init__(self, text: str) -> None:
        super().__init__(Text(text, style="yellow" if "WARNING" in text else "green"),
                         classes="citations")


class ArtifactsList(Static):
    """Render the files produced or changed by a request."""

    def __init__(self, found) -> None:
        from rich.style import Style
        t = Text("Files from this request:", style="dim")
        for n, a in enumerate(found.items, start=1):
            size = f"{a.size / 1_000_000:.1f} MB" if a.size >= 1_000_000 else f"{max(1, a.size // 1024)} KB"
            line = f"\n  {n}. {a.status:<7} {a.rel}  {size}"
            try:
                t.append(line, style=Style(link=a.path.as_uri()))
            except Exception:
                t.append(line)
        if found.more:
            t.append(f"\n  ... and {found.more} more", style="dim")
        if found.deleted:
            t.append(f"\n  ({found.deleted} file(s) deleted)", style="dim")
        super().__init__(t, classes="artifacts")


"""
⣇⣿⠘⣿⣿⣿⡿⡿⣟⣟⢟⢟⢝⠵⡝⣿⡿⢂⣼⣿⣷⣌⠩⡫⡻⣝⠹⢿⣿⣷
⡆⣿⣆⠱⣝⡵⣝⢅⠙⣿⢕⢕⢕⢕⢝⣥⢒⠅⣿⣿⣿⡿⣳⣌⠪⡪⣡⢑⢝⣇
⡆⣿⣿⣦⠹⣳⣳⣕⢅⠈⢗⢕⢕⢕⢕⢕⢈⢆⠟⠋⠉⠁⠉⠉⠁⠈⠼⢐⢕⢽
⡗⢰⣶⣶⣦⣝⢝⢕⢕⠅⡆⢕⢕⢕⢕⢕⣴⠏⣠⡶⠛⡉⡉⡛⢶⣦⡀⠐⣕⢕
⡝⡄⢻⢟⣿⣿⣷⣕⣕⣅⣿⣔⣕⣵⣵⣿⣿⢠⣿⢠⣮⡈⣌⠨⠅⠹⣷⡀⢱⢕
⡝⡵⠟⠈⢀⣀⣀⡀⠉⢿⣿⣿⣿⣿⣿⣿⣿⣼⣿⢈⡋⠴⢿⡟⣡⡇⣿⡇⡀⢕
⡝⠁⣠⣾⠟⡉⡉⡉⠻⣦⣻⣿⣿⣿⣿⣿⣿⣿⣿⣧⠸⣿⣦⣥⣿⡇⡿⣰⢗⢄
⠁⢰⣿⡏⣴⣌⠈⣌⠡⠈⢻⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣬⣉⣉⣁⣄⢖⢕⢕⢕
⡀⢻⣿⡇⢙⠁⠴⢿⡟⣡⡆⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⣵⣵⣿
⡻⣄⣻⣿⣌⠘⢿⣷⣥⣿⠇⣿⣿⣿⣿⣿⣿⠛⠻⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿
⣷⢄⠻⣿⣟⠿⠦⠍⠉⣡⣾⣿⣿⣿⣿⣿⣿⢸⣿⣦⠙⣿⣿⣿⣿⣿⣿⣿⣿⠟
⡕⡑⣑⣈⣻⢗⢟⢞⢝⣻⣿⣿⣿⣿⣿⣿⣿⠸⣿⠿⠃⣿⣿⣿⣿⣿⣿⡿⠁⣠
⡝⡵⡈⢟⢕⢕⢕⢕⣵⣿⣿⣿⣿⣿⣿⣿⣿⣿⣶⣶⣿⣿⣿⣿⣿⠿⠋⣀⣈⠙
⡝⡵⡕⡀⠑⠳⠿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠿⠛⢉⡠⡲⡫⡪⡪⡣ 
"""

def welcome_panel(project_name: str | None, ws_label: str, version: str = "1.4.0",
                  open_mode: bool = False) -> Panel:
    """Build the welcome panel with session tips and recent activity."""
    left = Text(justify="center")
    left.append("Welcome to Danica\n\n", style=f"bold {ACCENT}")
    left.append("""
⣿⠛⠛⠛⠛⠻⡆⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀
⠛⢛⣿⠋⢀⡾⠃⠀⠀⠀⠀⢀⣤⣤⠤⠤⣤⣤⣀⣀⣀⣠⠶⡶⣤⣀⣠⠾⡷⣦⣀⣤⣤⡤⠤⠦⢤⣤⣄⡀⠀⢠⡶⢶⡄⠀⠀
⢠⡟⠁⣴⣿⢤⡄⣴⢶⠶⡆⠈⢷⡀⠀⠀⠀⠀⢀⣭⣫⠵⠥⠽⣄⣝⠵⢍⣘⣄⠳⣤⣀⠀⠀⢀⡤⠊⣽⠁⠀⠸⣇⠀⢿⠀⠀
⠸⢷⣴⣤⡤⠾⠇⣽⠋⠼⣷⠀⠈⢷⡄⢀⣤⡶⠋⠀⣀⡄⠤⠀⡲⡆⠀⠀⠈⠙⡄⠘⢮⢳⡴⠯⣀⢠⡏⠀⠀⠀⢻⠀⢸⠇⠀
⠀⠀⠀⠀⠀⠀⠀⠙⠛⠋⠉⢀⣴⠟⠉⢯⡞⡠⢲⠉⣼⠀⠀⡰⠁⡇⢀⢷⠀⣄⢵⠀⠈⡟⢄⠀⠀⠙⢷⣤⣤⣤⡿⢢⡿⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣠⠟⠑⠊⠁⡼⣌⢠⢿⢸⢸⡀⢰⠁⡸⡇⡸⣸⢰⢈⠘⡄⠀⢸⠀⢣⡀⠀⠈⢮⢢⣏⣤⡾⠃⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⢰⣯⣴⠞⡠⣼⠁⡘⣾⠏⣿⢇⣳⣸⣞⣀⢱⣧⣋⣞⡜⢳⡇⠀⢸⠀⢆⢧⠀⠰⣄⢏⢧⣾⠁⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠈⢹⡏⢰⠁⡻⠀⡟⡏⠉⠀⣀⠀⠀⠀⠀⣀⠁⠀⠉⠛⢽⠇⠀⣼⡆⠈⡆⠃⠀⡏⠻⣾⣽⣇⡀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢸⠁⡇⠀⡇⡄⣿⠷⠿⠿⠛⠀⠀⠀⠀⠛⠻⠿⠿⠿⡜⢀⡴⡟⢸⣸⡼⠀⠀⡇⠀⡞⡆⢻⠙⢦⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢸⡶⢀⣼⣿⣬⣽⠧⠬⠇⠀⠀⠀⠀⠀⠀⢞⣯⣭⢺⣔⣪⣾⣤⠺⡇⢳⠀⢠⣧⡾⠛⠛⠻⠶⠞⠁
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠘⠷⢿⠟⠉⡀⠈⢦⡀⠀⠀⣠⠖⠒⠒⢤⡀⠀⢀⡼⠿⢇⡣⢬⣶⠷⢿⣤⡾⠁⠀⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠘⠷⠾⠷⠖⠛⠛⠲⠶⠿⠤⣤⠤⠤⢷⣶⠋⠀⠀⠀⣱⠞⠁⠀⠈⠉⠀⠀⠀⠀⠀⠀⠀⠀⠀
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠉⠛⠓⠒⠚⠋⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀
""", style=f"bold {ACCENT}")
    if open_mode:                      # impossible to miss: this session can reach the whole disk
        
        left.append("full disk access\n", style="bold red")
        left.append("start folder:\n", style="dim")
    else:
        left.append(f"project: {project_name}\n" if project_name else "chat mode\n", style="dim")
    left.append(f"{ws_label}", style="dim")


    # Right column content
    right = Text()
    right.append("Tips for getting started\n", style=f"bold {ACCENT}")
    right.append(f"{random.choice(TIPS_LABEL)}\n")

    right.append("─" * 40 + "\n", style="dim")

    right.append("Recent activity\n", style=f"bold {ACCENT}")

    from danica.sessions import list_sessions
    infos = list_sessions()
    
    if not infos:
        right.append("No recent activity", style="dim")
    else:
        lines = []
        for info in infos[:4]:
            text = Text()
            text.append(info["id"], style="dim")
            text.append(f" {info.get('title') or info['preview']}", style="bold")
            lines.append(text)
        right.append(Text("\n").join(lines))
        if len(infos) > 4:
            right.append(f"\n… and {len(infos) - 4} more", style="dim")

    # Calculate required height for the vertical divider
    left_height = left.plain.count("\n") + 1
    right_height = right.plain.count("\n") + 1
    max_height = max(left_height, right_height)

    # Use a 3-column table: Left content, Divider line, Right content
    body = Table(
        box=None, 
        show_edge=False, 
        show_header=False, 
        padding=(0, 1), 
        expand=True
    )
    body.add_column(ratio=2, justify="center", vertical="middle")
    body.add_column(width=1, justify="center", vertical="middle")  # Divider column
    body.add_column(ratio=3, vertical="top")
    
    # Add the vertical line character to the middle column
    divider = Text("\n".join(["│"] * max_height), style="dim")
    
    body.add_row(left, divider, right)

    return Panel(
        body, 
        title=f" Danica v{version} ",
        title_align="left",
        border_style="bold red" if open_mode else ACCENT,
        box=box.ROUNDED,
        padding=(1, 1)
    )

# ----------------------------------------------------------------------------- bottom area

def load_history(path: Path = HISTORY_PATH) -> list[str]:
    """Reads prompt_toolkit's FileHistory format, so up-arrow history carries over.
    A multiline entry is several "+" lines under one "# timestamp" header."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries: list[str] = []
    cur: list[str] | None = None
    for line in lines:
        if line.startswith("#"):
            if cur:
                entries.append("\n".join(cur))
            cur = []
        elif line.startswith("+"):
            if cur is None:
                cur = []
            cur.append(line[1:])
    if cur:
        entries.append("\n".join(cur))
    return [e for e in entries if e.strip()]


class PromptInput(TextArea):
    """The prompt. Enter sends. A new line: Ctrl+J, Shift+Enter (terminals that report it), or a
    trailing backslash then Enter. Up/Down walk the history when the cursor is on the first/last
    line. With the slash-command popup open, Up/Down pick, Tab/Enter complete and Esc closes it."""

    class Submitted(Message):
        def __init__(self, prompt: "PromptInput", value: str) -> None:
            super().__init__()
            self.prompt = prompt
            self.value = value

    class PopupKey(Message):
        """Asks the app to act on the popup: action is up, down, accept or close."""
        def __init__(self, action: str) -> None:
            super().__init__()
            self.action = action

    def __init__(self, **kwargs) -> None:
        super().__init__("", soft_wrap=True, show_line_numbers=False, highlight_cursor_line=False,
                         tab_behavior="focus", **kwargs)
        self.prompt_history = load_history()
        self._pos: int | None = None
        self._draft = ""
        self.popup_open = False          # set by the app while the command popup is showing

    # `value` mirrors Input's API so the rest of the app reads the same either way
    @property
    def value(self) -> str:
        """Return or update the current prompt text."""
        return self.text

    @value.setter
    def value(self, text: str) -> None:
        """Return or update the current prompt text."""
        self.load_text(text)
        self.move_cursor(self.document.end)

    def remember(self, text: str) -> None:
        """Save a submitted prompt to input history."""
        self._pos = None
        if not self.prompt_history or self.prompt_history[-1] != text:
            self.prompt_history.append(text)
            try:
                HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
                body = "".join(f"+{line}\n" for line in text.split("\n"))
                with open(HISTORY_PATH, "a", encoding="utf-8") as f:
                    f.write(f"\n# {datetime.now():%Y-%m-%d %H:%M:%S.%f}\n{body}")
            except OSError:
                pass

    def _recall(self, step: int) -> None:
        if not self.prompt_history:
            return
        if self._pos is None:
            if step > 0:
                return
            self._draft = self.text
            self._pos = len(self.prompt_history)
        pos = self._pos + step
        if pos >= len(self.prompt_history):
            self._pos = None
            self.value = self._draft
            return
        self._pos = max(pos, 0)
        self.value = self.prompt_history[self._pos]

    async def _on_key(self, event) -> None:
        key = event.key
        if self.popup_open:
            popup_action = {"up": "up", "down": "down", "tab": "accept", "escape": "close"}.get(key)
            if key == "enter":
                popup_action = "accept"       # the app decides: complete, or submit if already exact
            if popup_action:
                event.stop()
                event.prevent_default()
                self.post_message(self.PopupKey(popup_action))
                return
        if key == "enter":
            event.stop()
            event.prevent_default()
            text = self.text
            if text.endswith("\\") and self.cursor_location == self.document.end:
                self.load_text(text[:-1] + "\n")           # backslash + Enter = continue on a new line
                self.move_cursor(self.document.end)
                return
            self.post_message(self.Submitted(self, text))
            return
        if key in ("ctrl+j", "shift+enter"):
            event.stop()
            event.prevent_default()
            self.insert("\n")
            return
        row = self.cursor_location[0]
        if key == "up" and row == 0 and self.selection.is_empty:
            event.stop()
            event.prevent_default()
            self._recall(-1)
            self.move_cursor((0, len(self.document.get_line(0))))
            return
        if key == "down" and row == self.document.line_count - 1 and self._pos is not None:
            event.stop()
            event.prevent_default()
            self._recall(1)
            return
        await super()._on_key(event)


class CommandPopup(OptionList):
    """Slash-command suggestions above the prompt. Never takes focus: the prompt keeps it."""

    can_focus = False

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.matches: list[str] = []

    def show_matches(self, matches: list[tuple[str, str]]) -> None:
        """Display commands matching the current prompt prefix."""
        self.matches = [name for name, _ in matches]
        self.clear_options()
        width = max(len(name) for name, _ in matches) + 2
        for name, desc in matches:
            label = Text()
            label.append(f"{name:<{width}}", style=f"bold {ACCENT}")
            label.append(desc, style="dim")
            self.add_option(Option(label, id=name))
        self.highlighted = 0
        self.display = True

    def hide(self) -> None:
        """Hide the command completion popup."""
        self.matches = []
        self.display = False

    def current(self) -> str | None:
        """Return the currently selected command completion."""
        index = self.highlighted
        if index is None or not 0 <= index < len(self.matches):
            return None
        return self.matches[index]

class SubagentPane(Vertical):
    """Right-hand half of the split screen: a live view of the running subagent.
    head = status, log = finished lines, live = the line being streamed right now.
    All methods run on the UI thread."""

    def compose(self) -> Any:
        """Compose the delegated worker activity pane."""
        yield Static("", id="sub-head")
        yield RichLog(id="sub-log", wrap=True, markup=False, highlight=False)
        yield Static("", id="sub-live")

    def on_mount(self) -> None:
        """Prepare the activity pane after it is mounted."""
        self.border_title = "Subagent"
        self._text = ""                                   # streamed text not yet a full line

    def _log(self) -> RichLog:
        return self.query_one("#sub-log", RichLog)

    def _head(self, text: str, style: str = "bold") -> None:
        self.query_one("#sub-head", Static).update(Text(text, style=style))

    def _live(self, text: str = "", style: str = "dim") -> None:
        self.query_one("#sub-live", Static).update(Text(text[-200:], style=style))

    def _flush(self) -> None:
        """Turn any half-streamed text into a finished log line."""
        if self._text.strip():
            self._log().write(Text(self._text.strip()))
        self._text = ""
        self._live()

    def handle(self, child_id: str, kind: str, data: Any) -> None:
        """Update the activity pane from a worker event."""
        from danica.subagents import describe_event
        log = self._log()
        if kind == "start":
            if log.lines:                                 # a second subagent: separate the runs
                log.write(Text("─" * 40, style="dim"))
            self._text = ""
            self._head(f"● {child_id} running", "bold green")
            log.write(Text("Task: " + " ".join(str(data).split())[:300], style="bold"))
        elif kind == "content":
            self._text += str(data)
            *done, self._text = self._text.split("\n")     # complete lines go to the log
            for line in done:
                if line.strip():
                    log.write(Text(line))
            self._live(self._text, "")
        elif kind == "thinking":
            self._live("thinking… " + " ".join(str(data).split()), "dim italic")
        elif kind in ("done", "failed", "killed"):
            self._flush()
            label = {"done": ("✔ finished", "bold green"), "failed": ("✘ failed", "bold red"),
                     "killed": ("■ stopped", "bold yellow")}[kind]
            self._head(f"{label[0]}  {child_id}", label[1])
            if kind != "done":                            # "done" carries the summary, and the
                log.write(Text(str(data), style=label[1]))   # main transcript already gets it
        else:                                             # tool_call / tool_result / notice / ...
            self._flush()
            described = describe_event(kind, data)
            if described:
                log.write(Text(described[1], style=described[0]))

class BusyLine(Static):
    """Render an animated status line while the agent is working."""

    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, **kwargs) -> None:
        super().__init__("", **kwargs)
        self._label = ""
        self._stoppable = False
        self._t0 = 0.0
        self._i = 0
        self._timer = None

    def on_mount(self) -> None:
        """Initialize the animation when the status line mounts."""
        self._timer = self.set_interval(0.1, self._tick, pause=True)

    def start(self, label: str, stoppable: bool = False) -> None:
        """Start the working status animation."""
        self._label, self._stoppable = label, stoppable
        self._t0, self._i = time.monotonic(), 0
        if self._timer:
            self._timer.resume()
        self._tick()

    def stop(self) -> None:
        """Stop the working status animation."""
        if self._timer:
            self._timer.pause()
        self.update("")

    def _tick(self) -> None:
        self._i += 1
        secs = int(time.monotonic() - self._t0)
        t = Text()
        t.append(self.FRAMES[self._i % len(self.FRAMES)] + " ", style=f"bold {ACCENT}")
        suffix = " · Esc to stop" if self._stoppable else ""
        t.append(f"{self._label}… ({secs}s{suffix})", style="#d8deeb")
        self.update(t)


class StatusBar(Static):
    """Render compact session and context status in the footer."""
