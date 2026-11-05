"""
diagram_test.py - Checks diagram validation, the layout steps (layers, cycles, ordering,
spacing), edge geometry, drawing, and the diagram tool. No model needed.
Run from the project root:  python -m scripts.diagram_test
"""

import io
import tempfile
from pathlib import Path

from PIL import Image

from danica.charts import ChartError
from danica.diagrams import (assign_layers, boundary, node_size, order_layers, place, render, validate)
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


def mk(nodes: list[str], edges: list[tuple[str, str]], **extra) -> dict:
    return validate({"nodes": [{"id": n} for n in nodes],
                     "edges": [{"from": a, "to": b} for a, b in edges], **extra})


PNG = b"\x89PNG\r\n\x1a\n"

# --- validation ---
good = validate({"nodes": [{"id": "a"}, {"id": "b", "label": "B $5\nline"}], "edges": [{"from": "a", "to": "b"}]})
check("defaults: direction, shape, colour, label = id",
      good["direction"] == "down" and good["nodes"][0] == {"id": "a", "label": "a", "shape": "box", "color": "blue"})
check("labels are cleaned and $ is escaped", good["nodes"][1]["label"] == "B \\$5 line")
check("not an object", "one JSON object" in error_of([1]))
check("only flowcharts", "flowchart" in error_of({"type": "gantt", "nodes": [{"id": "a"}]}))
check("direction is checked", "direction" in error_of({"direction": "up", "nodes": [{"id": "a"}]}))
check("nodes are required", '"nodes"' in error_of({}))
check("too many nodes", "1 to 40" in error_of({"nodes": [{"id": f"n{i}"} for i in range(41)]}))
check("an id with spaces is refused", '"id"' in error_of({"nodes": [{"id": "a b"}]}))
check("a duplicate id is refused", "two nodes" in error_of({"nodes": [{"id": "a"}, {"id": "a"}]}))
check("a bad shape is refused", "shape" in error_of({"nodes": [{"id": "a", "shape": "star"}]}))
check("a bad colour is refused", "color" in error_of({"nodes": [{"id": "a", "color": "#ff0000"}]}))
check("an edge to a missing node is refused",
      "no node with that id" in error_of({"nodes": [{"id": "a"}], "edges": [{"from": "a", "to": "z"}]}))
check("a self-loop is refused", "itself" in error_of({"nodes": [{"id": "a"}], "edges": [{"from": "a", "to": "a"}]}))
check("too many edges", "at most 80" in error_of({"nodes": [{"id": "a"}, {"id": "b"}], "edges": [{"from": "a", "to": "b"}] * 81}))
check("repeated edges are merged",
      len(validate({"nodes": [{"id": "a"}, {"id": "b"}], "edges": [{"from": "a", "to": "b"}] * 3})["edges"]) == 1)

# --- layers and cycles ---
layer, back = assign_layers(["a", "b", "c"], [("a", "b"), ("b", "c")])
check("a chain gets layers 0, 1, 2", layer == {"a": 0, "b": 1, "c": 2} and not back)
layer, _ = assign_layers(list("abcd"), [("a", "b"), ("a", "c"), ("b", "d"), ("c", "d")])
check("a diamond: the join is below both branches", layer == {"a": 0, "b": 1, "c": 1, "d": 2})
layer, _ = assign_layers(list("abc"), [("a", "b"), ("b", "c"), ("a", "c")])
check("a skipping edge does not pull the target up", layer["c"] == 2)
layer, back = assign_layers(list("abc"), [("a", "b"), ("b", "c"), ("c", "a")])
check("a cycle is broken with one back edge", back == {("c", "a")} and layer == {"a": 0, "b": 1, "c": 2})
layer, _ = assign_layers(list("abx"), [("a", "b")])
check("an isolated node sits on the first layer", layer["x"] == 0)

# --- ordering ---
rows = order_layers(list("abcd"), {"a": 0, "b": 0, "c": 1, "d": 1}, [("b", "c"), ("a", "d")])
check("nodes are ordered under their parents to avoid a crossing", rows == [["a", "b"], ["d", "c"]])
rows = order_layers(list("abcdef"), {"a": 0, "b": 1, "c": 1, "d": 2, "e": 2, "f": 2},
                    [("a", "b"), ("a", "c"), ("b", "d"), ("b", "e"), ("c", "f")])
check("every node appears exactly once", sorted(n for r in rows for n in r) == list("abcdef"))

# --- positions ---
spec = mk(list("abcd"), [("a", "b"), ("a", "c"), ("b", "d"), ("c", "d")])
pos, sizes, layer, back = place(spec)
check("nodes in the same layer do not overlap",
      all(abs(pos[p][0] - pos[q][0]) >= (sizes[p][1] + sizes[q][1]) / 2 for p, q in [("b", "c")]))
check("flow goes down", pos["a"][1] > pos["b"][1] > pos["d"][1])
pos, *_ = place(mk(list("abc"), [("a", "b"), ("b", "c")], direction="right"))
check("direction right: flow goes right", pos["a"][0] < pos["b"][0] < pos["c"][0])
lines, w, h = node_size({"label": "x " * 60, "shape": "box"})
check("a long label wraps into at most 4 lines", len(lines) <= 4 and w <= 3.4)
_, dw, dh = node_size({"label": "ok?", "shape": "diamond"})
_, bw, bh = node_size({"label": "ok?", "shape": "box"})
check("a diamond is bigger than a box with the same label", dw > bw and dh > bh)

