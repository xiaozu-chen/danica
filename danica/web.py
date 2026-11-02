"""
web.py - Turning raw web content into safe, citable text. NO network code here,
so everything in this file can be tested offline.

Pipeline:  html_to_text() -> clean_text() -> split_passages() -> WebState.add()
Output of the web tools is ALWAYS treated as untrusted data (see render()).

HONEST LIMITS:
  - Hidden-text detection reads inline styles and attributes only. Text hidden
    through an external stylesheet or by JavaScript is not detected. That is why
    the real defence is wrapping + taint tracking, not this filter.
  - Pages that need JavaScript to show their content come back (nearly) empty.
  - Link URLs are not kept (only the text), to save context.
"""

import re
import time
import unicodedata
from dataclasses import dataclass, field
from html.parser import HTMLParser

PASSAGE_CHARS = 1000        # target size of one citable passage
MAX_TEXT_CHARS = 200_000    # cap on the text kept from one page
CACHE_SECONDS = 900         # how long a fetched page stays cached
MAX_DEPTH = 300             # deeper HTML nesting is refused (fail closed)
RENDER_BUDGET = 6000        # max characters of passages returned per tool call

# Tags whose whole content is dropped (never useful as reading text)
SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "iframe", "object",
             "embed", "canvas", "nav", "footer", "aside", "form"}
# Tags with no closing tag
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
             "meta", "param", "source", "track", "wbr"}
HEADINGS = {f"h{n}": "#" * n for n in range(1, 7)}
BLOCK_TAGS = {"p", "div", "li", "ul", "ol", "tr", "table", "section", "article",
              "blockquote", "pre", "header", "main", "figure", "figcaption",
              "dl", "dt", "dd", "br", "hr"} | set(HEADINGS)

# Inline styles that hide text from a human reader
HIDDEN_STYLE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden"
    r"|font-size\s*:\s*0(?![\d.])|opacity\s*:\s*0(?![\d.])"
    r"|text-indent\s*:\s*-\d{3,}|(?:left|top)\s*:\s*-\d{4,}",
    re.IGNORECASE)

# A page must never be able to fake the end of our untrusted-content wrapper
WRAPPER_RX = re.compile(r"<\s*/?\s*untrusted_web_content", re.IGNORECASE)


# ------------------------------------------------------------- HTML to text

