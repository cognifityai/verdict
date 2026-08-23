"""HashingEmbedder determinism tests.

Regression lock: the embedder used to lazy-fit a TruncatedSVD on the first
batch it saw, so embeddings depended on whatever data arrived first and two
instances could embed the same text differently. The deterministic fixed
random projection makes the embedder stateless — identical across runs and
instances.
"""

from __future__ import annotations

import numpy as np
import pytest
import verdict_eval.clustering_strategies as strategy_module
from verdict.schema import Trace
from verdict_eval.clustering import HashingEmbedder
from verdict_eval.clustering_strategies import (
    AssignmentStatus,
    ClusterInput,
    ExplicitClusteringStrategy,
    FitConfig,
    HybridClusteringStrategy,
    SemanticClusteringStrategy,
    select_cluster_input,
)


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


def test_selector_uses_only_latest_supported_user_message() -> None:
    trace = Trace(
        trace_id="trace-1",
        raw_messages=[
            {"role": "system", "content": "secret policy"},
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "old answer"},
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "  reset\t my  "},
                    {"type": "file", "file_id": "ignored"},
                    {"type": "text", "text": "password@example.com"},
                ],
            },
        ],
    )

    selected = select_cluster_input(trace)

    assert selected.text == "reset my <EMAIL>"
    assert selected.reason is None


def test_selector_rejects_malformed_latest_user_message_without_fallback() -> None:
    trace = Trace(
        trace_id="trace-1",
        raw_messages=[
            {"role": "user", "content": "older valid question"},
            {"role": "user", "content": [{"type": "text", "text": 7}]},
        ],
    )

    selected = select_cluster_input(trace)

    assert selected.text is None
    assert selected.reason == "malformed_messages"


def test_selector_rejects_unknown_roles_without_using_older_user_content() -> None:
    selected = select_cluster_input(
        Trace(
            trace_id="trace-1",
            raw_messages=[
                {"role": "user", "content": "older valid question"},
                {"role": "unknown", "content": "untrusted shape"},
            ],
        )
    )
    assert selected.text is None
    assert selected.reason == "malformed_messages"


def _input(
    trace_id: str,
    *,
    key: str | None = None,
    text: str | None = None,
    reason: str | None = None,
) -> ClusterInput:
    return ClusterInput(
        trace_id=trace_id,
        tenant_id="tenant-a",
        started_at_us=1,
        explicit_key=key,
        semantic_text=text,
        ineligible_reason=reason,
    )


class _AxisEmbedder:
    dim = 2

    def embed(self, texts: list[str]) -> np.ndarray:
        values = {
            "billing one": [1.0, 0.0],
            "billing two": [0.99, 0.01],
            "billing three": [0.98, 0.02],
            "billing four": [0.97, 0.03],
            "billing five": [0.96, 0.04],
            "shipping one": [0.0, 1.0],
        }
        return np.asarray([values[text] for text in texts], dtype=np.float32)


def test_explicit_strategy_groups_exact_keys_without_an_embedder() -> None:
    result = ExplicitClusteringStrategy().fit(
        [_input("b", key="billing"), _input("s", key="shipping")],
        FitConfig(strategy="explicit"),
    )

    assert [cluster.explicit_key for cluster in result.clusters] == ["billing", "shipping"]
    assert [assignment.status for assignment in result.assignments] == [
        AssignmentStatus.ASSIGNED,
        AssignmentStatus.ASSIGNED,
    ]


@pytest.mark.parametrize("reason", ["invalid_workload", "unsafe_workload"])
def test_preclassified_ineligibility_precedes_explicit_key(reason: str) -> None:
    strategy = ExplicitClusteringStrategy()
    result = strategy.fit(
        [
            _input("valid", key="billing"),
            _input("invalid", key="billing", reason=reason),
        ],
        FitConfig(strategy="explicit"),
    )
    by_trace = {item.trace_id: item for item in result.assignments}
    assert by_trace["valid"].status is AssignmentStatus.ASSIGNED
    assert by_trace["invalid"].status is AssignmentStatus.INELIGIBLE
    assert by_trace["invalid"].reason == reason

    [assigned] = strategy.assign(
        strategy_module.RegistryDefinition("explicit", result.clusters),
        [_input("later-invalid", key="billing", reason=reason)],
    )
    assert assigned.status is AssignmentStatus.INELIGIBLE
    assert assigned.reason == reason


