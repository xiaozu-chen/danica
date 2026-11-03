"""Offline template-based charts, documents, diagrams, and slide artifacts."""

import html
import json
import math
import re
from pathlib import Path

from danica.secret_scan import is_sensitive_path
from danica.secret_scan import find_secrets
from danica.tools.files import _secret_check, _write_guard
from danica.tools.registry import tool
from danica.workspace import Workspace

MAX_SOURCE_BYTES = 1_000_000
MAX_SERIES = 12
MAX_POINTS = 100
MAX_SLIDES = 40
SVG_NS = "http://www.w3.org/2000/svg"
MERMAID_EDGE_RX = re.compile(
    r"^\s*([\w.-]+)(?:\s*(?:\[([^\]]*)\]|\(([^)]*)\)|\{([^}]*)\}))?"
    r"\s*(-->|--)\s*(?:\|([^|]*)\|\s*)?([\w.-]+)"
    r"(?:\s*(?:\[([^\]]*)\]|\(([^)]*)\)|\{([^}]*)\}))?"
)
NODE_LABEL_RX = re.compile(r"([\w.-]+)\s*\[\s*label\s*=\s*[\"']([^\"']*)[\"']\s*\]")
DOT_EDGE_RX = re.compile(
    r"\b([\w.-]+)\s*(->|--)\s*([\w.-]+)"
    r"(?:\s*\[\s*label\s*=\s*[\"']([^\"']*)[\"']\s*\])?"
)
MERMAID_NODE_RX = re.compile(
    r"^\s*([\w.-]+)\s*(?:\[([^\]]*)\]|\(([^)]*)\)|\{([^}]*)\})?"
)


class ArtifactError(ValueError):
    """A render request or artifact path is invalid."""


def _read_text(ws: Workspace, source_path: str) -> str:
    path = ws.resolve(source_path)
    if not path.is_file():
        raise ArtifactError(f"source '{source_path}' is not a file")
    if is_sensitive_path(path.relative_to(ws.root)):
        raise ArtifactError(f"source '{source_path}' is a protected credential path")
    if path.stat().st_size > MAX_SOURCE_BYTES:
        raise ArtifactError(f"source exceeds the {MAX_SOURCE_BYTES}-byte limit")
    raw = path.read_bytes()
    if b"\x00" in raw[:8192]:
        raise ArtifactError(f"source '{source_path}' looks binary")
    text = raw.decode("utf-8-sig")
    if find_secrets(text):
        raise ArtifactError(f"source '{source_path}' contains a secret-like value")
    return text


def _output_path(ws: Workspace, output_path: str, suffix: str) -> Path:
    path = ws.resolve(output_path)
    if path.suffix.casefold() != suffix:
        raise ArtifactError(f"output path must end with '{suffix}'")
    if path.exists() and path.is_dir():
        raise ArtifactError("output path is a directory")
    if is_sensitive_path(path.relative_to(ws.root)):
        raise ArtifactError("output path is protected")
    guarded = _write_guard(ws, path)
    if guarded:
        raise ArtifactError(guarded)
    return path


def _save(ws: Workspace, output_path: str, suffix: str, content: str | bytes) -> str:
    path = _output_path(ws, output_path, suffix)
    if isinstance(content, str):
        refused = _secret_check(content)
        if refused:
            raise ArtifactError(refused)
        data = content.encode("utf-8")
    else:
        data = content
    if len(data) > 20_000_000:
        raise ArtifactError("generated artifact exceeds the 20 MB limit")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_bytes(data)
        temporary.replace(path)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise
    ws.artifacts.add(ws.display(path))
    if suffix in (".html", ".svg"):
        ws.presentables.add(ws.display(path))
    return f"OK: created {ws.display(path)} ({len(data):,} bytes)"


def _json_source(ws: Workspace, source_path: str) -> dict:
    try:
        data = json.loads(_read_text(ws, source_path))
    except json.JSONDecodeError as exc:
        raise ArtifactError(f"invalid JSON in '{source_path}': {exc}") from exc
    if not isinstance(data, dict):
        raise ArtifactError("JSON source must be an object")
    return data


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _validate_title(title: str) -> str:
    title = title.strip()
    if not title or len(title) > 160:
        raise ArtifactError("title must contain 1-160 characters")
    if find_secrets(title):
        raise ArtifactError("title contains a secret-like value")
    return title


