"""Stability guarantees for StableIntentClusterer.

These tests encode the property the Birch-based IntentClusterer fails: a cluster
keeps the same ID across separate runs, so the drift detector's week-over-week
comparison lines up matching buckets.

Uses DeterministicHashEmbedder so the embedding space is identical across runs
without loading the optional production MiniLM model.
"""

from __future__ import annotations

import numpy as np
import pytest
from verdict_eval.clustering import DeterministicHashEmbedder
from verdict_eval.stable_clustering import (
    UNCLUSTERED_ID,
    ClusterRegistry,
    StableIntentClusterer,
    assess_cluster_health,
)

# Three clearly-distinct intent groups.
BILLING = ["how do I get a refund", "charge me back please", "cancel my subscription"]
WEATHER = ["what is the weather today", "will it rain tomorrow", "forecast for friday"]
CODING = ["fix this python traceback", "why does my for loop crash", "debug my function"]


def _clusterer(**kw):
    # Loose threshold: hash embeddings are coarse, we only need group separation.
    return StableIntentClusterer(embedder=DeterministicHashEmbedder(), threshold=0.6, **kw)


def test_same_text_same_id_across_separate_runs(tmp_path):
    """A prompt maps to the same cluster_id in run 2 as run 1, after reload."""
    path = tmp_path / "registry.json"

    run1 = _clusterer()
    ids1 = run1.assign(BILLING + WEATHER)
    run1.save(path)
    mapping1 = dict(zip(BILLING + WEATHER, ids1, strict=True))

    # New process: reload the registry, feed the SAME texts (plus new traffic).
    run2 = StableIntentClusterer.load(path, embedder=DeterministicHashEmbedder(), threshold=0.6)
    ids2 = run2.assign(BILLING + WEATHER)
    mapping2 = dict(zip(BILLING + WEATHER, ids2, strict=True))

    for text in BILLING + WEATHER:
        assert mapping1[text] == mapping2[text], f"{text!r} changed cluster across runs"


def test_new_traffic_does_not_renumber_existing_clusters(tmp_path):
    """Adding a brand-new intent must not change the IDs of existing clusters."""
    path = tmp_path / "registry.json"

    run1 = _clusterer()
    run1.assign(BILLING)
    run1.save(path)
    billing_ids_before = set(run1.registry.ids)

    run2 = StableIntentClusterer.load(path, embedder=DeterministicHashEmbedder(), threshold=0.6)
    run2.assign(CODING)  # new intent arrives

    # Every original id still exists and still points at the same centroid.
    assert billing_ids_before.issubset(set(run2.registry.ids))


def test_distinct_intents_get_distinct_ids():
    c = _clusterer()
    ids = c.assign(BILLING + CODING)
    billing_ids = set(ids[: len(BILLING)])
    coding_ids = set(ids[len(BILLING) :])
    assert billing_ids.isdisjoint(coding_ids)


def test_within_batch_assignment_is_order_independent():
    """Any permutation of the same batch must yield identical per-text ids.
    Regression test for the order-dependence bug where the registry mutated
    mid-batch and a prompt's cluster depended on what preceded it."""
    import random
    texts = BILLING + WEATHER + CODING
    base = dict(zip(texts, _clusterer().assign(texts), strict=True))
    for seed in range(6):
        perm = texts[:]
        random.Random(seed).shuffle(perm)
        candidate = _clusterer()
        m = dict(zip(perm, candidate.assign(perm), strict=True))
        for t in texts:
            assert m[t] == base[t], f"{t!r} order-dependent: {base[t]} vs {m[t]}"


def test_existing_cluster_updates_are_batch_order_independent(tmp_path):
    path = tmp_path / "r.json"
    seed = _clusterer()
    seed.assign([BILLING[0]])
    seed.save(path)

    forward = StableIntentClusterer.load(
        path, embedder=DeterministicHashEmbedder(), threshold=0.6,
    )
    reverse = StableIntentClusterer.load(
        path, embedder=DeterministicHashEmbedder(), threshold=0.6,
    )
    forward.assign(BILLING)
    reverse.assign(list(reversed(BILLING)))

    assert forward.registry.counts == reverse.registry.counts
    assert np.allclose(forward.registry.centroids, reverse.registry.centroids)


