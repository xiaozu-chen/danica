"""
charts.py - Turning a small data spec (JSON) into a PNG chart or table.

The MODEL never writes plotting code: it writes data, and this file draws it. So every
value is checked (types, sizes, matching lengths), and every piece of text is cleaned
before it is drawn (matplotlib would read "$...$" as math, so "$" is escaped).

Spec formats (validate() explains mistakes in plain words):
    bar / line / area:  {"type", "title", "x_label", "y_label", "labels": [...],
                         "series": [{"name", "values": [numbers]}]}      (area is stacked)
    pie / donut:        the same with exactly ONE series, no negative values
    hbar:               horizontal bars (same format as bar; good for long labels and rankings)
    stacked:            stacked bars (same format as bar; each label's bar is split by series)
    scatter:            {"series": [{"name", "points": [[x, y], ...]}]}
    table:              {"columns": [...], "rows": [[...], ...]}
"""

import io
import unicodedata
from typing import Any

TYPES = ("bar", "line", "area", "pie", "donut", "hbar", "stacked", "scatter", "table")
MAX_LABELS, MAX_SERIES, MAX_POINTS, MAX_PIE = 100, 8, 2000, 12
MAX_COLUMNS, MAX_ROWS = 8, 30
MAX_TEXT, MAX_TITLE = 60, 120
MAX_SPEC_BYTES = 200_000
MAX_VALUE = 1e15

# Okabe-Ito: distinguishable for most kinds of colour blindness
PALETTE = ["#0072B2", "#E69F00", "#009E73", "#D55E00", "#CC79A7", "#56B4E9", "#F0E442", "#999999"]


class ChartError(Exception):
    """A problem with the data or the drawing. The message is safe to show the model."""


def _text(value, limit: int = MAX_TEXT) -> str:
    """Text ready to draw: one line, invisible characters removed, cut to length,
    and "$" escaped so matplotlib does not try to render it as math."""
    s = " ".join(str(value).split())
    s = "".join(ch for ch in s if unicodedata.category(ch) not in ("Cc", "Cf", "Co", "Cn", "Cs"))
    return s[:limit].replace("$", "\\$")


def _num(v) -> bool:
    """A real number (not text, not true/false, not NaN or infinite, not absurdly big)."""
    return (
        isinstance(v, (int, float))
        and not isinstance(v, bool)
        and abs(v) < MAX_VALUE
    )


# --------------------------------------------------------------- validation

