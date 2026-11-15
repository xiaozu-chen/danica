"""
flowgen.py - Flow charts drawn from shapes placed on a GRID.

The model does not draw and does not guess coordinates. It says WHICH shape goes in WHICH grid cell,
what it is labelled, and what connects to what. This file turns that into exact positions and
routed connector lines, so a small model gets a tidy chart every time (no auto-layout surprises).

Data format (validate() explains mistakes in plain words):
    {"type": "flow", "title": "Login",
     "nodes": [{"id": "start", "label": "Start",         "shape": "oval",    "col": 0, "row": 0},
               {"id": "ok",    "label": "Password OK?",  "shape": "diamond", "col": 0, "row": 1},
               {"id": "in",    "label": "Show profile",  "shape": "box",     "col": 0, "row": 2, "color": "green"},
               {"id": "no",    "label": "Show error",    "shape": "box",     "col": 1, "row": 1, "color": "red"}],
     "edges": [{"from": "start", "to": "ok"},
               {"from": "ok", "to": "in", "label": "yes"},
               {"from": "ok", "to": "no", "label": "no"}]}
    col counts from 0 on the left, row from 0 at the top. Leave a free cell between boxes that a
    connector has to go around. One node per cell.
    shapes: box (default), rounded, oval (start/end), diamond (decision), parallelogram (input/output),
            circle (connector)
    colors (optional): blue (default), green, orange, red, pink, sky, yellow, grey

layout(spec) gives the geometry (used by the PNG drawing here AND by the interactive viewer in
htmlview.py, so both always show the same chart). render(spec) gives PNG bytes.
"""

from __future__ import annotations

import io
import re
import textwrap

from danica.charts import _plt, _text

SHAPES = ("box", "rounded", "oval", "diamond", "parallelogram", "circle")
COLORS = {"blue": "#d6e9f8", "green": "#d4f0e3", "orange": "#fbe6c2", "red": "#f8d6cc",
          "pink": "#f4dcea", "sky": "#dcf0fb", "yellow": "#fbf5c8", "grey": "#e5e7eb"}
MAX_NODES, MAX_EDGES, MAX_COLS, MAX_ROWS = 40, 80, 12, 12
MAX_LABEL, MAX_EDGE_LABEL, MAX_TITLE = 60, 30, 120
ID_RX = re.compile(r"[A-Za-z0-9_-]{1,30}")

CW, CH = 210, 120                      # one grid cell, in drawing units (1 unit = 1 pixel in the PNG)
SIZES = {"box": (150, 70), "rounded": (150, 70), "oval": (150, 70), "parallelogram": (164, 70),
         "diamond": (180, 104), "circle": (84, 84)}
WRAP = {"diamond": 11, "circle": 8, "parallelogram": 14, "oval": 16}      # characters per line, by shape
MARGIN, TITLE_H = 40, 40


class FlowError(Exception):
    """A problem with the data. The message is safe to show the model."""


# --------------------------------------------------------------- validation

def validate(data) -> dict:
    """Check a flow chart spec and return a cleaned copy. Raises FlowError with a fix-it message."""
    if not isinstance(data, dict):
        raise FlowError('the file must hold one JSON object: {"type": "flow", "nodes": [...], "edges": [...]}')
    nodes, edges = data.get("nodes"), data.get("edges", [])
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= MAX_NODES:
        raise FlowError(f'"nodes" must be a list of 1 to {MAX_NODES} shapes')
    if not isinstance(edges, list) or len(edges) > MAX_EDGES:
        raise FlowError(f'"edges" must be a list of at most {MAX_EDGES} connections')
    out_nodes, ids, cells = [], set(), {}
    for n, node in enumerate(nodes, start=1):
        if not isinstance(node, dict):
            raise FlowError(f"node {n} must be an object like " + '{"id": "a", "label": "Start", "col": 0, "row": 0}')
        nid = node.get("id")
        if not isinstance(nid, str) or not ID_RX.fullmatch(nid):
            raise FlowError(f'node {n} needs an "id": 1 to 30 letters, digits, - or _ (like "start")')
        if nid in ids:
            raise FlowError(f'two nodes have the id "{nid}"')
        ids.add(nid)
        shape = node.get("shape", "box")
        if shape not in SHAPES:
            raise FlowError(f'node "{nid}": "shape" must be one of ' + ", ".join(SHAPES))
        color = node.get("color", "blue")
        if color not in COLORS:
            raise FlowError(f'node "{nid}": "color" must be one of ' + ", ".join(COLORS))
        col, row = node.get("col"), node.get("row")
        for name, v, top in (("col", col, MAX_COLS), ("row", row, MAX_ROWS)):
            if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v < top:
                raise FlowError(f'node "{nid}": "{name}" must be a whole number from 0 to {top - 1}')
        if (col, row) in cells:
            raise FlowError(f'nodes "{cells[(col, row)]}" and "{nid}" are in the same cell (col {col}, row {row}): '
                            "give each shape its own cell")
        cells[(col, row)] = nid
        label = _text(node.get("label", nid), MAX_LABEL)
        out_nodes.append({"id": nid, "label": label or nid, "shape": shape, "color": color, "col": col, "row": row})
    out_edges = []
    for n, edge in enumerate(edges, start=1):
        if not isinstance(edge, dict) or "from" not in edge or "to" not in edge:
            raise FlowError(f'connection {n} must be an object like {{"from": "a", "to": "b", "label": "yes"}}')
        a, b = edge["from"], edge["to"]
        for end in (a, b):
            if end not in ids:
                raise FlowError(f'connection {n} uses the id "{end}", but no node has that id')
        if a == b:
            raise FlowError(f'connection {n} goes from "{a}" to itself')
        out_edges.append({"from": a, "to": b, "label": _text(edge.get("label", ""), MAX_EDGE_LABEL)})
    return {"type": "flow", "title": _text(data.get("title", ""), MAX_TITLE), "nodes": out_nodes, "edges": out_edges}


