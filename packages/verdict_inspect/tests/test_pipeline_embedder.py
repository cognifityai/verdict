"""Tests for verdict_inspect.pipeline._make_embedder fallback.

Two regressions are guarded here:

  1. The fallback used to call `HashingEmbedder(dim=128)`, but HashingEmbedder
     is now stateless and takes NO constructor args — that call would raise
     TypeError. The fallback must call `HashingEmbedder()`.

  2. SentenceTransformerEmbedder can fail at RUNTIME (model not cached, network
     down, torch mismatch), not only with ImportError. The except clause used to
     catch only ImportError, so a runtime failure would propagate and crash the
     pipeline. It must catch Exception and fall back.

`verdict_eval.clustering` hard-imports scikit-learn at module level, so it may
be unimportable in a minimal sandbox. To exercise the fallback logic anywhere
(and to avoid depending on a real model download), we inject a FAKE
`verdict_eval.clustering` module into sys.modules. `_make_embedder` does its
imports lazily (`from verdict_eval.clustering import ...`), so it resolves to
our fake. A separate test runs the real HashingEmbedder when sklearn is present
and skips otherwise.
"""

from __future__ import annotations

import sys
import types

import numpy as np
import pytest
from verdict_inspect import pipeline
from verdict_inspect.parsers.base import ParsedTurn


def _install_fake_clustering(monkeypatch, *, strans_raises: Exception | None,
                             record: dict) -> None:
    """Inject a fake verdict_eval.clustering module.

    SentenceTransformerEmbedder optionally raises `strans_raises` at
    construction. HashingEmbedder records how it was constructed (to assert the
    no-arg call) and returns a tiny working 128-dim embedder.
    """
    import numpy as np

    fake = types.ModuleType("verdict_eval.clustering")

    class FakeSentenceTransformerEmbedder:
        def __init__(self, *a, **k) -> None:
            if strans_raises is not None:
                raise strans_raises

    class FakeHashingEmbedder:
        dim = 128

        def __init__(self, *args, **kwargs) -> None:
            record["args"] = args
            record["kwargs"] = kwargs

        def embed(self, texts):
            return np.zeros((len(texts), self.dim), dtype=np.float32)

    fake.SentenceTransformerEmbedder = FakeSentenceTransformerEmbedder
    fake.HashingEmbedder = FakeHashingEmbedder
    monkeypatch.setitem(sys.modules, "verdict_eval.clustering", fake)


def test_make_embedder_falls_back_on_runtime_error(monkeypatch) -> None:
    """A non-ImportError from SentenceTransformerEmbedder must trigger the
    HashingEmbedder fallback (the except must catch Exception, not just
    ImportError), and HashingEmbedder must be called with NO args."""
    record: dict = {}
    _install_fake_clustering(
        monkeypatch,
        strans_raises=RuntimeError("model not cached and network unreachable"),
        record=record,
    )

    embedder, name = pipeline._make_embedder()

    # Fallback was taken and HashingEmbedder() got no args.
    assert record.get("args") == ()
    assert record.get("kwargs") == {}
    assert "HashingEmbedder" in name

    # Returned embedder works and produces 128-dim vectors.
    vecs = embedder.embed(["x"])
    assert vecs.shape == (1, 128)


def test_make_embedder_falls_back_on_import_error(monkeypatch) -> None:
    """The original ImportError path must still fall back to a no-arg
    HashingEmbedder."""
    record: dict = {}
    _install_fake_clustering(
        monkeypatch,
        strans_raises=ImportError("sentence-transformers not installed"),
        record=record,
    )

    embedder, name = pipeline._make_embedder()
    assert record.get("args") == ()
    assert "HashingEmbedder" in name
    assert embedder.embed(["x"]).shape == (1, 128)


def test_real_hashing_embedder_takes_no_args() -> None:
    """Direct guard on the API contract the fallback relies on, against the
    REAL HashingEmbedder. Skipped when sklearn isn't importable."""
    try:
        from verdict_eval.clustering import HashingEmbedder
        emb = HashingEmbedder()
    except Exception as e:  # sklearn missing in this sandbox
        pytest.skip(f"HashingEmbedder needs sklearn, unavailable here: {e}")
    assert emb.dim == 128
    vecs = emb.embed(["hello", "world"])
    assert vecs.shape == (2, 128)


def test_semantic_report_uses_detector_statistics_without_reembedding() -> None:
    base = np.array(
        [[100.0, 0.0], [1.0, 1.0]] * 4,
        dtype=np.float64,
    )
    current = np.array(
        [[1.0, 0.0], [100.0, 100.0]] * 4,
        dtype=np.float64,
    )

    class RecordingEmbedder:
        def __init__(self) -> None:
            # The old pipeline embeds current/baseline in the detector, then
            # repeats both calls when the row does not trigger.
            self.arrays = [current, base, current, base]
            self.calls = 0

        def embed(self, _texts):
            value = self.arrays[self.calls]
            self.calls += 1
            return value

    def turns(prefix: str) -> list[ParsedTurn]:
        return [
            ParsedTurn(
                conversation_id=prefix,
                turn_index=index,
                timestamp=None,
                user_text="question",
                assistant_text=f"{prefix} answer {index}",
            )
            for index in range(8)
        ]

    embedder = RecordingEmbedder()
    rows = pipeline._semantic_drift(
        [
            pipeline.Window(name="early", turns=turns("early")),
            pipeline.Window(name="late", turns=turns("late")),
        ],
        embedder,
    )

    from verdict_eval.semantic_drift import _cosine_distance, _l2_normalize_rows

    expected = _cosine_distance(
        _l2_normalize_rows(current).mean(axis=0),
        _l2_normalize_rows(base).mean(axis=0),
    )
    assert len(rows) == 1
    assert rows[0].triggered is False
    assert rows[0].centroid_distance == pytest.approx(expected)
    assert embedder.calls == 2