def validate(data: dict[str, Any]) -> dict[str, Any]:
    """Check a spec and return a cleaned copy. Raises ChartError with a fix-it message."""
    if not isinstance(data, dict):
        raise ChartError('the file must hold one JSON object, like {"type": "bar", "labels": [...], "series": [...]}')
    kind = data.get("type")
    if kind not in TYPES:
        raise ChartError('"type" must be one of: ' + ", ".join(TYPES))
    spec = {"type": kind, "title": _text(data.get("title", ""), MAX_TITLE),
            "x_label": _text(data.get("x_label", "")), "y_label": _text(data.get("y_label", ""))}

    if kind == "table":
        columns, rows = data.get("columns"), data.get("rows")
        if not isinstance(columns, list) or not 1 <= len(columns) <= MAX_COLUMNS:
            raise ChartError(f'a table needs "columns": a list of 1 to {MAX_COLUMNS} column names')
        if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ROWS:
            raise ChartError(f'a table needs "rows": a list of 1 to {MAX_ROWS} rows')
        for i, row in enumerate(rows, start=1):
            if not isinstance(row, list) or len(row) != len(columns):
                raise ChartError(f"row {i} must be a list of {len(columns)} cells (one per column)")
        spec["columns"] = [_text(c) for c in columns]
        spec["rows"] = [["" if c is None else _text(c, 40) for c in row] for row in rows]
        return spec

    series = data.get("series")
    if not isinstance(series, list) or not 1 <= len(series) <= MAX_SERIES:
        raise ChartError(f'a {kind} chart needs "series": a list of 1 to {MAX_SERIES} items')
    for i, s in enumerate(series, start=1):
        if not isinstance(s, dict):
            raise ChartError(f"series {i} must be an object like " + (
                '{"name": "A", "points": [[1, 2]]}' if kind == "scatter" else '{"name": "A", "values": [1, 2]}'))

    if kind == "scatter":
        total, out = 0, []
        for i, s in enumerate(series, start=1):
            points = s.get("points")
            if not isinstance(points, list) or not points:
                raise ChartError(f'series {i} needs "points": a list of [x, y] pairs')
            total += len(points)
            if total > MAX_POINTS:
                raise ChartError(f"too many points (limit {MAX_POINTS})")
            clean = []
            for p in points:
                if not (
                    isinstance(p, list)
                    and len(p) == 2
                    and _num(p[0])
                    and _num(p[1])
                ):
                    raise ChartError(f"series {i} has a point that is not [number, number]")
                clean.append((float(p[0]), float(p[1])))
            out.append({"name": _text(s.get("name") or f"Series {i}"), "points": clean})
        spec["series"] = out
        return spec

    labels = data.get("labels")
    if not isinstance(labels, list) or not 1 <= len(labels) <= MAX_LABELS:
        raise ChartError(f'a {kind} chart needs "labels": a list of 1 to {MAX_LABELS} names')
    if any(isinstance(l, bool) or not isinstance(l, (str, int, float)) for l in labels):
        raise ChartError('"labels" must be text or numbers')
    out = []
    for i, s in enumerate(series, start=1):
        values = s.get("values")
        if not isinstance(values, list):
            raise ChartError(f'series {i} needs "values": a list of numbers')
        if len(values) != len(labels):
            raise ChartError(f"series {i} has {len(values)} values but there are {len(labels)} labels")
        if not all(_num(v) for v in values):
            raise ChartError(f'series {i} has a value that is not a number (numbers only: not text like '
                             f'"12%", true/false or null)')
        out.append({"name": _text(s.get("name") or f"Series {i}"), "values": [float(v) for v in values]})

    if kind in ("pie", "donut"):
        if len(out) != 1:
            raise ChartError(f"a {kind} chart takes exactly one series")
        if len(labels) > MAX_PIE:
            raise ChartError(f"a {kind} chart can have at most {MAX_PIE} slices")
        if any(v < 0 for v in out[0]["values"]):
            raise ChartError(f"a {kind} chart cannot have negative values")
        if sum(out[0]["values"]) <= 0:
            raise ChartError(f"the {kind} values add up to zero")

    spec["labels"] = [_text(l, 30) for l in labels]
    spec["series"] = out
    return spec


# ------------------------------------------------------------------ drawing

