"""
memory_index.py - SQLite + FTS5 + vectors: search over the memory pages.

The Markdown pages are the truth. facts.db is a DERIVED index: sync() makes it match
the pages open right now (adds new, updates edited, removes deleted), so a hand edit or
a deleted file never leaves a stale row, and deleting facts.db is safe.

Search is HYBRID when an embedding server is available:
    keyword results (FTS5, stemmed)  +  meaning results (cosine similarity of vectors)
    merged by reciprocal rank fusion:  score = sum of 1 / (60 + rank) over each list.
A note found by both lists beats a note found by one. Vector-only matches must reach
min_similarity, because the nearest note exists even for nonsense.
If the embedding server or sqlite-vec is unavailable, search silently degrades to
keywords and records why in self.note (shown to the user and the model).

Queries only see the scopes open in the session. Rows of other projects and sessions
are never touched or shown.

HONEST LIMITS:
  - Meaning search compares every open note (exact, not an approximate index): fine
    for thousands of notes, not for a whole document library (Phase 10 adds vec0).
  - Rows of other projects/sessions stay until a cleanup pass (a later chunk).
  - secure_delete zeroes deleted text in the main tables, but FTS5 keeps its own
    index segments until they merge: the forget step (chunk 4) deals with that.
"""

import hashlib
import re
import sqlite3
import struct
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from danica.config import APP_DIR
from danica.embeddings import Embedder, EmbeddingError
from danica.memory import MemoryStore, MemoryStoreError, Page

FACTS_DB = APP_DIR / "facts.db"
MAX_HITS = 8
CANDIDATES = 20      # how many results each method (keywords, meaning) contributes to the merge
RRF_K = 60           # the standard reciprocal-rank-fusion constant
EMBED_BATCH = 16     # notes embedded per request (and per commit)

# Letters and digits only: nothing from a query can ever become FTS5 syntax
TERM_RX = re.compile(r"[^\W_]+")
STOPWORDS = set(
    "a an and are as at be but by can could did do does for from had has have how i if in is it "
    "its me my of on or our so than that the their them then there these they this to was we were "
    "what when where which who why will with would you your about not".split())

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY,
    uuid TEXT NOT NULL UNIQUE,
    scope TEXT NOT NULL,
    owner TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    body TEXT NOT NULL,
    source TEXT NOT NULL,
    confidence REAL NOT NULL,
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    page TEXT NOT NULL,
    hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS facts_owner ON facts(scope, owner);
CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(title, body, tokenize='porter unicode61');
CREATE TABLE IF NOT EXISTS facts_vec (
    uuid TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    dim INTEGER NOT NULL,
    hash TEXT NOT NULL,
    vec BLOB NOT NULL
);
"""


class VectorError(MemoryStoreError):
    """sqlite-vec can't be used (not installed, or this Python can't load extensions)."""


def query_terms(text: str, max_terms: int = 12) -> list[str]:
    """The searchable words of a query: lowercase letters and digits, no stop words,
    no one-letter words, no duplicates, at most max_terms."""
    terms: list[str] = []
    for word in TERM_RX.findall(text.lower()):
        if len(word) < 2 or word in STOPWORDS or word in terms:
            continue
        terms.append(word)
        if len(terms) >= max_terms:
            break
    return terms


@dataclass
class Hit:
    """Represent one result from the memory search index."""

    uuid: str
    scope: str
    summary: str
    body: str
    score: float = 0.0           # the merged (RRF) score: higher is better
    sim: float | None = None     # cosine similarity, if the meaning search found it
    via: str = "keyword"         # "keyword", "meaning" or "both"

    @property
    def id(self) -> str:
        """Return the short identifier shown in search results."""
        return self.uuid[:8]


def _confidence(page: Page) -> float:
    try:
        return min(max(float(page.confidence), 0.0), 1.0)
    except ValueError:
        return 0.5            # a hand-edited, unreadable value: "unknown"


def _digest(page: Page, owner: str) -> str:
    """Fingerprint of everything that goes into the text index, to spot changed pages."""
    parts = (
        page.scope,
        owner,
        page.title,
        page.summary,
        page.body,
        page.source,
        page.confidence,
    )
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


def embed_text(page: Page) -> str:
    """What gets embedded: the title and the text (without repeating a title the body starts with)."""
    return page.body if page.body.startswith(page.title) else f"{page.title}\n{page.body}"


def _vec_hash(text: str, model: str) -> str:
    return hashlib.sha256(f"{model}\x00{text}".encode("utf-8")).hexdigest()


def _pack(vec: list[float]) -> bytes:
    """A vector as raw little-endian float32 bytes (the format sqlite-vec reads)."""
    return struct.pack(f"<{len(vec)}f", *vec)


def _load_vec(db: sqlite3.Connection) -> None:
    """Load the sqlite-vec extension into this connection."""
    try:
        import sqlite_vec
    except ImportError:
        raise VectorError("the sqlite-vec package is not installed (pip install sqlite-vec)")
    try:
        db.enable_load_extension(True)
        sqlite_vec.load(db)
        db.enable_load_extension(False)
    except (AttributeError, sqlite3.Error, OSError) as e:
        raise VectorError(f"sqlite-vec could not be loaded ({type(e).__name__}): this Python's "
                          f"SQLite may not allow extensions")

def _shred(path: Path) -> None:
    """Overwrite a file with zeros, in place, and flush it to disk. (Does NOT delete it.)
    HONEST LIMIT: best effort. SSD wear-levelling, filesystem journals, shadow copies and
    backups can keep old bytes: no program can promise more."""
    size = path.stat().st_size
    with open(path, "r+b") as f:
        left = size
        while left > 0:
            n = min(left, 1 << 20)
            f.write(b"\0" * n)
            left -= n
        f.flush()
        os.fsync(f.fileno())


class MemoryIndex:
    """Build and query the local index of memory notes."""

    def __init__(
        self,
        path: Path,
        embedder: Embedder | None = None,
        min_similarity: float = 0.50,
    ):
        self.path = Path(path)
        self.embedder = embedder              # None = keyword search only
        self.min_similarity = min_similarity
        # What the LAST recall() did (shown by /memory search and the recall tool)
        self.mode = "keyword"                 # "keyword" or "hybrid"
        self.note = ""                        # why meaning search was skipped, if it was
        self.nearest: float | None = None     # best meaning score seen (even below the threshold)

    @contextmanager
    def _conn(self, vectors: bool = False):
        """A connection that commits on success, rolls back on error and always closes.
        sqlite problems become MemoryStoreError. vectors=True also loads sqlite-vec
        (raising VectorError if it can't)."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(self.path, timeout=10)
        except (OSError, sqlite3.Error) as e:
            raise MemoryStoreError(f"could not open the memory index: {e}")
        try:
            db.execute("PRAGMA secure_delete=ON")      # deleted text is zeroed, not left behind
            try:
                db.executescript(SCHEMA)
            except sqlite3.OperationalError as e:
                if "fts5" in str(e).lower():
                    raise MemoryStoreError("this Python's SQLite has no FTS5 support, so memory "
                                           "search is unavailable")
                raise
            if vectors:
                _load_vec(db)
            yield db
            db.commit()
        except sqlite3.Error as e:
            try:
                db.rollback()
            except sqlite3.Error:
                pass
            raise MemoryStoreError(f"memory index problem: {e}")
        finally:
            db.close()

    @staticmethod
    def _scopes(store: MemoryStore, alias: str = "f") -> tuple[str, list[str]]:
        """SQL condition (on the given table alias) matching only the scopes open in this session."""
        clauses, params = [], []
        for scope in store.dirs:
            clauses.append(f"({alias}.scope=? AND {alias}.owner=?)")
            params += [scope, store.owner(scope)]
        return " OR ".join(clauses), params

    # ------------------------------------------------------------------ text index

    def sync(self, store: MemoryStore) -> dict[str, int]:
        """Make the text index match the pages open in this session."""
        counts = {"added": 0, "updated": 0, "removed": 0}
        wanted, seen = [], set()
        for page in store.list_pages():
            if page.uuid not in seen:          # a copied file (same uuid) is indexed once
                seen.add(page.uuid)
                wanted.append(page)

        with self._conn() as db:
            for p in wanted:
                owner = store.owner(p.scope)
                fingerprint = _digest(p, owner)
                row = db.execute("SELECT id, hash FROM facts WHERE uuid=?", (p.uuid,)).fetchone()
                values = (
                    p.scope,
                    owner,
                    p.title,
                    p.summary,
                    p.body,
                    p.source,
                    _confidence(p),
                    p.created,
                    p.updated,
                    str(p.path),
                    fingerprint,
                )
                if row is None:
                    cur = db.execute(
                        "INSERT INTO facts(uuid, scope, owner, title, summary, body, source, confidence, "
                        "created, updated, page, hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (p.uuid,) + values)
                    db.execute("INSERT INTO facts_fts(rowid, title, body) VALUES (?,?,?)",
                               (cur.lastrowid, p.title, p.body))
                    counts["added"] += 1
                elif row[1] != fingerprint:
                    db.execute(
                        "UPDATE facts SET scope=?, owner=?, title=?, summary=?, body=?, source=?, confidence=?, "
                        "created=?, updated=?, page=?, hash=? WHERE id=?", values + (row[0],))
                    db.execute("DELETE FROM facts_fts WHERE rowid=?", (row[0],))
                    db.execute("INSERT INTO facts_fts(rowid, title, body) VALUES (?,?,?)",
                               (row[0], p.title, p.body))
                    counts["updated"] += 1

            # Rows of the OPEN scopes whose page is gone. Other projects' rows are never touched.
            for scope in store.dirs:
                rows = db.execute("SELECT id, uuid FROM facts WHERE scope=? AND owner=?",
                                  (scope, store.owner(scope))).fetchall()
                for row_id, uid in rows:
                    if uid not in seen:
                        db.execute("DELETE FROM facts_fts WHERE rowid=?", (row_id,))
                        db.execute("DELETE FROM facts WHERE id=?", (row_id,))
                        counts["removed"] += 1
        return counts

    # --------------------------------------------------------------------- vectors

    def sync_vectors(self, store: MemoryStore) -> int:
        """Embed notes that are new or changed; drop vectors of removed notes and of any
        other embedding model. Returns how many notes were embedded. Run sync() first.
        Raises EmbeddingError if the server can't be used (earlier batches stay saved)."""
        if self.embedder is None:
            return 0
        model = self.embedder.model_name

        wanted: dict[str, tuple[str, str]] = {}          # uuid -> (text to embed, its hash)
        for page in store.list_pages():
            text = embed_text(page)
            wanted.setdefault(page.uuid, (text, _vec_hash(text, model)))

        with self._conn() as db:
            db.execute("DELETE FROM facts_vec WHERE model != ?", (model,))   # a swapped model: start over
            have = dict(db.execute("SELECT uuid, hash FROM facts_vec").fetchall())
        todo = [
            (uid, text, digest)
            for uid, (text, digest) in wanted.items()
            if have.get(uid) != digest
        ]

        for i in range(0, len(todo), EMBED_BATCH):
            batch = todo[i:i + EMBED_BATCH]
            vectors = self.embedder.embed_documents([text for _, text, _ in batch])   # network: no db open
            rows = [
                (uid, model, len(vector), digest, _pack(vector))
                for (uid, _, digest), vector in zip(batch, vectors)
            ]
            with self._conn() as db:
                db.executemany("INSERT OR REPLACE INTO facts_vec(uuid, model, dim, hash, vec) "
                               "VALUES (?,?,?,?,?)", rows)

        with self._conn() as db:
            db.execute("DELETE FROM facts_vec WHERE uuid NOT IN (SELECT uuid FROM facts)")
        return len(todo)

    def reindex(self) -> int:
        """Throw away every vector (they are rebuilt on the next search). Returns how many."""
        with self._conn() as db:
            n = db.execute("SELECT count(*) FROM facts_vec").fetchone()[0]
            db.execute("DELETE FROM facts_vec")
        return n

    # ---------------------------------------------------------------------- search

    def _search(self, store: MemoryStore, terms: list[str], qvec: list[float] | None):
        """Raw candidates from both methods: (keyword rows, meaning rows). Needs sqlite-vec
        only when qvec is given (raises VectorError if it can't be loaded)."""
        # Every term is a quoted string; words of 4+ letters also match as a prefix
        match = " OR ".join(f'"{t}"*' if len(t) >= 4 else f'"{t}"' for t in terms)
        where, params = self._scopes(store)      # only the scopes open in THIS session

        fts_sql = ("SELECT f.uuid, f.scope, f.summary, f.body, bm25(facts_fts, 4.0, 1.0) AS score "
                   "FROM facts_fts JOIN facts f ON f.id = facts_fts.rowid "
                   f"WHERE facts_fts MATCH ? AND ({where}) "
                   "ORDER BY score, CASE f.scope WHEN 'chat' THEN 0 WHEN 'project' THEN 1 ELSE 2 END "
                   "LIMIT ?")
        vec_sql = ("SELECT f.uuid, f.scope, f.summary, f.body, 1.0 - vec_distance_cosine(v.vec, ?) AS sim "
                   "FROM facts_vec v JOIN facts f ON f.uuid = v.uuid "
                   f"WHERE v.model = ? AND v.dim = ? AND ({where}) "
                   "ORDER BY sim DESC LIMIT ?")

        with self._conn(vectors=qvec is not None) as db:
            fts_rows = db.execute(fts_sql, [match] + params + [CANDIDATES]).fetchall()
            vec_rows = []
            if qvec is not None:
                vec_rows = db.execute(
                    vec_sql,
                    [_pack(qvec), self.embedder.model_name, len(qvec)]
                    + params
                    + [CANDIDATES],
                ).fetchall()
        return fts_rows, vec_rows

    def recall(
        self,
        store: MemoryStore,
        query: str,
        count: int = 5,
        max_terms: int = 12,
    ) -> list[Hit]:
        """The best-matching notes among the open scopes, best first. [] if nothing matches.
        Sets self.mode / self.note / self.nearest to describe what happened."""
        self.mode, self.note, self.nearest = "keyword", "", None
        terms = query_terms(query, max_terms)
        if not terms:
            return []                       # nothing searchable: skip the embedding call too
        count = min(max(1, int(count)), MAX_HITS)

        self.sync(store)
        qvec = None
        if self.embedder is not None:
            try:
                self.sync_vectors(store)
                qvec = self.embedder.embed_query(query)
            except EmbeddingError as e:
                self.note = str(e)          # fall back to keywords, and say why

        try:
            fts_rows, vec_rows = self._search(store, terms, qvec)
            if qvec is not None:
                self.mode = "hybrid"
        except VectorError as e:
            self.note = str(e)
            fts_rows, vec_rows = self._search(store, terms, None)

        # --- reciprocal rank fusion ---
        entries: dict[str, dict] = {}

        def entry(uid, scope, summary, body) -> dict:
            return entries.setdefault(uid, {"scope": scope, "summary": summary, "body": body,
                                            "rrf": 0.0, "sim": None, "fts": False, "vec": False})

        for rank, (uid, scope, summary, body, _) in enumerate(fts_rows, start=1):
            e = entry(uid, scope, summary, body)
            e["rrf"] += 1.0 / (RRF_K + rank)
            e["fts"] = True

        self.nearest = vec_rows[0][4] if vec_rows else None
        kept = [r for r in vec_rows if r[4] >= self.min_similarity]
        for rank, (uid, scope, summary, body, sim) in enumerate(kept, start=1):
            e = entry(uid, scope, summary, body)
            e["rrf"] += 1.0 / (RRF_K + rank)
            e["vec"] = True
            e["sim"] = sim

        precedence = {"chat": 0, "project": 1, "global": 2}     # ties: the more specific scope first
        ranked = sorted(entries.items(), key=lambda kv: (-kv[1]["rrf"], precedence.get(kv[1]["scope"], 3)))
        return [Hit(uid, e["scope"], e["summary"], e["body"], e["rrf"], e["sim"],
                    "both" if e["fts"] and e["vec"] else "meaning" if e["vec"] else "keyword")
                for uid, e in ranked[:count]]

    def status_lines(self, store: MemoryStore) -> list[str]:
        """A short health report for /memory status."""
        self.sync(store)
        where, params = self._scopes(store)
        with self._conn() as db:
            total = db.execute(f"SELECT count(*) FROM facts f WHERE {where}", params).fetchone()[0]
            embedded = 0
            if self.embedder is not None:
                embedded = db.execute("SELECT count(*) FROM facts_vec v JOIN facts f ON f.uuid = v.uuid "
                                      f"WHERE v.model = ? AND ({where})",
                                      [self.embedder.model_name] + params).fetchone()[0]
        lines = [f"notes in the open scopes: {total}"]
        if self.embedder is None:
            lines.append("meaning search: off ([embeddings] enabled = false in config.toml)")
            return lines
        lines.append(f"notes with meaning vectors: {embedded} of {total} "
                     f"(the rest are embedded on the next search)")
        try:
            dim = len(self.embedder.embed_query("status check"))
            lines.append(f"embedding server: reachable, {dim} dimensions, model label "
                         f"'{self.embedder.model_name}'")
        except EmbeddingError as e:
            lines.append(f"embedding server: NOT available ({e})")
        try:
            with self._conn(vectors=True):
                pass
            lines.append("sqlite-vec: loaded")
        except VectorError as e:
            lines.append(f"sqlite-vec: NOT available ({e})")
        lines.append(f"similarity threshold: {self.min_similarity:.2f}")
        return lines

    def near_duplicates(self, store: MemoryStore, threshold: float = 0.88,
                        limit: int = 10) -> list[tuple[float, str, str]]:
        """Pairs of notes in the SAME scope with almost the same meaning:
        (similarity, uuid_a, uuid_b), most similar first. Needs the embedding server."""
        if self.embedder is None:
            raise MemoryStoreError("finding near-duplicates needs meaning search "
                                   "([embeddings] enabled = true in config.toml)")
        self.sync(store)
        try:
            self.sync_vectors(store)
        except EmbeddingError as e:
            raise MemoryStoreError(f"finding near-duplicates needs the embedding server: {e}")

        where, params = self._scopes(store, "a")       # only the scopes open in THIS session
        model = self.embedder.model_name
        sql = ("SELECT a.uuid, b.uuid, 1.0 - vec_distance_cosine(va.vec, vb.vec) AS sim "
               "FROM facts_vec va JOIN facts a ON a.uuid = va.uuid "
               "JOIN facts b ON b.scope = a.scope AND b.owner = a.owner AND b.uuid > a.uuid "
               "JOIN facts_vec vb ON vb.uuid = b.uuid "
               f"WHERE va.model = ? AND vb.model = ? AND va.dim = vb.dim AND ({where}) "
               "AND (1.0 - vec_distance_cosine(va.vec, vb.vec)) >= ? "
               "ORDER BY sim DESC LIMIT ?")
        with self._conn(vectors=True) as db:
            rows = db.execute(
                sql, [model, model] + params + [threshold, limit]
            ).fetchall()
        return [(float(sim), a, b) for a, b, sim in rows]

    def purge(self, uuids: list[str]) -> list[int]:
        """Delete everything the index holds about these notes (rows, text index, vectors),
        then merge the text index's segments so deleted words are not kept in old ones.
        Returns the text-index row ids the notes had (forget uses them to verify)."""
        if not uuids:
            return []
        marks = ",".join("?" * len(uuids))
        with self._conn() as db:
            rowids = [r[0] for r in db.execute(f"SELECT id FROM facts WHERE uuid IN ({marks})", uuids)]
            for rid in rowids:
                db.execute("DELETE FROM facts_fts WHERE rowid=?", (rid,))
            db.execute(f"DELETE FROM facts WHERE uuid IN ({marks})", uuids)
            db.execute(f"DELETE FROM facts_vec WHERE uuid IN ({marks})", uuids)
            db.execute("INSERT INTO facts_fts(facts_fts) VALUES('optimize')")
        return rowids

    def rebuild_file(self) -> None:
        """Rewrite facts.db from its live content only (VACUUM INTO a new file), zero the
        old file, and swap the new one in. Raises MemoryStoreError if it can't (Windows
        refuses to replace a file another Danica window has open)."""
        new = self.path.with_name(self.path.name + ".new")
        siblings = [self.path.with_name(self.path.name + s) for s in ("-wal", "-shm", "-journal")]
        try:
            if new.exists():
                new.unlink()
            raw = sqlite3.connect(self.path, timeout=10, isolation_level=None)
            try:
                raw.execute("VACUUM INTO ?", (str(new),))
            finally:
                raw.close()
            for extra in siblings:
                if extra.exists():
                    _shred(extra)
                    extra.unlink()
            _shred(self.path)
            os.replace(new, self.path)
        except (OSError, sqlite3.Error) as e:
            try:
                if new.exists():
                    new.unlink()
            except OSError:
                pass
            raise MemoryStoreError(f"could not rebuild the index file ({type(e).__name__}: {e}). "
                                   f"Another Danica window may be using it.")

    def leftovers(self, uuids: list[str], rowids: list[int]) -> dict[str, int]:
        """How many rows still mention these notes, per table (all zeros = clean)."""
        marks = ",".join("?" * len(uuids))
        with self._conn() as db:
            counts = {
                "notes": db.execute(f"SELECT count(*) FROM facts WHERE uuid IN ({marks})", uuids).fetchone()[0],
                "vectors": db.execute(f"SELECT count(*) FROM facts_vec WHERE uuid IN ({marks})", uuids).fetchone()[0],
                "text index": 0,
            }
            if rowids:
                rm = ",".join("?" * len(rowids))
                counts["text index"] = db.execute(
                    f"SELECT count(*) FROM facts_fts WHERE rowid IN ({rm})", rowids).fetchone()[0]
        return counts

    def remaining_text(self) -> str:
        """All text still legitimately in the database (every note's fields plus the schema),
        lowercased. Forget uses it to tell a deleted note's words from words other notes share."""
        with self._conn() as db:
            rows = db.execute("SELECT title, summary, body, page FROM facts").fetchall()
            schema = db.execute("SELECT name, sql FROM sqlite_master").fetchall()
        parts = [" ".join(str(x) for x in r) for r in rows] + [" ".join(str(x) for x in r) for r in schema]
        return "\n".join(parts).lower()
