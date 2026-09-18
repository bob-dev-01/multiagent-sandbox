"""Task embeddings for the reuse decision.

Local `sentence-transformers` rather than a hosted embedding API. Three reasons,
in order: the model is pinned and offline so the study is reproducible years
later; there is no second vendor, key or bill; and the Anthropic API has no
embeddings endpoint, so the `voyage-3-lite "via Anthropic"` line in Task 7 does
not describe a real integration (OQ-12).

The threshold tau = 0.85 is a property of *this model's* geometry, not a
universal constant. Changing `MODEL_NAME` means re-tuning tau and reindexing the
registry, which is why the model name is stored on every row.
"""

from __future__ import annotations

import hashlib
import math
import struct
from functools import lru_cache
from typing import Protocol

MODEL_NAME = "all-MiniLM-L6-v2"
EMBEDDING_DIM = 384


class Embedder(Protocol):
    model_name: str
    dimension: int

    def embed(self, text: str) -> list[float]: ...
    def embed_batch(self, texts: list[str]) -> list[list[float]]: ...


class SentenceTransformerEmbedder:
    """The real embedder. Loads the model once per process."""

    def __init__(self, model_name: str = MODEL_NAME) -> None:
        self.model_name = model_name
        self.dimension = EMBEDDING_DIM
        self._model = None

    def _load(self):  # type: ignore[no-untyped-def]
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError(
                    "sentence-transformers is not installed. "
                    "Install the 'embeddings' extra, or use HashingEmbedder for tests."
                ) from exc
            self._model = SentenceTransformer(self.model_name)
            self.dimension = int(self._model.get_sentence_embedding_dimension())
        return self._model

    def embed(self, text: str) -> list[float]:
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        model = self._load()
        vectors = model.encode(
            texts, normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False
        )
        return [[float(x) for x in row] for row in vectors]


class HashingEmbedder:
    """Deterministic stand-in for tests and offline CI.

    Produces stable unit vectors from token hashes. It captures lexical overlap
    and nothing else — it has no semantics — so it is fine for exercising the
    registry's SQL and the reuse plumbing, and useless for any claim about
    semantic matching. Never use it to produce a number that goes in the thesis.
    """

    def __init__(self, dimension: int = EMBEDDING_DIM) -> None:
        self.model_name = "hashing-stub"
        self.dimension = dimension

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        for token in _tokenise(text):
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = struct.unpack("<I", digest[:4])[0] % self.dimension
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(v * v for v in vector))
        return [v / norm for v in vector] if norm else vector

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]


def _tokenise(text: str) -> list[str]:
    return [t for t in "".join(c.lower() if c.isalnum() else " " for c in text).split() if t]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        raise ValueError("vectors must have the same dimension")
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


@lru_cache(maxsize=1)
def default_embedder() -> Embedder:
    """The real embedder if it is installed, otherwise the hashing stub.

    The fallback keeps tests and CI runnable without a 2 GB torch download. It
    is loud about which one is in use because a silent downgrade would put
    meaningless similarities into real results.
    """
    import logging

    try:
        embedder = SentenceTransformerEmbedder()
        embedder._load()  # noqa: SLF001 — fail now rather than at first query
        return embedder
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "sentence-transformers unavailable; falling back to HashingEmbedder. "
            "Similarity scores from this embedder are lexical only and must not "
            "be reported as results."
        )
        return HashingEmbedder()
