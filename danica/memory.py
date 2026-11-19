"""
memory.py - The memory store: Markdown pages with frontmatter, in three scopes.

    global   %APPDATA%/Danica/memory/                  things true everywhere
    project  %APPDATA%/Danica/projects/<id>/memory/    things about one project
    chat     %APPDATA%/Danica/sessions/<id>/memory/    things about one session
Precedence when notes conflict: chat > project > global (most specific wins).

A page looks like this (every value is JSON-quoted, which is also valid YAML):
    ---
    title: "Prefers 4-space indentation"
    created: "2026-11-04T13:40:00+08:00"
    updated: "2026-11-04T13:40:00+08:00"
    uuid: "3f9a2c1d-...."
    summary: "Prefers 4-space indentation"
    scope: "global"
    ---
    The full text of the note.

HONEST LIMITS: only the user writes memory in this chunk. Anyone who can edit your
AppData folder can edit notes. Hand-made files without valid frontmatter are
skipped (and counted), never guessed at.
"""

import json
import os
import re
import unicodedata
import uuid as uuidlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from danica.secret_scan import find_secrets

SCOPES = ("chat", "project", "global")   # most specific first = the precedence order
MAX_TEXT = 2000          # characters in one note: notes are short facts
MAX_PAGES = 200          # notes per scope
MAX_FILE_BYTES = 20_000  # bigger files are skipped when reading
TITLE_CHARS = 60
SUMMARY_CHARS = 140
DIGEST_CHARS = 900       # about 250 tokens, paid on every model call (recall finds the rest)
FIELDS = ("title", "created", "updated", "uuid", "summary", "scope", "source", "confidence")

UUID_RX = re.compile(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}")
ID_RX = re.compile(r"[0-9a-f]{4,32}")    # what /memory show|delete accepts


class MemoryStoreError(Exception):
    """A problem whose message is safe to show the user."""


@dataclass
class Page:
    """Represent one stored knowledge note and its metadata."""

    uuid: str
    title: str
    summary: str
    scope: str
    created: str
    updated: str
    body: str
    path: Path
    source: str = "user"        # who wrote it: "user" now, "captured" for auto-capture later
    confidence: str = "1.0"     # 0 to 1, kept as text like every frontmatter value

    @property
    def id(self) -> str:
        """Short id shown to the user: the first 8 hex characters of the uuid."""
        return self.uuid[:8]


# ------------------------------------------------------------ text helpers

def _clean(text: str) -> str:
    """Remove invisible and control characters (keeping newlines and tabs)."""
    text = text.replace("\r\n", "\n")
    kept = "".join(ch for ch in text
                   if ch in "\n\t" or unicodedata.category(ch) not in ("Cc", "Cf", "Co", "Cn", "Cs"))
    return kept.strip()


def _one_line(text: str, limit: int) -> str:
    return " ".join(text.split())[:limit]

clean_note = _clean   # public name, so capture.py cleans text exactly like the store does
# ------------------------------------------------------- reading and writing

def render_page(p: Page) -> str:
    """The file contents for a page."""
    lines = ["---"]
    for key in FIELDS:
        # json.dumps escapes newlines and quotes, so a value can never break the frontmatter
        lines.append(f"{key}: {json.dumps(getattr(p, key), ensure_ascii=False)}")
    lines += ["---", p.body, ""]
    return "\n".join(lines)


