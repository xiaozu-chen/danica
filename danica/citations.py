"""
citations.py - Checks the [S#.N] citations in an answer against the real sources.

WHAT IT PROVES: every cited id exists, and every cited passage number is inside
that page. The Sources list is built from pages the harness really fetched,
never from text the model wrote.
WHAT IT DOES NOT PROVE: that the passage actually says what the answer claims.
That needs a reading pass (Phase 8, self-reflection).
"""

import re
from dataclasses import dataclass, field

from danica.web import URL_RX, WebState

# A bracket group like [S1.2] or [S1.1, S1.3] or [S1.2-4]. Anything else in
# brackets ([1], [note], [Section 3]) is ordinary text and is ignored.
BRACKET_RX = re.compile(r"\[([^\[\]\n]{1,80})\]")
GROUP_RX = re.compile(r"(?:\s*S\d+(?:\.\d+(?:-\d+)?)?\s*[,;]?)+")
TOKEN_RX = re.compile(r"S(\d+)(?:\.(\d+)(?:-(\d+))?)?")
CODE_RX = re.compile(r"```.*?```|`[^`\n]*`", re.DOTALL)   # code is not prose: skip it
MAX_UNFETCHED = 3     # how many unfetched URLs to list (keeps the notice short)


@dataclass
class CitationReport:
    """Store citation validation results for a response."""

    cited: list[str] = field(default_factory=list)            # valid source ids, first-cited first
    invalid: list[str] = field(default_factory=list)          # citations that point to nothing
    unfetched_urls: list[str] = field(default_factory=list)   # URLs in the answer never fetched
    count: int = 0                                            # number of valid citations


def _norm(url: str) -> str:
    """Comparison form of a URL: no #fragment, no trailing slash."""
    return url.split("#", 1)[0].rstrip("/")


def _urls(text: str) -> list[str]:
    """URLs in text, minus punctuation that belongs to the sentence."""
    return [u.rstrip(".,;:!?*") for u in URL_RX.findall(text)]


def verify(answer: str, state: WebState | None, ignore_text: str = "") -> CitationReport:
    """Check one answer. ignore_text = what the USER wrote: URLs they typed
    are not suspicious, so they are never flagged."""
    report = CitationReport()
    sources = state.by_id if state else {}
    prose = CODE_RX.sub("", answer)

    for group in BRACKET_RX.finditer(prose):
        inner = group.group(1)
        if not GROUP_RX.fullmatch(inner):
            continue
        for m in TOKEN_RX.finditer(inner):
            src = sources.get("S" + m.group(1))
            ok = src is not None
            if ok and m.group(2):                        # a passage number was given
                first = int(m.group(2))
                last = int(m.group(3) or first)
                ok = 1 <= first <= last <= len(src.passages)
            if not ok:
                if m.group(0) not in report.invalid:
                    report.invalid.append(m.group(0))
                continue
            report.count += 1
            if src.id not in report.cited:
                report.cited.append(src.id)

    # URLs that were never fetched: from search snippets (not citable) or invented
    known = {_norm(u) for u in _urls(ignore_text)}
    for src in sources.values():
        known.update((_norm(src.url), _norm(src.final_url)))
    for url in _urls(prose):
        if _norm(url) not in known and url not in report.unfetched_urls:
            report.unfetched_urls.append(url)
    del report.unfetched_urls[MAX_UNFETCHED:]
    return report


def build_notice(report: CitationReport, state: WebState | None, web_used: bool) -> str:
    """The text shown after an answer. "" means there is nothing to say."""
    lines = []
    if report.cited:
        lines.append("Sources (verified: these pages were fetched in this session):")
        for sid in report.cited:
            src = state.by_id[sid]
            title = " ".join(src.title.split())[:100] or "(no title)"
            lines.append(f"  [{sid}] {title} - {src.final_url or src.url}")
    if report.invalid:
        lines.append("WARNING: these citations point to nothing: " + ", ".join(report.invalid)
                     + ". The model may have made them up.")
    if report.unfetched_urls:
        lines.append("WARNING: URL(s) in the answer that were never fetched (unverified; they may "
                     "come from search snippets or be invented): " + ", ".join(report.unfetched_urls))
    if web_used and not report.cited:
        lines.append("WARNING: this answer used web tools but cites no passage ids like [S1.2]. "
                     "Treat its claims as unverified.")
    return "\n".join(lines)
