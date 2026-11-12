"""
webcache.py - A persistent, searchable memory of web pages and web searches.

WHY: web_search / web_fetch hit the network every time, and search providers rate-limit.
This module keeps what Danica already read in %APPDATA%/Danica/webcache.db so that:
  1. the same web_fetch URL / web_search query is answered from disk (no request),
  2. when a live request FAILS (rate limit, offline), a stale copy is served instead of an error,
  3. Danica can search what is stored by MEANING (web_cache_search) and is shown a short list of it
     every request (see Agent._cache_context), so it looks there before searching the web.

DESIGN (from Proposal.md):
  - Tiered TTL     : fast-changing 30 min | normal 3 days | evergreen 30 days (ttl_for)
  - Two ages       : "fresh" = inside its TTL (served without asking the network). "stale" = past
                     its TTL but under MAX_AGE_DAYS (only served when the live request fails).
                     Older than MAX_AGE_DAYS = deleted.
  - Hash diffing   : refetching an unchanged page only resets its clock; no re-embedding.
  - Recency decay  : search score x e^(-lambda * age_days), so newer copies win ties.
  - Intent routing : fresh-sensitive searches ("latest", "price", "today"...) skip the cache
                     (see wants_live) and use it only as a fallback.

SECURITY: cached text is still UNTRUSTED web content. This file only stores and finds it; the
tools that return it wrap it in <untrusted_web_content> and mark the session tainted, exactly as
for a live fetch. Nothing here ever raises into a tool: callers wrap cache calls in try/except.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import struct
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass

from danica.config import APP_DIR, CONFIG
from danica.embeddings import EmbeddingError, get_embedder
from danica.web import host_of

MINUTE, HOUR, DAY = 60, 3600, 86400
TTL_FAST = 30 * MINUTE          # prices, weather, news, live status
TTL_NORMAL = 3 * DAY            # docs, forums, product pages
TTL_EVERGREEN = 30 * DAY        # history, definitions, core concepts
MAX_AGE_DAYS = 90               # past this a row is deleted, even as a fallback
MAX_EMBED_CHARS = 2000          # one vector per PAGE: its title plus the start of its text
TERM_RX = re.compile(r"[^\W_]+", re.UNICODE)

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


@dataclass
class CachedPage:
    url: str
    final_url: str
    title: str
    passages: list[str]
    fetched_at: float
    ttl: float
    stale: bool            # True = past its TTL (only returned when the caller allows stale)

    @property
    def age(self) -> float:
        return time.time() - self.fetched_at


@dataclass
class CachedSearch:
    query: str
    results: list          # [[title, url, snippet], ...]
    searched_at: float
    stale: bool

    @property
    def age(self) -> float:
        return time.time() - self.searched_at


@dataclass
class CacheHit:
    url: str
    title: str
    snippet: str
    age: float
    stale: bool
    score: float
    via: str               # "keyword" | "meaning" | "both"


class WebCache:
    def __init__(self, path, embedder=None, max_pages: int = 400, half_life_days: float = 30.0):
        self.path = path
        self.embedder = embedder
        self.max_pages = max_pages
        self.decay = math.log(2) / max(half_life_days, 1.0)     # lambda, per day
        self.note = ""                       # why meaning-search was skipped (our own words)
        self._lock = threading.RLock()
        self._cleaned = False

    # ------------------------------------------------------------------ storage

    @contextmanager
    def _db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=15)   # subagent processes share this file
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS web_pages (
                    url TEXT PRIMARY KEY, final_url TEXT NOT NULL, title TEXT NOT NULL,
                    passages TEXT NOT NULL, content_hash TEXT NOT NULL,
                    fetched_at REAL NOT NULL, ttl REAL NOT NULL,
                    hits INTEGER NOT NULL DEFAULT 0, last_hit REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS web_searches (
                    qkey TEXT PRIMARY KEY, query TEXT NOT NULL, results TEXT NOT NULL,
                    searched_at REAL NOT NULL, ttl REAL NOT NULL, hits INTEGER NOT NULL DEFAULT 0
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS web_fts USING fts5(
                    url UNINDEXED, ordinal UNINDEXED, title, content, tokenize='porter unicode61'
                );
                CREATE TABLE IF NOT EXISTS web_vectors (
                    url TEXT PRIMARY KEY, model TEXT NOT NULL, dim INTEGER NOT NULL, vec BLOB NOT NULL
                );
            """)
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _cleanup(self, db: sqlite3.Connection) -> None:
        """Once per process: delete rows past MAX_AGE_DAYS, then the least-recently-used pages
        beyond max_pages. (Replaces the proposal's daily cron job: no scheduler needed.)"""
        if self._cleaned:
            return
        self._cleaned = True
        now = time.time()
        old = [r[0] for r in db.execute("SELECT url FROM web_pages WHERE fetched_at < ?",
                                        (now - MAX_AGE_DAYS * DAY,))]
        over = [r[0] for r in db.execute(
            "SELECT url FROM web_pages ORDER BY last_hit DESC, fetched_at DESC LIMIT -1 OFFSET ?",
            (self.max_pages,))]
        self._delete_pages(db, set(old) | set(over))
        db.execute("DELETE FROM web_searches WHERE searched_at < ?", (now - MAX_AGE_DAYS * DAY,))

    @staticmethod
    def _delete_pages(db: sqlite3.Connection, urls) -> None:
        for url in urls:
            db.execute("DELETE FROM web_pages WHERE url=?", (url,))
            db.execute("DELETE FROM web_fts WHERE url=?", (url,))
            db.execute("DELETE FROM web_vectors WHERE url=?", (url,))

    # ------------------------------------------------------------------ pages

    def get_page(self, url: str, allow_stale: bool = False) -> CachedPage | None:
        """The stored page. Fresh pages are always returned; stale ones only if allow_stale."""
        with self._lock, self._db() as db:
            self._cleanup(db)
            row = db.execute("SELECT url,final_url,title,passages,fetched_at,ttl FROM web_pages "
                             "WHERE url=?", (url,)).fetchone()
            if row is None:
                return None
            stale = time.time() - row[4] > row[5]
            if stale and not allow_stale:
                return None
            db.execute("UPDATE web_pages SET hits=hits+1, last_hit=? WHERE url=?", (time.time(), url))
            return CachedPage(row[0], row[1], row[2], json.loads(row[3]), row[4], row[5], stale)

    def put_page(self, url: str, final_url: str, title: str, passages: list[str]) -> str:
        """Store a freshly fetched page. Returns "new", "changed" or "unchanged".
        Unchanged (same SHA-256) only resets the clock: no re-indexing, no re-embedding."""
        digest = hashlib.sha256("\n\n".join(passages).encode("utf-8")).hexdigest()
        now = time.time()
        ttl = ttl_for(title, final_url or url)
        with self._lock, self._db() as db:
            self._cleanup(db)
            row = db.execute("SELECT content_hash FROM web_pages WHERE url=?", (url,)).fetchone()
            if row and row[0] == digest:
                db.execute("UPDATE web_pages SET fetched_at=?, ttl=? WHERE url=?", (now, ttl, url))
                return "unchanged"
            self._delete_pages(db, [url])
            db.execute("INSERT INTO web_pages VALUES (?,?,?,?,?,?,?,1,?)",
                       (url, final_url, title, json.dumps(passages, ensure_ascii=False),
                        digest, now, ttl, now))
            db.executemany("INSERT INTO web_fts(url, ordinal, title, content) VALUES (?,?,?,?)",
                           [(url, i, title, p) for i, p in enumerate(passages, start=1)])
            self._embed_page(db, url, title, passages)
            return "changed" if row else "new"

    def _embed_page(self, db: sqlite3.Connection, url: str, title: str, passages: list[str]) -> None:
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

    # ------------------------------------------------------------------ searches

    def get_search(self, query: str, allow_stale: bool = False) -> CachedSearch | None:
        key = normalize_query(query)
        if not key:
            return None
        with self._lock, self._db() as db:
            self._cleanup(db)
            row = db.execute("SELECT query,results,searched_at,ttl FROM web_searches WHERE qkey=?",
                             (key,)).fetchone()
            if row is None:
                return None
            stale = time.time() - row[2] > row[3]
            if stale and not allow_stale:
                return None
            db.execute("UPDATE web_searches SET hits=hits+1 WHERE qkey=?", (key,))
            return CachedSearch(row[0], json.loads(row[1]), row[2], stale)

    def put_search(self, query: str, results: list) -> None:
        key = normalize_query(query)
        if not key or not results:
            return                                   # never cache an empty answer
        with self._lock, self._db() as db:
            db.execute("INSERT OR REPLACE INTO web_searches VALUES (?,?,?,?,?,0)",
                       (key, query[:300], json.dumps([list(r) for r in results], ensure_ascii=False),
                        time.time(), ttl_for(query)))

    # ------------------------------------------------------------------ meaning + keyword search

    def search(self, query: str, limit: int = 5) -> list[CacheHit]:
        """Hybrid search over stored pages: keyword (BM25) + meaning (embeddings), merged with
        reciprocal-rank fusion, then multiplied by a recency decay."""
        terms = list(dict.fromkeys(t.lower() for t in TERM_RX.findall(query) if len(t) > 1))[:12]
        if not terms:
            return []
        limit = max(1, min(int(limit), 10))
        match = " OR ".join(f'"{t}"' for t in terms)
        now = time.time()
        with self._lock, self._db() as db:
            self._cleanup(db)
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
                row = db.execute("SELECT title, passages, fetched_at, ttl FROM web_pages WHERE url=?",
                                 (url,)).fetchone()
                if row is None:
                    continue
                age_days = (now - row[2]) / DAY
                snippet = snippets.get(url) or (json.loads(row[1]) or [""])[0]
                kind = "both" if len(via[url]) == 2 else next(iter(via[url]))
                hits.append(CacheHit(url, row[0], " ".join(snippet.split())[:280], now - row[2],
                                     now - row[2] > row[3],
                                     score * math.exp(-self.decay * age_days), kind))
            hits.sort(key=lambda h: h.score, reverse=True)
            return hits[:limit]

    # ------------------------------------------------------------------ overview + housekeeping

    def recent(self, limit: int = 8) -> list[tuple[str, str, float, bool]]:
        """(url, title, age_seconds, stale) of the most recently used pages."""
        with self._lock, self._db() as db:
            self._cleanup(db)
            now = time.time()
            return [(u, t, now - f, now - f > ttl) for u, t, f, ttl in db.execute(
                "SELECT url, title, fetched_at, ttl FROM web_pages "
                "ORDER BY last_hit DESC, fetched_at DESC LIMIT ?", (limit,))]

    def status(self) -> dict:
        with self._lock, self._db() as db:
            self._cleanup(db)
            pages = db.execute("SELECT COUNT(*), COALESCE(SUM(fetched_at + ttl < ?), 0) FROM web_pages",
                               (time.time(),)).fetchone()
            return {"pages": pages[0], "stale_pages": int(pages[1]),
                    "searches": db.execute("SELECT COUNT(*) FROM web_searches").fetchone()[0],
                    "vectors": db.execute("SELECT COUNT(*) FROM web_vectors").fetchone()[0],
                    "embedding_note": self.note}

    def clear(self) -> int:
        """Forget everything. Returns how many pages were removed."""
        with self._lock, self._db() as db:
            count = db.execute("SELECT COUNT(*) FROM web_pages").fetchone()[0]
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
        _cache = WebCache(APP_DIR / "webcache.db", get_embedder(),
                          int(cfg.get("max_pages", 400)), float(cfg.get("half_life_days", 30)))
    return _cache


def safe_call(fn, *args, default=None):
    """Run a cache operation. A broken cache (locked file, bad row) must never break a web tool:
    it just behaves like an empty cache."""
    try:
        return fn(*args)
    except Exception:
        return default