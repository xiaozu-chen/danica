"""
widgets.py - The things that appear in the transcript, plus the prompt, busy line and status bar.
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from rich import box
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from textual.binding import Binding
from textual.widgets import Input, Markdown, Static

from danica.config import APP_DIR
from danica.ui import TAG_COLORS, TAG_WIDTH

ACCENT = "#ff9e64"
HISTORY_PATH = APP_DIR / "history.txt"


# ----------------------------------------------------------------------------- transcript

class UserMessage(Static):
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
        self.set_interval(0.08, self._flush)

    def append(self, text: str) -> None:
        self._buf += text
        self._dirty = True

    def _flush(self) -> None:
        if self._dirty:
            self._dirty = False
            self.update(self._buf)

    def finish(self) -> None:
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
        self.buf += text
        self._paint()

    def finish(self) -> None:
        self.done = True
        self._paint()

    def on_click(self) -> None:
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


def summarize_args(args) -> str:
    if not isinstance(args, dict):
        return str(args)[:120]
    parts = []
    for key, value in args.items():
        if isinstance(value, str) and key in ("content", "new_string", "old_string", "text") and len(value) > 60:
            parts.append(f"{key}=<{len(value):,} chars>")
        else:
            shown = str(value).replace("\n", "\\n")
            parts.append(f"{key}={shown[:80] + '…' if len(shown) > 80 else shown}")
    return "  ".join(parts)


class ToolCall(Static):
    """● Tool Name  arg=value   (result preview below; click or Ctrl+O for the full output)."""

    PREVIEW = 400
    FULL = 6000

    def __init__(self, name: str, args) -> None:
        self.title_text = name.replace("_", " ").title()
        self.summary = summarize_args(args)
        self.result: str | None = None
        self.expanded = False
        super().__init__(self._render_text(), classes="toolcall")

    def _render_text(self) -> Text:
        t = Text()
        t.append("● ", style="bold #7aa2f7" if self.result is None else "bold #9ece6a")
        t.append(self.title_text, style="bold")
        if self.summary:
            t.append("  " + self.summary, style="#7aa2f7")
        if self.result is not None:
            is_error = self.result.lstrip().startswith("ERROR")
            limit = self.FULL if self.expanded else self.PREVIEW
            body = self.result[:limit]
            more = len(self.result) - len(body)
            if more > 0:
                body += f"… ({more:,} more chars, click to expand)"
            t.append("\n" + body, style="#f7768e" if is_error else "dim")
        return t

    def set_result(self, text) -> None:
        self.result = str(text)
        self.update(self._render_text())

    def toggle(self) -> None:
        self.expanded = not self.expanded
        self.update(self._render_text())

    def on_click(self) -> None:
        self.toggle()


class Notice(Static):
    """The coloured badge from the old _write_log: ' WARNING ' ::message"""

    def __init__(self, level: str, text: str) -> None:
        key = level.upper()
        color = TAG_COLORS.get(key, "white")
        t = Text()
        t.append(f" {key:^{TAG_WIDTH}} ", style=f"bold #000000 on {color}")
        t.append("::" + text)
        super().__init__(t, classes="notice")


class Say(Static):
    """Plain command output."""

    def __init__(self, text: str, style: str | None = None) -> None:
        super().__init__(Text(text, style=style or ""), classes="say")


class Citations(Static):
    def __init__(self, text: str) -> None:
        super().__init__(Text(text, style="yellow" if "WARNING" in text else "green"),
                         classes="citations")


class ArtifactsList(Static):
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


def image_widget(path: Path, label: str):
    """Inline picture via the optional `textual-image` package, else a one-line placeholder."""
    try:
        from textual_image.widget import Image
        widget = Image(path)
        widget.add_class("inline-image")
        return widget
    except Exception:
        return Static(Text(f"▣ image: {label}   (pip install textual-image to see it inline)", style="dim"),
                      classes="say")


def welcome_panel(project_name: str | None, ws_label: str, version: str = "0.1") -> Panel:
    left = Text(justify="center")
    left.append("Welcome back!\n\n", style="bold")
    left.append("  /\\_/\\ \n ( o.o )\n  > ^ <\n\n", style=ACCENT)
    left.append(f"project: {project_name}\n" if project_name else "chat mode\n", style="dim")
    left.append(ws_label, style="dim")

    right = Text()
    right.append("Tips for getting started\n", style=f"bold {ACCENT}")
    right.append("/help      list every command\n")
    right.append("/new       fresh session\n")
    right.append("/think     toggle thinking\n")
    right.append("Ctrl+O     expand tool output\n")
    right.append("\n")
    right.append("Esc/Ctrl+C stops a task · Ctrl+D quits", style="dim")

    body = Table(box=box.SQUARE, show_edge=False, show_header=False, padding=(0, 2), expand=True)
    body.add_column(ratio=2, justify="center", vertical="middle")
    body.add_column(ratio=3, vertical="middle")
    body.add_row(left, right)
    return Panel(body, title=f" Danica v{version} ", title_align="left",
                 border_style=ACCENT, box=box.ROUNDED, padding=(1, 1))


# ----------------------------------------------------------------------------- bottom area

def load_history(path: Path = HISTORY_PATH) -> list[str]:
    """Reads prompt_toolkit's FileHistory format, so up-arrow history carries over."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries: list[str] = []
    cur: list[str] | None = None
    for line in lines:
        if line.startswith("#"):
            if cur:
                entries.append(" ".join(cur))
            cur = []
        elif line.startswith("+"):
            if cur is None:
                cur = []
            cur.append(line[1:])
    if cur:
        entries.append(" ".join(cur))
    return [e for e in entries if e.strip()]


class PromptInput(Input):
    BINDINGS = [
        Binding("up", "history(-1)", show=False),
        Binding("down", "history(1)", show=False),
    ]

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.history = load_history()
        self._pos: int | None = None
        self._draft = ""

    def remember(self, text: str) -> None:
        self._pos = None
        if not self.history or self.history[-1] != text:
            self.history.append(text)
            try:
                HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
                with open(HISTORY_PATH, "a", encoding="utf-8") as f:
                    f.write(f"\n# {datetime.now():%Y-%m-%d %H:%M:%S.%f}\n+{text}\n")
            except OSError:
                pass

    def _set(self, value: str) -> None:
        self.value = value
        self.cursor_position = len(value)

    def action_history(self, step: int) -> None:
        if not self.history:
            return
        if self._pos is None:
            if step > 0:
                return
            self._draft = self.value
            self._pos = len(self.history)
        pos = self._pos + step
        if pos >= len(self.history):
            self._pos = None
            self._set(self._draft)
            return
        self._pos = max(pos, 0)
        self._set(self.history[self._pos])


class BusyLine(Static):
    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, **kwargs) -> None:
        super().__init__("", **kwargs)
        self._label = ""
        self._t0 = 0.0
        self._i = 0
        self._timer = None

    def on_mount(self) -> None:
        self._timer = self.set_interval(0.1, self._tick, pause=True)

    def start(self, label: str) -> None:
        self._label, self._t0, self._i = label, time.monotonic(), 0
        if self._timer:
            self._timer.resume()
        self._tick()

    def stop(self) -> None:
        if self._timer:
            self._timer.pause()
        self.update("")

    def _tick(self) -> None:
        self._i += 1
        secs = int(time.monotonic() - self._t0)
        t = Text()
        t.append(self.FRAMES[self._i % len(self.FRAMES)] + " ", style=f"bold {ACCENT}")
        t.append(f"{self._label}… ({secs}s · Esc to stop)", style=ACCENT)
        self.update(t)


class StatusBar(Static):
    pass