@pytest.mark.parametrize("strategy_name", ["semantic", "hybrid"])
def test_preclassified_ineligibility_precedes_semantic_model_work(
    strategy_name: str,
) -> None:
    strategy = (
        SemanticClusteringStrategy(_AxisEmbedder())
        if strategy_name == "semantic"
        else HybridClusteringStrategy(_AxisEmbedder())
    )
    valid = [
        _input(str(index), text=text)
        for index, text in enumerate(
            [
                "billing one",
                "billing two",
                "billing three",
                "billing four",
                "billing five",
            ]
        )
    ]
    invalid = _input(
        "invalid",
        key="billing" if strategy_name == "hybrid" else None,
        text="must not be embedded",
        reason="invalid_workload",
    )
    result = strategy.fit([*valid, invalid], FitConfig(strategy=strategy_name))
    by_trace = {item.trace_id: item for item in result.assignments}
    assert by_trace["invalid"].status is AssignmentStatus.INELIGIBLE
    assert by_trace["invalid"].reason == "invalid_workload"

    [assigned] = strategy.assign(
        strategy_module.RegistryDefinition(strategy_name, result.clusters),
        [invalid],
    )
    assert assigned.status is AssignmentStatus.INELIGIBLE
    assert assigned.reason == "invalid_workload"


def test_semantic_strategy_has_defined_k1_fit_for_five_rows() -> None:
    strategy = SemanticClusteringStrategy(_AxisEmbedder())
    result = strategy.fit(
        [
            _input("1", text="billing one"),
            _input("2", text="billing two"),
            _input("3", text="billing three"),
            _input("4", text="billing four"),
            _input("5", text="billing five"),
        ],
        FitConfig(strategy="semantic", min_cluster_size=5),
    )

    assert result.chosen_k == 1
    assert len(result.clusters) == 1
    assert all(item.status is AssignmentStatus.ASSIGNED for item in result.assignments)
    assert result.clusters[0].radius >= max(item.distance or 0.0 for item in result.assignments)


def test_cosine_distance_roundoff_is_clamped_to_the_persisted_domain() -> None:
    distances = strategy_module._cosine_distances(
        np.asarray([1.0 + np.finfo(float).eps, -1.0 - np.finfo(float).eps])
    )
    assert distances.tolist() == [0.0, 2.0]

    class RoundoffEmbedder:
        def embed(self, texts: list[str]) -> np.ndarray:
            return np.asarray(
                [[0.10541424899789856, -0.9304680447082047] for _ in texts],
                dtype=np.float64,
            )

    result = SemanticClusteringStrategy(RoundoffEmbedder()).fit(
        [_input(str(index), text=f"same-{index}") for index in range(5)],
        FitConfig(
            strategy="semantic",
            min_cluster_size=5,
            radius_margin=0,
        ),
    )
    assert all((item.distance or 0.0) >= 0 for item in result.assignments)


@pytest.mark.parametrize(
    "field",
    [
        "silhouette_tolerance",
        "radius_margin",
        "max_assignment_distance",
        "carryover_distance",
        "carryover_ambiguity_margin",
    ],
)
def test_fit_config_rejects_geometry_outside_cosine_domain(field: str) -> None:
    with pytest.raises(ValueError, match=r"\[0,2\]"):
        FitConfig(**{field: 2.000001})