# --------------------------------------------------------------- layout (shared by PNG and viewer)

def _box(node: dict) -> tuple[float, float, float, float]:
    return (node["x"] - node["w"] / 2, node["y"] - node["h"] / 2, node["x"] + node["w"] / 2, node["y"] + node["h"] / 2)


def _hits(p: tuple, q: tuple, rect: tuple, pad: float = 6) -> bool:
    """Does the axis-aligned segment p-q cross the (padded) rectangle?"""
    x0, y0, x1, y1 = rect[0] - pad, rect[1] - pad, rect[2] + pad, rect[3] + pad
    sx0, sx1 = sorted((p[0], q[0]))
    sy0, sy1 = sorted((p[1], q[1]))
    return sx1 > x0 and sx0 < x1 and sy1 > y0 and sy0 < y1


def _anchor(node: dict, side: str) -> tuple[float, float]:
    return {"L": (node["x"] - node["w"] / 2, node["y"]), "R": (node["x"] + node["w"] / 2, node["y"]),
            "U": (node["x"], node["y"] - node["h"] / 2), "D": (node["x"], node["y"] + node["h"] / 2)}[side]


def _candidates(a: dict, b: dict) -> list[list[tuple]]:
    """Possible axis-aligned routes from a to b, best first."""
    dx, dy = b["x"] - a["x"], b["y"] - a["y"]
    h = "R" if dx > 0 else "L"                       # side of a that faces b horizontally
    v = "D" if dy > 0 else "U"
    hb = "L" if dx > 0 else "R"                      # the side of b that faces back to a
    vb = "U" if dy > 0 else "D"
    routes = []
    if dx == 0:
        routes.append([_anchor(a, v), _anchor(b, vb)])
    elif dy == 0:
        routes.append([_anchor(a, h), _anchor(b, hb)])
    else:
        routes.append([_anchor(a, h), (b["x"], a["y"]), _anchor(b, vb)])           # sideways, then into top/bottom
        routes.append([_anchor(a, v), (a["x"], b["y"]), _anchor(b, hb)])           # down/up, then into the side
        ymid = (a["row"] + (1 if dy > 0 else 0)) * CH
        routes.append([_anchor(a, v), (a["x"], ymid), (b["x"], ymid), _anchor(b, vb)])
        xmid = (a["col"] + (1 if dx > 0 else 0)) * CW
        routes.append([_anchor(a, h), (xmid, a["y"]), (xmid, b["y"]), _anchor(b, hb)])
    # going round something that sits between two aligned shapes
    if dy == 0:
        for s in ("U", "D"):
            up = -CH / 2 if s == "U" else CH / 2
            routes.append([_anchor(a, s), (a["x"], a["y"] + up), (b["x"], b["y"] + up), _anchor(b, s)])
    if dx == 0:
        for s in ("R", "L"):
            out = CW / 2 if s == "R" else -CW / 2
            routes.append([_anchor(a, s), (a["x"] + out, a["y"]), (b["x"] + out, b["y"]), _anchor(b, s)])
    routes.append([_anchor(a, h if abs(dx) >= abs(dy) else v), _anchor(b, hb if abs(dx) >= abs(dy) else vb)])
    return routes


