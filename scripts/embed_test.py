"""
embed_test.py - Checks the embedding client (against a local fake server), hybrid
search (keyword + meaning, merged), scope isolation, re-embedding rules and every
fallback. No model needed, but sqlite-vec must be installed.
Run from the project root:  python -m scripts.embed_test
"""

import json
import math
import re
import sqlite3
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import danica.memory_index as memory_index
from danica.embeddings import DOC_PREFIX, QUERY_PREFIX, Embedder, EmbeddingError
from danica.memory import MemoryStore
from danica.memory_index import MemoryIndex, VectorError
from danica.tools import run_tool
from danica.workspace import Workspace


def check(label: str, condition: bool) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    assert condition, label


def fails(fn, *args) -> bool:
    try:
        fn(*args)
        return False
    except EmbeddingError:
        return True


# --- sqlite-vec must load in this Python ---
try:
    import sqlite_vec
    probe = sqlite3.connect(":memory:")
    probe.enable_load_extension(True)
    sqlite_vec.load(probe)
    probe.execute("select vec_version()").fetchone()
    loaded = True
except Exception:
    loaded = False
check("sqlite-vec loads in this Python", loaded)

# ------------------------------------------------------- the client, over HTTP
REQUESTS: list[list[str]] = []
STATE = {"mode": "ok"}


class EmbedHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _reply(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        inputs = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["input"]
        REQUESTS.append(inputs)
        mode = STATE["mode"]
        if mode == "http500":
            return self._reply(500, b"{}")
        if mode == "nan":
            return self._reply(200, b'{"data":[{"index":0,"embedding":[NaN, 1.0]}]}')
        if mode == "zero":
            return self._reply(200, b'{"data":[{"index":0,"embedding":[0, 0]}]}')
        if mode == "shape":
            return self._reply(200, b'{"nope": 1}')
        # Input i gets the vector [i+1, 1, 0, 0]. Sent REVERSED: the client must sort by "index".
        data = [{"index": i, "embedding": [float(i + 1), 1.0, 0.0, 0.0]} for i in range(len(inputs))]
        if mode == "wrongcount":
            data = data[:1]
        self._reply(200, json.dumps({"data": list(reversed(data))}).encode())


server = ThreadingHTTPServer(("127.0.0.1", 0), EmbedHandler)
threading.Thread(target=server.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{server.server_address[1]}"

try:
    emb = Embedder(url, "test-model")
    vecs = emb.embed_documents([f"doc {i}" for i in range(20)])
    check("20 documents go out as batches of 16 and 4", [len(r) for r in REQUESTS] == [16, 4])
    check("every document gets the search_document prefix",
          all(t.startswith(DOC_PREFIX) for r in REQUESTS for t in r))
    check("vectors come back in input order (the client sorts by index)",
          all(abs(v[0] / v[1] - ((i % 16) + 1)) < 1e-6 for i, v in enumerate(vecs)))
    check("vectors are normalised", all(abs(math.sqrt(sum(x * x for x in v)) - 1) < 1e-9 for v in vecs))

    emb.embed_query("hello")
    check("queries get the search_query prefix", REQUESTS[-1] == [QUERY_PREFIX + "hello"])
    emb.embed_query("x" * 1000)
    check("long queries are cut", len(REQUESTS[-1][0]) == len(QUERY_PREFIX) + 500)

    for mode, label in [("http500", "an HTTP error"), ("wrongcount", "the wrong number of vectors"),
                        ("nan", "a NaN vector"), ("zero", "a zero vector"), ("shape", "an unexpected answer")]:
        STATE["mode"] = mode
        # Two texts, so that "the server sent back only one vector" is actually a mismatch
        check(f"the client reports {label}", fails(Embedder(url, "m").embed_documents, ["one", "two"]))
    STATE["mode"] = "ok"
finally:
    server.shutdown()

dead = Embedder("http://127.0.0.1:9", "m")
started = time.time()
check("a dead server is reported", fails(dead.embed_query, "hello"))
check("and remembered, so the next call fails instantly",
      dead.down_until > time.time() and fails(dead.embed_query, "again") and time.time() - started < 6)


# ------------------------------------------- the index, with a fake embedder
CONCEPTS = {"indentation": 0, "indent": 0, "whitespace": 0, "spacing": 0, "tabs": 0,
            "raspberry": 1, "pi": 1, "hardware": 1, "deploy": 1, "debian": 1,
            "global": 2, "variables": 2, "unittest": 3, "pytest": 3, "testing": 3}


class FakeEmbedder:
    """Words in the same concept group have the same direction, so 'whitespace' is a
    neighbour of 'indentation'. Notes and queries with NO concept words point at different
    axes (7 and 6), so nonsense queries match nothing."""
    model_name = "fake-v1"

    def __init__(self):
        self.docs_embedded = 0
        self.fail = ""

    def _vec(self, text: str, doc: bool) -> list[float]:
        v = [0.0] * 8
        for word in re.findall(r"[a-z0-9]+", text.lower()):
            if word in CONCEPTS:
                v[CONCEPTS[word]] += 1
        if sum(v[:6]) == 0:
            v[7 if doc else 6] = 1.0
        else:
            v[7] = 0.05
        n = math.sqrt(sum(x * x for x in v))
        return [x / n for x in v]

    def embed_documents(self, texts):
        if self.fail:
            raise EmbeddingError(self.fail)
        self.docs_embedded += len(texts)
        return [self._vec(t, True) for t in texts]

    def embed_query(self, text):
        if self.fail:
            raise EmbeddingError(self.fail)
        return self._vec(text, False)


with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    tmp = Path(tmp)
    db_path = tmp / "facts.db"
    fake = FakeEmbedder()
    index = MemoryIndex(db_path, embedder=fake, min_similarity=0.5)

    def make_store(project: str, session: str, idx: MemoryIndex = index) -> MemoryStore:
        s = MemoryStore(tmp / "g", tmp / "projects" / project / "memory", tmp / "sessions" / session / "memory")
        s.index = idx
        return s

    def vector_rows() -> int:
        raw = sqlite3.connect(db_path)
        n = raw.execute("SELECT count(*) FROM facts_vec").fetchone()[0]
        raw.close()
        return n

    a = make_store("A", "s1")
    a.add("global", "I prefer 4-space indentation and type hints")
    a.add("project", "The deploy target is a Raspberry Pi 4 running Debian")
    a.add("project", "Never use global variables in this codebase")
    a.add("chat", "Today I only fix bugs in testing")

    # --- meaning search: no shared keyword, same concept ---
    hits = index.recall(a, "what whitespace style do I like")
    check("a paraphrase finds the note by meaning", bool(hits) and hits[0].scope == "global" and hits[0].via == "meaning")
    check("the search ran in hybrid mode", index.mode == "hybrid" and index.note == "")
    check("every open note was embedded once", fake.docs_embedded == 4 and vector_rows() == 4)

    hits = index.recall(a, "which hardware do we deploy to")
    check("keyword AND meaning agreeing is reported as both", bool(hits) and hits[0].via == "both" and "Raspberry" in hits[0].body)
    check("nonsense matches nothing, and the closest score is reported",
          index.recall(a, "xylophone quasar") == [] and index.nearest is not None and index.nearest < 0.5)

    before = fake.docs_embedded
    index.recall(a, "whitespace")
    check("a second search embeds no notes again", fake.docs_embedded == before)

    # --- scope isolation ---
    a.add("project", "Staging hardware is an old laptop")
    b = make_store("B", "s2")
    check("another project's notes are invisible to meaning search too", index.recall(b, "deploy hardware") == [])
    check("global notes are still shared", bool(index.recall(b, "whitespace")))

    # --- edits, deletes, model swaps ---
    note = a.find(a.list_pages("project")[0].id)
    before = fake.docs_embedded
    note.path.write_text(note.path.read_text(encoding="utf-8").replace("laptop", "orchard"), encoding="utf-8")
    index.recall(a, "hardware")
    check("an edited note is re-embedded alone", fake.docs_embedded == before + 1)

    rows = vector_rows()
    a.delete(note)
    index.recall(a, "hardware")
    check("a deleted note loses its vector", vector_rows() == rows - 1)

    fake2 = FakeEmbedder()
    fake2.model_name = "fake-v2"
    index2 = MemoryIndex(db_path, embedder=fake2, min_similarity=0.5)
    a2 = make_store("A", "s1", index2)
    index2.recall(a2, "whitespace")
    raw = sqlite3.connect(db_path)
    old = raw.execute("SELECT count(*) FROM facts_vec WHERE model='fake-v1'").fetchone()[0]
    raw.close()
    check("a new model label re-embeds everything and drops the old vectors",
          fake2.docs_embedded == len(a2.list_pages()) and old == 0)

    check("reindex clears every vector", index2.reindex() > 0 and vector_rows() == 0)
    index2.recall(a2, "whitespace")
    check("and the next search rebuilds them", vector_rows() == len(a2.list_pages()))

    status = " | ".join(index2.status_lines(a2))
    check("status reports counts, the server and sqlite-vec",
          "notes in the open scopes" in status and "reachable" in status and "sqlite-vec: loaded" in status)

    # --- reciprocal rank fusion: found twice beats found once ---
    # Its OWN global folder: the shared one holds an "indentation" note that would join the results
    rrf = MemoryStore(tmp / "rrf_global", tmp / "projects" / "R" / "memory", tmp / "sessions" / "s3" / "memory")
    rrf.index = MemoryIndex(tmp / "rrf.db", embedder=FakeEmbedder(), min_similarity=0.5)
    rrf.add("project", "indentation rules for this codebase")
    rrf.add("project", "whitespace tabs policy")
    hits = rrf.index.recall(rrf, "indentation")
    check("the note found by both methods ranks first",
          len(hits) == 2 and hits[0].via == "both" and "indentation rules" in hits[0].body and hits[1].via == "meaning")

    # --- fallbacks: every failure degrades to keywords and says why ---
    fake.fail = "the embedding server is down"
    hits = index.recall(a, "indentation")
    check("embedding server down: keyword results with the reason",
          bool(hits) and all(h.via == "keyword" for h in hits) and index.mode == "keyword" and "down" in index.note)
    fake.fail = ""

    real_load = memory_index._load_vec

    def broken(db):
        raise VectorError("sqlite-vec is not installed (test)")

    memory_index._load_vec = broken
    try:
        hits = index.recall(a, "indentation")
        check("sqlite-vec missing: keyword results with the reason",
              bool(hits) and index.mode == "keyword" and "not installed" in index.note)
    finally:
        memory_index._load_vec = real_load

    plain = make_store("A", "s1", MemoryIndex(db_path))
    plain.index.recall(plain, "indentation")
    check("no embedder configured: plain keyword search, no note", plain.index.mode == "keyword" and plain.index.note == "")

    # --- what the model is told ---
    ws = Workspace(tmp / "ws")
    ws.memory = a
    fake.fail = "the embedding server is down"
    out = run_tool("recall", {"query": "indentation"}, ws)
    check("the recall tool says when it is keyword-only", "Keyword matches only" in out and "down" in out)
    fake.fail = ""
    out = run_tool("recall", {"query": "whitespace style"}, ws)
    check("the recall tool finds the note by meaning", "indentation" in out and "Keyword matches only" not in out)

print("\nAll good.")