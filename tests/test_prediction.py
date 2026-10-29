"""Learned next-word suggestions: only the user's own sequences and phrases."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from cantonese_ime import prediction
from cantonese_ime.prediction import PredictionStore, phrase_pairs


class PredictionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="cantonese-prediction-test-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "prediction.json"

    def store(self) -> PredictionStore:
        return PredictionStore(self.path)

    def test_empty_until_the_user_types_a_sequence(self) -> None:
        store = self.store()
        self.assertEqual(store.suggest("我"), [])
        store.record("我", "哋"); store.record("我", "哋"); store.record("我", "想")
        self.assertEqual(store.suggest("我"), ["哋", "想"])
        self.assertEqual(store.suggest("你"), [])

    def test_whole_text_and_last_character_both_learn(self) -> None:
        store = self.store()
        store.record("我哋", "去")
        self.assertEqual(store.suggest("我哋"), ["去"])
        self.assertEqual(store.suggest("哋"), ["去"])      # via the last character
        store.record("佢哋", "嚟"); store.record("佢哋", "嚟")
        # Exact context outranks evidence borrowed from the last character.
        self.assertEqual(store.suggest("我哋")[0], "去")

    def test_learned_phrases_seed_suggestions_at_lower_weight(self) -> None:
        selections = {("forgiving", "ngodei", "我哋", "ngo5 dei6"): 3, ("forgiving", "ngo", "我", "ngo5"): 9,
                      ("forgiving", "ngoseung", "我想", "ngo5 soeng2"): 1}
        self.assertEqual(phrase_pairs(selections.items()), {"我": {"哋": 3.0, "想": 1.0}})
        store = self.store()
        self.assertEqual(store.suggest("我", selections=selections), ["哋", "想"])
        store.record("我", "想"); store.record("我", "想")
        self.assertEqual(store.suggest("我", selections=selections)[0], "想")

    def test_persists_reloads_and_clears_when_the_file_is_deleted(self) -> None:
        store = self.store()
        store.record("我", "哋")
        self.assertTrue(store.save(force=True) or self.path.exists())
        self.assertEqual(self.store().suggest("我"), ["哋"])
        self.path.unlink()
        self.assertEqual(store.suggest("我"), [])          # outside clear is honoured
        store.record("你", "好")
        store.save(force=True)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["pairs"], {"你": {"好": 1.0}})

    def test_saves_are_rate_limited_until_forced(self) -> None:
        store = self.store()
        with mock.patch.object(prediction.time, "monotonic", return_value=1000.0):
            store.record("我", "哋")                       # first record saves
            stamp = self.path.stat().st_mtime_ns
            store.record("我", "想")                       # within the interval: deferred
            self.assertEqual(self.path.stat().st_mtime_ns, stamp)
            self.assertNotIn("想", self.path.read_text(encoding="utf-8"))
            self.assertTrue(store.save(force=True))
        self.assertIn("想", self.path.read_text(encoding="utf-8"))

    def test_rejects_invalid_text_and_ignores_damaged_files(self) -> None:
        store = self.store()
        for previous, following in (("", "哋"), ("我", ""), ("我", "a b"), ("我", "x" * 9), (None, "哋")):
            with self.assertRaises(ValueError):
                store.record(previous, following)
        self.assertEqual(store.suggest(""), [])
        self.assertEqual(store.suggest("我", limit=0), [])
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(self.store().suggest("我"), [])
        self.path.write_text(json.dumps({"schema_version": 1, "pairs": {"我": {"哋": 2, "bad text": 5, "x": -1}}}), encoding="utf-8")
        self.assertEqual(self.store().suggest("我"), ["哋"])

    def test_bounded_followers_and_contexts(self) -> None:
        store = self.store()
        for index in range(prediction.MAX_FOLLOWERS + 5):
            store.record("我", chr(0x4E00 + index))
        self.assertEqual(len(store._pairs["我"]), prediction.MAX_FOLLOWERS)
        with mock.patch.object(prediction, "MAX_CONTEXTS", 3):
            for index in range(6):
                store.record(chr(0x5000 + index), "好")
            self.assertLessEqual(len(store._pairs), 3)

    def test_context_limit_holds_for_descending_equal_weight_contexts(self) -> None:
        store = self.store()
        contexts = [chr(0x5005 - index) for index in range(6)]
        with mock.patch.object(prediction, "MAX_CONTEXTS", 3):
            for index, context in enumerate(contexts):
                with self.subTest(context=context):
                    store.record(context, "x")
                    expected = set(sorted(contexts[:index + 1], reverse=True)[:3])
                    self.assertLessEqual(len(store._pairs), prediction.MAX_CONTEXTS)
                    self.assertEqual(set(store._pairs), expected)
                    store.save(force=True)
                    saved = json.loads(self.path.read_text(encoding="utf-8"))["pairs"]
                    self.assertLessEqual(len(saved), prediction.MAX_CONTEXTS)
                    self.assertEqual(saved, store._pairs)
                    restored = self.store()
                    self.assertLessEqual(len(restored._pairs), prediction.MAX_CONTEXTS)
                    self.assertEqual(restored._pairs, saved)
                    store = restored

    def test_no_path_means_memory_only(self) -> None:
        store = PredictionStore(None)
        store.record("我", "哋")
        self.assertEqual(store.suggest("我"), ["哋"])
        self.assertFalse(store.save(force=True))


class PredictionProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="cantonese-prediction-protocol-")
        self.addCleanup(temporary.cleanup)
        self.store = PredictionStore(Path(temporary.name) / "prediction.json")
        import threading
        self.engine = type("FakeEngine", (), {})()
        self.engine._lock = threading.RLock()
        self.engine._selections = {("forgiving", "ngodei", "我哋", "ngo5 dei6"): 1}

    def test_round_trip_and_dispatch(self) -> None:
        from cantonese_ime.native_protocol import (
            PredictionRequest, SequenceRequest, Status, decode_prediction_request, decode_response,
            decode_sequence_request, dispatch, encode_prediction_request, encode_response, encode_sequence_request)
        sequence = encode_sequence_request(SequenceRequest(7, "我", "想"))
        self.assertEqual(decode_sequence_request(sequence), SequenceRequest(7, "我", "想"))
        self.assertEqual(dispatch(self.engine, sequence, self.store).status, Status.OK)
        predict = encode_prediction_request(PredictionRequest(8, "我"))
        self.assertEqual(decode_prediction_request(predict), PredictionRequest(8, "我"))
        response = decode_response(encode_response(dispatch(self.engine, predict, self.store)), expected_request_id=8)
        # The user's own sequence (weight 2) leads the learned phrase (weight 0.5).
        self.assertEqual([(c.text, c.reading) for c in response.candidates], [("想", ""), ("哋", "")])

    def test_without_a_store_suggestions_are_empty_and_records_refused(self) -> None:
        from cantonese_ime.native_protocol import (
            PredictionRequest, SequenceRequest, Status, dispatch, encode_prediction_request, encode_sequence_request)
        self.assertEqual(dispatch(self.engine, encode_prediction_request(PredictionRequest(1, "我"))).candidates, ())
        self.assertEqual(dispatch(self.engine, encode_sequence_request(SequenceRequest(2, "我", "想"))).status,
                         Status.INTERNAL)

    def test_malformed_requests_are_invalid(self) -> None:
        import struct
        from cantonese_ime.native_protocol import (
            PREDICTION_MAGIC, SEQUENCE_MAGIC, PredictionRequest, ProtocolError, Status, dispatch, encode_prediction_request)
        with self.assertRaises(ProtocolError):
            encode_prediction_request(PredictionRequest(1, "a b"))
        bad = [struct.pack("<3I", PREDICTION_MAGIC, 3, 9) + b"\xe6\x88",           # truncated
               struct.pack("<3I", PREDICTION_MAGIC, 4, 2) + b"\xff\xfe",           # not UTF-8
               struct.pack("<4I", SEQUENCE_MAGIC, 5, 3, 0) + "我".encode()]       # empty following
        for payload in bad:
            self.assertEqual(dispatch(self.engine, payload, self.store).status, Status.INVALID)


if __name__ == "__main__":
    unittest.main()
