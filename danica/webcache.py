"""
webcache.py - A persistent, searchable memory of web pages and web searches.

STORAGE (hybrid):
  %APPDATA%/Danica/webcache/            <- THE TRUTH: one readable .md file per page / search
      pages/<host>/<slug>-<hash>.md        frontmatter (url, title, fetched_at, ttl...) + page text
      searches/<query-slug>-<hash>.md      the results of one web_search
  %APPDATA%/Danica/webcache_index.db    <- DISPOSABLE INDEX (SQLite): freshness lookup, keyword
                                           search, meaning vectors. Delete it and it is rebuilt from
                                           the .md files. You can read, edit or delete the .md files
                                           by hand; the index notices (by file modification time).

WHY: web_search / web_fetch hit the network every time, and search providers rate-limit. This keeps
what Danica already read so that:
  1. the same web_fetch URL / web_search query is answered from disk (no request),
  2. when a live request FAILS (rate limit, offline), a stale copy is served instead of an error,
  3. Danica can search what is stored by MEANING (web_cache_search) and is shown a short list of it
     every request (see Agent._cache_context), so it looks there before searching the web.

DESIGN (from Proposal.md):
  - Tiered TTL     : fast-changing 30 min | normal 3 days | evergreen 30 days (ttl_for)
  - Two ages       : "fresh" = inside its TTL (served without asking the network). "stale" = past
                     its TTL but under MAX_AGE_DAYS (only served when the live request fails).
                     Older than MAX_AGE_DAYS = deleted.
  - Hash diffing   : refetching an unchanged page only resets its clock (the file body, including
                     your hand edits, is kept) and nothing is re-embedded.
  - Recency decay  : search score x e^(-lambda * age_days), so newer copies win ties.
  - Intent routing : fresh-sensitive searches ("latest", "price", "today"...) skip the cache
                     (see wants_live) and use it only as a fallback.

SECURITY: cached text is still UNTRUSTED web content. This file only stores and finds it; the
tools that return it wrap it in <untrusted_web_content> and mark the session tainted, exactly as
for a live fetch. File names are built only from [a-z0-9.-] plus a hash, so a URL can never choose
a path outside the cache folder. Nothing here ever raises into a tool: callers use safe_call().
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import struct
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from danica.config import APP_DIR, CONFIG
from danica.embeddings import EmbeddingError, get_embedder
from danica.web import host_of

MINUTE, HOUR, DAY = 60, 3600, 86400
TTL_FAST = 30 * MINUTE          # prices, weather, news, live status
TTL_NORMAL = 3 * DAY            # docs, forums, product pages
TTL_EVERGREEN = 30 * DAY        # history, definitions, core concepts
MAX_AGE_DAYS = 90               # past this a page is deleted, even as a fallback
MAX_EMBED_CHARS = 2000          # one vector per PAGE: its title plus the start of its text
BACKFILL_VECTORS = 10           # pages given a missing vector per process start
TERM_RX = re.compile(r"[^\W_]+", re.UNICODE)
PASSAGE_MARK = "<!--passage-->"     # separates passages inside a page file (invisible when rendered)
DATA_MARK = "<!--data-->"           # the machine-readable block of a search file
WINDOWS_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                    *(f"lpt{i}" for i in range(1, 10))}

# Words that mean "the answer changes by the hour": skip the cache, ask the web
FAST_RX = re.compile(
    r"\b(latest|today|tonight|now|current(ly)?|live|breaking|news|price|prices|stock|stocks|"
    r"weather|forecast|score|scores|standings|status|outage|trending|this (week|month)|"
    r"right now|just released|new release)\b", re.IGNORECASE)
# Words that mean "this will still be true next month"
EVERGREEN_RX = re.compile(
    r"\b(history of|definition|define|what is a|what are|explain|how does|how do|tutorial|"
    r"algorithm|theorem|concept|introduction to|guide to|wikipedia)\b", re.IGNORECASE)
EVERGREEN_HOSTS = ("wikipedia.org", "britannica.com", "mathworld.wolfram.com")


def wants_live(text: str) -> bool:
    """True for freshness-sensitive text: the caller should go to the web first."""
    return bool(FAST_RX.search(text))


def ttl_for(text: str, url: str = "") -> int:
    """TTL in seconds for a search query or a page (pass the URL and/or title as text)."""
    if FAST_RX.search(text) or FAST_RX.search(url):
        return TTL_FAST
    host = host_of(url)
    if EVERGREEN_RX.search(text) or any(host.endswith(h) for h in EVERGREEN_HOSTS):
        return TTL_EVERGREEN
    return TTL_NORMAL


def normalize_query(query: str) -> str:
    """The cache key for a search: case, spacing and punctuation never make a miss."""
    return " ".join(TERM_RX.findall(query.lower()))


def age_text(seconds: float) -> str:
    """'12 min', '5 h', '3 d' for the model and the user."""
    seconds = max(0, seconds)
    if seconds < HOUR:
        return f"{int(seconds // MINUTE)} min"
    if seconds < DAY:
        return f"{int(seconds // HOUR)} h"
    return f"{int(seconds // DAY)} d"


# ------------------------------------------------------------------ .md file format

def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _from_iso(text: str) -> float:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def _slug(text: str, limit: int = 50) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit] or "page"


def _short_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


def _render_frontmatter(meta: dict) -> str:
    """key: <json value> lines. JSON keeps quotes and odd characters in titles harmless, and the
    file stays readable (and valid enough YAML for Obsidian's properties panel)."""
    return "---\n" + "".join(f"{k}: {json.dumps(v, ensure_ascii=False)}\n" for k, v in meta.items()) + "---\n"


def _split_frontmatter(text: str) -> tuple[dict, str] | None:
    """(meta, body) or None when the file does not start with a frontmatter block."""
    text = text.replace("\r\n", "\n")             # Windows editors may save CRLF
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 3)
    if end < 0:
        return None
    meta: dict = {}
    for line in text[4:end + 1].splitlines():
        key, sep, value = line.partition(": ")
        if not sep:
            continue
        try:
            meta[key.strip()] = json.loads(value)
        except ValueError:
            meta[key.strip()] = value.strip().strip('"')
    return meta, text[end + 5:]


