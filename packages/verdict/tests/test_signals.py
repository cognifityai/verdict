"""Tests for the user-signal capture API (verdict.record_user_signal).

Written against stdlib ``unittest`` so they are fully runnable with no
third-party deps (no pytest, no provider SDKs). Run standalone:

    python3 -m unittest verdict.tests.test_signals    # from packages/verdict/src on sys.path

or directly:

    python3 packages/verdict/tests/test_signals.py
"""

from __future__ import annotations

import unittest

import verdict
from verdict import client as verdict_client
from verdict.schema import UserSignalRecord
from verdict.signals import VALID_SIGNAL_KINDS, record_user_signal
from verdict.storage.memory import InMemoryStorage


class RecordUserSignalTest(unittest.TestCase):
    def setUp(self) -> None:
        # Ensure a clean singleton before each test regardless of prior state.
        verdict_client.shutdown()

    def tearDown(self) -> None:
        # Always tear the singleton down so tests don't leak a client into
        # each other (the "no client" test in particular must start clean).
        # shutdown() lives on the client module (not re-exported at package
        # top level), so call it there.
        verdict_client.shutdown()

    def _init_with_memory_storage(self) -> InMemoryStorage:
        storage = InMemoryStorage()
        # init() does not eagerly import provider SDKs; instrumentors are
        # skipped when their SDK is absent, so this works in a bare sandbox.
        verdict.init(storage=storage)
        # Return the storage actually held by the client, to be safe.
        client = verdict_client.get_client()
        assert client is not None
        return client.storage  # type: ignore[return-value]

    def test_valid_signal_persists(self) -> None:
        storage = self._init_with_memory_storage()

        record_user_signal("trace-123", "thumbs_up")

        signals = storage.list_user_signals()
        self.assertEqual(len(signals), 1)
        rec = signals[0]
        self.assertIsInstance(rec, UserSignalRecord)
        self.assertEqual(rec.trace_id, "trace-123")
        self.assertEqual(rec.kind, "thumbs_up")
        # A real id and timestamp should have been assigned by the schema.
        self.assertTrue(rec.signal_id)
        self.assertIsNotNone(rec.created_at)

    def test_unknown_kind_raises_value_error(self) -> None:
        self._init_with_memory_storage()

        with self.assertRaises(ValueError):
            record_user_signal("trace-123", "thumbz_up")  # typo

    def test_unknown_kind_raises_even_without_client(self) -> None:
        # No init() here — a bad kind is a programming error and must surface
        # regardless of whether Verdict is initialized.
        self.assertIsNone(verdict_client.get_client())
        with self.assertRaises(ValueError):
            record_user_signal("trace-123", "not_a_real_kind")

    def test_no_client_is_noop(self) -> None:
        # No init() called: there is no global client.
        self.assertIsNone(verdict_client.get_client())
        # Must not raise even though nothing can be persisted.
        record_user_signal("trace-123", "thumbs_up")

    def test_multiple_signals_same_trace_all_persist(self) -> None:
        storage = self._init_with_memory_storage()

        record_user_signal("trace-abc", "thumbs_up")
        record_user_signal("trace-abc", "copy")
        record_user_signal("trace-abc", "regenerate")

        signals = storage.list_user_signals()
        self.assertEqual(len(signals), 3)
        self.assertTrue(all(s.trace_id == "trace-abc" for s in signals))
        self.assertEqual(
            {s.kind for s in signals},
            {"thumbs_up", "copy", "regenerate"},
        )

    def test_all_valid_kinds_accepted(self) -> None:
        storage = self._init_with_memory_storage()
        for kind in sorted(VALID_SIGNAL_KINDS):
            record_user_signal("trace-all", kind)
        signals = storage.list_user_signals()
        self.assertEqual(len(signals), len(VALID_SIGNAL_KINDS))
        self.assertEqual({s.kind for s in signals}, set(VALID_SIGNAL_KINDS))

    def test_storage_failure_does_not_propagate(self) -> None:
        # A storage write failure is telemetry loss, not a caller-facing error.
        class BoomStorage(InMemoryStorage):
            def insert_user_signal(self, sig: UserSignalRecord) -> None:
                raise RuntimeError("storage exploded")

        verdict.init(storage=BoomStorage())
        # Should be swallowed (logged), not raised.
        record_user_signal("trace-x", "thumbs_up")


if __name__ == "__main__":
    unittest.main(verbosity=2)
