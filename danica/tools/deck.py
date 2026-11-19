"""
deck.py - The deck tool: make a PowerPoint file or an HTML slideshow from a Markdown outline.
Same safety rules as the document tool: secrets refuse the build, citations are verified
(a bad id refuses it) and the Sources slides are added here, images must be real PNG/JPEG
files inside the workspace, and only Danica's own files are ever replaced (for .html: files
whose signature verifies, see seal.py).
"""

from danica.citations import verify
from danica.deckgen import THEMES, build_pptx
from danica.docgen import image_resolver, is_danica_file
from danica.markdown_doc import (
    MAX_CHARS,
    DocError,
    first_heading,
    parse,
    strip_invisible,
)
from danica.secret_scan import find_secrets, is_sensitive_path
from danica.slideshow import build_html
from danica.tools.files import _write_guard
from danica.tools.registry import tool
from danica.workspace import Workspace

MAX_SOURCE_BYTES = MAX_CHARS * 4


@tool(
    "deck",
    "Make a PowerPoint (.pptx) slide deck, or a single-file HTML slideshow, from a Markdown outline you "
    "wrote with write_file (for example decks/pitch.md). '# Title' makes the title slide (text under it is "
    "the subtitle). Each '## Heading' starts a slide: put at most 6 short bullets (- item, 2 levels) or a "
    "short paragraph under it, and at most ONE picture ![caption](charts/sales.png) or ONE | table | (12 "
    "rows max). A line starting 'Notes:' becomes speaker notes. Cite web sources as [S1.2]: they are "
    "verified and a Sources slide is added by the tool, so do NOT write one. Themes: clean, dark, warm, "
    "mono. format 'pptx' (default) makes PowerPoint; format 'html' makes a slideshow that opens in a "
    "browser (arrow keys, f = fullscreen, n = notes). Saved next to the Markdown file. Never write pptx or "
    "HTML code yourself.",
    {
        "source": {"type": "string", "description": "Path of the Markdown outline, for example decks/pitch.md"},
        "theme": {"type": "string", "enum": ["clean", "dark", "warm", "mono"], "description": "Look of the slides",
                  "default": "clean"},
        "title": {"type": "string", "description": "Deck title if the outline has no # heading (optional)", "default": ""},
        "sources": {"type": "boolean", "description": "Append the verified Sources slides", "default": True},
        "format": {"type": "string", "enum": ["pptx", "html"], "description": "PowerPoint file or HTML slideshow",
                   "default": "pptx"},
    },
)
def deck(ws: Workspace, source: str, theme: str = "clean", title: str = "", sources: bool = True,
         format: str = "pptx") -> str:
    """Make a PowerPoint (.pptx) slide deck, or a single-file HTML slideshow, from
        a Markdown outline you wrote with write_file (for example decks/pitch.md).
        '# Title' makes the title slide (text under it is the subtitle).
    """
    fmt = str(format).lower().lstrip(".")
    if fmt not in ("pptx", "html"):
        return "ERROR: format must be pptx or html"
    theme = str(theme).lower()
    if theme not in THEMES:
        return "ERROR: theme must be one of: " + ", ".join(THEMES)
    p = ws.resolve(source)
    if p.suffix.lower() not in (".md", ".markdown", ".txt"):
        return "ERROR: the source must be a Markdown outline (.md). Write it with write_file first."
    if not p.is_file():
        return f"ERROR: '{source}' is not a file. Write the Markdown outline with write_file first."
    if is_sensitive_path(ws.rel(p)):
        return "ERROR: that file is blocked: it looks like a credentials file"
    if p.stat().st_size > MAX_SOURCE_BYTES:
        return f"ERROR: the outline is too big (limit {MAX_CHARS:,} characters)"

    text = strip_invisible(p.read_text(encoding="utf-8-sig", errors="replace"))
    if find_secrets(text):
        return "ERROR: refused: the text contains what looks like a secret (key, token or password). Remove it."
    try:
        blocks = parse(text)
    except DocError as e:
        return f"ERROR: {e}"
    if not blocks:
        return "ERROR: the outline is empty"

    report = verify(text, ws.web)
    if report.invalid:
        return (f"ERROR: these citations point to nothing: {', '.join(report.invalid)}. Fix or remove "
                f"them (use only ids that web_fetch returned), then call deck again.")
    listing = []
    if sources and report.cited:
        for sid in report.cited:
            src = ws.web.by_id[sid]
            listing.append((sid, " ".join(src.title.split())[:100] or "(no title)", src.final_url or src.url))

    out = p.with_suffix("." + fmt)
    guarded = _write_guard(ws, out)                  # never into .git, .husky or .githooks
    if guarded:
        return guarded
    if out.exists() and (not out.is_file() or not is_danica_file(out)):
        return (f"ERROR: '{ws.display(out)}' already exists and was not made by Danica, so it was not "
                f"replaced. Use a different file name for the outline.")

    clean_title = " ".join(str(title).split())[:120]
    build = build_pptx if fmt == "pptx" else build_html
    try:
        count, warnings = build(blocks, out, theme=theme, deck_title=clean_title,
            meta_title=clean_title or first_heading(blocks) or p.stem,
                                sources=listing, image_for=image_resolver(ws, p))
    except DocError as e:
        return f"ERROR: {e}"

    if report.unfetched_urls:
        warnings.append("these web addresses in the text were never fetched, so they are unverified: "
                        + ", ".join(report.unfetched_urls))
    images = sum(1 for b in blocks if b["t"] == "img")
    shown = ws.display(out)
    result = (f"OUTPUT: saved {shown} ({max(1, out.stat().st_size // 1024)} KB, {count} slides, "
              f"{images} image(s), {len(listing)} source(s)). Tell the user where it is.")
    if warnings:
        result += "\nNotes: " + "; ".join(warnings)
    return result