def _chart_data(data: dict) -> tuple[list[str], list[dict]]:
    labels = data.get("labels")
    series = data.get("series")
    if (not isinstance(labels, list) or not labels or len(labels) > MAX_POINTS
            or not isinstance(series, list) or not series or len(series) > MAX_SERIES):
        raise ArtifactError("chart JSON needs 1-100 labels and 1-12 series")
    clean_series = []
    for item in series:
        if not isinstance(item, dict) or not isinstance(item.get("values"), list):
            raise ArtifactError("each chart series must have a name and a values list")
        values = item["values"]
        if len(values) != len(labels):
            raise ArtifactError("each series must have one value per label")
        try:
            values = [float(value) for value in values]
        except (ValueError, TypeError) as exc:
            raise ArtifactError("chart values must be numbers") from exc
        if not all(math.isfinite(value) for value in values):
            raise ArtifactError("chart values must be finite numbers")
        clean_series.append({"name": str(item.get("name", "Series")), "values": values})
    return [str(label) for label in labels], clean_series


def _svg_chart(kind: str, title: str, labels: list[str], series: list[dict]) -> str:
    width, height = 720, 390
    left, top, plot_width, plot_height = 64, 42, 620, 280
    colors = ["#2563eb", "#dc2626", "#059669", "#d97706", "#7c3aed", "#0891b2"]
    parts = [
        f'<svg xmlns="{SVG_NS}" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{_escape(title)}">',
        f'<text x="{width / 2}" y="25" text-anchor="middle" font-size="18" '
        f'font-family="sans-serif" font-weight="bold">{_escape(title)}</text>',
    ]
    flat = [value for row in series for value in row["values"]]
    minimum, maximum = min(0.0, min(flat)), max(0.0, max(flat))
    if minimum == maximum:
        maximum = minimum + 1
    scale_y = lambda value: top + plot_height * (maximum - value) / (maximum - minimum)
    zero_y = scale_y(0)
    parts.extend([
        f'<line x1="{left}" y1="{zero_y:.1f}" x2="{left + plot_width}" '
        f'y2="{zero_y:.1f}" stroke="#64748b"/>',
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_height}" '
        f'stroke="#64748b"/>',
    ])
    n = len(labels)
    if kind == "bar":
        group_width = plot_width / n
        bar_width = min(42, group_width * 0.75 / len(series))
        for si, row in enumerate(series):
            for index, value in enumerate(row["values"]):
                x = left + index * group_width + group_width * 0.12 + si * bar_width
                y = min(zero_y, scale_y(value))
                bar_height = max(1, abs(scale_y(value) - zero_y))
                parts.append(
                    f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width - 2:.1f}" '
                    f'height="{bar_height:.1f}" fill="{colors[si % len(colors)]}"/>'
                )
    elif kind in ("line", "area", "scatter"):
        for si, row in enumerate(series):
            points = [
                (left + (index + 0.5) * plot_width / n,
                 scale_y(value))
                for index, value in enumerate(row["values"])
            ]
            coords = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
            color = colors[si % len(colors)]
            if kind == "area":
                polygon = f"{left},{zero_y:.1f} {coords} {left + plot_width},{zero_y:.1f}"
                parts.append(f'<polygon points="{polygon}" fill="{color}" opacity="0.2"/>')
            if kind != "scatter":
                parts.append(f'<polyline points="{coords}" fill="none" stroke="{color}" '
                             'stroke-width="3"/>')
            for x, y in points:
                parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="{color}"/>')
    elif kind == "pie":
        values = [max(0, value) for value in series[0]["values"]]
        total = sum(values)
        if not total:
            raise ArtifactError("pie charts need at least one positive value")
        cx, cy, radius = 265, 195, 125
        angle = -math.pi / 2
        for index, value in enumerate(values):
            next_angle = angle + value / total * 2 * math.pi
            x1, y1 = cx + radius * math.cos(angle), cy + radius * math.sin(angle)
            x2, y2 = cx + radius * math.cos(next_angle), cy + radius * math.sin(next_angle)
            large = 1 if next_angle - angle > math.pi else 0
            parts.append(
                f'<path d="M {cx},{cy} L {x1:.2f},{y1:.2f} '
                f'A {radius},{radius} 0 {large} 1 {x2:.2f},{y2:.2f} Z" '
                f'fill="{colors[index % len(colors)]}"/>'
            )
            parts.append(f'<text x="445" y="{72 + index * 24}" font-size="13" '
                         f'font-family="sans-serif">{_escape(labels[index])}: '
                         f'{value:g}</text>')
            angle = next_angle
    for index, label in enumerate(labels):
        x = left + (index + 0.5) * plot_width / n
        parts.append(f'<text x="{x:.1f}" y="{top + plot_height + 22}" text-anchor="middle" '
                     f'font-size="11" font-family="sans-serif">{_escape(label[:24])}</text>')
    for si, row in enumerate(series):
        x = 70 + si * 145
        parts.append(f'<rect x="{x}" y="365" width="12" height="12" '
                     f'fill="{colors[si % len(colors)]}"/>'
                     f'<text x="{x + 18}" y="376" font-size="12" '
                     f'font-family="sans-serif">{_escape(row["name"][:24])}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _table_html(title: str, labels: list[str], series: list[dict]) -> str:
    headings = "".join(f"<th>{_escape(label)}</th>" for label in labels)
    rows = "".join(
        "<tr><th>" + _escape(row["name"]) + "</th>"
        + "".join(f"<td>{value:g}</td>" for value in row["values"]) + "</tr>"
        for row in series
    )
    return (f"<h2>{_escape(title)}</h2><div class=\"table-wrap\"><table><thead><tr>"
            f"<th>Series</th>{headings}</tr></thead><tbody>{rows}</tbody></table></div>")


def _chart_html(chart_type: str, title: str, data: dict) -> str:
    labels, series = _chart_data(data)
    if chart_type == "table":
        return _table_html(title, labels, series)
    svg = _svg_chart(chart_type, title, labels, series)
    return f'<figure class="chart">{svg}</figure>'


def _html_page(title: str, body: str, extra_css: str = "") -> str:
    return (
        "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{_escape(title)}</title><style>"
        "body{font:16px/1.5 system-ui,sans-serif;max-width:1100px;margin:2rem auto;"
        "padding:0 1rem;color:#172033;background:#f8fafc}"
        ".chart,section{margin:1rem 0;padding:1rem;background:white;border-radius:12px;"
        "box-shadow:0 1px 5px #cbd5e1}svg{display:block;width:100%;height:auto}"
        "table{border-collapse:collapse;width:100%}th,td{padding:.5rem;border:1px solid #cbd5e1;"
        "text-align:left}th{background:#e2e8f0}.table-wrap{overflow-x:auto}"
        + extra_css + "</style><body>" + body + "</body></html>"
    )


@tool(
    "create_chart",
    "Create a self-contained offline HTML chart or table from a workspace JSON file. "
    "JSON shape: {labels:[...],series:[{name:'...',values:[...]}]}.",
    {
        "source_path": {"type": "string", "description": "Workspace path to chart JSON"},
        "output_path": {"type": "string", "description": "Workspace output path ending in .html"},
        "chart_type": {"type": "string", "enum": ["bar", "line", "scatter", "area", "pie", "table"],
                       "description": "Chart template"},
        "title": {"type": "string", "description": "Chart title"},
    },
)
def create_chart(
    ws: Workspace, source_path: str, output_path: str, chart_type: str, title: str
) -> str:
    title = _validate_title(title)
    data = _json_source(ws, source_path)
    body = f"<h1>{_escape(title)}</h1>" + _chart_html(chart_type, title, data)
    return _save(ws, output_path, ".html", _html_page(title, body))


@tool(
    "create_dashboard",
    "Create a self-contained HTML dashboard from JSON: {charts:[{type,title,data:{labels,series}}]}.",
    {
        "source_path": {"type": "string", "description": "Workspace path to dashboard JSON"},
        "output_path": {"type": "string", "description": "Workspace output path ending in .html"},
        "title": {"type": "string", "description": "Dashboard title"},
    },
)
def create_dashboard(ws: Workspace, source_path: str, output_path: str, title: str) -> str:
    title = _validate_title(title)
    data = _json_source(ws, source_path)
    charts = data.get("charts")
    if not isinstance(charts, list) or not 1 <= len(charts) <= 12:
        raise ArtifactError("dashboard JSON needs 1-12 charts")
    body = f"<h1>{_escape(title)}</h1>"
    for chart in charts:
        if not isinstance(chart, dict) or chart.get("type") not in (
            "bar", "line", "scatter", "area", "pie", "table"
        ):
            raise ArtifactError("each dashboard chart needs a supported type and data")
        chart_title = str(chart.get("title", "Chart"))
        body += "<section>" + _chart_html(chart["type"], chart_title, chart.get("data", {})) + "</section>"
    return _save(ws, output_path, ".html", _html_page(title, body))


def _markdown_blocks(text: str):
    for line in text.splitlines():
        if line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            yield "heading", min(level, 6), line[level:].strip()
        elif line.lstrip().startswith(("- ", "* ")):
            yield "bullet", 0, line.lstrip()[2:]
        elif line.strip():
            yield "paragraph", 0, line.strip()
        else:
            yield "blank", 0, ""


def _docx_bytes(title: str, content: str) -> bytes:
    from docx import Document

    document = Document()
    document.add_heading(title, level=0)
    for kind, level, text in _markdown_blocks(content):
        if kind == "heading":
            document.add_heading(text, level=min(level, 9))
        elif kind == "bullet":
            document.add_paragraph(text, style="List Bullet")
        elif kind == "paragraph":
            document.add_paragraph(text)
    from io import BytesIO

    output = BytesIO()
    document.save(output)
    return output.getvalue()


def _pdf_bytes(title: str, content: str) -> bytes:
    from io import BytesIO

    from reportlab.lib import pagesizes
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        ListFlowable, ListItem, PageBreak, Paragraph, SimpleDocTemplate, Spacer,
    )
    from xml.sax.saxutils import escape

    output = BytesIO()
    document = SimpleDocTemplate(
        output, pagesize=pagesizes.LETTER, rightMargin=0.7 * inch, leftMargin=0.7 * inch
    )
    styles = getSampleStyleSheet()
    flow = [Paragraph(escape(title), styles["Title"]), Spacer(1, 12)]
    bullets = []

    def flush_bullets():
        if bullets:
            flow.append(ListFlowable(
                [ListItem(Paragraph(escape(item), styles["BodyText"])) for item in bullets],
                bulletType="bullet",
            ))
            bullets.clear()

    for kind, level, text in _markdown_blocks(content):
        if kind == "bullet":
            bullets.append(text)
            continue
        flush_bullets()
        if kind == "heading":
            flow.append(Paragraph(escape(text), styles[f"Heading{min(level, 3)}"]))
        elif kind == "paragraph":
            flow.append(Paragraph(escape(text), styles["BodyText"]))
        elif kind == "blank":
            flow.append(Spacer(1, 6))
    flush_bullets()
    document.build(flow)
    return output.getvalue()


