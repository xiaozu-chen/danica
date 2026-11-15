"""
chart.py - The chart tool: draw a PNG from a JSON data file the model wrote.

The model never writes plotting code or HTML. It writes DATA (with write_file), this tool
validates and draws it with the templates in charts.py, and the answer then shows the image
with <present file="..."/> (handled by the CLI).
"""

import json

from danica.charts import MAX_SPEC_BYTES, ChartError, render, validate
from danica.secret_scan import is_sensitive_path
from danica.tools.files import _write_guard
from danica.tools.registry import tool
from danica.workspace import Workspace


@tool(
    "chart",
    "Draw a chart or table as a PNG image from a JSON data file you wrote with write_file "
    "(for example charts/sales.json). The image is saved next to it (charts/sales.png). "
    'Data formats. bar, hbar (horizontal bars), stacked (stacked bars), line, area (stacked) or pie / donut: {"type": "bar", "title": "...", "x_label": "...", '
    '"y_label": "...", "labels": ["Jan", "Feb"], "series": [{"name": "Sales", "values": [10, 20]}]} '
    "(a pie or donut has exactly one series). "
    'scatter: {"type": "scatter", "series": [{"name": "A", "points": [[1, 2], [3, 4]]}]}. '
    'table: {"type": "table", "columns": ["Name", "Qty"], "rows": [["apple", 3]]}. '
    "Values must be plain numbers. Never write plotting code or HTML yourself.",
    {
        "file": {"type": "string", "description": "Path of the JSON data file, for example charts/sales.json"},
    },
)
def chart(ws: Workspace, file: str) -> str:
    p = ws.resolve(file)
    if p.suffix.lower() != ".json":
        return "ERROR: the data file must end in .json (write it with write_file first)"
    if not p.is_file():
        return f"ERROR: '{file}' is not a file. Write the JSON data file with write_file first."
    if is_sensitive_path(ws.rel(p)):
        return "ERROR: that file is blocked: it looks like a credentials file"
    if p.stat().st_size > MAX_SPEC_BYTES:
        return f"ERROR: the data file is too big (limit {MAX_SPEC_BYTES // 1000} KB)"

    out = p.with_suffix(".png")           # the picture goes right next to its data
    guarded = _write_guard(ws, out)       # never into .git, .husky or .githooks
    if guarded:
        return guarded

    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except (ValueError, UnicodeError, RecursionError) as e:
        return f"ERROR: the data file is not valid JSON ({type(e).__name__}: {str(e)[:100]})"
    try:
        png = render(validate(data))
    except ChartError as e:
        return f"ERROR: {e}"

    out.write_bytes(png)
    shown = ws.display(out)
    return (f'OUTPUT: saved {shown} ({max(1, len(png) // 1024)} KB). It is finished: do NOT call this tool again for this file. To show it in chat, write '
            f'<present file="{shown}"/> on its own line in your reply, then add one short sentence and stop. To put it in a document, '
            f'write ![caption]({shown}) in the Markdown file instead.')