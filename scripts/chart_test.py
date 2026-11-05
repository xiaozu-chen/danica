"""
chart_test.py - Checks spec validation, drawing every chart type, and the chart tool.
No model needed, but matplotlib and Pillow must be installed (the first run can take a
while: matplotlib builds its font cache).
Run from the project root:  python -m scripts.chart_test
"""

import io
import tempfile
from pathlib import Path

from PIL import Image

import danica.tools.chart as chart_tool
from danica.charts import ChartError, _text, render, validate
from danica.permissions import Tier, classify
from danica.tools import run_tool
from danica.workspace import Workspace


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    assert condition, label


def error_of(spec) -> str:
    """The validation error message ('' if the spec is fine)."""
    try:
        validate(spec)
        return ""
    except ChartError as e:
        return str(e)


PNG = b"\x89PNG\r\n\x1a\n"
BAR = {"type": "bar", "title": "Sales", "labels": ["Jan", "Feb"], "series": [{"name": "2026", "values": [10, 20]}]}

# --- text cleaning ---
check("text is one line, clean, and $ is escaped", _text("a\n b\u200b $5") == "a b \\$5")
check("text is cut to length", len(_text("x" * 100, 10)) == 10)

# --- validation ---
check("a good spec validates", validate(BAR)["type"] == "bar")
check("a missing series name gets a default", validate({**BAR, "series": [{"values": [1, 2]}]})["series"][0]["name"] == "Series 1")
check("not an object", "one JSON object" in error_of([1]))
check("unknown type lists the choices", "must be one of" in error_of({"type": "donut"}))
check("labels are required", '"labels"' in error_of({"type": "bar", "series": [{"values": [1]}]}))
check("labels must be text or numbers", '"labels" must be' in error_of({**BAR, "labels": [True, "b"]}))
check("series are required", "1 to 8" in error_of({"type": "bar", "labels": ["a"]}))
check("too many series", "1 to 8" in error_of({**BAR, "series": [{"values": [1, 2]}] * 9}))
check("value count must match label count",
      "3 values but there are 2 labels" in error_of({**BAR, "series": [{"values": [1, 2, 3]}]}))
for bad in ["12%", True, None, float("nan"), float("inf"), 10 ** 400, [1]]:
    check(f"value {str(bad)[:12]!r} is refused",
          "not a number" in error_of({**BAR, "series": [{"values": [1, bad]}]}))

PIE = {"type": "pie", "labels": ["A", "B", "C"], "series": [{"values": [5, 3, 2]}]}
check("a good pie validates", validate(PIE)["type"] == "pie")
check("a pie takes one series", "exactly one series" in error_of({**PIE, "series": PIE["series"] * 2}))
check("a pie has at most 12 slices",
      "at most 12" in error_of({"type": "pie", "labels": list("abcdefghijklm"), "series": [{"values": [1] * 13}]}))
check("a pie refuses negatives", "negative" in error_of({**PIE, "series": [{"values": [5, -1, 2]}]}))
check("a pie that adds to zero is refused", "add up to zero" in error_of({**PIE, "series": [{"values": [0, 0, 0]}]}))

check("a scatter point must be [number, number]",
      "[number, number]" in error_of({"type": "scatter", "series": [{"points": [[1, "x"]]}]}))
check("a scatter series needs points", "points" in error_of({"type": "scatter", "series": [{}]}))
check("too many scatter points",
      "too many points" in error_of({"type": "scatter", "series": [{"points": [[1, 1]] * 1500}, {"points": [[1, 1]] * 600}]}))
check("a table needs columns", '"columns"' in error_of({"type": "table", "rows": [[1]]}))
check("a ragged table row names the row",
      "row 2" in error_of({"type": "table", "columns": ["a", "b"], "rows": [[1, 2], [3]]}))

# --- drawing every type ---
SPECS = {
    "bar": {**BAR, "series": [{"name": "a", "values": [1, 2]}, {"name": "b", "values": [2, 1]}]},
    "line": {**BAR, "type": "line"},
    "area": {**BAR, "type": "area", "series": [{"values": [1, 2]}, {"values": [2, 1]}]},
    "pie": PIE,
    "scatter": {"type": "scatter", "title": "Cost in $ and $", "series": [
        {"name": "a", "points": [[1, 2], [2, 3], [3, 5]]}, {"name": "b", "points": [[1, 1], [2, 1.5]]}]},
    "table": {"type": "table", "title": "Stock", "columns": ["Name", "Price"], "rows": [["apple", "$5"], ["pear", 3.5]]},
}
for kind, spec in SPECS.items():
    png = render(validate(spec))
    img = Image.open(io.BytesIO(png))
    check(f"{kind} draws a real PNG", png.startswith(PNG) and img.width == 800 and img.height >= 150)

many = {"type": "bar", "labels": [f"label number {i}" for i in range(60)], "series": [{"values": list(range(60))}]}
check("60 long labels still draw", render(validate(many)).startswith(PNG))

# --- the tool ---
check("chart is a free WRITE-tier tool", classify("chart", {})[0] == Tier.WRITE)

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    ws = Workspace(Path(tmp) / "ws")
    (ws.root / "charts").mkdir()
    (ws.root / "charts" / "sales.json").write_text('{"type": "bar", "labels": ["a", "b"], "series": [{"values": [1, 2]}]}',
                                                   encoding="utf-8")
    out = run_tool("chart", {"file": "charts/sales.json"}, ws)
    check("the tool saves a PNG next to the data", out.startswith("OK: saved charts/sales.png")
          and (ws.root / "charts" / "sales.png").read_bytes().startswith(PNG))
    check("the result tells the model how to show it", '<present file="charts/sales.png"/>' in out)

    check("a non-json file is refused", run_tool("chart", {"file": "charts/sales.png"}, ws).startswith("ERROR"))
    check("a missing file says to write it first", "write_file" in run_tool("chart", {"file": "charts/nope.json"}, ws))
    check("a path outside the workspace is refused", "outside the workspace" in run_tool("chart", {"file": "..\\x.json"}, ws))

    (ws.root / "charts" / "bad.json").write_text("{not json", encoding="utf-8")
    check("invalid JSON is explained", "not valid JSON" in run_tool("chart", {"file": "charts/bad.json"}, ws))
    (ws.root / "charts" / "wrong.json").write_text('{"type": "bar", "labels": ["a"], "series": [{"values": ["x"]}]}', encoding="utf-8")
    check("a bad spec gives the fix-it message", "not a number" in run_tool("chart", {"file": "charts/wrong.json"}, ws))
    (ws.root / "credentials.json").write_text("{}", encoding="utf-8")
    check("credential files are blocked", "blocked" in run_tool("chart", {"file": "credentials.json"}, ws))
    (ws.root / ".husky").mkdir()
    (ws.root / ".husky" / "c.json").write_text("{}", encoding="utf-8")
    check("hook folders are never written to", "blocked" in run_tool("chart", {"file": ".husky/c.json"}, ws)
          and not (ws.root / ".husky" / "c.png").exists())

    real_limit = chart_tool.MAX_SPEC_BYTES
    chart_tool.MAX_SPEC_BYTES = 10
    try:
        check("an oversized data file is refused", "too big" in run_tool("chart", {"file": "charts/sales.json"}, ws))
    finally:
        chart_tool.MAX_SPEC_BYTES = real_limit

print("\nAll good.")