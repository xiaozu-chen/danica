"""
document.py - The document tool: make a Word or PDF file from a Markdown file the model wrote.

The model writes Markdown (with write_file); this tool validates it and draws the file with
the generators in docgen.py. It checks [S#.N] citations against the pages really fetched
(a bad id refuses the build) and appends a Sources section itself.
"""

from danica.citations import verify
from danica.config import CONFIG
from danica.docgen import build_docx, build_pdf, is_danica_file
from danica.imageview import ImageError, check_image
from danica.markdown_doc import MAX_CHARS, DocError, first_heading, parse, strip_invisible
from danica.secret_scan import find_secrets, is_sensitive_path
from danica.tools.files import _write_guard
from danica.tools.registry import tool
from danica.workspace import Workspace
from danica.present import TAG_RX

MAX_SOURCE_BYTES = MAX_CHARS * 4        # a Markdown file can hold multi-byte characters


@tool(
    "document",
    "Make a Word (.docx) or PDF document from a Markdown file you wrote with write_file (for example "
    "reports/summary.md). Markdown supports # headings, **bold**, *italic*, bullet and numbered lists, "
    "| tables |, > quotes, code blocks, and ![caption](charts/sales.png) to embed a chart you already made. "
    "Cite web sources like [S1.2]: the tool checks them and adds a verified Sources section itself, so do "
    "NOT write one. The document is saved next to the Markdown file. Never write docx or PDF code yourself.",
    {
        "source": {"type": "string", "description": "Path of the Markdown file, for example reports/summary.md"},
        "format": {"type": "string", "enum": ["docx", "pdf"], "description": "Output format", "default": "docx"},
        "title": {"type": "string", "description": "Title shown at the top (optional)", "default": ""},
        "sources": {"type": "boolean", "description": "Append the verified Sources list", "default": True},
    },
)
def document(ws: Workspace, source: str, format: str = "docx", title: str = "", sources: bool = True) -> str:
    fmt = str(format).lower().lstrip(".")
    if fmt not in ("docx", "pdf"):
        return "ERROR: format must be docx or pdf"
    p = ws.resolve(source)
    if p.suffix.lower() not in (".md", ".markdown", ".txt"):
        return "ERROR: the source must be a Markdown file (.md). Write it with write_file first."
    if not p.is_file():
        return f"ERROR: '{source}' is not a file. Write the Markdown with write_file first."
    if is_sensitive_path(p.relative_to(ws.root)):
        return "ERROR: that file is blocked: it looks like a credentials file"
    if p.stat().st_size > MAX_SOURCE_BYTES:
        return f"ERROR: the Markdown file is too big (limit {MAX_CHARS:,} characters)"

    text = strip_invisible(p.read_text(encoding="utf-8-sig", errors="replace"))
    if find_secrets(text):
        return "ERROR: refused: the text contains what looks like a secret (key, token or password). Remove it."
    try:
        blocks = parse(text)
    except DocError as e:
        return f"ERROR: {e}"
    if not blocks:
        return "ERROR: the Markdown file is empty"

    # --- citations: every [S#.N] must point at a page that was really fetched ---
    state = ws.web
    report = verify(text, state)
    if report.invalid:
        return (f"ERROR: these citations point to nothing: {', '.join(report.invalid)}. Fix or remove "
                f"them (use only ids that web_fetch returned), then call document again.")
    listing = []
    if sources and report.cited:
        for sid in report.cited:
            src = state.by_id[sid]
            listing.append((sid, " ".join(src.title.split())[:150] or "(no title)", src.final_url or src.url))

    # --- where it goes, and what it may replace ---
    out = p.with_suffix("." + fmt)
    guarded = _write_guard(ws, out)                  # never into .git, .husky or .githooks
    if guarded:
        return guarded
    if out.exists() and (not out.is_file() or not is_danica_file(out)):
        return (f"ERROR: '{ws.display(out)}' already exists and was not made by Danica, so it was not "
                f"replaced. Use a different file name for the Markdown source.")

    md_dir = ws.display(p.parent)

    def image_for(rel: str):
        """The real path of an image the Markdown mentions, or DocError (becomes a warning)."""
        if "://" in rel:
            raise DocError(f"image {rel[:60]}: remote images are not downloaded")
        candidates = [rel] + ([f"{md_dir}/{rel}"] if md_dir != "." else [])     # workspace-relative, then next to the .md
        last = "not found"
        for candidate in candidates:
            try:
                return check_image(ws, candidate)
            except ImageError as e:
                last = str(e)
        raise DocError(f"image {rel[:60]}: {last}")

    page = "LETTER" if str(CONFIG["documents"]["page"]).upper() == "LETTER" else "A4"
    clean_title = " ".join(str(title).split())[:120]
    build = build_docx if fmt == "docx" else build_pdf
    try:
        warnings = build(blocks, out, title=clean_title, meta_title=clean_title or first_heading(blocks) or p.stem,
                         sources=listing, page=page, image_for=image_for)
    except DocError as e:
        return f"ERROR: {e}"
    if TAG_RX.search(text):
        warnings.append("<present> tags were turned into images. In documents write ![caption](path) "
                        "instead: <present> is only for your chat answer")
    if report.unfetched_urls:
        warnings.append("these web addresses in the text were never fetched, so they are unverified: "
                        + ", ".join(report.unfetched_urls))
    images = sum(1 for b in blocks if b["t"] == "img")
    shown = ws.display(out)
    result = (f"OK: saved {shown} ({max(1, out.stat().st_size // 1024)} KB, {len(blocks)} sections, "
              f"{images} image(s), {len(listing)} source(s)). Tell the user where it is.")
    if warnings:
        result += "\nNotes: " + "; ".join(warnings)
    return result