def test_semantic_strategy_excludes_k_that_fails_terminal_minimum(
    monkeypatch,
) -> None:
    class Embedder:
        def embed(self, texts: list[str]) -> np.ndarray:
            index = int(texts[0].removeprefix("text-"))
            return np.asarray([[1.0, index / 100.0]], dtype=np.float64)

    monkeypatch.setattr(strategy_module, "linkage", lambda *args, **kwargs: object())

    def cut(_hierarchy, *, n_clusters):
        k = n_clusters[0]
        if k == 2:
            return np.asarray([0] * 7 + [1] * 8).reshape(-1, 1)
        if k == 3:
            return np.asarray([0] * 5 + [1] * 5 + [2] * 5).reshape(-1, 1)
        raise AssertionError(k)

    monkeypatch.setattr(strategy_module, "cut_tree", cut)
    monkeypatch.setattr(
        strategy_module,
        "silhouette_score",
        lambda _vectors, labels, **_kwargs: 0.50 if len(set(labels)) == 3 else 0.10,
    )

    def terminal_assignments(clusters, inputs, _vectors):
        if len(clusters) == 3:
            owners = [0] * 4 + [None] + [1] * 5 + [2] * 5
        elif len(clusters) == 2:
            owners = [0] * 7 + [1] * 8
        else:
            owners = [0] * len(inputs)
        return [
            (
                strategy_module.AssignmentResult(
                    item.trace_id,
                    AssignmentStatus.OUTLIER,
                    reason="distance",
                    distance=0.5,
                )
                if owner is None
                else strategy_module.AssignmentResult(
                    item.trace_id,
                    AssignmentStatus.ASSIGNED,
                    clusters[owner].cluster_id,
                    "semantic",
                    distance=0.0,
                )
            )
            for item, owner in zip(inputs, owners, strict=True)
        ]

    monkeypatch.setattr(
        SemanticClusteringStrategy,
        "_assign_embedded",
        staticmethod(terminal_assignments),
    )
    result = SemanticClusteringStrategy(Embedder()).fit(
        [_input(str(index), text=f"text-{index}") for index in range(15)],
        FitConfig(strategy="semantic", min_cluster_size=5),
    )

    assert result.chosen_k == 2
    assert len(result.clusters) == 2
    assert min(cluster.member_count for cluster in result.clusters) >= 5


def test_semantic_provisional_order_uses_canonical_rank_before_trace_id() -> None:
    strategy = SemanticClusteringStrategy(_AxisEmbedder())
    inputs = [
        *[_input(f"z-first-ranked-{index}", text="billing one") for index in range(5)],
        *[_input(f"a-later-ranked-{index}", text="shipping one") for index in range(5)],
    ]
    vectors = np.asarray([*([[1.0, 0.0]] * 5), *([[0.0, 1.0]] * 5)], dtype=np.float64)
    clusters = strategy._prototypes(
        inputs,
        vectors,
        np.asarray([1] * 5 + [0] * 5),
        FitConfig(
            strategy="semantic",
            min_cluster_size=5,
            radius_margin=0.5,
            max_assignment_distance=0.5,
        ),
    )

    assert clusters[0].centroid == (1.0, 0.0)
    [assignment] = strategy._assign_embedded(
        clusters,
        [_input("tie", text="billing one")],
        np.asarray([[2**-0.5, 2**-0.5]], dtype=np.float64),
    )
    assert assignment.status is AssignmentStatus.ASSIGNED
    assert assignment.cluster_id == "semantic:0000"


def test_hybrid_preserves_explicit_precedence_and_semantic_fallback() -> None:
    strategy = HybridClusteringStrategy(_AxisEmbedder())
    result = strategy.fit(
        [
            _input("explicit", key="billing", text="shipping one"),
            *[
                _input(str(index), text=text)
                for index, text in enumerate(
                    [
                        "billing one",
                        "billing two",
                        "billing three",
                        "billing four",
                        "billing five",
                    ]
                )
            ],
        ],
        FitConfig(strategy="hybrid", min_cluster_size=5),
    )

    by_trace = {item.trace_id: item for item in result.assignments}
    assert by_trace["explicit"].cluster_kind == "explicit"
    assert all(by_trace[str(index)].cluster_kind == "semantic" for index in range(5))