def layout(spec: dict) -> dict:
    """Positions and routed connectors for a validated spec, in drawing units."""
    cols = max(n["col"] for n in spec["nodes"]) + 1
    rows = max(n["row"] for n in spec["nodes"]) + 1
    top = MARGIN + (TITLE_H if spec["title"] else 0)
    nodes = {}
    for n in spec["nodes"]:
        w, h = SIZES[n["shape"]]
        width = WRAP.get(n["shape"], 16)
        lines = textwrap.wrap(n["label"], width=width, break_long_words=True) or [n["label"]]
        if len(lines) > 4:
            lines = lines[:4]
            lines[-1] = lines[-1][:max(1, width - 1)] + "…"
        nodes[n["id"]] = {"id": n["id"], "label": n["label"], "lines": lines, "shape": n["shape"],
                          "fill": COLORS[n["color"]], "col": n["col"], "row": n["row"], "w": w, "h": h,
                          "x": MARGIN + (n["col"] + 0.5) * CW, "y": top + (n["row"] + 0.5) * CH}
    out_edges = []
    for e in spec["edges"]:
        a, b = nodes[e["from"]], nodes[e["to"]]
        obstacles = [_box(n) for k, n in nodes.items() if k not in (e["from"], e["to"])]
        chosen = None
        for route in _candidates(a, b):
            if not any(_hits(p, q, r) for p, q in zip(route, route[1:]) for r in obstacles):
                chosen = route
                break
        chosen = chosen or _candidates(a, b)[-1]
        # the label sits on the longest segment
        seg = max(zip(chosen, chosen[1:]), key=lambda s: abs(s[0][0] - s[1][0]) + abs(s[0][1] - s[1][1]))
        out_edges.append({"from": e["from"], "to": e["to"], "label": e["label"],
                          "points": [[round(x, 1), round(y, 1)] for x, y in chosen],
                          "lx": round((seg[0][0] + seg[1][0]) / 2, 1), "ly": round((seg[0][1] + seg[1][1]) / 2, 1)})
    return {"width": MARGIN * 2 + cols * CW, "height": top + rows * CH + MARGIN - 10,
            "title": spec["title"], "title_y": MARGIN, "nodes": list(nodes.values()), "edges": out_edges}


# --------------------------------------------------------------- drawing (PNG)

def _patch(n: dict):
    """The matplotlib shape for one node (y grows downward: the axes are inverted)."""
    from matplotlib import patches
    x, y, w, h = n["x"], n["y"], n["w"], n["h"]
    style = dict(facecolor=n["fill"], edgecolor="#374151", linewidth=1.6, zorder=3)
    if n["shape"] == "diamond":
        return patches.Polygon([(x, y - h / 2), (x + w / 2, y), (x, y + h / 2), (x - w / 2, y)], closed=True, **style)
    if n["shape"] == "parallelogram":
        k = 18
        return patches.Polygon([(x - w / 2 + k, y - h / 2), (x + w / 2, y - h / 2), (x + w / 2 - k, y + h / 2),
                                (x - w / 2, y + h / 2)], closed=True, **style)
    if n["shape"] == "oval":
        return patches.Ellipse((x, y), w, h, **style)
    if n["shape"] == "circle":
        return patches.Circle((x, y), w / 2, **style)
    if n["shape"] == "rounded":
        return patches.FancyBboxPatch((x - w / 2, y - h / 2), w, h, boxstyle="round,pad=0,rounding_size=16", **style)
    return patches.Rectangle((x - w / 2, y - h / 2), w, h, **style)


def render(spec: dict) -> bytes:
    """A validated flow chart spec as PNG bytes."""
    plt = _plt()
    try:
        lay = layout(spec)
        scale = min(1.0, 1500 / lay["width"], 1200 / lay["height"])           # keep the picture a sensible size
        fig, ax = plt.subplots(figsize=(lay["width"] * scale / 100, lay["height"] * scale / 100), dpi=100)
        ax.set_xlim(0, lay["width"])
        ax.set_ylim(lay["height"], 0)                                          # row 0 at the top
        ax.axis("off")
        fs = 11 * max(scale, 0.7)
        if lay["title"]:
            ax.text(lay["width"] / 2, lay["title_y"], lay["title"], ha="center", va="center",
                    fontsize=14 * max(scale, 0.8), fontweight="bold")
        for e in lay["edges"]:
            pts = e["points"]
            for p, q in zip(pts[:-2], pts[1:-1]):                              # all but the last segment: plain lines
                ax.plot([p[0], q[0]], [p[1], q[1]], color="#374151", linewidth=1.6, zorder=1, solid_capstyle="round")
            ax.annotate("", xy=pts[-1], xytext=pts[-2], zorder=2,
                        arrowprops=dict(arrowstyle="-|>", color="#374151", linewidth=1.6, shrinkA=0, shrinkB=0,
                                        mutation_scale=16))
            if e["label"]:
                ax.text(e["lx"], e["ly"], e["label"], ha="center", va="center", fontsize=fs * 0.9, zorder=5,
                        bbox=dict(boxstyle="round,pad=0.2", facecolor="white", edgecolor="none"))
        for n in lay["nodes"]:
            ax.add_patch(_patch(n))
            ax.text(n["x"], n["y"], "\n".join(n["lines"]), ha="center", va="center", fontsize=fs, zorder=4)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor="white")
        return buf.getvalue()
    except FlowError:
        raise
    except Exception as e:
        raise FlowError(f"could not draw the flow chart ({type(e).__name__})")
    finally:
        plt.close("all")
