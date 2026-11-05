"""
diagrams.py - Flowcharts and boxes-and-arrows diagrams from a small JSON spec, drawn with
matplotlib. No Node.js, Mermaid or Graphviz needed.

Spec: {"type": "flowchart", "title": "...", "direction": "down" | "right",
       "nodes": [{"id": "a", "label": "Start", "shape": "box|round|diamond|oval",
                  "color": "blue|green|orange|red|purple|gray"}],
       "edges": [{"from": "a", "to": "b", "label": "yes"}]}

Layout (a small version of the classic layered method):
  1. find edges that close a cycle (back edges) and ignore them for layering
  2. layer = the longest path from a start node
  3. order the nodes inside each layer to reduce crossings (barycenter sweeps)
  4. place layers one after another, nodes side by side inside a layer
HONEST LIMITS: an edge that skips layers can still run across a node (it is curved to help);
no groups or subgraphs; many layers make the picture small.
"""

import io
import math
import re
import textwrap

from danica.charts import ChartError as DiagramError
from danica.charts import _plt, _text

SHAPES = ("box", "round", "diamond", "oval")
COLORS = {                        # name: (fill, outline)
    "blue": ("#DCEBFA", "#0072B2"), "green": ("#DDF3E8", "#009E73"), "orange": ("#FDEBD0", "#E69F00"),
    "red": ("#F8DDD3", "#D55E00"), "purple": ("#EBDDF0", "#CC79A7"), "gray": ("#EEEEEE", "#777777")}
MAX_NODES, MAX_EDGES = 40, 80
MAX_LABEL, MAX_EDGE_LABEL, MAX_TITLE = 80, 30, 100
WRAP, MAX_LINES = 18, 4                      # labels wrap at 18 characters, at most 4 lines
ID_RX = re.compile(r"[A-Za-z0-9_-]{1,30}")
GAP_ACROSS, GAP_ALONG = 0.5, 0.9             # spacing in inches (data units) between neighbours / layers


# --------------------------------------------------------------- validation

def validate(data) -> dict:
    """Check a spec and return a cleaned copy. Raises DiagramError with a fix-it message."""
    if not isinstance(data, dict):
        raise DiagramError('the file must hold one JSON object, like {"nodes": [...], "edges": [...]}')
    if data.get("type", "flowchart") != "flowchart":
        raise DiagramError('"type" must be "flowchart" (the only diagram type for now)')
    direction = data.get("direction", "down")
    if direction not in ("down", "right"):
        raise DiagramError('"direction" must be "down" or "right"')

    nodes = data.get("nodes")
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= MAX_NODES:
        raise DiagramError(f'"nodes" must be a list of 1 to {MAX_NODES} items like {{"id": "a", "label": "Start"}}')
    clean_nodes, ids = [], set()
    for i, nd in enumerate(nodes, start=1):
        if not isinstance(nd, dict):
            raise DiagramError(f'node {i} must be an object like {{"id": "a", "label": "Start"}}')
        nid = nd.get("id")
        if not isinstance(nid, str) or not ID_RX.fullmatch(nid):
            raise DiagramError(f'node {i} needs an "id": letters, digits, - or _ (up to 30 characters)')
        if nid in ids:
            raise DiagramError(f'two nodes have the id "{nid}"')
        ids.add(nid)
        shape, color = nd.get("shape", "box"), nd.get("color", "blue")
        if shape not in SHAPES:
            raise DiagramError(f'node "{nid}": "shape" must be one of: ' + ", ".join(SHAPES))
        if color not in COLORS:
            raise DiagramError(f'node "{nid}": "color" must be one of: ' + ", ".join(COLORS))
        clean_nodes.append({"id": nid, "label": _text(nd.get("label") or nid, MAX_LABEL),
                            "shape": shape, "color": color})

    raw = data.get("edges", [])
    if not isinstance(raw, list) or len(raw) > MAX_EDGES:
        raise DiagramError(f'"edges" must be a list of at most {MAX_EDGES} items like {{"from": "a", "to": "b"}}')
    edges, seen = [], set()
    for i, e in enumerate(raw, start=1):
        if not isinstance(e, dict) or not isinstance(e.get("from"), str) or not isinstance(e.get("to"), str):
            raise DiagramError(f'edge {i} must be an object like {{"from": "a", "to": "b", "label": "yes"}}')
        a, b = e["from"], e["to"]
        for end in (a, b):
            if end not in ids:
                raise DiagramError(f'edge {i} points to "{_text(end, 30)}" but there is no node with that id')
        if a == b:
            raise DiagramError(f'edge {i}: a node cannot point to itself ("{a}")')
        if (a, b) in seen:
            continue                                  # a repeated edge is merged (the first label wins)
        seen.add((a, b))
        edges.append({"from": a, "to": b, "label": _text(e.get("label", ""), MAX_EDGE_LABEL)})

    return {"type": "flowchart", "title": _text(data.get("title", ""), MAX_TITLE),
            "direction": direction, "nodes": clean_nodes, "edges": edges}