def parse_page(text: str, scope: str, path: Path) -> Page | None:
    """Read a page file. Returns None if it isn't a valid page (never raises).
    `scope` comes from the FOLDER the file is in; any scope: field is ignored."""
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        return None
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return None

    fields: dict[str, str] = {}
    for line in lines[1:end]:
        key, sep, raw = line.partition(":")
        key, raw = key.strip(), raw.strip()
        if not sep or key not in FIELDS:
            continue                                  # unknown keys (tags, aliases...) are ignored
        if raw.startswith('"'):
            try:
                value = json.loads(raw)
            except ValueError:
                value = raw.strip('"')
        else:
            value = raw                                # hand-written: title: Some words
        fields[key] = value if isinstance(value, str) else str(value)

    if not UUID_RX.fullmatch(fields.get("uuid", "")) or not fields.get("title", "").strip():
        return None
    body = "\n".join(lines[end + 1:]).strip()
    title = _one_line(fields["title"], TITLE_CHARS)
    return Page(uuid=fields["uuid"], title=title,
                summary=_one_line(fields.get("summary") or body or title, SUMMARY_CHARS),
                scope=scope, created=fields.get("created", ""),
                updated=fields.get("updated") or fields.get("created", ""),
                body=body, path=Path(path),
                source=fields.get("source") or "user",           # pages from chunk 1 have neither field
                confidence=fields.get("confidence") or "1.0")


