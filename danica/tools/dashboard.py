"""
dashboard.py - The dashboard tool: draw a one-page dashboard PNG from a JSON file the model wrote.
Same shape as the chart tool: the model writes DATA, this tool validates and draws it.
"""

import json

from danica.charts import MAX_SPEC_BYTES, ChartError
from danica.dashboard import render, validate
from danica.secret_scan import is_sensitive_path
from danica.tools.files import _write_guard
from danica.tools.registry import tool
from danica.workspace import Workspace


@tool(
    "dashboard",
    "Draw a one-page dashboard (big-number tiles plus several charts and tables) as ONE PNG image from a "
    "JSON file you wrote with write_file (for example dashboards/q3.json). Format: "
    '{"title": "...", "columns": 2, "kpis": [{"label": "Revenue", "value": "$1.2M", "note": "+4% vs last '
    'quarter"}], "panels": [ ...up to 6 charts or tables, each written EXACTLY like a chart tool file '
    '(type, title, labels, series / columns, rows)... ]}. Up to 4 kpis, up to 6 panels, tables at most 10 rows. '
    "The image is saved next to the file. Never write plotting code yourself.",
    {
        "file": {"type": "string", "description": "Path of the JSON file, for example dashboards/q3.json"},
    },
)
def dashboard(ws: Workspace, file: str) -> str:
    p = ws.resolve(file)
    if p.suffix.lower() != ".json":
        return "ERROR: the file must end in .json (write it with write_file first)"
    if not p.is_file():
        return f"ERROR: '{file}' is not a file. Write the JSON file with write_file first."
    if is_sensitive_path(ws.rel(p)):
        return "ERROR: that file is blocked: it looks like a credentials file"
    if p.stat().st_size > MAX_SPEC_BYTES:
        return f"ERROR: the file is too big (limit {MAX_SPEC_BYTES // 1000} KB)"

    out = p.with_suffix(".png")
    guarded = _write_guard(ws, out)           # never into .git, .husky or .githooks
    if guarded:
        return guarded
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except (ValueError, UnicodeError, RecursionError) as e:
        return f"ERROR: the file is not valid JSON ({type(e).__name__}: {str(e)[:100]})"
    try:
        png = render(validate(data))
    except ChartError as e:
        return f"ERROR: {e}"

    out.write_bytes(png)
    shown = ws.display(out)
    return (f'OK: saved {shown} ({max(1, len(png) // 1024)} KB). To show it in chat, write '
            f'<present file="{shown}"/> on its own line in your answer. To put it in a document or '
            f'a deck, write ![caption]({shown}) in the Markdown file instead.')