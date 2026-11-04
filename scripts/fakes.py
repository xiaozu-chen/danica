"""
fakes.py - Test doubles shared by several test scripts. Not a test itself.
"""

import math
import re

from danica.embeddings import EmbeddingError

# Words in the same group point the same way, so 'whitespace' is a neighbour of 'indentation'.
CONCEPTS = {"indentation": 0, "indent": 0, "whitespace": 0, "spacing": 0, "tabs": 0,
            "raspberry": 1, "pi": 1, "hardware": 1, "deploy": 1, "debian": 1,
            "global": 2, "variables": 2, "unittest": 3, "pytest": 3, "testing": 3}


class FakeEmbedder:
    """A pretend embedding server. Notes and queries with NO concept words point at
    different axes (7 and 6), so nonsense queries match nothing."""
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