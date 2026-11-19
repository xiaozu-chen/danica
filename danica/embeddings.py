"""
embeddings.py - Client for the second llama-server (the embedding model).

nomic-embed-text-v1.5 expects task prefixes. Forgetting them degrades retrieval:
    notes   ->  "search_document: <text>"
    queries ->  "search_query: <text>"

Vectors come back L2-normalised (we normalise again to be sure), so a dot product
equals cosine similarity. When the server can't be reached we remember that for 5
minutes, so a missing server costs one fast failure instead of a delay on every request.
"""

import math
import time

import httpx

from danica import llm
from danica.config import CONFIG

DOC_PREFIX = "search_document: "
QUERY_PREFIX = "search_query: "
BATCH = 16                 # notes per request
MAX_DOC_CHARS = 3000       # notes are at most 2000 characters, so this never cuts one
MAX_QUERY_CHARS = 500
COOLDOWN_SECONDS = 300     # after a failed connection or an error answer, don't try again for this long (a model without
                           # embedding support answers with an error EVERY time: asking on every message just costs time)


class EmbeddingError(Exception):
    """A problem whose message is safe to show the user and the model."""


def _normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if not math.isfinite(norm) or norm == 0:
        raise EmbeddingError("the embedding server returned an unusable vector")
    return [x / norm for x in vec]


class Embedder:
    """Generate text embeddings with the configured local model."""

    def __init__(self, url: str, model_name: str):
        self.url = url.rstrip("/")
        self.model_name = model_name     # a LABEL: change it when you swap models (rebuilds the index)
        self.down_until = 0.0
        self.down_reason = ""

    def _request(self, inputs: list[str], read_seconds: float) -> list[list[float]]:
        now = time.time()
        if now < self.down_until:
            raise EmbeddingError(self.down_reason)       # recently failed: don't wait again
        try:
            resp = llm.local_http().post(f"{self.url}/v1/embeddings",
                              json={"input": inputs, "model": self.model_name},
                    timeout=httpx.Timeout(
                        5.0, connect=2.0, read=read_seconds
                    ),
                              )                          # (shared client: ignores proxy variables)
        except (httpx.ConnectError, httpx.TimeoutException):
            self.down_reason = (f"the embedding server at {self.url} did not answer (start it, or set "
                                f"[embeddings] enabled = false)")
            self.down_until = now + COOLDOWN_SECONDS
            raise EmbeddingError(self.down_reason)
        except httpx.HTTPError as e:
            raise EmbeddingError(f"the embedding request failed ({type(e).__name__})")
        if resp.status_code >= 400:
            self.down_reason = f"the embedding server returned HTTP {resp.status_code} (it may not support embeddings)"
            self.down_until = now + COOLDOWN_SECONDS
            raise EmbeddingError(self.down_reason)

        try:
            items = sorted(resp.json()["data"], key=lambda d: d.get("index", 0))
            vectors = [d["embedding"] for d in items]
        except (ValueError, KeyError, TypeError, AttributeError):
            raise EmbeddingError("the embedding server's answer was not in the expected format")
        if len(vectors) != len(inputs):
            raise EmbeddingError("the embedding server returned the wrong number of vectors")

        out, dim = [], None
        for v in vectors:
            if (
                not isinstance(v, list)
                or not v
                or not all(isinstance(x, (int, float)) for x in v)
            ):
                raise EmbeddingError("the embedding server's answer was not in the expected format")
            if dim is not None and len(v) != dim:
                raise EmbeddingError("the embedding server returned vectors of different sizes")
            dim = len(v)
            out.append(_normalize([float(x) for x in v]))
        return out

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Vectors for notes, in order. Sent in batches."""
        out: list[list[float]] = []
        for i in range(0, len(texts), BATCH):
            batch = [DOC_PREFIX + t[:MAX_DOC_CHARS] for t in texts[i:i + BATCH]]
            out += self._request(batch, read_seconds=60.0)
        return out

    def embed_query(self, text: str) -> list[float]:
        """The vector for a search query."""
        return self._request(
            [QUERY_PREFIX + text[:MAX_QUERY_CHARS]], read_seconds=15.0
        )[0]


_cached: Embedder | None = None


def get_embedder() -> Embedder | None:
    """The shared Embedder (so the cooldown is remembered across sessions), or None if
    embeddings are switched off in config.toml."""
    global _cached
    cfg = CONFIG["embeddings"]
    if not cfg.get("enabled"):
        return None
    if _cached is None or (_cached.url, _cached.model_name) != (cfg["url"].rstrip("/"), cfg["model_name"]):
        _cached = Embedder(cfg["url"], cfg["model_name"])
    return _cached
