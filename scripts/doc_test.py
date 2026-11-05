"""
doc_test.py - Checks the Markdown reader, the Word and PDF generators (read back with
python-docx and pypdf) and the document tool. No model needed.
Run from the project root:  python -m scripts.doc_test
"""

import tempfile
from pathlib import Path

from docx import Document
from PIL import Image
from pypdf import PdfReader

import danica.tools.document as doc_tool
from danica.docgen import _markup, build_docx, build_pdf, is_danica_file
from danica.markdown_doc import DocError, labelled, parse, parse_inline
from danica.permissions import Tier, classify
from danica.tools import run_tool
from danica.web import state_for
from danica.workspace import Workspace


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    assert condition, label


FAKE_AWS = "AKIA" + "IOSFODNN7EXAMPLE"   # split so this file stays clean
F = "`" * 3                               # a code fence, built so this file shows up cleanly in chat

MD = "\n".join([
    "# Report title", "",
    "Intro with **bold**, *italic*, `code` and a [link](https://a.org/x).", "",
    "## List", "", "- first", "  - nested", "- second", "", "1. one", "2. two", "",
    "| Name | Qty |", "|---|---|", "| apple | 3 |", "| pear |", "",
    "> a quote", "", F + "python", "x = 1", F, "", "---", "", "![Sales chart](charts/sales.png)", ""])

# --- inline text ---
runs = parse_inline("a **b** *c* `d` [e](https://x.org)")
check("bold, italic and code are flagged",
      ("b", frozenset({"b"})) in runs and ("c", frozenset({"i"})) in runs and ("d", frozenset({"code"})) in runs)
check("a link keeps its address visible", ("e", frozenset()) in runs and (" (https://x.org)", frozenset()) in runs)
check("snake_case words are not italic", parse_inline("my_var_name") == [("my_var_name", frozenset())])
check("a * b * c with spaces is plain text", parse_inline("2 * 3 * 4") == [("2 * 3 * 4", frozenset())])
check("page text is escaped before reportlab sees it",
      _markup([("<b>x</b> & y", frozenset())], "Courier") == "&lt;b&gt;x&lt;/b&gt; &amp; y")

# --- blocks ---
blocks = parse(MD)
check("the blocks come out in order",
      [b["t"] for b in blocks] == ["h", "p", "h", "ul", "ol", "table", "quote", "code", "hr", "img"])
check("heading text is kept", blocks[0]["runs"] == [("Report title", frozenset())])
check("list markers and nesting", [(lv, lab) for lv, lab, _ in labelled(blocks[3])] == [(0, "•"), (1, "–"), (0, "•")])
check("numbered lists count from 1", [lab for _, lab, _ in labelled(blocks[4])] == ["1.", "2."])
check("a short table row is padded", len(blocks[5]["rows"]) == 2 and len(blocks[5]["rows"][1]) == 2)
check("code blocks keep their text", blocks[7]["text"] == "x = 1")
check("an image block has its path and caption", blocks[9] == {"t": "img", "alt": "Sales chart", "path": "charts/sales.png"})
tagged = parse('Intro\n\n<present file="charts/a.png"/>\n\nText <present file="charts/b.png"/> more')
check("a <present> tag in a document becomes an image block",
      [b["t"] for b in tagged] == ["p", "img", "p", "img", "p"] and tagged[1]["path"] == "charts/a.png"
      and tagged[3]["path"] == "charts/b.png")
check("a tag whose path has spaces is left as text", parse('<present file="my charts/a.png"/>')[0]["t"] == "p")
check("invisible characters are removed", parse("a\u200bb")[0]["runs"] == [("ab", frozenset())])
try:
    parse("x" * 300_001)
    too_long = False
except DocError:
    too_long = True
check("a huge document is refused", too_long)

