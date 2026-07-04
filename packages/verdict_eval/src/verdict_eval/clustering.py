"""Intent clustering for prompts.

We cluster prompts so the drift detector compares like-with-like even when
the underlying production prompt distribution shifts (Mondays vs Fridays,
new product launches, etc.).

For v0 we use a simple HashingVectorizer + fixed random-projection embedder
with online Birch. A more sophisticated embedding model can swap in via the
Embedder port — both adapters need to exist for the abstraction to be real
(ADR-001).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import numpy as np
from sklearn.cluster import Birch
from sklearn.feature_extraction.text import HashingVectorizer


@runtime_checkable
class Embedder(Protocol):
    """Port: turn texts into dense vectors. Two adapters ship below."""

    dim: int

    def embed(self, texts: list[str]) -> np.ndarray: ...


class HashingEmbedder:
    """Cheap STATELESS embedder using 1990s NLP tech.

    Fast, deterministic, no external dependencies. Useful as a fallback when
    sentence-transformers isn't installed or when tests need determinism.
    For real production cluster quality, use SentenceTransformerEmbedder.

    Pipeline: HashingVectorizer (512 features) → a *deterministic* fixed
    Gaussian random projection down to `dim`, followed by L2 normalization.

    The previous implementation fit a TruncatedSVD on the first batch it saw
    and reused that basis forever — so the embedding of a given text depended
    on whatever data happened to arrive first, and two instances (or two runs)
    could embed the same text differently. A random projection with a fixed
    seed is data-independent: the projection matrix is identical across runs
    and instances, so the same text always maps to the same vector. (Johnson-
    Lindenstrauss says a random projection preserves pairwise distances well
    enough for clustering.) This makes the embedder genuinely stateless — no
    lazy fit, no per-instance state.
    """

    dim = 128
    _N_FEATURES = 512
    _SEED = 42

    def __init__(self) -> None:
        self._vectorizer = HashingVectorizer(
            n_features=self._N_FEATURES, alternate_sign=False
        )
        # Deterministic Gaussian projection 512 -> dim, scaled by 1/sqrt(dim)
        # per the standard random-projection construction.
        rng = np.random.default_rng(self._SEED)
        self._projection = rng.standard_normal(
            (self._N_FEATURES, self.dim)
        ).astype(np.float64) / np.sqrt(self.dim)

    def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        sparse = self._vectorizer.transform(texts)        # (n, 512) sparse
        projected = sparse @ self._projection             # (n, dim) dense
        projected = np.asarray(projected, dtype=np.float64)
        norms = np.linalg.norm(projected, axis=1, keepdims=True)
        norms = np.where(norms == 0.0, 1.0, norms)
        return np.asarray(projected / norms, dtype=np.float32)


class SentenceTransformerEmbedder:
    """Modern dense-embedding adapter via sentence-transformers (Apache 2.0).

    Default model: `sentence-transformers/all-MiniLM-L6-v2` — 384-dim, runs on
    CPU, ~80MB download, semantic embeddings genuinely better than the
    HashingEmbedder for text clustering.

    Install: `pip install -e "packages/verdict_eval[semantic]"`
    (adds sentence-transformers and its torch stack).

    Lazy-imports the dependency so this module's import is cheap.
    """

    def __init__(self, model_name: str = "sentence-transformers/all-MiniLM-L6-v2") -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            raise ImportError(
                "SentenceTransformerEmbedder requires "
                '`pip install -e "packages/verdict_eval[semantic]"`'
            ) from e
        self._model = SentenceTransformer(model_name)
        # Discover dimension from a quick test embedding
        test_vec = self._model.encode(["test"], convert_to_numpy=True)
        self.dim = int(test_vec.shape[1])

    def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        vecs = self._model.encode(texts, convert_to_numpy=True, show_progress_bar=False)
        return np.asarray(vecs, dtype=np.float32)


class DeterministicHashEmbedder:
    """Fully deterministic, no sklearn dependency at runtime. For tests."""

    dim = 64

    def embed(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, t in enumerate(texts):
            digest = hashlib.sha256(t.encode("utf-8")).digest()
            for j in range(self.dim):
                out[i, j] = (digest[j % len(digest)] - 128) / 128.0
        return out


@dataclass
class IntentClusterer:
    """Online intent clusterer using Birch + a provided Embedder.

    Birch is suited to streaming workloads (incremental partial_fit) and has
    bounded memory. Once cluster count grows past `threshold`, Birch
    consolidates.
    """

    embedder: Embedder = field(default_factory=HashingEmbedder)
    n_clusters: int | None = None         # let Birch decide
    threshold: float = 0.5
    _model: Birch = field(init=False)

    def __post_init__(self) -> None:
        self._model = Birch(
            n_clusters=self.n_clusters,
            threshold=self.threshold,
            branching_factor=50,
        )
        self._seen = False

    def fit_predict(self, texts: list[str]) -> list[str]:
        """Train and assign cluster IDs in one call. Returns string cluster ids."""
        if not texts:
            return []
        vecs = self.embedder.embed(texts)
        if not self._seen:
            self._model.fit(vecs)
            self._seen = True
        else:
            self._model.partial_fit(vecs)
        labels = self._model.predict(vecs)
        return [f"c{int(label):04d}" for label in labels]

    def predict(self, texts: list[str]) -> list[str]:
        if not self._seen:
            raise RuntimeError("IntentClusterer not fitted yet; call fit_predict first")
        vecs = self.embedder.embed(texts)
        labels = self._model.predict(vecs)
        return [f"c{int(label):04d}" for label in labels]

    def partial_fit(self, texts: list[str]) -> list[str]:
        """Incrementally update the model and assign labels.

        Use for streaming production ingestion.
        """
        if not texts:
            return []
        vecs = self.embedder.embed(texts)
        if not self._seen:
            self._model.fit(vecs)
            self._seen = True
        else:
            self._model.partial_fit(vecs)
        labels = self._model.predict(vecs)
        return [f"c{int(label):04d}" for label in labels]
