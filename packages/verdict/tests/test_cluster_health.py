from verdict.cluster_health import UNCLUSTERED_ID, assess_cluster_health


def test_cluster_health_is_available_without_optional_evaluator() -> None:
    health = assess_cluster_health(["a"] * 30 + ["b", UNCLUSTERED_ID, None])

    assert health.n_traces == 31
    assert health.n_clusters == 2
    assert health.clusters_meeting_sample_floor == 1
    assert health.is_fragmented is False
    assert health.messages == (
        "Median cluster size is 15.5; 1/2 clusters meet the 30-sample floor. "
        "Drift tests remain inactive for clusters below that floor; add traffic or use "
        "coarser, validated clusters.",
    )