@tool(
    "create_document",
    "Generate Markdown, DOCX, or PDF from a workspace Markdown source. Keep citations "
    "in the source content; this tool does not invent or validate citations.",
    {
        "source_path": {"type": "string", "description": "Workspace path to Markdown source"},
        "output_path": {"type": "string", "description": "Workspace output path"},
        "format": {"type": "string", "enum": ["markdown", "docx", "pdf"],
                   "description": "Output format"},
        "title": {"type": "string", "description": "Document title"},
    },
)
def create_document(
    ws: Workspace, source_path: str, output_path: str, format: str, title: str
) -> str:
    title = _validate_title(title)
    content = _read_text(ws, source_path)
    if format == "markdown":
        return _save(ws, output_path, ".md", f"# {title}\n\n{content}")
    if format == "docx":
        return _save(ws, output_path, ".docx", _docx_bytes(title, content))
    if format == "pdf":
        return _save(ws, output_path, ".pdf", _pdf_bytes(title, content))
    raise ArtifactError(f"unsupported document format '{format}'")


def _diagram_edges(source: str, syntax: str):
    nodes: dict[str, str] = {}
    edges = []
    direction = "LR"
    for line in source.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("%%", "//", "#")):
            continue
        if syntax == "mermaid":
            head = re.match(r"^(?:flowchart|graph)\s+(TD|TB|LR|RL|BT)\b", stripped, re.I)
            if head:
                direction = head.group(1).upper()
                continue
            match = MERMAID_EDGE_RX.match(stripped)
            if match:
                (left, left_square, left_round, left_brace, arrow, edge_label, right,
                 right_square, right_round, right_brace) = match.groups()
                nodes.setdefault(left, left_square or left_round or left_brace or left)
                nodes.setdefault(right, right_square or right_round or right_brace or right)
                edges.append((left, right, edge_label or "", arrow == "-->"))
                continue
            node_match = MERMAID_NODE_RX.match(stripped)
            if node_match and node_match.group(1) not in ("subgraph", "end"):
                node_id, square, round_label, brace = node_match.groups()
                nodes.setdefault(node_id, square or round_label or brace or node_id)
        else:
            for node_id, label in NODE_LABEL_RX.findall(stripped):
                nodes[node_id] = label
            for match in DOT_EDGE_RX.finditer(stripped):
                left, arrow, right, edge_label = match.groups()
                nodes.setdefault(left, left)
                nodes.setdefault(right, right)
                edges.append((left, right, edge_label or "", arrow == "->"))
    if not nodes or not edges:
        raise ArtifactError(
            "diagram source needs labeled nodes and edges "
            "(Mermaid flowchart syntax or Graphviz node -> node syntax)"
        )
    if len(nodes) > 80 or len(edges) > 120:
        raise ArtifactError("diagram exceeds the limit of 80 nodes or 120 edges")
    return nodes, edges, direction


