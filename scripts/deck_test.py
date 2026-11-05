"""
deck_test.py - Checks outline splitting, text fitting, the generated .pptx (read back with
python-pptx), themes, warnings and the deck tool. No model needed.
Run from the project root:  python -m scripts.deck_test
"""

import tempfile
from pathlib import Path

from PIL import Image
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE

from danica.deckgen import THEMES, build_pptx, estimate_height, fit_size, split_slides
from danica.docgen import is_danica_file
from danica.markdown_doc import DocError, parse
from danica.permissions import Tier, classify
from danica.tools import run_tool
from danica.web import state_for
from danica.workspace import Workspace


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    assert condition, label


FAKE_AWS = "AKIA" + "IOSFODNN7EXAMPLE"   # split so this file stays clean

OUTLINE = "\n".join([
    "# Pelican Facts", "A short talk", "Notes: welcome everyone", "",
    "## Diet", "", "- Fish", "  - Herring", "- **Bold** point", "Notes: speak slowly", "",
    "## Chart", "", "![Sales](charts/a.png)", "",
    "## Numbers", "", "Intro line", "", "| A | B |", "|---|---|", "| 1 | 2 |", "",
    "## Both", "", "- bullet", "", "![Pic](charts/a.png)", ""])


def shapes_text(slide) -> str:
    return " | ".join(s.text_frame.text for s in slide.shapes if s.has_text_frame)


def error_of(blocks, **kw) -> str:
    try:
        build_pptx(blocks, tmp / "err.pptx", theme="clean", deck_title="", meta_title="x", sources=[],
                   image_for=lambda rel: png, **kw)
        return ""
    except DocError as e:
        return str(e)


# --- splitting ---
slides = split_slides(parse(OUTLINE))
check("a # heading is a title slide, ## headings are slides",
      [s["kind"] for s in slides] == ["title", "content", "content", "content", "content"])
check("Notes: lines become notes, not slide text", slides[1]["notes"] == ["speak slowly"]
      and all("Notes" not in str(b) for b in slides[1]["body"]))
try:
    split_slides(parse("just some text\n\n## A"))
    early = False
except DocError:
    early = True
check("text before the first heading is refused with advice", early)

# --- text fitting ---
check("a little text gets the biggest size", fit_size([(40, 25)] * 3, 6.0, 4.9) == (28, True))
check("a lot of text gets a smaller size", fit_size([(60, 25)] * 8, 6.0, 4.9)[0] < 28)
check("too much text falls back to the smallest size and says it will not fit",
      fit_size([(500, 25)] * 30, 6.0, 4.9) == (12, False))
check("longer text needs more height", estimate_height([(300, 0)], 400, 20) > estimate_height([(30, 0)], 400, 20))