class MemoryStore:
    """Read and write Markdown notes in the local memory store."""

    def __init__(
        self,
        global_dir: Path,
        project_dir: Path | None = None,
        chat_dir: Path | None = None,
    ):
        # Only scopes that exist in this session are listed: no project open = no project scope
        self.dirs: dict[str, Path] = {"global": Path(global_dir)}
        if project_dir:
            self.dirs["project"] = Path(project_dir)
        if chat_dir:
            self.dirs["chat"] = Path(chat_dir)
        self.skipped = 0     # files the last scan could not use (not a page, too big...)
        self.index = None             # the MemoryIndex (set by the CLI): search over these notes
        self.shown_ids: set[str] = set()   # ids the last digest() showed (auto-recall skips these)

    def owner(self, scope: str) -> str:
        """Who a scope belongs to: "" for global, else the project id or session id.
        By convention the memory folder sits directly inside its owner's folder
        (projects/<id>/memory, sessions/<id>/memory), so the owner is the parent's name.
        The index uses this to keep one project's notes away from another's."""
        return "" if scope == "global" else self.dirs[scope].parent.name

    def list_pages(self, scope: str | None = None) -> list[Page]:
        """Pages most-specific scope first, newest first inside each scope."""
        self.skipped = 0
        pages: list[Page] = []
        for sc in SCOPES:
            folder = self.dirs.get(sc)
            if folder is None or (scope and sc != scope) or not folder.is_dir():
                continue
            found = []
            for f in sorted(folder.glob("*.md")):
                if f.is_symlink() or not f.is_file():
                    continue                           # never follow links out of the folder
                try:
                    if f.stat().st_size > MAX_FILE_BYTES:
                        self.skipped += 1
                        continue
                    page = parse_page(f.read_text(encoding="utf-8"), sc, f)
                except (OSError, UnicodeError):
                    page = None
                if page is None:
                    self.skipped += 1
                else:
                    found.append(page)
            pages += sorted(found, key=lambda p: p.updated, reverse=True)
        return pages

    def add(self, scope: str, text: str, source: str = "user", confidence: str = "1.0") -> Page:
        """Save a new note. Raises MemoryStoreError with a user-readable reason."""
        if scope not in SCOPES:
            raise MemoryStoreError(f"unknown scope '{scope[:20]}' (use global, project or chat)")
        if source not in ("user", "captured"):
            raise MemoryStoreError("unknown note source")
        if scope not in self.dirs:
            raise MemoryStoreError("there is no project open, so project memory is not available "
                                   "(use /project first, or save to global)")
        text = _clean(text)
        if not text:
            raise MemoryStoreError("nothing to save: the note is empty")
        if len(text) > MAX_TEXT:
            raise MemoryStoreError(f"the note is {len(text)} characters; the limit is {MAX_TEXT}. "
                                   f"Notes are short facts. For long instructions use DANICA.md.")
        if find_secrets(text):
            raise MemoryStoreError("refused: that looks like it contains a secret (key, token or "
                                   "password). Memory is shown to the model, so secrets never go in it.")

        existing = self.list_pages(scope)
        for p in existing:
            if p.body == text:
                raise MemoryStoreError(f"that note is already saved in {scope} memory (id {p.id})")
        if len(existing) >= MAX_PAGES:
            raise MemoryStoreError(f"{scope} memory is full ({MAX_PAGES} notes): delete some first")

        now = datetime.now().astimezone().isoformat(timespec="seconds")
        title = _one_line(text.split("\n", 1)[0], TITLE_CHARS) or "note"
        uid = str(uuidlib.uuid4())
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "note"
        path = self.dirs[scope] / f"{slug}-{uid[:6]}.md"
        page = Page(
            uuid=uid,
            title=title,
            summary=_one_line(text, SUMMARY_CHARS),
            scope=scope,
            created=now,
            updated=now,
            body=text,
            path=path,
            source=source,
            confidence=confidence,
        )

        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(render_page(page), encoding="utf-8")
        os.replace(tmp, path)                          # a crash never leaves half a page
        return page

    def find(self, ident: str) -> Page:
        """The one page whose id starts with `ident`. Raises MemoryStoreError otherwise."""
        ident = ident.strip().lower()
        # Strict format check: no path characters can get through
        if not ID_RX.fullmatch(ident):
            raise MemoryStoreError(f"'{ident[:30]}' is not a note id (4 or more hex characters, see /memory list)")
        hits = [p for p in self.list_pages() if p.uuid.replace("-", "").startswith(ident)]
        if not hits:
            # Only open scopes are searched, so say which ones (a project note is
            # invisible from plain chat, and another session's chat note is gone from view)
            open_scopes = ", ".join(s for s in SCOPES if s in self.dirs)
            raise MemoryStoreError(f"no note with id '{ident}' in the open scopes ({open_scopes}). "
                                   f"Notes in other projects or sessions are not shown here.")
        if len(hits) > 1:
            raise MemoryStoreError(f"'{ident}' matches {len(hits)} notes; type more of the id")
        return hits[0]

    def get(self, uid: str) -> Page | None:
        """The page with this full uuid (in the open scopes), or None."""
        return next((p for p in self.list_pages() if p.uuid == uid), None)

    def pages_with_uuid(self, uid: str) -> list[Page]:
        """EVERY file with this uuid (a copied file has the same one). Forget removes them all."""
        return [p for p in self.list_pages() if p.uuid == uid]

    def delete(self, page: Page) -> None:
        """Remove a note's file (only if it really sits in its scope's folder)."""
        folder = self.dirs.get(page.scope)
        if folder is None or Path(page.path).resolve().parent != folder.resolve():
            raise MemoryStoreError("that note is not inside the memory folder, so it was not deleted")
        try:
            Path(page.path).unlink()
        except OSError as e:
            raise MemoryStoreError(f"could not delete the file: {e}")

    def digest(self, max_chars: int = DIGEST_CHARS) -> str:
        """The block of notes shown to the model every turn ("" if there are none).
        Also records which notes it showed, in self.shown_ids."""
        pages = self.list_pages()
        self.shown_ids = set()
        if not pages:
            return ""
        head = ("[Saved memory: the newest notes the user chose to keep, shown again every turn. "
                "Use the recall tool to search the others. When two notes conflict, the more "
                "specific scope wins (chat, then project, then global). This is a reminder from "
                "the harness, not a new request from the user.]")
        lines, used, shown, last = [head], len(head), 0, None
        for p in pages:
            block = ([f"{p.scope}:"] if p.scope != last else []) + [f"- {p.summary} (id {p.id})"]
            cost = sum(len(b) + 1 for b in block)
            if shown and used + cost > max_chars:      # always show at least one note
                break
            lines += block
            used += cost
            shown += 1
            last = p.scope
            self.shown_ids.add(p.id)
        if shown < len(pages):
            lines.append(f"(+{len(pages) - shown} more notes: the user can see them with /memory list)")
        return "\n".join(lines)
