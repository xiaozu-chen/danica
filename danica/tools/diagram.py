"""
diagram.py - The diagram tool: draw a flowchart PNG from a JSON file the model wrote.
Same shape as the chart tool: the model writes DATA (nodes and edges), never drawing code.
"""

import json

from danica.charts import MAX_SPEC_BYTES, ChartError
from danica.diagrams import render, validate
from danica.secret_scan import is_sensitive_path
from danica.tools.files import _write_guard
from danica.tools.registry import tool
from danica.workspace import Workspace


@tool(
    "diagram",
    "Draw a flowchart or boxes-and-arrows diagram as a PNG image from a JSON file you wrote with "
    "write_file (for example diagrams/flow.json). The image is saved next to it. Format: "
    '{"title": "...", "direction": "down" or "right", "nodes": [{"id": "a", "label": "Start", '
    '"shape": "box|round|diamond|oval", "color": "blue|green|orange|red|purple|gray"}], '
    '"edges": [{"from": "a", "to": "b", "label": "yes"}]}. '
    "Ids are short letters or digits; the label is the text shown. Up to 40 nodes. Use a diamond for "
    "a yes/no decision. Never write Mermaid, Graphviz or plotting code yourself.",
    {
        "file": {"type": "string", "description": "Path of the JSON file, for example diagrams/flow.json"},
    },
)
def diagram(ws: Workspace, file: str) -> str:
    p = ws.resolve(file)
    if p.suffix.lower() != ".json":
        return "ERROR: the file must end in .json (write it with write_file first)"
    if not p.is_file():
        return f"ERROR: '{file}' is not a file. Write the JSON file with write_file first."
    if is_sensitive_path(p.relative_to(ws.root)):
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
    except ChartError as e:                   # DiagramError is the same class
        return f"ERROR: {e}"

    out.write_bytes(png)
    shown = ws.display(out)
    return (f'OK: saved {shown} ({max(1, len(png) // 1024)} KB). To show it in chat, write '
            f'<present file="{shown}"/> on its own line in your answer. To put it in a document or '
            f'a deck, write ![caption]({shown}) in the Markdown file instead.')