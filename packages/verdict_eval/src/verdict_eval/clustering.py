"""Embedding adapters used by Verdict's intent clustering pipeline.

The persistent cluster assignment algorithm lives in ``stable_clustering.py``.
This module provides the embedding port and its local adapters: a lightweight
hashing fallback, sentence-transformers for semantic clustering, and a
deterministic test adapter.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Protocol, runtime_checkable

import numpy as np
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


class FrozenMiniLMEmbedder:
    """Local-only, singleton-batch MiniLM adapter for registry fit/assign."""

    dim = 384
    model_name = "sentence-transformers/all-MiniLM-L6-v2"
    model_revision = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"

    def __init__(self, model_path: str | Path) -> None:
        root = Path(model_path).resolve(strict=True)
        if not root.is_dir():
            raise ValueError("model path must be a local directory")
        self._root = root
        config = json.loads((root / "sentence_bert_config.json").read_text())
        if config.get("max_seq_length") != 256:
            raise ValueError("MiniLM max sequence length must be 256")
        aggregate = hashlib.sha256()
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            relative = path.relative_to(root).as_posix().encode()
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
            aggregate.update(len(relative).to_bytes(4,"big"))
            aggregate.update(relative)
            aggregate.update(digest.digest())
        self.model_file_sha256 = aggregate.hexdigest()
        self._torch = None
        self._tokenizer = None
        self._model = None

    def _load(self) -> None:
        if self._model is not None:
            return
        try:
            import torch
            from transformers import AutoModel, AutoTokenizer
        except ImportError as exc:
            raise ImportError(
                "FrozenMiniLMEmbedder requires the semantic extra"
            ) from exc
        torch.set_num_threads(1)
        torch.use_deterministic_algorithms(True)
        self._torch = torch
        self._tokenizer = AutoTokenizer.from_pretrained(
            self._root,local_files_only=True,trust_remote_code=False
        )
        self._model = AutoModel.from_pretrained(
            self._root,local_files_only=True,trust_remote_code=False,use_safetensors=True
        ).to("cpu").eval()

    def embed(self, texts: list[str]) -> np.ndarray:
        self._load()
        rows: list[np.ndarray] = []
        with self._torch.no_grad():
            for text in texts:
                tokens = self._tokenizer(
                    text,padding="max_length",truncation=True,max_length=256,
                    return_tensors="pt",
                )
                output = self._model(**tokens).last_hidden_state
                mask = tokens["attention_mask"].unsqueeze(-1).expand(output.size()).float()
                pooled = (output * mask).sum(1) / mask.sum(1).clamp(min=1e-9)
                pooled = self._torch.nn.functional.normalize(pooled,p=2,dim=1)
                rows.append(pooled[0].cpu().numpy().astype(np.float32,copy=False))
        return np.asarray(rows,dtype=np.float32).reshape((-1,self.dim))


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