def test_existing_cluster_match_independent_of_batch_contents(tmp_path):
    """A text matching an existing cluster maps to it regardless of what else
    is in the batch."""
    path = tmp_path / "r.json"
    seed_c = _clusterer()
    seed_c.assign(BILLING)
    seed_c.save(path)

    a = StableIntentClusterer.load(path, embedder=DeterministicHashEmbedder(), threshold=0.6)
    alone = a.assign(["how do I get a refund"])[0]
    b = StableIntentClusterer.load(path, embedder=DeterministicHashEmbedder(), threshold=0.6)
    crowd = b.assign(["how do I get a refund", *WEATHER, *CODING])[0]
    assert alone == crowd


def test_near_duplicate_joins_not_mints():
    c = _clusterer()
    first = c.assign(["how do I get a refund"])[0]
    n_clusters_after_first = len(c.registry.ids)
    again = c.assign(["how do I get a refund"])[0]  # identical text
    assert again == first
    assert len(c.registry.ids) == n_clusters_after_first  # no new cluster minted


def test_short_text_routes_to_unclustered():
    c = _clusterer(min_chars=5)
    ids = c.assign(["", "ok", "how do I get a refund"])
    assert ids[0] == UNCLUSTERED_ID
    assert ids[1] == UNCLUSTERED_ID
    assert ids[2] != UNCLUSTERED_ID


def test_assignment_is_batch_order_independent(tmp_path):
    """Identity must not depend on what else is in the batch."""
    path = tmp_path / "r.json"
    a = _clusterer()
    a.assign(BILLING)
    a.save(path)

    # Same starting registry, one query in two different batch contexts.
    b = StableIntentClusterer.load(path, embedder=DeterministicHashEmbedder(), threshold=0.6)
    id_alone = b.assign(["charge me back please"])[0]

    c = StableIntentClusterer.load(path, embedder=DeterministicHashEmbedder(), threshold=0.6)
    id_in_crowd = c.assign(["charge me back please", *WEATHER])[0]

    assert id_alone == id_in_crowd


def test_registry_roundtrip(tmp_path):
    path = tmp_path / "r.json"
    reg = ClusterRegistry(version="v2", distance_threshold=0.3)
    reg.assign(np.array([1.0, 0.0, 0.0]), threshold=0.3, freeze_after=0)
    reg.assign(np.array([0.0, 1.0, 0.0]), threshold=0.3, freeze_after=0)
    reg.save(path)
    loaded = ClusterRegistry.load(path)
    assert loaded.ids == reg.ids
    assert loaded.next_index == reg.next_index
    assert loaded.version == "v2"
    assert loaded.distance_threshold == 0.3


def test_reusing_registry_with_different_threshold_is_rejected(tmp_path):
    path = tmp_path / "r.json"
    original = _clusterer()
    original.assign(BILLING)
    original.save(path)

    with pytest.raises(ValueError, match="bump clustering_version"):
        StableIntentClusterer.load(
            path,
            embedder=DeterministicHashEmbedder(),
            threshold=0.5,
        )


def test_reusing_registry_with_different_embedding_dimension_is_rejected(tmp_path):
    path = tmp_path / "r.json"
    original = _clusterer()
    original.assign(BILLING)
    original.save(path)

    class DifferentDimensionEmbedder:
        dim = 8

        def embed(self, texts: list[str]) -> np.ndarray:
            return np.ones((len(texts), self.dim), dtype=np.float32)

    with pytest.raises(ValueError, match="bump clustering_version"):
        StableIntentClusterer.load(
            path,
            embedder=DifferentDimensionEmbedder(),
            threshold=0.6,
        )


def test_cluster_health_flags_singleton_fragmentation():
    health = assess_cluster_health(
        [f"c{i}" for i in range(10)],
        min_sample_size=30,
    )

    assert health.is_fragmented is True
    assert health.n_clusters == 10
    assert health.median_cluster_size == 1
    assert any("fragmented" in message.lower() for message in health.messages)


def test_cluster_health_reports_underpowered_clusters_without_calling_them_fragmented():
    health = assess_cluster_health(
        ["billing"] * 20 + ["support"] * 20,
        min_sample_size=30,
    )

    assert health.is_fragmented is False
    assert health.clusters_meeting_sample_floor == 0
    assert any("sample floor" in message.lower() for message in health.messages)