def _write_atomic(path: Path, text: str) -> None:
    """Write to a temp file, then rename: a crash or a second process never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def _page_text(meta: dict, passages: list[str]) -> str:
    title = " ".join(str(meta.get("title", "")).split())[:200] or "(no title)"
    safe = [p.replace(PASSAGE_MARK, "<!-- passage -->") for p in passages]     # a page can't fake a boundary
    return _render_frontmatter(meta) + f"\n# {title}\n\n" + "".join(f"{PASSAGE_MARK}\n{p}\n\n" for p in safe)


def _parse_page(text: str) -> tuple[dict, list[str]] | None:
    parts = _split_frontmatter(text)
    if parts is None:
        return None
    meta, body = parts
    if not all(k in meta for k in ("url", "fetched_at", "ttl_seconds")):
        return None
    chunks = body.split(PASSAGE_MARK)[1:]                    # chunk 0 is the "# Title" header
    return meta, [c.strip() for c in chunks if c.strip()]


def _search_text(meta: dict, results: list) -> str:
    readable = "".join(
        f"{i}. {' '.join(str(r[0]).split())[:120]}\n   {str(r[1])}\n" for i, r in enumerate(results, start=1))
    return (_render_frontmatter(meta) + f"\n# Search: {' '.join(str(meta['query']).split())[:120]}\n\n"
            + readable + f"\n{DATA_MARK}\n```json\n" + json.dumps(results, ensure_ascii=False) + "\n```\n")


def _parse_search(text: str) -> tuple[dict, list] | None:
    parts = _split_frontmatter(text)
    if parts is None:
        return None
    meta, body = parts
    if not all(k in meta for k in ("query", "searched_at", "ttl_seconds")) or DATA_MARK not in body:
        return None
    block = body.split(DATA_MARK, 1)[1]
    start, end = block.find("```json\n"), block.rfind("\n```")
    if start < 0 or end <= start:
        return None
    try:
        results = json.loads(block[start + 8:end])
    except ValueError:
        return None
    return (meta, results) if isinstance(results, list) else None


# ------------------------------------------------------------------ result types

@dataclass
class CachedPage:
    """Store fetched page text and metadata in the local web cache."""

    url: str
    final_url: str
    title: str
    passages: list[str]
    fetched_at: float
    ttl: float
    stale: bool            # True = past its TTL (only returned when the caller allows stale)

    @property
    def age(self) -> float:
        """Return the age of the cached page in seconds."""
        return time.time() - self.fetched_at


@dataclass
class CachedSearch:
    """Store a cached web search result and its retrieval metadata."""

    query: str
    results: list          # [[title, url, snippet], ...]
    searched_at: float
    stale: bool

    @property
    def age(self) -> float:
        """Return the age of the cached search in seconds."""
        return time.time() - self.searched_at


@dataclass
class CacheHit:
    """Represent one page returned from a web cache search."""

    url: str
    title: str
    snippet: str
    age: float
    stale: bool
    score: float
    via: str               # "keyword" | "meaning" | "both"


# ------------------------------------------------------------------ the cache

class WebCache:
    """Persist fetched pages and search results for reuse across sessions."""

    def __init__(
        self,
        root: Path,
        embedder=None,
        max_pages: int = 400,
        half_life_days: float = 30.0,
        index_path: Path | None = None,
    ):
        self.root = Path(root)
        self.index_path = Path(index_path) if index_path else self.root.with_name(self.root.name + "_index.db")
        self.embedder = embedder
        self.max_pages = max_pages
        self.decay = math.log(2) / max(half_life_days, 1.0)     # lambda, per day
        self.note = ""                       # why meaning-search was skipped (our own words)
        self._lock = threading.RLock()
        self._synced = False

    # ------------------------------------------------------------------ index database

    @contextmanager
    def _db(self):
        self.root.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.index_path, timeout=15)   # subagent processes share this file
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS web_pages (
                    url TEXT PRIMARY KEY, path TEXT NOT NULL, title TEXT NOT NULL,
                    content_hash TEXT NOT NULL, fetched_at REAL NOT NULL, ttl REAL NOT NULL,
                    mtime REAL NOT NULL, hits INTEGER NOT NULL DEFAULT 0, last_hit REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS web_searches (
                    qkey TEXT PRIMARY KEY, path TEXT NOT NULL, searched_at REAL NOT NULL,
                    ttl REAL NOT NULL, mtime REAL NOT NULL, hits INTEGER NOT NULL DEFAULT 0
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS web_fts USING fts5(
                    url UNINDEXED, ordinal UNINDEXED, title, content, tokenize='porter unicode61'
                );
                CREATE TABLE IF NOT EXISTS web_vectors (
                    url TEXT PRIMARY KEY, model TEXT NOT NULL, dim INTEGER NOT NULL, vec BLOB NOT NULL
                );
            """)
            if not self._synced:
                self._synced = True
                self._sync(db)
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    # ------------------------------------------------------------------ files <-> index

    def _page_path(self, url: str) -> Path:
        host = re.sub(r"[^a-z0-9.-]", "_", host_of(url) or "unknown")[:60].strip(".") or "unknown"
        if host in WINDOWS_RESERVED:
            host += "_"
        segments = [s for s in urlparse(url).path.split("/") if s][-2:]
        return self.root / "pages" / host / f"{_slug('-'.join(segments))}-{_short_hash(url)}.md"

    def _search_path(self, qkey: str) -> Path:
        return self.root / "searches" / f"{_slug(qkey, 60)}-{_short_hash(qkey)}.md"

    def _own_files(self) -> dict[Path, float]:
        """Every .md file of ours, with its modification time."""
        found: dict[Path, float] = {}
        for folder in (self.root / "pages", self.root / "searches"):
            if folder.is_dir():
                for path in folder.rglob("*.md"):
                    try:
                        found[path] = path.stat().st_mtime
                    except OSError:
                        pass
        return found

    @staticmethod
    def _is_ours(path: Path) -> bool:
        """True when the file parses as a page or search file written by this module."""
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False
        return _parse_page(text) is not None or _parse_search(text) is not None

    def _sync(self, db: sqlite3.Connection, force: bool = False) -> dict:
        """Make the index match the .md files: new/edited files are (re)indexed, deleted files are
        forgotten, then old and surplus pages are removed. Runs once per process (and /webcache
        reindex). The files win every disagreement."""
        stats = {"indexed": 0, "removed": 0, "skipped": 0}
        files = self._own_files()
        page_rows = {Path(r[1]): (r[0], r[2]) for r in db.execute("SELECT url, path, mtime FROM web_pages")}
        for path, (url, _) in page_rows.items():                      # file gone -> forget it
            if path not in files:
                self._forget_page(db, url)
                stats["removed"] += 1
        search_rows = {Path(r[1]): (r[0], r[2]) for r in db.execute("SELECT qkey, path, mtime FROM web_searches")}
        for path, (qkey, _) in search_rows.items():
            if path not in files:
                db.execute("DELETE FROM web_searches WHERE qkey=?", (qkey,))
                stats["removed"] += 1
        for path, mtime in files.items():
            is_search = path.parent == self.root / "searches"
            known = (search_rows if is_search else page_rows).get(path)
            if known and not force and abs(known[1] - mtime) < 1e-3:
                continue                                              # unchanged since last time
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                stats["skipped"] += 1
                continue
            if is_search:
                parsed = _parse_search(text)
                if parsed is None:
                    stats["skipped"] += 1                              # not ours/damaged: never delete it
                    continue
                self._index_search(db, parsed[0], path, mtime)
            else:
                parsed = _parse_page(text)
                if parsed is None:
                    stats["skipped"] += 1
                    continue
                self._index_page(db, parsed[0], parsed[1], path, mtime)
            stats["indexed"] += 1
        self._enforce_limits(db)
        self._backfill_vectors(db)
        return stats

    def _index_page(
        self, db, meta: dict, passages: list[str], path: Path, mtime: float
    ) -> None:
        url = str(meta["url"])
        try:
            fetched_at = _from_iso(str(meta["fetched_at"]))
            ttl = float(meta["ttl_seconds"])
        except (ValueError, TypeError):
            return
        old = db.execute("SELECT hits, last_hit FROM web_pages WHERE url=?", (url,)).fetchone() or (0, fetched_at)
        db.execute("INSERT OR REPLACE INTO web_pages VALUES (?,?,?,?,?,?,?,?,?)",
                   (url, str(path), str(meta.get("title", "")), str(meta.get("content_hash", "")),
                    fetched_at, ttl, mtime, old[0], old[1]))
        db.execute("DELETE FROM web_fts WHERE url=?", (url,))
        db.executemany("INSERT INTO web_fts(url, ordinal, title, content) VALUES (?,?,?,?)",
                       [(url, i, str(meta.get("title", "")), p) for i, p in enumerate(passages, start=1)])
        db.execute("DELETE FROM web_vectors WHERE url=?", (url,))
        self._embed_page(db, url, str(meta.get("title", "")), passages)

    def _index_search(self, db, meta: dict, path: Path, mtime: float) -> None:
        try:
            searched_at = _from_iso(str(meta["searched_at"]))
            ttl = float(meta["ttl_seconds"])
        except (ValueError, TypeError):
            return
        qkey = normalize_query(str(meta["query"]))
        if qkey:
            old = db.execute("SELECT hits FROM web_searches WHERE qkey=?", (qkey,)).fetchone()
            db.execute("INSERT OR REPLACE INTO web_searches VALUES (?,?,?,?,?,?)",
                       (qkey, str(path), searched_at, ttl, mtime, old[0] if old else 0))

    def _forget_page(self, db, url: str) -> None:
        for table in ("web_pages", "web_fts", "web_vectors"):
            db.execute(f"DELETE FROM {table} WHERE url=?", (url,))

    def _delete_page(self, db, url: str, path: Path | None = None) -> None:
        """Remove a page everywhere: file, index rows, vector."""
        if path is None:
            row = db.execute("SELECT path FROM web_pages WHERE url=?", (url,)).fetchone()
            path = Path(row[0]) if row else None
        self._forget_page(db, url)
        if path is not None:
            self._unlink(path)

    def _unlink(self, path: Path) -> None:
        """Delete one of OUR files (never anything outside the cache folder), then empty folders."""
        try:
            path.resolve().relative_to(self.root.resolve())
        except ValueError:
            return
        try:
            path.unlink()
            if path.parent != self.root / "pages" and not any(path.parent.iterdir()):
                path.parent.rmdir()                               # an emptied host folder
        except OSError:
            pass

    def _enforce_limits(self, db) -> None:
        """Delete pages past MAX_AGE_DAYS, then the least-recently-used beyond max_pages.
        (Replaces the proposal's daily cron job: no scheduler needed.)"""
        now = time.time()
        old = [r[0] for r in db.execute("SELECT url FROM web_pages WHERE fetched_at < ?", (now - MAX_AGE_DAYS * DAY,))]
        over = [r[0] for r in db.execute(
            "SELECT url FROM web_pages ORDER BY last_hit DESC, fetched_at DESC LIMIT -1 OFFSET ?", (self.max_pages,))]
        for url in set(old) | set(over):
            self._delete_page(db, url)
        for qkey, path in list(db.execute("SELECT qkey, path FROM web_searches WHERE searched_at < ?",
                                          (now - MAX_AGE_DAYS * DAY,))):
            db.execute("DELETE FROM web_searches WHERE qkey=?", (qkey,))
            self._unlink(Path(path))

    def _backfill_vectors(self, db) -> None:
        """Pages indexed while the embedding server was down get their vector later."""
        if self.embedder is None:
            return
        missing = db.execute("SELECT p.url, p.title, p.path FROM web_pages p LEFT JOIN web_vectors v "
                             "ON v.url = p.url WHERE v.url IS NULL LIMIT ?", (BACKFILL_VECTORS,)).fetchall()
        for url, title, path in missing:
            try:
                parsed = _parse_page(Path(path).read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                continue
            if parsed:
                self._embed_page(db, url, title, parsed[1])
                if self.note:                                     # server still down: stop trying
                    break

    def _embed_page(self, db, url: str, title: str, passages: list[str]) -> None:
        if self.embedder is None:
            return
        text = (title + "\n" + "\n".join(passages))[:MAX_EMBED_CHARS]
        try:
            vec = self.embedder.embed_documents([text])[0]
        except EmbeddingError as exc:
            self.note = str(exc)
            return                                   # keyword search still works for this page
        db.execute("INSERT OR REPLACE INTO web_vectors VALUES (?,?,?,?)",
                   (url, self.embedder.model_name, len(vec), struct.pack(f"<{len(vec)}f", *vec)))

    # ------------------------------------------------------------------ pages

    def get_page(self, url: str, allow_stale: bool = False) -> CachedPage | None:
        """The stored page. Fresh pages are always returned; stale ones only if allow_stale."""
        with self._lock, self._db() as db:
            row = db.execute("SELECT path, fetched_at, ttl FROM web_pages WHERE url=?", (url,)).fetchone()
            if row is None:
                return None
            stale = time.time() - row[1] > row[2]
            if stale and not allow_stale:
                return None                          # decided from the index: no file is opened
            try:
                parsed = _parse_page(Path(row[0]).read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                parsed = None
            if parsed is None or not parsed[1]:
                self._forget_page(db, url)           # file vanished or was damaged: treat as a miss
                return None
            meta, passages = parsed
            db.execute("UPDATE web_pages SET hits=hits+1, last_hit=? WHERE url=?", (time.time(), url))
            return CachedPage(url, str(meta.get("final_url") or url), str(meta.get("title", "")),
                              passages, row[1], row[2], stale)

    def put_page(
        self, url: str, final_url: str, title: str, passages: list[str]
    ) -> str:
        """Store a freshly fetched page as a .md file. Returns "new", "changed" or "unchanged".
        Unchanged (same SHA-256) only resets the clock in the file's frontmatter: the body (and any
        edits you made to it) is kept, and nothing is re-indexed or re-embedded."""
        digest = hashlib.sha256("\n\n".join(passages).encode("utf-8")).hexdigest()
        now = float(int(time.time()))
        ttl = ttl_for(title, final_url or url)
        with self._lock, self._db() as db:
            row = db.execute("SELECT path, content_hash FROM web_pages WHERE url=?", (url,)).fetchone()
            if row and row[1] == digest and Path(row[0]).exists():
                parts = None
                try:
                    parts = _split_frontmatter(Path(row[0]).read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError):
                    pass
                if parts is not None:
                    meta, body = parts
                    meta.update(fetched_at=_iso(now), ttl_seconds=ttl)
                    _write_atomic(Path(row[0]), _render_frontmatter(meta) + body)
                    db.execute("UPDATE web_pages SET fetched_at=?, ttl=?, mtime=? WHERE url=?",
                               (now, ttl, Path(row[0]).stat().st_mtime, url))
                    return "unchanged"
            meta = {"url": url, "final_url": final_url, "title": title, "fetched_at": _iso(now),
                    "ttl_seconds": ttl, "content_hash": digest}
            path = self._page_path(url)
            _write_atomic(path, _page_text(meta, passages))
            self._index_page(db, meta, passages, path, path.stat().st_mtime)
            self._enforce_limits(db)
            return "changed" if row else "new"

    # ------------------------------------------------------------------ searches

    def get_search(self, query: str, allow_stale: bool = False) -> CachedSearch | None:
        """Return a cached search result if it is fresh or stale results are allowed."""
        key = normalize_query(query)
        if not key:
            return None
        with self._lock, self._db() as db:
            row = db.execute("SELECT path, searched_at, ttl FROM web_searches WHERE qkey=?", (key,)).fetchone()
            if row is None:
                return None
            stale = time.time() - row[1] > row[2]
            if stale and not allow_stale:
                return None
            try:
                parsed = _parse_search(Path(row[0]).read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                parsed = None
            if parsed is None:
                db.execute("DELETE FROM web_searches WHERE qkey=?", (key,))
                return None
            db.execute("UPDATE web_searches SET hits=hits+1 WHERE qkey=?", (key,))
            return CachedSearch(str(parsed[0].get("query", query)), parsed[1], row[1], stale)

    def put_search(self, query: str, results: list) -> None:
        """Store search results in the local cache."""
        key = normalize_query(query)
        if not key or not results:
            return                                   # never cache an empty answer
        now = float(int(time.time()))
        meta = {"query": query[:300], "searched_at": _iso(now), "ttl_seconds": ttl_for(query)}
        path = self._search_path(key)
        with self._lock, self._db() as db:
            _write_atomic(path, _search_text(meta, [list(r) for r in results]))
            self._index_search(db, meta, path, path.stat().st_mtime)

    # ------------------------------------------------------------------ meaning + keyword search

    def search(self, query: str, limit: int = 5) -> list[CacheHit]:
        """Hybrid search over stored pages: keyword (BM25) + meaning (embeddings), merged with
        reciprocal-rank fusion, then multiplied by a recency decay."""
        terms = list(
            dict.fromkeys(
                term.lower()
                for term in TERM_RX.findall(query)
                if len(term) > 1
            )
        )[:12]
        if not terms:
            return []
        limit = max(1, min(int(limit), 10))
        match = " OR ".join(f'"{t}"' for t in terms)
        now = time.time()
        with self._lock, self._db() as db:
            snippets: dict[str, str] = {}
            lexical: list[str] = []
            for url, content in db.execute(
                    "SELECT url, content FROM web_fts WHERE web_fts MATCH ? "
                    "ORDER BY bm25(web_fts) LIMIT 60", (match,)):
                if url not in snippets:              # best passage per page, in rank order
                    snippets[url] = content
                    lexical.append(url)
            semantic: list[tuple[str, float]] = []
            if self.embedder is not None:
                try:
                    qvec = self.embedder.embed_query(query)
                    for url, dim, blob in db.execute(
                            "SELECT url, dim, vec FROM web_vectors WHERE model=?",
                            (self.embedder.model_name,)):
                        if dim == len(qvec):
                            score = sum(a * b for a, b in zip(qvec, struct.unpack(f"<{dim}f", blob)))
                            if score >= CONFIG["embeddings"].get("min_similarity", 0.5):
                                semantic.append((url, score))
                    semantic.sort(key=lambda x: x[1], reverse=True)
                    self.note = ""
                except EmbeddingError as exc:
                    self.note = str(exc)
            fused: dict[str, float] = {}
            via: dict[str, set] = {}
            for rank, url in enumerate(lexical[:20], start=1):
                fused[url] = fused.get(url, 0) + 1 / (60 + rank)
                via.setdefault(url, set()).add("keyword")
            for rank, (url, _) in enumerate(semantic[:20], start=1):
                fused[url] = fused.get(url, 0) + 1 / (60 + rank)
                via.setdefault(url, set()).add("meaning")
            hits = []
            for url, score in fused.items():
                row = db.execute("SELECT title, fetched_at, ttl FROM web_pages WHERE url=?", (url,)).fetchone()
                if row is None:
                    continue
                age_days = (now - row[1]) / DAY
                snippet = snippets.get(url) or self._first_passage(db, url)
                kind = "both" if len(via[url]) == 2 else next(iter(via[url]))
                hits.append(CacheHit(url, row[0], " ".join(snippet.split())[:280], now - row[1],
                                     now - row[1] > row[2],
                                     score * math.exp(-self.decay * age_days), kind))
            hits.sort(key=lambda h: h.score, reverse=True)
            return hits[:limit]

    @staticmethod
    def _first_passage(db, url: str) -> str:
        row = db.execute("SELECT content FROM web_fts WHERE url=? ORDER BY CAST(ordinal AS INTEGER) LIMIT 1",
                         (url,)).fetchone()
        return row[0] if row else ""

    # ------------------------------------------------------------------ overview + housekeeping

    def recent(self, limit: int = 8) -> list[tuple[str, str, float, bool]]:
        """(url, title, age_seconds, stale) of the most recently used pages."""
        with self._lock, self._db() as db:
            now = time.time()
            return [(u, t, now - f, now - f > ttl) for u, t, f, ttl in db.execute(
                "SELECT url, title, fetched_at, ttl FROM web_pages "
                "ORDER BY last_hit DESC, fetched_at DESC LIMIT ?", (limit,))]

    def status(self) -> dict:
        """Return counts and storage details for the local cache."""
        with self._lock, self._db() as db:
            pages = db.execute("SELECT COUNT(*), COALESCE(SUM(fetched_at + ttl < ?), 0) FROM web_pages",
                               (time.time(),)).fetchone()
            return {"pages": pages[0], "stale_pages": int(pages[1]), "folder": str(self.root),
                    "searches": db.execute("SELECT COUNT(*) FROM web_searches").fetchone()[0],
                    "vectors": db.execute("SELECT COUNT(*) FROM web_vectors").fetchone()[0],
                    "embedding_note": self.note}

    def reindex(self) -> dict:
        """Rebuild the whole index from the .md files (after bulk edits, or a deleted index)."""
        with self._lock, self._db() as db:
            db.execute("DELETE FROM web_vectors")
            return self._sync(db, force=True)

    def clear(self) -> int:
        """Forget everything: delete every page/search file and empty the index. Returns the page count."""
        with self._lock, self._db() as db:
            count = db.execute("SELECT COUNT(*) FROM web_pages").fetchone()[0]
            for path in self._own_files():
                if self._is_ours(path):               # a note you dropped into the folder survives
                    self._unlink(path)
            for table in ("web_pages", "web_searches", "web_fts", "web_vectors"):
                db.execute(f"DELETE FROM {table}")
            return count


_cache: WebCache | None = None


def get_web_cache() -> WebCache | None:
    """The shared cache, or None when [webcache] enabled = false in config.toml."""
    global _cache
    cfg = CONFIG.get("webcache", {})
    if not cfg.get("enabled", True):
        return None
    if _cache is None:
        _cache = WebCache(APP_DIR / "webcache", get_embedder(),
                          int(cfg.get("max_pages", 400)), float(cfg.get("half_life_days", 30)))
    return _cache


def safe_call(
    fn: Callable[..., Any], *args: Any, default: Any = None
) -> Any:
    """Run a cache operation. A broken cache (locked file, bad row) must never break a web tool:
    it just behaves like an empty cache."""
    try:
        return fn(*args)
    except Exception:
        return default