def _plt():
    """matplotlib, imported on first use (it is slow to import) and set to draw off-screen."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        raise ChartError("charts need matplotlib (pip install matplotlib)")
    return plt


def _draw(ax, spec: dict, fs: float = 1.0) -> None:
    """Draw one chart onto an existing axes. fs scales the fonts (dashboard panels use less than 1)."""
    kind, series = spec["type"], spec["series"]
    n = len(series)

    def colour(i: int) -> str:
        return PALETTE[i % len(PALETTE)]

    if kind in ("pie", "donut"):
        values = series[0]["values"]
        ax.pie(values, labels=spec["labels"], autopct="%1.1f%%", startangle=90,
               colors=[colour(i) for i in range(len(values))], textprops={"fontsize": 10 * fs},
               # a donut is a pie with a hole: wedge width < 1 leaves the middle empty
               wedgeprops={"width": 0.45, "edgecolor": "white"} if kind == "donut" else None,
               pctdistance=0.78 if kind == "donut" else 0.6)
        ax.axis("equal")
    elif kind == "scatter":
        for i, s in enumerate(series):
            ax.scatter([p[0] for p in s["points"]], [p[1] for p in s["points"]],
                       label=s["name"], color=colour(i), s=28 * fs, alpha=0.85)
    else:
        labels = spec["labels"]
        x = list(range(len(labels)))
        if kind == "bar":
            width = 0.8 / n
            for i, s in enumerate(series):
                offset = (i - (n - 1) / 2) * width              # side by side, centred on each label
                ax.bar([v + offset for v in x], s["values"], width, label=s["name"], color=colour(i))
        elif kind == "hbar":                                     # horizontal: labels on the y axis, first label on top
            height = 0.8 / n
            for i, s in enumerate(series):
                offset = (i - (n - 1) / 2) * height
                ax.barh([v + offset for v in x], s["values"], height, label=s["name"], color=colour(i))
            ax.set_yticks(x)
            ax.set_yticklabels(spec["labels"], fontsize=10 * fs)
            ax.invert_yaxis()
        elif kind == "stacked":                                  # each series sits on top of the previous ones
            bottom = [0.0] * len(x)
            for i, s in enumerate(series):
                ax.bar(x, s["values"], 0.6, bottom=bottom, label=s["name"], color=colour(i))
                bottom = [b + v for b, v in zip(bottom, s["values"])]
        elif kind == "line":
            for i, s in enumerate(series):
                ax.plot(x, s["values"], label=s["name"], color=colour(i), linewidth=2,
                        marker="o" if len(x) <= 30 else None)
        else:                                                    # area: stacked
            ax.stackplot(x, *[s["values"] for s in series], labels=[s["name"] for s in series],
                         colors=[colour(i) for i in range(n)], alpha=0.85)
        if kind != "hbar":
            step = max(1, len(labels) // 20)                     # never draw more than ~20 tick labels
            ticks = x[::step]
            rotate = len(labels) > 8 or max(len(l) for l in labels) > 8
            ax.set_xticks(ticks)
            ax.set_xticklabels([labels[i] for i in ticks], rotation=30 if rotate else 0,
                               ha="right" if rotate else "center", fontsize=10 * fs)

    if kind not in ("pie", "donut"):
        ax.set_xlabel(spec["x_label"], fontsize=10 * fs)
        ax.set_ylabel(spec["y_label"], fontsize=10 * fs)
        ax.tick_params(labelsize=10 * fs)
        ax.grid(axis="x" if kind == "hbar" else "y", alpha=0.3)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        if n > 1:
            ax.legend(frameon=False, fontsize=9 * fs)
    if spec["title"]:
        ax.set_title(spec["title"], fontsize=13 * fs)


def _plot(plt, spec: dict):
    fig, ax = plt.subplots(figsize=(8, 5), dpi=100)          # 800 x 500 pixels
    _draw(ax, spec)
    fig.tight_layout()
    return fig


def _draw_table(ax, spec: dict, fs: float = 1.0, top: bool = False) -> None:
    """Draw a table onto an existing axes. top=True pins it to the top (dashboard panels)."""
    rows, columns = spec["rows"], spec["columns"]
    ax.axis("off")
    table = ax.table(cellText=rows, colLabels=columns, loc="upper center" if top else "center", cellLoc="left")
    table.auto_set_font_size(False)
    table.set_fontsize(10 * fs)
    table.scale(1, 1.4)
    table.auto_set_column_width(list(range(len(columns))))
    for (r, _), cell in table.get_celld().items():
        cell.set_edgecolor("#cccccc")
        if r == 0:                                               # header row
            cell.set_facecolor(PALETTE[0])
            cell.set_text_props(color="white", weight="bold")
        elif r % 2 == 0:
            cell.set_facecolor("#f2f2f2")
    if spec["title"]:
        ax.set_title(spec["title"], fontsize=13 * fs)


def _table(plt, spec: dict):
    fig, ax = plt.subplots(figsize=(8, min(7.0, 1.0 + 0.38 * (len(spec["rows"]) + 1))), dpi=100)
    _draw_table(ax, spec)
    fig.tight_layout()
    return fig


def render(spec: dict) -> bytes:
    """A validated spec as PNG bytes."""
    plt = _plt()
    try:
        fig = _table(plt, spec) if spec["type"] == "table" else _plot(plt, spec)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor="white")
        return buf.getvalue()
    except ChartError:
        raise
    except Exception as e:
        raise ChartError(f"could not draw the chart ({type(e).__name__})")
    finally:
        plt.close("all")                  # never leak figures between requests