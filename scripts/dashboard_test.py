"""
dashboard_test.py - Checks dashboard validation, drawing and the tool, plus a regression check
that single charts still draw at the same size after the _draw refactor.
Run from the project root:  python -m scripts.dashboard_test
"""

import io
import tempfile
from pathlib import Path

from PIL import Image

from danica.charts import ChartError, render as render_chart, validate as validate_chart
from danica.dashboard import render, validate
from danica.permissions import Tier, classify
from danica.tools import run_tool
from danica.workspace import Workspace


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    assert condition, label


def error_of(spec) -> str:
    try:
        validate(spec)
        return ""
    except ChartError as e:
        return str(e)


PNG = b"\x89PNG\r\n\x1a\n"
BAR = {"type": "bar", "title": "Sales", "labels": ["Jan", "Feb"], "series": [{"name": "2026", "values": [10, 20]}]}
PIE = {"type": "pie", "title": "Share", "labels": ["A", "B", "C"], "series": [{"values": [5, 3, 2]}]}
SCATTER = {"type": "scatter", "title": "Cost in $ and $", "series": [{"name": "a", "points": [[1, 2], [2, 3], [3, 5]]}]}
TABLE = {"type": "table", "title": "Stock", "columns": ["Name", "Price"], "rows": [["apple", "$5"], ["pear", 3.5]]}
FULL = {"title": "Q3 overview", "kpis": [{"label": "Revenue", "value": "$1.2M", "note": "+4% vs Q2"},
                                         {"label": "Churn", "value": "3.1%", "note": "-0.4%"}, {"label": "NPS", "value": 48}],
        "panels": [BAR, PIE, SCATTER, TABLE]}

# --- regression: single charts are unchanged by the refactor ---
img = Image.open(io.BytesIO(render_chart(validate_chart(BAR))))
check("a single chart is still 800 x 500", (img.width, img.height) == (800, 500))
check("a single table still draws", render_chart(validate_chart(TABLE)).startswith(PNG))

# --- validation ---
good = validate(FULL)
check("defaults: two columns for several panels", good["columns"] == 2)
check("one panel defaults to one column", validate({"panels": [BAR]})["columns"] == 1)
check("tile text is cleaned and $ is escaped", good["kpis"][0]["value"] == "\\$1.2M" and good["kpis"][2]["value"] == "48")
check("not an object", "one JSON object" in error_of([1]))
check("panels are required", '"panels"' in error_of({"title": "x"}))
check("too many panels", "1 to 6" in error_of({"panels": [BAR] * 7}))
check("a bad panel names its number", error_of({"panels": [BAR, {"type": "donut"}]}).startswith("panel 2:"))
check("a panel error keeps the chart tool's advice", "must be one of" in error_of({"panels": [{"type": "donut"}]}))
check("tables are limited to 10 rows",
      "at most 10 rows" in error_of({"panels": [{"type": "table", "columns": ["a"], "rows": [["x"]] * 11}]}))
check("too many tiles", "at most 4" in error_of({"panels": [BAR], "kpis": [{"value": 1}] * 5}))
check("a tile needs a value", 'needs a "value"' in error_of({"panels": [BAR], "kpis": [{"label": "x"}]}))
check("columns must be 1 to 3", '"columns"' in error_of({"panels": [BAR], "columns": 4})
      and '"columns"' in error_of({"panels": [BAR], "columns": True}))

# --- drawing ---
def drawn(spec: dict) -> Image.Image:
    png = render(validate(spec))
    assert png.startswith(PNG)
    return Image.open(io.BytesIO(png))


big = drawn(FULL)
check("a full dashboard is 1200 pixels wide and tall enough for two panel rows", big.width == 1200 and 900 <= big.height <= 2000)
check("a single panel draws", drawn({"panels": [BAR]}).width == 1200)
check("three columns draw", drawn({"columns": 3, "panels": [BAR, PIE, SCATTER, TABLE, BAR]}).width == 1200)
check("no title and no tiles draws", drawn({"panels": [PIE, PIE]}).width == 1200)
check("a tile with a $ draws", drawn({"panels": [BAR], "kpis": [{"label": "Cost $", "value": "$5", "note": "+$1"}]}).width == 1200)

# --- the tool ---
check("dashboard is a free WRITE-tier tool", classify("dashboard", {})[0] == Tier.WRITE)
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    ws = Workspace(Path(tmp) / "ws")
    (ws.root / "dashboards").mkdir()
    (ws.root / "dashboards" / "q3.json").write_text(__import__("json").dumps(FULL), encoding="utf-8")
    out = run_tool("dashboard", {"file": "dashboards/q3.json"}, ws)
    check("the tool saves one PNG next to the data", out.startswith("OK: saved dashboards/q3.png")
          and (ws.root / "dashboards" / "q3.png").read_bytes().startswith(PNG))
    check("the result explains how to show it", '<present file="dashboards/q3.png"/>' in out)
    check("a non-json file is refused", run_tool("dashboard", {"file": "dashboards/q3.png"}, ws).startswith("ERROR"))
    check("a missing file says to write it first", "write_file" in run_tool("dashboard", {"file": "dashboards/no.json"}, ws))
    check("a path outside the workspace is refused", "outside the workspace" in run_tool("dashboard", {"file": "..\\x.json"}, ws))
    (ws.root / "dashboards" / "bad.json").write_text('{"panels": [{"type": "bar"}]}', encoding="utf-8")
    check("a bad panel gives a fix-it message", "panel 1" in run_tool("dashboard", {"file": "dashboards/bad.json"}, ws))
    (ws.root / "dashboards" / "broken.json").write_text("{nope", encoding="utf-8")
    check("invalid JSON is explained", "not valid JSON" in run_tool("dashboard", {"file": "dashboards/broken.json"}, ws))
    (ws.root / ".husky").mkdir()
    (ws.root / ".husky" / "d.json").write_text("{}", encoding="utf-8")
    check("hook folders are never written to", "blocked" in run_tool("dashboard", {"file": ".husky/d.json"}, ws)
          and not (ws.root / ".husky" / "d.png").exists())

print("\nAll good.")