# --- the generators ---
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    tmp = Path(tmp)
    png = tmp / "chart.png"
    Image.new("RGB", (300, 200), "red").save(png)
    sources = [("S1", "Pelican facts", "https://a.org/p")]

    out = tmp / "a.docx"
    warnings = build_docx(blocks, out, title="My Title", meta_title="Report", sources=sources, page="A4",
                          image_for=lambda rel: png)
    d = Document(str(out))
    texts = [p.text for p in d.paragraphs]
    check("docx: title, headings and quote are there",
          "My Title" in texts and "Report title" in texts and "List" in texts and "a quote" in texts)
    check("docx: list markers", any(t.startswith("•") and "first" in t for t in texts))
    check("docx: bold text is bold", any(r.bold for p in d.paragraphs if p.text.startswith("Intro") for r in p.runs))
    check("docx: the table has its rows", len(d.tables) == 1 and len(d.tables[0].rows) == 3
          and d.tables[0].rows[0].cells[0].text == "Name" and d.tables[0].rows[1].cells[0].text == "apple")
    check("docx: the image is embedded", len(d.inline_shapes) == 1 and not warnings)
    check("docx: the Sources section is there", "Sources" in texts and any("https://a.org/p" in t for t in texts))
    check("docx: it carries the Danica marker", is_danica_file(out))

    def no_image(rel):
        raise DocError("image x: not found")

    warnings = build_docx(blocks, tmp / "b.docx", title="", meta_title="r", sources=[], page="LETTER", image_for=no_image)
    check("a missing image is a warning, not a failure",
          warnings == ["image x: not found"] and len(Document(str(tmp / "b.docx")).inline_shapes) == 0)

    out = tmp / "a.pdf"
    warnings = build_pdf(blocks, out, title="My Title", meta_title="Report", sources=sources, page="A4",
                         image_for=lambda rel: png)
    reader = PdfReader(str(out))
    text = "\n".join(page.extract_text() for page in reader.pages)
    check("pdf: it is a PDF with text", out.read_bytes().startswith(b"%PDF") and "Report title" in text and "apple" in text)
    check("pdf: the Sources section is there", "Sources" in text and "Pelican facts" in text)
    check("pdf: it carries the Danica marker", is_danica_file(out))
    check("pdf: A4 page size", round(float(reader.pages[0].mediabox.width)) == 595)
    build_pdf(blocks, tmp / "l.pdf", title="", meta_title="r", sources=[], page="LETTER", image_for=lambda rel: png)
    check("pdf: Letter page size", round(float(PdfReader(str(tmp / "l.pdf")).pages[0].mediabox.width)) == 612)
    nasty = parse("A <b>tag</b> & <font color=red>x</font> in text")
    build_pdf(nasty, tmp / "n.pdf", title="", meta_title="n", sources=[], page="A4", image_for=lambda rel: png)
    check("pdf: tags in page text are shown as text, not obeyed",
          "<b>tag</b>" in PdfReader(str(tmp / "n.pdf")).pages[0].extract_text())

    plain = tmp / "plain.docx"
    Document().save(str(plain))
    check("a file without the marker is not Danica's", not is_danica_file(plain) and not is_danica_file(tmp / "missing.pdf"))

    # --- the tool ---
    check("document is a free WRITE-tier tool", classify("document", {})[0] == Tier.WRITE)
    ws = Workspace(tmp / "ws")
    (ws.root / "charts").mkdir()
    (ws.root / "reports").mkdir()
    Image.new("RGB", (300, 200), "red").save(ws.root / "charts" / "a.png")
    state_for(ws).add("https://a.org/p", "https://a.org/p", "Pelican facts", ["p one", "p two"], [])

    def write(rel: str, content: str) -> None:
        (ws.root / rel).write_text(content, encoding="utf-8")

    write("reports/p.md", "# Pelicans\n\nThey eat fish [S1.2].\n\n![Chart](charts/a.png)\n")
    out = run_tool("document", {"source": "reports/p.md"}, ws)
    check("a Word file is made next to the source", out.startswith("OK: saved reports/p.docx") and "1 source(s)" in out
          and "1 image(s)" in out)
    write("reports/t.md", '# T\n\n<present file="charts/a.png"/>\n')
    out = run_tool("document", {"source": "reports/t.md"}, ws)
    check("a copied <present> tag still gives a real picture, with a note",
          out.startswith("OK") and "1 image(s)" in out and "<present> tags were turned into images" in out
          and len(Document(str(ws.root / "reports" / "t.docx")).inline_shapes) == 1)
    d = Document(str(ws.root / "reports" / "p.docx"))
    check("it holds the image and a harness-built Sources section",
          len(d.inline_shapes) == 1 and any(p.text == "Sources" for p in d.paragraphs)
          and any("Pelican facts" in p.text for p in d.paragraphs))
    check("a PDF works too", run_tool("document", {"source": "reports/p.md", "format": "pdf"}, ws)
          .startswith("OK: saved reports/p.pdf"))
    out = run_tool("document", {"source": "reports/p.md", "sources": False}, ws)
    check("sources=false leaves the list out", "0 source(s)" in out
          and not any(p.text == "Sources" for p in Document(str(ws.root / "reports" / "p.docx")).paragraphs))
    check("re-making Danica's own file replaces it", out.startswith("OK"))

    write("reports/bad.md", "A claim [S9.1] and another [S1.99].")
    out = run_tool("document", {"source": "reports/bad.md"}, ws)
    check("a citation that points to nothing refuses the build", out.startswith("ERROR") and "point to nothing" in out
          and not (ws.root / "reports" / "bad.docx").exists())

    write("reports/rel.md", "![x](../charts/a.png)\n")
    run_tool("document", {"source": "reports/rel.md"}, ws)
    check("an image path relative to the .md file works",
          len(Document(str(ws.root / "reports" / "rel.docx")).inline_shapes) == 1)
    write("reports/out.md", "![x](../../x.png)\n\n![y](https://a.org/i.png)\n")
    out = run_tool("document", {"source": "reports/out.md"}, ws)
    check("an outside or remote image is a note, not a failure",
          out.startswith("OK") and "outside the workspace" in out and "remote images are not downloaded" in out)
    write("reports/url.md", "See https://evil.test/x for more.\n")
    check("a never-fetched address is flagged", "never fetched" in run_tool("document", {"source": "reports/url.md"}, ws))

    write("reports/q.md", "# Q\n")
    Document().save(str(ws.root / "reports" / "q.docx"))
    out = run_tool("document", {"source": "reports/q.md"}, ws)
    check("a file Danica did not make is never replaced", "not made by Danica" in out)

    write("reports/secret.md", f"key {FAKE_AWS}\n")
    check("secrets refuse the build", "secret" in run_tool("document", {"source": "reports/secret.md"}, ws))
    write("reports/e.md", "   \n")
    check("an empty file is refused", "empty" in run_tool("document", {"source": "reports/e.md"}, ws))
    check("a non-Markdown source is refused", run_tool("document", {"source": "charts/a.png"}, ws).startswith("ERROR"))
    check("a missing source says to write it first", "write_file" in run_tool("document", {"source": "reports/none.md"}, ws))
    check("a bad format is refused", run_tool("document", {"source": "reports/p.md", "format": "exe"}, ws).startswith("ERROR"))
    check("a path outside the workspace is refused", "outside the workspace" in run_tool("document", {"source": "..\\x.md"}, ws))
    (ws.root / ".husky").mkdir()
    write(".husky/h.md", "# hi\n")
    check("hook folders are never written to", "blocked" in run_tool("document", {"source": ".husky/h.md"}, ws)
          and not (ws.root / ".husky" / "h.docx").exists())
    real = doc_tool.MAX_SOURCE_BYTES
    doc_tool.MAX_SOURCE_BYTES = 10
    try:
        check("an oversized source is refused", "too big" in run_tool("document", {"source": "reports/p.md"}, ws))
    finally:
        doc_tool.MAX_SOURCE_BYTES = real

print("\nAll good.")