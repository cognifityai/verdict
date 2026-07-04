"""HashingEmbedder determinism tests.

Regression lock: the embedder used to lazy-fit a TruncatedSVD on the first
batch it saw, so embeddings depended on whatever data arrived first and two
instances could embed the same text differently. The deterministic fixed
random projection makes the embedder stateless — identical across runs and
instances.
"""

from __future__ import annotations

import numpy as np

from verdict_eval.clustering import HashingEmbedder


def test_two_instances_embed_text_identically() -> None:
    e1 = HashingEmbedder()
    e2 = HashingEmbedder()
    text = ["how do I reset my password?"]
    v1 = e1.embed(text)
    v2 = e2.embed(text)
    assert v1.shape == v2.shape == (1, HashingEmbedder.dim)
    assert np.allclose(v1, v2, atol=1e-6)


def test_embedding_independent_of_prior_batch() -> None:
    """A stateful (fit-on-first-batch) embedder would give different vectors
    depending on what it saw first. A stateless one must not."""
    target = ["the quick brown fox"]

    e_a = HashingEmbedder()
    e_a.embed(["unrelated warmup text about databases"])  # "first batch"
    v_a = e_a.embed(target)

    e_b = HashingEmbedder()
    v_b = e_b.embed(target)  # fresh, no warmup

    assert np.allclose(v_a, v_b, atol=1e-6)


def test_embeddings_are_l2_normalized() -> None:
    e = HashingEmbedder()
    vecs = e.embed(["alpha beta gamma", "delta epsilon zeta"])
    norms = np.linalg.norm(vecs, axis=1)
    assert np.allclose(norms, 1.0, atol=1e-5)


def test_empty_input_returns_empty() -> None:
    e = HashingEmbedder()
    out = e.embed([])
    assert out.shape == (0, HashingEmbedder.dim)
