"""English words typed in Cantonese mode: the bundled list and learned words."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from cantonese_ime.engine import Candidate, Engine
from cantonese_ime.english import WORD_LIST, LearnedEnglish, load_words
from cantonese_ime.native_protocol import LearningRequest, ProtocolError, validate_learning_request
from tests.test_engine import create_fixture_database


class EnglishWordTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)  # runs after every engine is closed
        self.root = Path(self.directory.name)
        self.database = self.root / "fixture.sqlite3"
        create_fixture_database(self.database)
        self.learning = self.root / "learning.json"

    def engine(self) -> Engine:
        engine = Engine(self.database, learning_path=self.learning)
        self.addCleanup(engine.close)
        return engine

    def test_list_keeps_lowercase_letter_words_only(self) -> None:
        words = load_words(WORD_LIST)
        self.assertIn("meeting", words)
        self.assertNotIn("London", words)
        self.assertFalse(any("'" in word or not word.islower() for word in words))
        self.assertEqual(load_words(self.root / "missing.txt"), frozenset())

    def test_listed_word_is_offered_without_composed_sentences(self) -> None:
        candidates = self.engine().lookup("meeting")
        self.assertEqual([(c.text, c.reading, c.match_type) for c in candidates], [("meeting", "", "english")])

    def test_dictionary_words_stay_ahead_of_a_listed_word(self) -> None:
        texts = [c.text for c in self.engine().lookup("long")]
        self.assertEqual(texts[-1], "long")
        self.assertIn("狼", texts[:-1])

    def test_committed_english_word_is_remembered_and_leads(self) -> None:
        engine = self.engine()
        self.assertEqual(engine.lookup("zzqx"), [])
        engine.record_selection("zzqx", Candidate("zzqx", "", 0.0, "native-selection", 4))
        self.assertEqual([c.text for c in engine.lookup("zzqx")], ["zzqx"])
        self.assertEqual([c.text for c in self.engine().lookup("zzqx")], ["zzqx"])  # persisted
        # An exact reading still comes first; the learned word follows it.
        engine.record_selection("long", Candidate("long", "", 0.0, "native-selection", 4))
        texts = [c.text for c in engine.lookup("long")]
        self.assertEqual(set(texts[:2]), {"狼", "朗"})
        self.assertEqual(texts[2], "long")

    def test_custom_phrases_and_mismatched_text_are_not_learned_as_english(self) -> None:
        engine = self.engine()
        engine.record_selection("hh", Candidate("曈曈", "", 0.0, "native-selection", 2))
        self.assertFalse((self.root / "english-words.json").exists())

    def test_learned_words_are_bounded_and_damage_tolerant(self) -> None:
        path = self.root / "english-words.json"
        path.write_text("not json", encoding="utf-8")
        learned = LearnedEnglish(path)
        self.assertEqual(learned.words, {})
        self.assertFalse(learned.record("a"))
        self.assertTrue(learned.record("commit"))
        self.assertIn("commit", LearnedEnglish(path))
        self.assertFalse(LearnedEnglish(None).record("commit"))

    def test_protocol_accepts_reading_less_learning_only_for_the_typed_word(self) -> None:
        validate_learning_request(LearningRequest(1, 0, "commit", "commit", ""))
        for raw, text in (("commit", "comet"), ("ho'ping", "ho'ping"), ("hh", "曈曈")):
            with self.subTest(raw=raw, text=text), self.assertRaises(ProtocolError):
                validate_learning_request(LearningRequest(1, 0, raw, text, ""))


if __name__ == "__main__":
    unittest.main()