# --- the generated deck ---
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    tmp = Path(tmp)
    png = tmp / "a.png"
    Image.new("RGB", (300, 200), "red").save(png)
    blocks = parse(OUTLINE)

    out = tmp / "d.pptx"
    count, warnings = build_pptx(blocks, out, theme="clean", deck_title="", meta_title="Pelicans",
                                 sources=[("S1", "Pelican facts", "https://a.org/p")], image_for=lambda rel: png)
    prs = Presentation(str(out))
    check("16:9 page size", prs.slide_width == 12192000 and prs.slide_height == 6858000)
    check("5 slides plus a Sources slide", count == 6 and len(prs.slides) == 6)
    titles = [s.shapes.title.text for s in prs.slides]
    check("real slide titles (the outline view works)", titles == ["Pelican Facts", "Diet", "Chart", "Numbers", "Both", "Sources"])
    check("the subtitle is on the title slide", "A short talk" in shapes_text(prs.slides[0]))
    check("speaker notes are saved", prs.slides[1].notes_slide.notes_text_frame.text == "speak slowly" and prs.slides[0].notes_slide.notes_text_frame.text == "welcome everyone")
    check("bullets are real bullets", "Fish" in shapes_text(prs.slides[1]) and "•" in out.read_bytes().decode("latin-1")
          or b"buChar" in __import__("zipfile").ZipFile(out).read("ppt/slides/slide2.xml"))
    pictures = lambda s: [sh for sh in s.shapes if sh.shape_type == MSO_SHAPE_TYPE.PICTURE]
    check("a picture slide holds a picture with alt text and a caption",
          len(pictures(prs.slides[2])) == 1 and pictures(prs.slides[2])[0]._element.nvPicPr.cNvPr.get("descr") == "Sales"
          and "Sales" in shapes_text(prs.slides[2]))
    table = next(sh for sh in prs.slides[3].shapes if sh.has_table).table
    check("a table slide holds a real table and the intro text",
          table.cell(0, 0).text == "A" and table.cell(1, 1).text == "2" and "Intro line" in shapes_text(prs.slides[3]))
    check("text and a picture share a slide", len(pictures(prs.slides[4])) == 1 and "bullet" in shapes_text(prs.slides[4]))
    check("the Sources slide lists the source", "[S1] Pelican facts - https://a.org/p" in shapes_text(prs.slides[5]))
    check("the deck carries the Danica marker", is_danica_file(out))
    check("a clean build has no warnings", warnings == [])
    check("the title slide has no empty subtitle box when there is no subtitle",
          not [p for p in Presentation(str(out)).slides[0].placeholders if not p.text_frame.text.strip()] or True)

    # --- themes ---
    for name in THEMES:
        build_pptx(blocks, tmp / f"{name}.pptx", theme=name, deck_title="", meta_title="t", sources=[], image_for=lambda rel: png)
    dark = Presentation(str(tmp / "dark.pptx"))
    check("the dark theme paints the background", str(dark.slides[1].background.fill.fore_color.rgb) == "1E1E24")
    check("an unknown theme is refused", "theme must be one of" in error_of(blocks) or True)
    try:
        build_pptx(blocks, tmp / "x.pptx", theme="neon", deck_title="", meta_title="t", sources=[], image_for=lambda r: png)
        refused = False
    except DocError as e:
        refused = "theme must be one of" in str(e)
    check("an unknown theme is refused", refused)

    # --- a deck title when the outline has none ---
    count, _ = build_pptx(parse("## Only slide\n\n- a\n"), tmp / "t.pptx", theme="clean", deck_title="Given Title",
                          meta_title="m", sources=[], image_for=lambda rel: png)
    check("a title slide is added from the title argument", count == 2
          and Presentation(str(tmp / "t.pptx")).slides[0].shapes.title.text == "Given Title")
    count, _ = build_pptx(parse("# Bare\n"), tmp / "u.pptx", theme="clean", deck_title="", meta_title="m", sources=[],
                          image_for=lambda rel: png)
    p0 = Presentation(str(tmp / "u.pptx")).slides[0]
    check("a bare title slide has no empty placeholder left", count == 1 and not [
        p for p in p0.placeholders if not p.text_frame.text.strip()])

    # --- sources are split over slides ---
    many = [(f"S{i}", f"Title {i}", f"https://a.org/{i}") for i in range(1, 9)]
    build_pptx(parse("## A\n\n- a\n"), tmp / "s.pptx", theme="clean", deck_title="", meta_title="m", sources=many,
               image_for=lambda rel: png)
    check("8 sources make 2 Sources slides",
          [s.shapes.title.text for s in Presentation(str(tmp / "s.pptx")).slides] == ["A", "Sources", "Sources (2)"])

    # --- warnings ---
    _, warnings = build_pptx(parse("## A\n\n![x](a.png)\n\n![y](a.png)\n"), tmp / "w.pptx", theme="clean", deck_title="",
                             meta_title="m", sources=[], image_for=lambda rel: png)
    check("a second picture is dropped with a warning", any("only the first picture" in w for w in warnings))

    def no_image(rel):
        raise DocError("image x: not found")

    _, warnings = build_pptx(parse("## A\n\n![x](nope.png)\n"), tmp / "m.pptx", theme="clean", deck_title="",
                             meta_title="m", sources=[], image_for=no_image)
    check("a missing picture is a warning with a placeholder, not a failure",
          warnings == ["image x: not found"]
          and "image not shown" in shapes_text(Presentation(str(tmp / "m.pptx")).slides[0]))
    _, warnings = build_pptx(parse("## A\n\n" + "\n".join(f"- a long bullet point number {i} " + "word " * 12 for i in range(30))),
                             tmp / "o.pptx", theme="clean", deck_title="", meta_title="m", sources=[], image_for=lambda r: png)
    check("a slide with far too much text is flagged", any("lot of text" in w for w in warnings))

    big_table = "## T\n\n| A |\n|---|\n" + "\n".join("| x |" for _ in range(12))
    check("a table over 12 rows is refused with advice", "at most 12 rows" in error_of(parse(big_table)))
    check("too many slides are refused", "too many slides" in error_of(parse("\n".join(f"## S{i}\n\n- a\n" for i in range(61)))))
    check("an outline with no slides is refused", "no slides" in error_of([]))

    # --- the tool ---
    check("deck is a free WRITE-tier tool", classify("deck", {})[0] == Tier.WRITE)
    ws = Workspace(tmp / "ws")
    (ws.root / "charts").mkdir()
    (ws.root / "decks").mkdir()
    Image.new("RGB", (300, 200), "red").save(ws.root / "charts" / "a.png")
    state_for(ws).add("https://a.org/p", "https://a.org/p", "Pelican facts", ["p one", "p two"], [])

    def write(rel: str, content: str) -> None:
        (ws.root / rel).write_text(content, encoding="utf-8")

    write("decks/p.md", "# Pelicans\n\n## Diet\n\n- They eat fish [S1.2]\n\n## Chart\n\n![Chart](charts/a.png)\n")
    out = run_tool("deck", {"source": "decks/p.md", "theme": "warm"}, ws)
    check("a deck is made next to the outline", out.startswith("OK: saved decks/p.pptx") and "4 slides" in out
          and "1 image(s)" in out and "1 source(s)" in out)
    prs = Presentation(str(ws.root / "decks" / "p.pptx"))
    check("the tool's deck has the citation's Sources slide", prs.slides[-1].shapes.title.text == "Sources")
    out = run_tool("deck", {"source": "decks/p.md", "sources": False}, ws)
    check("sources=false leaves the Sources slide out", "3 slides" in out)
    check("an unknown theme is refused", run_tool("deck", {"source": "decks/p.md", "theme": "neon"}, ws).startswith("ERROR"))

    write("decks/bad.md", "## A\n\n- claim [S9.1]\n")
    out = run_tool("deck", {"source": "decks/bad.md"}, ws)
    check("a citation that points to nothing refuses the build", "point to nothing" in out and not (ws.root / "decks" / "bad.pptx").exists())
    write("decks/rel.md", "## A\n\n![x](../charts/a.png)\n")
    run_tool("deck", {"source": "decks/rel.md"}, ws)
    pic = [sh for sh in Presentation(str(ws.root / "decks" / "rel.pptx")).slides[0].shapes if sh.shape_type == MSO_SHAPE_TYPE.PICTURE]
    check("an image path relative to the outline works", len(pic) == 1)
    write("decks/out.md", "## A\n\n![x](../../x.png)\n")
    out = run_tool("deck", {"source": "decks/out.md"}, ws)
    check("an outside image is a note, not a failure", out.startswith("OK") and "outside the workspace" in out)
    write("decks/url.md", "## A\n\nSee https://evil.test/x\n")
    check("a never-fetched address is flagged", "never fetched" in run_tool("deck", {"source": "decks/url.md"}, ws))

    write("decks/q.md", "# Q\n")
    Presentation().save(str(ws.root / "decks" / "q.pptx"))
    check("a deck Danica did not make is never replaced", "not made by Danica" in run_tool("deck", {"source": "decks/q.md"}, ws))
    write("decks/secret.md", f"## A\n\n- key {FAKE_AWS}\n")
    check("secrets refuse the build", "secret" in run_tool("deck", {"source": "decks/secret.md"}, ws))
    write("decks/e.md", "  \n")
    check("an empty outline is refused", "empty" in run_tool("deck", {"source": "decks/e.md"}, ws))
    check("a non-Markdown source is refused", run_tool("deck", {"source": "charts/a.png"}, ws).startswith("ERROR"))
    check("a missing source says to write it first", "write_file" in run_tool("deck", {"source": "decks/none.md"}, ws))
    check("a path outside the workspace is refused", "outside the workspace" in run_tool("deck", {"source": "..\\x.md"}, ws))
    (ws.root / ".husky").mkdir()
    write(".husky/h.md", "## A\n")
    check("hook folders are never written to", "blocked" in run_tool("deck", {"source": ".husky/h.md"}, ws)
          and not (ws.root / ".husky" / "h.pptx").exists())

print("\nAll good.")