"""
markdown_doc.py - A small Markdown reader for the document generators.

parse() turns Markdown text into a list of blocks (dicts). Both generators (docgen.py)
draw from the SAME blocks, so a Word file and a PDF always say the same thing.
    {"t": "h", "level": 1-3, "runs": [...]}        {"t": "p", "runs": [...]}
    {"t": "ul" | "ol", "items": [(level, runs)]}   {"t": "code", "text": "..."}
    {"t": "quote", "runs": [...]}                  {"t": "hr"}
    {"t": "table", "header": [runs], "rows": [[runs]]}
    {"t": "img", "alt": "...", "path": "..."}
A "run" is (text, flags) where flags is a frozenset of "b" (bold), "i" (italic), "code".

NOT supported (treated as plain paragraphs): indented code blocks, raw HTML, nested
quotes, task lists, footnotes. Text is never interpreted as markup by the generators.
"""

import re
import unicodedata

from danica.present import TAG_RX

MAX_CHARS = 300_000
MAX_BLOCKS = 3000
MAX_TABLE_ROWS = 200
MAX_LIST_ITEMS = 500

TICKS = "`" * 3     # a code fence; built this way so this file shows up cleanly in chat


class DocError(Exception):
    """A problem whose message is safe to show the model and the user."""


def strip_invisible(text: str) -> str:
    """Normalise line endings and remove control and invisible characters (keeping \\n and \\t)."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return "".join(ch for ch in text
                   if ch in "\n\t" or unicodedata.category(ch) not in ("Cc", "Cf", "Co", "Cn", "Cs"))


# ------------------------------------------------------------------- inline

INLINE_RX = re.compile(
    r"`([^`\n]+)`"                        # 1: code
    r"|\*\*(?=\S)(.+?)(?<=\S)\*\*"        # 2: bold
    r"|\*(?=\S)(.+?)(?<=\S)\*"            # 3: italic (only *, so snake_case words are safe)
    r"|\[([^\]\n]+)\]\(([^)\s]+)\)")      # 4, 5: link label and address


def parse_inline(text: str, style: frozenset = frozenset(), depth: int = 0) -> list[tuple[str, frozenset]]:
    runs: list[tuple[str, frozenset]] = []
    pos = 0

    def inner(part: str, flags: frozenset) -> list[tuple[str, frozenset]]:
        return parse_inline(part, flags, depth + 1) if depth < 3 else [(part, flags)]

    for m in INLINE_RX.finditer(text):
        if m.start() > pos:
            runs.append((text[pos:m.start()], style))
        pos = m.end()
        if m.group(1) is not None:
            runs.append((m.group(1), style | {"code"}))
        elif m.group(2) is not None:
            runs += inner(m.group(2), style | {"b"})
        elif m.group(3) is not None:
            runs += inner(m.group(3), style | {"i"})
        else:
            runs.append((m.group(4), style))
            if m.group(5) != m.group(4):
                runs.append((f" ({m.group(5)})", style))       # the address always stays visible
    if pos < len(text):
        runs.append((text[pos:], style))
    return [r for r in runs if r[0]]


# -------------------------------------------------------------------- blocks

FENCE_RX = re.compile(r"^\s*(" + TICKS + r"|~~~)")
HEADING_RX = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
HR_RX = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
TABLE_SEP_RX = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
IMAGE_RX = re.compile(r"^\s*!\[([^\]]*)\]\(([^)\s]+)\)\s*$")
ITEM_RX = re.compile(r"^(\s*)([-*+]|\d{1,3}[.)])\s+(.*)$")


def _cells(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]

def _tag_to_image(m) -> str:
    """A <present file="x.png"/> tag found inside a document means "put this picture here".
    It goes on its own lines so it becomes an image block. A path the image syntax can't hold
    (spaces or ")") is left alone as text."""
    path = m.group(1).strip()
    if re.search(r"[\s)]", path):
        return m.group(0)
    return f"\n\n![]({path})\n\n"

def parse(md: str) -> list[dict]:
    """Markdown text -> blocks. Raises DocError if the text is too big."""
    md = strip_invisible(md)
    if len(md) > MAX_CHARS:
        raise DocError(f"the document is too long (limit {MAX_CHARS:,} characters)")
    # Models often copy the chat-only <present .../> tag into a document after making a chart
    md = TAG_RX.sub(_tag_to_image, md)
    lines = md.split("\n")
    blocks: list[dict] = []
    para: list[str] = []

    def flush() -> None:
        if para:
            blocks.append({"t": "p", "runs": parse_inline(" ".join(s.strip() for s in para))})
            para.clear()

    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        s = line.strip()

        m = FENCE_RX.match(line)
        if m:                                                    # fenced code block
            flush()
            fence, code = m.group(1), []
            i += 1
            while i < n and not lines[i].strip().startswith(fence):
                code.append(lines[i])
                i += 1
            i += 1                                               # the closing fence (or the end)
            blocks.append({"t": "code", "text": "\n".join(code)})
            continue
        if not s:
            flush()
            i += 1
            continue
        m = HEADING_RX.match(s)
        if m:
            flush()
            blocks.append({"t": "h", "level": min(len(m.group(1)), 3), "runs": parse_inline(m.group(2))})
            i += 1
            continue
        if HR_RX.match(s):
            flush()
            blocks.append({"t": "hr"})
            i += 1
            continue
        if "|" in s and i + 1 < n and TABLE_SEP_RX.match(lines[i + 1]):
            flush()
            header = _cells(s)
            i += 2
            rows = []
            while i < n and lines[i].strip() and "|" in lines[i]:
                rows.append(_cells(lines[i]))
                i += 1
                if len(rows) > MAX_TABLE_ROWS:
                    raise DocError(f"a table has too many rows (limit {MAX_TABLE_ROWS})")
            width = len(header)
            blocks.append({"t": "table", "header": [parse_inline(c) for c in header],
                           "rows": [[parse_inline(c) for c in (r + [""] * width)[:width]] for r in rows]})
            continue
        m = IMAGE_RX.match(s)
        if m:
            flush()
            blocks.append({"t": "img", "alt": m.group(1).strip(), "path": m.group(2)})
            i += 1
            continue
        if s.startswith(">"):
            flush()
            quote = []
            while i < n and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip().lstrip(">").strip())
                i += 1
            blocks.append({"t": "quote", "runs": parse_inline(" ".join(q for q in quote if q))})
            continue
        m = ITEM_RX.match(line)
        if m:
            flush()
            ordered, items = m.group(2)[0].isdigit(), []
            while i < n:
                mm = ITEM_RX.match(lines[i])
                if not mm:
                    break
                level = min(len(mm.group(1).expandtabs(2)) // 2, 2)      # 2 spaces per nesting level
                items.append((level, parse_inline(mm.group(3))))
                i += 1
                if len(items) > MAX_LIST_ITEMS:
                    raise DocError(f"a list has too many items (limit {MAX_LIST_ITEMS})")
            blocks.append({"t": "ol" if ordered else "ul", "items": items})
            continue
        para.append(line)
        i += 1
    flush()
    if len(blocks) > MAX_BLOCKS:
        raise DocError(f"the document has too many sections (limit {MAX_BLOCKS})")
    return blocks


def labelled(block: dict) -> list[tuple[int, str, list]]:
    """List items with their marker: (level, "1." or a bullet, runs). Numbers restart per list."""
    counts, out = [0, 0, 0], []
    for level, runs in block["items"]:
        counts[level] += 1
        for deeper in range(level + 1, 3):
            counts[deeper] = 0
        label = f"{counts[level]}." if block["t"] == "ol" else ("•" if level == 0 else "–")
        out.append((level, label, runs))
    return out


def first_heading(blocks: list[dict]) -> str:
    for b in blocks:
        if b["t"] == "h":
            return "".join(t for t, _ in b["runs"])[:120]
    return ""