"""Offline Phase 7 exit test for template-based artifact generation."""

import json
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import danica.agent as agent_module
from danica.agent import Agent
from docx import Document
from danica.presentations import PresentationStream, remove_and_validate
from danica.permissions import PermissionPolicy
from pypdf import PdfReader
from danica.tools.present import (
    create_chart,
    create_dashboard,
    create_diagram,
    create_document,
    create_slides,
)
from danica.workspace import Workspace


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    assert condition, label


with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    ws = Workspace(root / "workspace")
    (ws.root / "chart.json").write_text(json.dumps({
        "labels": ["<script>alert(1)</script>", "Feb", "Mar"],
        "series": [{"name": "Sales", "values": [4, 8, 6]}],
    }), encoding="utf-8")
    (ws.root / "dashboard.json").write_text(json.dumps({
        "charts": [
            {"type": "bar", "title": "Sales", "data": {
                "labels": ["A", "B"], "series": [{"name": "S", "values": [1, 2]}],
            }},
            {"type": "table", "title": "Summary", "data": {
                "labels": ["A", "B"], "series": [{"name": "S", "values": [1, 2]}],
            }},
        ],
    }), encoding="utf-8")
    (ws.root / "report.md").write_text(
        "# Results\n\nA report with [verified citation S1.1] and a short summary.\n",
        encoding="utf-8",
    )
    (ws.root / "flow.mmd").write_text(
        "flowchart LR\nA[Start] -->|valid| B[Finish]\n", encoding="utf-8"
    )
    (ws.root / "graph.dot").write_text(
        'digraph G { A [label="Source"]; B [label="Target"]; A -> B [label="next"]; }',
        encoding="utf-8",
    )
    (ws.root / "slides.json").write_text(json.dumps({
        "slides": [
            {"title": "Overview", "bullets": ["Purpose", "Findings"]},
            {"title": "Results", "bullets": ["Result one"]},
        ],
    }), encoding="utf-8")

    for chart_type in ("bar", "line", "scatter", "area", "pie", "table"):
        result = create_chart(ws, "chart.json", f"charts/{chart_type}.html",
                              chart_type, f"{chart_type.title()} chart")
        page = (ws.root / "charts" / f"{chart_type}.html").read_text(encoding="utf-8")
        check(f"{chart_type} chart renders offline", result.startswith("OK:")
              and "<html" in page and "Sales" in page)
    safe_page = (ws.root / "charts" / "table.html").read_text(encoding="utf-8")
    check("chart input is escaped in generated HTML",
          "&lt;script&gt;alert(1)&lt;/script&gt;" in safe_page
          and "<script>alert(1)</script>" not in safe_page)

    check("dashboard renders multiple templates",
          create_dashboard(ws, "dashboard.json", "dashboard.html", "Test dashboard").startswith("OK:")
          and "Summary" in (ws.root / "dashboard.html").read_text(encoding="utf-8"))
    check("Markdown document generation preserves citations",
          create_document(ws, "report.md", "out/report.md", "markdown", "Report").startswith("OK:")
          and "S1.1" in (ws.root / "out" / "report.md").read_text(encoding="utf-8"))

    create_document(ws, "report.md", "out/report.docx", "docx", "Report")
    with zipfile.ZipFile(ws.root / "out" / "report.docx") as archive:
        ElementTree.fromstring(archive.read("word/document.xml"))
    docx = Document(ws.root / "out" / "report.docx")
    docx_text = " ".join(paragraph.text for paragraph in docx.paragraphs)
    check("DOCX document preserves report text and citations",
          "Results" in docx_text and "S1.1" in docx_text)
    create_document(ws, "report.md", "out/report.pdf", "pdf", "Report")
    pdf = PdfReader(ws.root / "out" / "report.pdf")
    pdf_text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    check("PDF document preserves report text and citations",
          (ws.root / "out" / "report.pdf").read_bytes().startswith(b"%PDF-")
          and "Results" in pdf_text and "S1.1" in pdf_text)

    create_diagram(ws, "flow.mmd", "diagrams/flow.svg", "mermaid", "Flow")
    flow_xml = ElementTree.parse(ws.root / "diagrams" / "flow.svg").getroot()
    check("Mermaid flowchart renders valid SVG with edge labels",
          flow_xml.tag.endswith("svg") and "valid" in "".join(flow_xml.itertext()))
    create_diagram(ws, "graph.dot", "diagrams/graph.svg", "graphviz", "Graph")
    graph_svg = ElementTree.parse(ws.root / "diagrams" / "graph.svg").getroot()
    check("Graphviz node/edge subset renders valid SVG",
          graph_svg.tag.endswith("svg") and "Source" in "".join(graph_svg.itertext()))

    create_slides(ws, "slides.json", "slides/deck.pptx", "slides/deck.html", "Deck")
    from pptx import Presentation

    deck = Presentation(ws.root / "slides" / "deck.pptx")
    with zipfile.ZipFile(ws.root / "slides" / "deck.pptx") as archive:
        ElementTree.fromstring(archive.read("ppt/presentation.xml"))
    slideshow = (ws.root / "slides" / "deck.html").read_text(encoding="utf-8")
    check("PowerPoint and self-contained HTML slideshow are created",
          "Overview" in slideshow and "ArrowRight" in slideshow and len(deck.slides) == 3
          and "Purpose" in " ".join(
              shape.text for slide in deck.slides for shape in slide.shapes if shape.has_text_frame
          ))

    chunks = ["before <pre", "sent file=\"charts/bar.html\"/> after"]
    stream = PresentationStream()
    streamed = "".join(stream.feed(chunk) for chunk in chunks) + stream.finish()
    check("present tags are withheld from streamed output",
          streamed == "before  after")
    cleaned, opened, warnings = remove_and_validate(
        'See <present file="charts/bar.html"/> now.', ws
    )
    check("only task-generated HTML can be presented",
          cleaned == "See  now." and opened == [(ws.root / "charts" / "bar.html").resolve()]
          and not warnings)
    cleaned, opened, warnings = remove_and_validate(
        'See <present file="report.md"/> now.', ws
    )
    check("arbitrary workspace files are not opened by present tags",
          not opened and warnings and "<present" not in cleaned)
    policy = PermissionPolicy()
    policy.project_mode = True
    allowed, reason = policy.check(
        "create_chart", {"output_path": "chart.html"},
        tainted_by=["untrusted page"], known_hosts=set(),
    )
    check("tainted web content cannot silently write project artifacts",
          not allowed and "ERROR:" in reason)

    events = []
    ws2 = Workspace(root / "agent-workspace")
    (ws2.root / "chart.json").write_text(json.dumps({
        "labels": ["One"], "series": [{"name": "Value", "values": [1]}],
    }), encoding="utf-8")
    (ws2.root / "report.md").write_text("# Test report\n\nReport body.\n", encoding="utf-8")
    responses = [
        {"type": "tool_calls", "calls": [{
            "id": "chart", "name": "create_chart",
            "arguments": json.dumps({
                "source_path": "chart.json", "output_path": "result.html",
                "chart_type": "bar", "title": "Result",
            }),
        }]},
        {"type": "tool_calls", "calls": [{
            "id": "report", "name": "create_document",
            "arguments": json.dumps({
                "source_path": "report.md", "output_path": "report.pdf",
                "format": "pdf", "title": "Report",
            }),
        }]},
        {"type": "content", "text": 'Done. <present file="result.html"/>'},
    ]

    def fake_stream(messages, **kwargs):
        yield responses.pop(0)

    real_stream = agent_module.stream_chat
    agent_module.stream_chat = fake_stream
    try:
        answer = Agent(ws2, on_event=lambda kind, data: events.append((kind, data))).run(
            "Make and show a chart."
        )
    finally:
        agent_module.stream_chat = real_stream
    check("agent emits a safe link event and lists non-HTML artifacts",
          "present" not in answer and "present" in [kind for kind, _ in events]
          and any(kind == "artifacts" and any(path.suffix == ".pdf" for path in data)
                  for kind, data in events))

print("\nPhase 7 exit test passed.")
