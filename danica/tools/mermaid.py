"""
mermaid.py - The mermaid tool: draw a diagram from Mermaid text the model wrote.
For the types the JSON diagram tool can't do (sequence, class, state, ER, gantt, ...).
The text is filtered by validate_mermaid before anything runs (see diagram_engines.py).
"""

from danica.charts import ChartError
from danica.diagram_engines import MAX_MERMAID_CHARS, mermaid_png
from danica.secret_scan import is_sensitive_path
from danica.tools.files import _write_guard
from danica.tools.registry import tool
from danica.workspace import Workspace


@tool(
    "mermaid",
    "Draw a diagram as a PNG image from Mermaid text you wrote with write_file (for example "
    "diagrams/login.mmd). Use it for sequenceDiagram, classDiagram, stateDiagram-v2, erDiagram, gantt, "
    "pie, journey, mindmap and timeline diagrams (for ordinary flowcharts use the diagram tool). The "
    "first line must be the diagram type. No HTML tags except <br/>, and no click, link or %%{init} "
    "lines. The image is saved next to the file.",
    {
        "file": {"type": "string", "description": "Path of the Mermaid file, for example diagrams/login.mmd"},
    },
)
def mermaid(ws: Workspace, file: str) -> str:
    p = ws.resolve(file)
    if p.suffix.lower() not in (".mmd", ".mermaid"):
        return "ERROR: the file must end in .mmd (write the Mermaid text with write_file first)"
    if not p.is_file():
        return f"ERROR: '{file}' is not a file. Write the Mermaid text with write_file first."
    if is_sensitive_path(ws.rel(p)):
        return "ERROR: that file is blocked: it looks like a credentials file"
    if p.stat().st_size > MAX_MERMAID_CHARS * 4:
        return f"ERROR: the file is too big (limit {MAX_MERMAID_CHARS:,} characters)"

    out = p.with_suffix(".png")
    guarded = _write_guard(ws, out)
    if guarded:
        return guarded
    try:
        png = mermaid_png(p.read_text(encoding="utf-8-sig", errors="replace"))
    except ChartError as e:                   # DiagramError is the same class
        return f"ERROR: {e}"

    out.write_bytes(png)
    shown = ws.display(out)
    return (f'OK: saved {shown} ({max(1, len(png) // 1024)} KB). To show it in chat, write '
            f'<present file="{shown}"/> on its own line in your answer. To put it in a document or '
            f'a deck, write ![caption]({shown}) in the Markdown file instead.')