# --- where an arrow leaves a node ---
check("box: leaving to the right", boundary((0, 0), 2, 1, "box", (10, 0)) == (1, 0))
check("box: leaving upward", boundary((0, 0), 2, 1, "box", (0, 10)) == (0, 0.5))
check("box: leaving diagonally", boundary((0, 0), 2, 1, "box", (10, 10)) == (0.5, 0.5))
check("oval: leaving to the right", abs(boundary((0, 0), 2, 1, "oval", (10, 0))[0] - 1) < 1e-9)
check("diamond: leaving upward", abs(boundary((0, 0), 2, 1, "diamond", (0, 10))[1] - 0.5) < 1e-9)
check("a point on the centre stays on the centre", boundary((3, 4), 2, 1, "box", (3, 4)) == (3, 4))

# --- drawing ---
def drawn(spec: dict) -> Image.Image:
    png = render(spec)
    assert png.startswith(PNG)
    return Image.open(io.BytesIO(png))


img = drawn(mk(list("abcd"), [("a", "b"), ("a", "c"), ("b", "d"), ("c", "d")], title="Cost in $ and $"))
check("a normal diagram draws", 300 <= img.width <= 1400 and 150 <= img.height <= 1000)
shapes = validate({"nodes": [{"id": "s", "label": "Start", "shape": "oval", "color": "green"},
                             {"id": "d", "label": "Is it ready?", "shape": "diamond", "color": "orange"},
                             {"id": "r", "label": "Rounded", "shape": "round", "color": "purple"},
                             {"id": "x", "label": "Box", "color": "red"}, {"id": "y", "label": "Grey", "color": "gray"}],
                   "edges": [{"from": "s", "to": "d"}, {"from": "d", "to": "r", "label": "yes"},
                             {"from": "d", "to": "x", "label": "no"}, {"from": "r", "to": "y"}, {"from": "x", "to": "y"}]})
check("every shape and colour draws, with edge labels", drawn(shapes).width > 300)
drawn(mk(list("abc"), [("a", "b"), ("b", "c"), ("c", "a")]))
check("a cycle draws", True)
drawn(mk(list("abc"), [("a", "b"), ("b", "a"), ("a", "c"), ("b", "c")]))
check("opposite arrows and skipped layers draw", True)
check("direction right draws", drawn(mk(list("abc"), [("a", "b"), ("b", "c")], direction="right")).width > drawn(
    mk(list("abc"), [("a", "b"), ("b", "c")])).width)
big = drawn(mk([f"n{i}" for i in range(40)], [(f"n{i}", f"n{i + 1}") for i in range(39)]))
check("a 40-node chain still draws within the size cap", big.width <= 1400 and big.height <= 1000)

# --- the tool ---
check("diagram is a free WRITE-tier tool", classify("diagram", {})[0] == Tier.WRITE)
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    ws = Workspace(Path(tmp) / "ws")
    (ws.root / "diagrams").mkdir()
    (ws.root / "diagrams" / "flow.json").write_text(
        '{"nodes": [{"id": "a", "label": "Start"}, {"id": "b", "label": "End"}], "edges": [{"from": "a", "to": "b"}]}',
        encoding="utf-8")
    out = run_tool("diagram", {"file": "diagrams/flow.json"}, ws)
    check("the tool saves a PNG next to the data",
          out.startswith("OK: saved diagrams/flow.png") and (ws.root / "diagrams" / "flow.png").read_bytes().startswith(PNG))
    check("the result explains both ways to show it", '<present file="diagrams/flow.png"/>' in out and "![caption]" in out)
    check("a non-json file is refused", run_tool("diagram", {"file": "diagrams/flow.png"}, ws).startswith("ERROR"))
    check("a missing file says to write it first", "write_file" in run_tool("diagram", {"file": "diagrams/no.json"}, ws))
    check("a path outside the workspace is refused", "outside the workspace" in run_tool("diagram", {"file": "..\\x.json"}, ws))
    (ws.root / "diagrams" / "bad.json").write_text('{"nodes": [{"id": "a"}], "edges": [{"from": "a", "to": "z"}]}', encoding="utf-8")
    check("a bad spec gives the fix-it message", "no node with that id" in run_tool("diagram", {"file": "diagrams/bad.json"}, ws))
    (ws.root / "diagrams" / "broken.json").write_text("{nope", encoding="utf-8")
    check("invalid JSON is explained", "not valid JSON" in run_tool("diagram", {"file": "diagrams/broken.json"}, ws))
    (ws.root / "credentials.json").write_text("{}", encoding="utf-8")
    check("credential files are blocked", "blocked" in run_tool("diagram", {"file": "credentials.json"}, ws))
    (ws.root / ".husky").mkdir()
    (ws.root / ".husky" / "d.json").write_text("{}", encoding="utf-8")
    check("hook folders are never written to", "blocked" in run_tool("diagram", {"file": ".husky/d.json"}, ws)
          and not (ws.root / ".husky" / "d.png").exists())

print("\nAll good.")