def _svg_diagram(source: str, syntax: str, title: str) -> str:
    nodes, edges, direction = _diagram_edges(source, syntax)
    ids = list(nodes)
    columns = max(1, min(5, math.ceil(math.sqrt(len(ids)))))
    if direction in ("LR", "RL"):
        columns = min(10, len(ids))
    cell_w, cell_h, node_w, node_h = 190, 110, 150, 54
    rows = math.ceil(len(ids) / columns)
    width, height = columns * cell_w + 50, rows * cell_h + 100
    positions = {}
    for index, node_id in enumerate(ids):
        row, col = divmod(index, columns)
        if direction == "RL":
            col = columns - col - 1
        if direction == "BT":
            row = rows - row - 1
        positions[node_id] = (35 + col * cell_w, 55 + row * cell_h)
    parts = [
        f'<svg xmlns="{SVG_NS}" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{_escape(title)}">',
        '<defs><marker id="arrow" markerWidth="10" markerHeight="7" refX="9" refY="3.5" '
        'orient="auto"><polygon points="0 0,10 3.5,0 7" fill="#64748b"/></marker></defs>',
        f'<text x="{width / 2}" y="28" text-anchor="middle" font-size="18" '
        f'font-family="sans-serif" font-weight="bold">{_escape(title)}</text>',
    ]
    for left, right, label, directed in edges:
        x1, y1 = positions[left]
        x2, y2 = positions[right]
        x1, y1 = x1 + node_w / 2, y1 + node_h / 2
        x2, y2 = x2 + node_w / 2, y2 + node_h / 2
        marker = ' marker-end="url(#arrow)"' if directed else ""
        parts.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
                     f'stroke="#64748b" stroke-width="2"{marker}/>')
        if label:
            parts.append(f'<text x="{(x1+x2)/2:.1f}" y="{(y1+y2)/2-5:.1f}" '
                         f'text-anchor="middle" font-size="11" font-family="sans-serif">'
                         f'{_escape(label)}</text>')
    for node_id, label in nodes.items():
        x, y = positions[node_id]
        parts.append(f'<rect x="{x}" y="{y}" width="{node_w}" height="{node_h}" rx="10" '
                     'fill="#eff6ff" stroke="#2563eb" stroke-width="2"/>')
        parts.append(f'<text x="{x + node_w/2}" y="{y + node_h/2 + 5}" '
                     f'text-anchor="middle" font-size="14" font-family="sans-serif">'
                     f'{_escape(label[:60])}</text>')
    parts.append("</svg>")
    return "".join(parts)


