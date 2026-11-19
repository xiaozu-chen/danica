"""
dashboard.py - One-page dashboards: big-number tiles plus several charts and tables, as ONE PNG.

Spec: {"title": "...", "columns": 1-3 (default 2), "kpis": [{"label", "value", "note"}]  (up to 4),
       "panels": [<a chart spec exactly as for the chart tool>, ...]  (up to 6)}
Every panel goes through charts.validate, so the same rules apply (types, sizes, text cleaning).
HONEST LIMITS: a static picture (there is no interactive HTML dashboard); at most 6 panels;
tables hold at most 10 rows.
"""

import io
from typing import Any

from danica.charts import ChartError, _draw, _draw_table, _plt, _text
from danica.charts import validate as validate_chart

MAX_PANELS, MAX_KPIS, MAX_PANEL_ROWS = 6, 4, 10
FONT_SCALE = {1: 1.0, 2: 0.85, 3: 0.7}          # panel fonts shrink as columns get narrower
WIDTH_IN, KPI_H, PANEL_H = 12.0, 1.4, 4.2       # inches: the picture is 1200 pixels wide


def validate(data: dict[str, Any]) -> dict[str, Any]:
    """Check a dashboard spec and return a cleaned copy. Raises ChartError with a fix-it message."""
    if not isinstance(data, dict):
        raise ChartError('the file must hold one JSON object, like {"title": "...", "panels": [...]}')
    panels = data.get("panels")
    if not isinstance(panels, list) or not 1 <= len(panels) <= MAX_PANELS:
        raise ChartError(f'"panels" must be a list of 1 to {MAX_PANELS} charts or tables')
    clean_panels = []
    for i, panel in enumerate(panels, start=1):
        try:
            spec = validate_chart(panel)
        except ChartError as e:
            raise ChartError(f"panel {i}: {e}")
        if spec["type"] == "table" and len(spec["rows"]) > MAX_PANEL_ROWS:
            raise ChartError(f"panel {i}: a table in a dashboard holds at most {MAX_PANEL_ROWS} rows")
        clean_panels.append(spec)

    raw = data.get("kpis", [])
    if not isinstance(raw, list) or len(raw) > MAX_KPIS:
        raise ChartError(f'"kpis" must be a list of at most {MAX_KPIS} tiles like '
                         f'{{"label": "Revenue", "value": "$1.2M", "note": "+4%"}}')
    kpis = []
    for i, k in enumerate(raw, start=1):
        if not isinstance(k, dict) or k.get("value") in (None, ""):
            raise ChartError(f'tile {i} needs a "value" (the big number or text)')
        kpis.append({"label": _text(k.get("label", ""), 30), "value": _text(k["value"], 20),
                     "note": _text(k.get("note", ""), 30)})

    columns = data.get("columns")
    if columns is None:
        columns = 2 if len(clean_panels) > 1 else 1
    if (
        isinstance(columns, bool)
        or not isinstance(columns, int)
        or columns not in (1, 2, 3)
    ):
        raise ChartError('"columns" must be 1, 2 or 3')
    return {"title": _text(data.get("title", ""), 120), "columns": columns, "kpis": kpis, "panels": clean_panels}


def _tile(ax, k: dict) -> None:
    """One big-number tile."""
    from matplotlib.patches import Rectangle
    ax.axis("off")
    ax.add_patch(Rectangle((0, 0), 1, 1, transform=ax.transAxes, fc="#F4F7FA", ec="#CCD6E0", lw=1.2))
    ax.text(0.5, 0.74, k["label"], ha="center", va="center", fontsize=11, color="#666666", transform=ax.transAxes)
    ax.text(0.5, 0.42, k["value"], ha="center", va="center", fontsize=24, weight="bold", color="#1F3A5F",
            transform=ax.transAxes)
    if k["note"]:
        colour = "#009E73" if k["note"].startswith("+") else "#D55E00" if k["note"].startswith("-") else "#777777"
        ax.text(0.5, 0.14, k["note"], ha="center", va="center", fontsize=10, color=colour, transform=ax.transAxes)


def render(spec: dict) -> bytes:
    """A validated dashboard spec as PNG bytes."""
    plt = _plt()
    try:
        panels, kpis, cols = spec["panels"], spec["kpis"], spec["columns"]
        rows = -(-len(panels) // cols)                           # ceiling division
        title_h = 0.8 if spec["title"] else 0.2
        total = title_h + (KPI_H if kpis else 0.0) + rows * PANEL_H + 0.3
        fig = plt.figure(figsize=(WIDTH_IN, total), dpi=100)

        if spec["title"]:
            fig.text(0.5, 1 - 0.45 / total, spec["title"], ha="center", va="center", fontsize=20, weight="bold")
        y = title_h
        if kpis:
            grid = fig.add_gridspec(
                1,
                len(kpis),
                left=0.04,
                right=0.96,
                top=1 - y / total,
                bottom=1 - (y + KPI_H - 0.25) / total,
                wspace=0.12,
            )
            for i, k in enumerate(kpis):
                _tile(fig.add_subplot(grid[0, i]), k)
            y += KPI_H

        grid = fig.add_gridspec(rows, cols, left=0.06, right=0.97, top=1 - y / total,
                                bottom=0.35 / total, hspace=0.6, wspace=0.28)
        fs = FONT_SCALE[cols]
        for i, panel in enumerate(panels):
            ax = fig.add_subplot(grid[i // cols, i % cols])
            if panel["type"] == "table":
                _draw_table(ax, panel, fs, top=True)
            else:
                _draw(ax, panel, fs)

        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor="white")
        return buf.getvalue()
    except ChartError:
        raise
    except Exception as e:
        raise ChartError(f"could not draw the dashboard ({type(e).__name__})")
    finally:
        plt.close("all")
