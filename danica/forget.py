"""
forget.py - Forgetting a memory note for real, then CHECKING that it worked.

Steps: delete the page file (and every copy with the same uuid) -> purge the index rows,
text index and vectors -> rebuild facts.db from live data only (old file zero-filled) ->
verify. Every verification line is a real check, shown to the user as ok/FAIL.

WHAT FORGET DOES NOT TOUCH (and reports instead): session transcripts, summaries, TODO
files and exports that quote the note; the audit log (editing it would break its hash
chain); backups; copies in memory folders that are not open (other projects).
HONEST LIMIT: zero-filling is best effort (SSDs, journals, shadow copies).
"""

from dataclasses import dataclass, field
from pathlib import Path

from danica.export import EXPORTS_DIR
from danica.memory import MemoryStore, MemoryStoreError, Page
from danica.memory_index import query_terms
from danica.sessions import SESSIONS_DIR

MAX_RAW = 200_000_000     # database files bigger than this are not read into memory
MAX_SCAN_FILES = 300      # files searched for mentions
MAX_SCAN_BYTES = 5_000_000
MIN_NEEDLE = 12           # shorter notes are too generic to search other files for
WORD_PREFIX = 6           # distinctive words are compared by their first 6 letters


@dataclass
class ForgetReport:
    """Report whether a forgotten note was removed from stored sources."""

    checks: list[tuple[str, bool, str]] = field(default_factory=list)   # (name, passed, detail)
    mentions: list[str] = field(default_factory=list)                   # files that still quote the note

    @property
    def ok(self) -> bool:
        """Return whether every deletion verification check passed."""
        return all(passed for _, passed, _ in self.checks)


def _raw_bytes(path: Path) -> bytes:
    """The database file plus any journal/WAL leftovers, as raw bytes."""
    data = b""
    for p in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-journal")):
        try:
            if p.is_file() and p.stat().st_size <= MAX_RAW:
                data += p.read_bytes()
        except OSError:
            pass
    return data


def find_mentions(text: str, roots: list[Path]) -> list[str]:
    """Files under roots (transcripts, summaries, TODO files, exports) that still contain
    the note's text, ignoring case and spacing. Read-only: nothing is changed."""
    needle = " ".join(text.lower().split())[:80]
    if len(needle) < MIN_NEEDLE:
        return []
    files: set[Path] = set()
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        for pattern in ("*/transcript.jsonl", "*/summaries/*.md", "*/TODO.md", "*/*.md"):
            files.update(root.glob(pattern))
    found = []
    for f in sorted(files)[:MAX_SCAN_FILES]:
        try:
            if not f.is_file() or f.is_symlink() or f.stat().st_size > MAX_SCAN_BYTES:
                continue
            content = " ".join(f.read_text(encoding="utf-8", errors="ignore").lower().split())
        except OSError:
            continue
        if needle in content:
            found.append(str(f))
    return found


def forget(
    store: MemoryStore, page: Page, roots: list[Path] | None = None
) -> ForgetReport:
    """Forget one note everywhere Danica keeps it, and verify. Raises MemoryStoreError only
    if a note FILE can't be deleted; every later problem is reported as a failed check."""
    report = ForgetReport()

    def add(name: str, ok: bool, detail: str = "") -> None:
        report.checks.append((name, ok, detail))

    copies = store.pages_with_uuid(page.uuid) or [page]      # a copied file has the same uuid
    for copy in copies:
        store.delete(copy)

    index = store.index
    rowids: list[int] = []
    if index is not None:
        try:
            rowids = index.purge([page.uuid])
            index.rebuild_file()
            add("search index rebuilt from the remaining notes only", True)
        except MemoryStoreError as e:
            add("search index rebuilt from the remaining notes only", False, str(e))

    add("note file(s) deleted", not any(Path(c.path).exists() for c in copies),
        f"{len(copies)} copies" if len(copies) > 1 else "")
    add("no note with that id is left in the memory folders", not store.pages_with_uuid(page.uuid))

    if index is not None:
        try:
            left = index.leftovers([page.uuid], rowids)
            add("no database rows are left (notes, text index, vectors)", not any(left.values()),
                ", ".join(f"{k}: {v}" for k, v in left.items() if v))

            raw = _raw_bytes(index.path)
            needles = {"its text": page.body, "its title": page.title, "its id": page.uuid}
            found = [name for name, n in needles.items() if len(n) >= 4 and n.encode("utf-8") in raw]
            add("the database file holds none of its text or id", not found, ", ".join(found))

            # Distinctive words: 6+ letters that no remaining note (or the schema) uses
            remaining = index.remaining_text()
            words = sorted({w[:WORD_PREFIX] for w in query_terms(page.body + " " + page.title, 60)
                            if len(w) >= WORD_PREFIX})
            unique = [w for w in words if w not in remaining]
            lower = raw.lower()
            residue = [w for w in unique if w.encode("utf-8") in lower]
            add("none of its distinctive words remain in the database file", not residue,
                ", ".join(residue[:5]))
        except MemoryStoreError as e:
            add("the database could be inspected", False, str(e))

    report.mentions = find_mentions(
        page.body,
        roots if roots is not None else [SESSIONS_DIR, EXPORTS_DIR],
    )
    return report