@tool(
    "create_diagram",
    "Render Mermaid flowchart or Graphviz DOT node/edge source from a workspace file to "
    "self-contained SVG. This offline renderer supports a documented subset of each syntax.",
    {
        "source_path": {"type": "string", "description": "Workspace path to Mermaid or DOT source"},
        "output_path": {"type": "string", "description": "Workspace output path ending in .svg"},
        "syntax": {"type": "string", "enum": ["mermaid", "graphviz"],
                   "description": "Source diagram syntax"},
        "title": {"type": "string", "description": "Diagram title"},
    },
)
def create_diagram(
    ws: Workspace, source_path: str, output_path: str, syntax: str, title: str
) -> str:
    title = _validate_title(title)
    source = _read_text(ws, source_path)
    svg = _svg_diagram(source, syntax, title)
    return _save(ws, output_path, ".svg", svg)


def _slide_data(data: dict) -> list[dict]:
    slides = data.get("slides")
    if not isinstance(slides, list) or not 1 <= len(slides) <= MAX_SLIDES:
        raise ArtifactError(f"slide JSON needs 1-{MAX_SLIDES} slides")
    clean = []
    for slide in slides:
        if not isinstance(slide, dict) or not isinstance(slide.get("title"), str):
            raise ArtifactError("each slide must have a title")
        bullets = slide.get("bullets", [])
        if not isinstance(bullets, list) or len(bullets) > 20:
            raise ArtifactError("each slide can have at most 20 bullet points")
        clean.append({"title": slide["title"], "bullets": [str(item) for item in bullets]})
    return clean


