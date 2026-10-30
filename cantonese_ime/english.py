"""English words typed while the IME is in Cantonese mode.

A whole input that is a common English word (the bundled SCOWL list) or one
the user has already committed as English gets the word itself as a candidate,
and the composed per-syllable sentences (meeting -> 咩誒停) are not offered.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile

WORD_LIST = Path(__file__).resolve().parent.parent / "data/vendor/scowl/2026.02.25/en_GB-ise.txt"
WORD = re.compile(r"[a-z]{2,24}\Z", re.ASCII)
MAX_LEARNED = 5000
_MAX_LIST_BYTES = 4 * 1024 * 1024


def load_words(path: Path = WORD_LIST) -> frozenset[str]:
    """Lowercase letter-only entries; names, abbreviations and possessives are skipped."""
    try:
        if path.stat().st_size > _MAX_LIST_BYTES:
            return frozenset()
        return frozenset(word for word in path.read_text(encoding="utf-8").split() if WORD.fullmatch(word))
    except (OSError, UnicodeDecodeError):
        return frozenset()  # English candidates are optional; conversion must not fail.


class LearnedEnglish:
    """Words the user committed as English, most recent last, in a small JSON file."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.words: dict[str, None] = {}
        if path is None or not path.exists():
            return
        try:
            content = json.loads(path.read_text(encoding="utf-8"))
            words = content.get("words") if content.get("schema_version") == 1 else None
            if isinstance(words, list):
                self.words = {word: None for word in words if isinstance(word, str) and WORD.fullmatch(word)}
        except (OSError, ValueError, AttributeError):
            self.words = {}  # A damaged file only forgets learned English words.

    def __contains__(self, word: str) -> bool:
        return word in self.words

    def record(self, word: str) -> bool:
        if self.path is None or not WORD.fullmatch(word):
            return False
        self.words.pop(word, None)
        self.words[word] = None
        while len(self.words) > MAX_LEARNED:
            del self.words[next(iter(self.words))]
        payload = json.dumps({"schema_version": 1, "words": list(self.words)}, indent=0) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".english-", suffix=".json", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(payload)
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return True
