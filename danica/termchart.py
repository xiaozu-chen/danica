"""
termchart.py - draw a chart in the TERMINAL from the same JSON data file the chart tool draws its PNG from.

This is only a preview in the chat: the PNG and the interactive HTML chart are made exactly as before. Plotext does the
drawing of bar, hbar, stacked, line, area and scatter charts. It cannot draw a pie or a table, so those are drawn here
with Rich (a pie / donut becomes ranked bars with percentages, a table becomes a normal table).

The spec is the CLEANED one from charts.validate(), so every value is already checked. render() never raises: if
plotext is not installed or something goes wrong it returns None and the chat simply shows no preview.

Plotext is pinned to 5.3.2 (requirements). Plotext 6.x was rewritten with a different API.
"""

from __future__ import annotations

import threading
from typing import Any

from rich import box
from rich.console import Group
from rich.table import Table
from rich.text import Text

from danica.charts import PALETTE

_LOCK = threading.Lock()                 # plotext draws on ONE global figure: never two at once
MIN_WIDTH, MAX_WIDTH = 40, 110


def _rgb(hex_colour: str) -> tuple[int, int, int]:
    return tuple(int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))             # type: ignore[return-value]


def _colour(i: int) -> tuple[int, int, int]:
    return _rgb(PALETTE[i % len(PALETTE)])


def _clean(text: str) -> str:
    """charts.py escapes "$" for matplotlib; a terminal does not need that."""
    return text.replace("\\$", "$")


def available() -> bool:
    """Is plotext installed?"""
    try:
        import plotext  # noqa: F401
    except ImportError:
        return False
    return True


# ------------------------------------------------------------------ pie / donut and table (Rich only)

def _share_bars(spec: dict[str, Any], width: int):
    """A pie or donut as bars, biggest first: label, bar, percentage and value."""
    labels = [_clean(l) for l in spec["labels"]]
    values = spec["series"][0]["values"]
    total = sum(values) or 1.0
    order = sorted(range(len(values)), key=lambda i: -values[i])
    label_w = min(24, max(len(l) for l in labels))
    bar_w = max(8, width - label_w - 20)
    lines = []
    if spec["title"]:
        lines.append(Text(_clean(spec["title"]), style="bold"))
    for i in order:
        share = values[i] / total
        line = Text(f"{labels[i][:label_w]:<{label_w}} ")
        line.append("█" * max(1 if values[i] > 0 else 0, round(share * bar_w)), style="rgb(%d,%d,%d)" % _colour(i))
        line.append(f" {share * 100:5.1f}%  {values[i]:g}", style="dim")
        lines.append(line)
    return Group(*lines)


def _table(spec: dict[str, Any]):
    table = Table(title=_clean(spec["title"]) or None, box=box.SIMPLE_HEAD, header_style="bold", title_justify="left")
    for column in spec["columns"]:
        table.add_column(_clean(column), overflow="fold")
    for row in spec["rows"]:
        table.add_row(*[_clean(cell) for cell in row])
    return table


# ------------------------------------------------------------------ plotext charts

def _ticks(labels: list[str], limit: int) -> tuple[list[int], list[str]]:
    """x positions 1..n and at most `limit` of the labels (a crowded axis is unreadable)."""
    step = max(1, -(-len(labels) // limit))
    positions = list(range(1, len(labels) + 1))[::step]
    return positions, labels[::step][:len(positions)]


def _plot(spec: dict[str, Any], width: int, height: int):
    import plotext as plt
    from rich.text import Text

    kind, series = spec["type"], spec["series"]
    n = len(series)
    names = [_clean(s["name"]) for s in series]
    colours = [_colour(i) for i in range(n)]
    horizontal = kind == "hbar" or (kind == "bar" and len(spec.get("labels", [])) > 14)   # many labels: bars lie down, names stay readable
    with _LOCK:
        try:
            plt.clear_figure()
            plt.theme("clear")                                    # no background colour: Danica's own shows through
            if kind in ("bar", "hbar", "stacked"):
                labels = [_clean(l) for l in spec["labels"]]
                data = [s["values"] for s in series]
                orient = "h" if horizontal else "v"
                if horizontal and kind != "stacked":
                    labels, data = labels[::-1], [d[::-1] for d in data]       # plotext draws the first label at the bottom; the PNG has it on top
                if kind == "stacked":
                    plt.stacked_bar(labels, data, labels=names, color=colours)
                elif n == 1:
                    plt.bar(labels, data[0], color=colours[0], orientation=orient)
                else:
                    plt.multiple_bar(labels, data, labels=names, color=colours, orientation=orient)
                if horizontal:
                    height = max(height, min(36, len(labels) * (n + 1) + 4))
            elif kind in ("line", "area"):
                labels = [_clean(l) for l in spec["labels"]]
                xs = list(range(1, len(labels) + 1))
                if kind == "line":
                    for i, s in enumerate(series):
                        plt.plot(xs, s["values"], label=names[i] if n > 1 else None, color=colours[i])
                else:                                            # area is stacked: draw the running totals, the biggest first, so each fill covers the one above
                    totals = [[sum(series[j]["values"][k] for j in range(i + 1)) for k in range(len(xs))] for i in range(n)]
                    for i in reversed(range(n)):
                        plt.plot(xs, totals[i], label=names[i] if n > 1 else None, color=colours[i], fillx=True)
                positions, shown = _ticks(labels, max(2, width // 9))
                plt.xticks(positions, shown)
            else:                                                # scatter
                for i, s in enumerate(series):
                    plt.scatter([p[0] for p in s["points"]], [p[1] for p in s["points"]],
                                label=names[i] if n > 1 else None, color=colours[i])
            if spec["title"]:
                plt.title(_clean(spec["title"]))
            if spec["x_label"]:
                plt.xlabel(_clean(spec["x_label"]))
            if spec["y_label"]:
                plt.ylabel(_clean(spec["y_label"]))
            plt.plotsize(width, height)
            ansi = plt.build()
        finally:
            plt.clear_figure()
    return Text.from_ansi(ansi)


# ------------------------------------------------------------------ public

def preview_for_tool(ws, tool: str, args: Any, result: Any, width: int) -> Any:
    """The terminal preview for a chart tool call that just succeeded, or None (another tool, a failed call, the setting
    switched off, or anything that can't be drawn). `args` are the arguments the model gave, `ws` the workspace."""
    import json

    from danica.charts import MAX_SPEC_BYTES, validate
    from danica.config import CONFIG
    if tool != "chart" or not str(result).startswith("OUTPUT: saved") or not isinstance(args, dict):
        return None
    if not (CONFIG.get("display") or {}).get("terminal_charts", True):
        return None
    try:
        path = ws.resolve(str(args.get("file", "")))             # same folder jail as the tool itself
        if path.suffix.lower() != ".json" or not path.is_file() or path.stat().st_size > MAX_SPEC_BYTES:
            return None
        return render(validate(json.loads(path.read_text(encoding="utf-8-sig"))), width)
    except Exception:
        return None


def render(spec: dict[str, Any], width: int = 80, height: int = 16):
    """A Rich renderable for a validated chart spec, or None if it can't be drawn (never raises)."""
    width = max(MIN_WIDTH, min(MAX_WIDTH, int(width)))
    try:
        if spec["type"] == "table":
            return _table(spec)
        if spec["type"] in ("pie", "donut"):
            return _share_bars(spec, width)
        if not available():
            return None
        return _plot(spec, width, max(8, min(40, int(height))))
    except Exception:                                            # a preview must never break the chat
        return None