def _pptx_bytes(title: str, slides: list[dict]) -> bytes:
    from io import BytesIO

    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.util import Inches, Pt

    presentation = Presentation()
    presentation.slide_width = Inches(13.333)
    presentation.slide_height = Inches(7.5)
    cover = presentation.slides.add_slide(presentation.slide_layouts[0])
    cover.background.fill.solid()
    cover.background.fill.fore_color.rgb = RGBColor(248, 250, 252)
    cover.shapes.title.text = title
    cover.shapes.title.text_frame.paragraphs[0].font.size = Pt(34)
    cover.shapes.title.text_frame.paragraphs[0].font.color.rgb = RGBColor(30, 64, 175)
    subtitle = cover.placeholders[1]
    subtitle.text = "Created with Danica"
    subtitle.text_frame.paragraphs[0].font.size = Pt(20)
    for item in slides:
        slide = presentation.slides.add_slide(presentation.slide_layouts[1])
        slide.background.fill.solid()
        slide.background.fill.fore_color.rgb = RGBColor(248, 250, 252)
        slide.shapes.title.text = item["title"]
        for paragraph in slide.shapes.title.text_frame.paragraphs:
            paragraph.font.name = "Aptos Display"
            paragraph.font.size = Pt(30)
            paragraph.font.color.rgb = RGBColor(30, 64, 175)
        body = slide.placeholders[1].text_frame
        body.clear()
        for bullet_index, text in enumerate(item["bullets"]):
            paragraph = body.paragraphs[0] if bullet_index == 0 else body.add_paragraph()
            paragraph.text = text
            paragraph.level = 0
            paragraph.font.size = Pt(22)
    output = BytesIO()
    presentation.save(output)
    return output.getvalue()


def _slideshow_html(title: str, slides: list[dict]) -> str:
    sections = []
    for index, slide in enumerate(slides):
        bullets = "".join(f"<li>{_escape(item)}</li>" for item in slide["bullets"])
        sections.append(
            f'<section class="slide" {"hidden" if index else ""}>'
            f"<p>{index + 1} / {len(slides)}</p><h1>{_escape(slide['title'])}</h1>"
            f"<ul>{bullets}</ul></section>"
        )
    script = (
        "<script>const s=[...document.querySelectorAll('.slide')];let i=0;"
        "function show(n){i=(n+s.length)%s.length;s.forEach((x,j)=>x.hidden=j!==i);"
        "document.getElementById('count').textContent=(i+1)+' / '+s.length;}"
        "document.addEventListener('keydown',e=>{if(e.key==='ArrowRight'||e.key===' '){show(i+1)}"
        "if(e.key==='ArrowLeft'){show(i-1)}});"
        "document.getElementById('next').onclick=()=>show(i+1);"
        "document.getElementById('prev').onclick=()=>show(i-1);</script>"
    )
    body = (
        f"<header>{_escape(title)}</header>{''.join(sections)}"
        '<nav><button id="prev">Previous</button> <span id="count">1 / '
        f'{len(slides)}</span> <button id="next">Next</button></nav>{script}'
    )
    css = (".slide{min-height:65vh;padding:4rem 2rem;background:white;border-radius:1rem}"
           ".slide h1{font-size:clamp(2rem,6vw,4rem);color:#1e40af}.slide li{font-size:1.5rem;"
           "margin:.8rem}header,nav{text-align:center;padding:1rem}button{padding:.6rem 1rem}")
    return _html_page(title, body, css)


@tool(
    "create_slides",
    "Build a themed PowerPoint deck and a self-contained HTML slideshow from JSON "
    "{slides:[{title,bullets:[...]}]}.",
    {
        "source_path": {"type": "string", "description": "Workspace path to slide JSON"},
        "output_path": {"type": "string", "description": "Workspace output path ending in .pptx"},
        "html_path": {"type": "string", "description": "Workspace slideshow path ending in .html"},
        "title": {"type": "string", "description": "Deck title"},
    },
)
def create_slides(
    ws: Workspace, source_path: str, output_path: str, html_path: str, title: str
) -> str:
    title = _validate_title(title)
    slides = _slide_data(_json_source(ws, source_path))
    pptx_result = _save(ws, output_path, ".pptx", _pptx_bytes(title, slides))
    html_result = _save(ws, html_path, ".html", _slideshow_html(title, slides))
    return f"{pptx_result}\n{html_result}"