# ------------------------------------------------------------------- layout

def assign_layers(ids: list[str], edges: list[tuple[str, str]]) -> tuple[dict[str, int], set[tuple[str, str]]]:
    """(layer of every node, the set of back edges). A back edge closes a cycle; it is ignored
    for layering and drawn as a curved arrow."""
    out: dict[str, list[str]] = {i: [] for i in ids}
    for a, b in edges:
        out[a].append(b)

    state: dict[str, int] = {}                       # 1 = on the current path, 2 = finished
    back: set[tuple[str, str]] = set()

    def visit(u: str) -> None:
        state[u] = 1
        for v in out[u]:
            if state.get(v) == 1:
                back.add((u, v))
            elif v not in state:
                visit(v)
        state[u] = 2

    for i in ids:
        if i not in state:
            visit(i)

    dag_out: dict[str, list[str]] = {i: [] for i in ids}
    indegree = {i: 0 for i in ids}
    for a, b in edges:
        if (a, b) not in back:
            dag_out[a].append(b)
            indegree[b] += 1
    layer = {i: 0 for i in ids}
    queue = [i for i in ids if indegree[i] == 0]
    while queue:                                     # longest path from a start node
        u = queue.pop(0)
        for v in dag_out[u]:
            layer[v] = max(layer[v], layer[u] + 1)
            indegree[v] -= 1
            if indegree[v] == 0:
                queue.append(v)
    return layer, back


def order_layers(ids: list[str], layer: dict[str, int], dag_edges: list[tuple[str, str]]) -> list[list[str]]:
    """Nodes grouped by layer, ordered inside each layer to reduce crossings: a few sweeps
    that sort each node by the average position of its neighbours (barycenter)."""
    depth = max(layer.values()) + 1
    rows = [[i for i in ids if layer[i] == l] for l in range(depth)]
    preds: dict[str, list[str]] = {i: [] for i in ids}
    succs: dict[str, list[str]] = {i: [] for i in ids}
    for a, b in dag_edges:
        preds[b].append(a)
        succs[a].append(b)

    def positions() -> dict[str, float]:
        return {n: (k + 0.5) / len(row) for row in rows for k, n in enumerate(row)}

    for _ in range(4):
        for l in range(1, depth):                    # top to bottom: follow the parents
            pos = positions()
            rows[l].sort(key=lambda n: sum(pos[p] for p in preds[n]) / len(preds[n]) if preds[n] else pos[n])
        for l in range(depth - 2, -1, -1):           # bottom to top: follow the children
            pos = positions()
            rows[l].sort(key=lambda n: sum(pos[s] for s in succs[n]) / len(succs[n]) if succs[n] else pos[n])
    return rows


def node_size(node: dict) -> tuple[list[str], float, float]:
    """(label lines, width, height) of a node in inches."""
    lines = textwrap.wrap(node["label"], WRAP, break_long_words=False) or [""]
    if len(lines) > MAX_LINES:
        lines = lines[:MAX_LINES]
        lines[-1] = lines[-1][:WRAP - 1] + "…"
    lines = [l if len(l) <= 28 else l[:27] + "…" for l in lines]
    w = max(1.4, min(3.4, 0.095 * max(len(l) for l in lines) + 0.7))
    h = max(0.8, 0.3 * len(lines) + 0.4)
    if node["shape"] == "diamond":                   # text must fit inside the diamond
        w, h = w * 1.45, h * 1.6
    elif node["shape"] == "oval":
        w, h = w * 1.15, h * 1.15
    return lines, w, h


def place(spec: dict):
    """Positions for every node. Returns (pos, sizes, layer, back): pos[id] = (x, y) of the
    centre, sizes[id] = (lines, w, h)."""
    nodes = {n["id"]: n for n in spec["nodes"]}
    ids = [n["id"] for n in spec["nodes"]]
    edges = [(e["from"], e["to"]) for e in spec["edges"]]
    layer, back = assign_layers(ids, edges)
    rows = order_layers(ids, layer, [e for e in edges if e not in back])
    sizes = {i: node_size(nodes[i]) for i in ids}
    down = spec["direction"] == "down"

    pos: dict[str, tuple[float, float]] = {}
    along = 0.0                                      # how far along the flow direction we are
    for row in rows:
        a_len = [sizes[i][2] if down else sizes[i][1] for i in row]      # sizes along the flow
        c_len = [sizes[i][1] if down else sizes[i][2] for i in row]      # sizes across it
        extent = max(a_len)
        cross = -(sum(c_len) + GAP_ACROSS * (len(row) - 1)) / 2
        for i, c in zip(row, c_len):
            centre_across, centre_along = cross + c / 2, along + extent / 2
            pos[i] = (centre_across, -centre_along) if down else (centre_along, -centre_across)
            cross += c + GAP_ACROSS
        along += extent + GAP_ALONG
    return pos, sizes, layer, back