class _Extractor(HTMLParser):
    """Collects visible text. Keeps a stack of open tags so that everything
    inside a hidden or skipped element is dropped, however deeply nested."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self.stack: list[tuple[str, str]] = []   # (tag, kind) kind: "" visible, "skip", "hidden"
        self.suppress = 0        # how many open elements currently hide their content
        self.hidden_n = 0        # how many of those are "hidden" (not just skipped)
        self.hidden_chars = 0    # characters of hidden text we removed (reported to the user)
        self.in_title = False
        self.overflow = False    # nesting too deep: stop extracting

    @staticmethod
    def _hidden(attrs: dict) -> bool:
        return ("hidden" in attrs
                or attrs.get("aria-hidden", "").strip().lower() == "true"
                or bool(HIDDEN_STYLE.search(attrs.get("style", ""))))

    def handle_starttag(self, tag, attrs):
        # Only the first <title> outside hidden/skipped elements is the page title
        if tag == "title" and not self.suppress and not self.title_parts:
            self.in_title = True
            return
        if self.overflow:
            return
        a = {k: (v or "") for k, v in attrs}
        if tag in VOID_TAGS:
            if tag in BLOCK_TAGS and not self.suppress:
                self.parts.append("\n")
            return
        if len(self.stack) >= MAX_DEPTH:
            # Absurd nesting is how an attacker would hide text from this
            # tracker, so stop reading the page instead of guessing.
            self.overflow = True
            return
        kind = "skip" if tag in SKIP_TAGS else ("hidden" if self._hidden(a) else "")
        self.stack.append((tag, kind))
        if kind:
            self.suppress += 1
            if kind == "hidden":
                self.hidden_n += 1
        elif tag in HEADINGS:
            self.parts.append("\n\n" + HEADINGS[tag] + " ")
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
            return
        # Close the nearest matching open tag (and anything left open inside it)
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                for _, kind in self.stack[i:]:
                    if kind:
                        self.suppress -= 1
                        if kind == "hidden":
                            self.hidden_n -= 1
                del self.stack[i:]
                if not self.suppress:
                    self.parts.append("\n\n" if tag in HEADINGS else ("\n" if tag in BLOCK_TAGS else ""))
                return
        # an end tag with no matching start tag is ignored

    def handle_data(self, data):
        if self.in_title:
            self.title_parts.append(data)
            return
        if self.overflow:
            return
        if self.suppress:
            if self.hidden_n:
                self.hidden_chars += len(data.strip())
            return
        self.parts.append(data)
    # Comments, processing instructions and declarations are never overridden,
    # so HTMLParser silently drops them. That is exactly what we want.


def html_to_text(html: str) -> tuple[str, str, int, bool]:
    """Return (text, title, hidden_chars_removed, nesting_overflow)."""
    ex = _Extractor()
    try:
        ex.feed(html)
        ex.close()
    except Exception:
        pass   # broken HTML: keep whatever was extracted so far
    return "".join(ex.parts), " ".join("".join(ex.title_parts).split()), ex.hidden_chars, ex.overflow


# --------------------------------------------------------------- sanitizing

def clean_text(text: str) -> tuple[str, int]:
    """Remove invisible and control characters, tidy whitespace, neutralize
    wrapper tags. Returns (clean_text, invisible_characters_removed).
    Side effect: joiners such as ZWJ (emoji sequences) and ZWNJ (Persian) are
    removed too. Accepted: invisible characters are a known injection channel."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    out, removed = [], 0
    for ch in text:
        if ch in "\n\t":
            out.append(ch)
        elif unicodedata.category(ch) in ("Cc", "Cf", "Co", "Cn", "Cs"):
            removed += 1     # controls, format chars (zero-width, bidi, tag chars), private use
        elif ch in "\u2028\u2029":
            out.append("\n")
        else:
            out.append(ch)
    text = "".join(out)

    lines = [re.sub(r"[ \t\u00a0]+", " ", ln).strip() for ln in text.split("\n")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    # Runs AFTER invisible characters are gone, so "</untr\u200busted..." can't slip through
    text = WRAPPER_RX.sub("[removed tag]", text)
    return text, removed


def split_passages(text: str, size: int = PASSAGE_CHARS) -> list[str]:
    """Pack paragraphs into passages of at most `size` characters."""
    passages: list[str] = []
    cur = ""
    for para in text.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        while len(para) > size:               # one huge paragraph: hard split at a space
            cut = para.rfind(" ", 0, size)
            cut = cut if cut > size // 2 else size
            piece, para = para[:cut], para[cut:].lstrip()
            if cur:
                passages.append(cur)
                cur = ""
            passages.append(piece)
        if cur and len(cur) + len(para) + 2 > size:
            passages.append(cur)
            cur = para
        else:
            cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        passages.append(cur)
    return passages


def safe_attr(text: str) -> str:
    """Make untrusted text safe to place inside a tag attribute."""
    return re.sub(r"[^\w .,:/?#@!$&'()*+;=%~|-]", "", text)[:150]


# ---------------------------------------------------------- sources and cache

@dataclass
class Source:
    id: str                     # "S1": what the model cites as [S1.3]
    url: str                    # the URL that was requested (cache key)
    final_url: str = ""         # where it ended up after redirects
    title: str = ""
    passages: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)   # harness-written remarks about the fetch
    fetched: float = 0.0


class WebState:
    """Everything the web tools remember during one session."""

    def __init__(self) -> None:
        self.by_id: dict[str, Source] = {}
        self.by_url: dict[str, Source] = {}
        self.counter = 0
        # Taint tracking (enforced in the next chunk): untrusted text is in context
        self.tainted = False
        self.taint_sources: list[str] = []

    @staticmethod
    def key(url: str) -> str:
        return url.split("#", 1)[0]    # a #fragment never changes what the server sends

    def lookup(self, url: str) -> Source | None:
        """A fresh cached page, or None."""
        src = self.by_url.get(self.key(url))
        if src and time.time() - src.fetched < CACHE_SECONDS:
            return src
        return None

    def add(self, url: str, final_url: str, title: str, passages: list[str], notes: list[str]) -> Source:
        key = self.key(url)
        src = self.by_url.get(key)
        if src is None:
            self.counter += 1
            src = Source(id=f"S{self.counter}", url=key)
            self.by_url[key] = src
            self.by_id[src.id] = src
        # A stale page refetched later keeps its id, so old citations stay valid
        src.final_url, src.title, src.passages = final_url, title, passages
        src.notes, src.fetched = notes, time.time()
        return src

    def mark_tainted(self, url: str) -> None:
        self.tainted = True
        if url not in self.taint_sources:
            self.taint_sources.append(url)


def render(src: Source, start: int, budget: int = RENDER_BUDGET) -> tuple[str, int]:
    """Passages from number `start` onward, up to `budget` characters.
    Returns (text, number_of_the_last_passage_shown). At least one is always shown."""
    out, used, i = [], 0, start
    while i <= len(src.passages):
        line = f"[{src.id}.{i}] {src.passages[i - 1]}"
        if out and used + len(line) > budget:
            break
        out.append(line)
        used += len(line) + 2
        i += 1
    return "\n\n".join(out), i - 1