def boundary(centre: tuple[float, float], w: float, h: float, shape: str,
             toward: tuple[float, float]) -> tuple[float, float]:
    """Where a straight line from the node's centre toward a point leaves the node's outline."""
    cx, cy = centre
    vx, vy = toward[0] - cx, toward[1] - cy
    if vx == 0 and vy == 0:
        return centre
    hw, hh = w / 2, h / 2
    if shape == "oval":
        s = 1 / math.sqrt((vx / hw) ** 2 + (vy / hh) ** 2)
    elif shape == "diamond":
        s = 1 / (abs(vx) / hw + abs(vy) / hh)
    else:
        s = min(hw / abs(vx) if vx else math.inf, hh / abs(vy) if vy else math.inf)
    return cx + vx * s, cy + vy * s


# ------------------------------------------------------------------ drawing

def render(spec: dict) -> bytes:
    """A validated spec as PNG bytes."""
    plt = _plt()
    try:
        from matplotlib.patches import Ellipse, FancyBboxPatch, Polygon

        pos, sizes, layer, back = place(spec)
        x0 = min(pos[i][0] - sizes[i][1] / 2 for i in pos)
        x1 = max(pos[i][0] + sizes[i][1] / 2 for i in pos)
        y0 = min(pos[i][1] - sizes[i][2] / 2 for i in pos)
        y1 = max(pos[i][1] + sizes[i][2] / 2 for i in pos)
        top = 0.9 if spec["title"] else 0.0                      # room for the title
        width, height = x1 - x0 + 1.2, y1 - y0 + 1.2 + top
        f = min(1.0, 14.0 / width, 10.0 / height)                # shrink everything to fit 14 x 10 inches
        fig_w, fig_h = max(width * f, 4.0), max(height * f, 2.5)

        fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=100)
        ax.set_position([0, 0, 1, 1])
        ax.axis("off")
        cx, cy = (x0 + x1) / 2, (y0 + y1 + top) / 2
        ax.set_xlim(cx - fig_w / (2 * f), cx + fig_w / (2 * f))  # equal scale on both axes
        ax.set_ylim(cy - fig_h / (2 * f), cy + fig_h / (2 * f))
        font = max(6.0, 10.0 * f)

        if spec["title"]:
            ax.text(cx, y1 + 0.35, spec["title"], ha="center", va="bottom", fontsize=max(8.0, 13 * f), weight="bold")

        for node in spec["nodes"]:
            i = node["id"]
            (x, y), (lines, w, h) = pos[i], sizes[i]
            fill, edge = COLORS[node["color"]]
            style = dict(fc=fill, ec=edge, lw=1.6 * f, zorder=2)
            if node["shape"] == "oval":
                patch = Ellipse((x, y), w, h, **style)
            elif node["shape"] == "diamond":
                patch = Polygon([(x, y + h / 2), (x + w / 2, y), (x, y - h / 2), (x - w / 2, y)], closed=True, **style)
            else:
                box = "round,pad=0,rounding_size=0.18" if node["shape"] == "round" else "square,pad=0"
                patch = FancyBboxPatch((x - w / 2, y - h / 2), w, h, boxstyle=box, **style)
            ax.add_patch(patch)
            ax.text(x, y, "\n".join(lines), ha="center", va="center", fontsize=font, color="#222222", zorder=3)

        shapes = {n["id"]: n["shape"] for n in spec["nodes"]}
        pairs = {(e["from"], e["to"]) for e in spec["edges"]}
        for e in spec["edges"]:
            a, b = e["from"], e["to"]
            start = boundary(pos[a], sizes[a][1], sizes[a][2], shapes[a], pos[b])
            end = boundary(pos[b], sizes[b][1], sizes[b][2], shapes[b], pos[a])
            if (a, b) in back:
                rad = 0.35                                       # closes a cycle: curve it
            elif (b, a) in pairs:
                rad = 0.2                                        # a pair of opposite arrows: bow them apart
            elif abs(layer[a] - layer[b]) > 1:
                rad = 0.25                                       # skips layers: curve around the middle
            else:
                rad = 0.0
            ax.annotate("", xy=end, xytext=start, zorder=1, arrowprops=dict(
                arrowstyle="-|>", color="#444444", lw=1.4 * f, shrinkA=0, shrinkB=0,
                connectionstyle=f"arc3,rad={rad}", mutation_scale=14 * max(f, 0.5)))
            if e["label"]:
                dx, dy = end[0] - start[0], end[1] - start[1]
                # the middle of an arc3 curve sits 0.5*rad of the chord length to the side
                mx = (start[0] + end[0]) / 2 + 0.5 * rad * dy
                my = (start[1] + end[1]) / 2 - 0.5 * rad * dx
                ax.text(mx, my, e["label"], ha="center", va="center", fontsize=max(6.0, 9 * f), zorder=4,
                        bbox=dict(boxstyle="round,pad=0.15", fc="white", ec="none"))

        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor="white")
        return buf.getvalue()
    except DiagramError:
        raise
    except Exception as e:
        raise DiagramError(f"could not draw the diagram ({type(e).__name__})")
    finally:
        plt.close("all")