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

ROOT = Path(__file__).resolve().parent.parent
WORD_LIST = ROOT / "data/vendor/scowl/2026.02.25/en_GB-ise.txt"
TRANSLATIONS = ROOT / "data/english/english-chinese.tsv"
TRANSLATION_SOURCES = (
    ROOT / "data/vendor/cc-cedict/2026-10-21-f6b2248b589a/cedict_ts.u8",
    ROOT / "data/vendor/cc-canto/2017-02-02-and-2015-09-23/cccanto-webdist.txt",
)
WORD = re.compile(r"[a-z]{2,24}\Z", re.ASCII)
MAX_LEARNED = 5000
MAX_TRANSLATIONS_PER_WORD = 30
_MAX_LIST_BYTES = 4 * 1024 * 1024
_ENTRY = re.compile(r"^(\S+) \S+ \[[^\]]*\](?: \{[^}]*\})? /(.*)/\s*$")
_SKIPPED_GLOSS = ("CL:", "variant of", "old variant", "see ", "surname ", "abbr. for", "used in ")


def gloss_index(lines) -> dict[str, dict[str, int]]:
    """English gloss word -> Traditional headword -> position of the earliest gloss naming it.

    Only a gloss (or a ;/, part of one) that is a single word after dropping
    parentheses, a leading to/a/an/the/I'm and trailing !/. counts, so
    "to meet; to encounter" indexes meet and encounter.
    """
    index: dict[str, dict[str, int]] = {}
    for line in lines:
        match = _ENTRY.match(line)
        if not match:
            continue
        headword, position = match.group(1), 0
        for gloss in match.group(2).split("/"):
            if gloss.startswith(_SKIPPED_GLOSS):
                continue
            for part in re.split(r"[;,]", gloss):
                part = re.sub(r"\([^)]*\)|\[[^\]]*\]", "", part).strip().lower().rstrip("!.")
                part = re.sub(r"^(?:to|a|an|the|i'm|i am) ", "", part).strip()
                if WORD.fullmatch(part):
                    entries = index.setdefault(part, {})
                    entries[headword] = min(entries.get(headword, position), position)
            position += 1
    return index


def load_translations(path: Path = TRANSLATIONS) -> dict[str, tuple[tuple[str, int], ...]]:
    """The generated english-chinese.tsv: word, then headword:position pairs."""
    result: dict[str, tuple[tuple[str, int], ...]] = {}
    try:
        if path.stat().st_size > 16 * 1024 * 1024:
            return {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line or line.startswith("#"):
                continue
            word, _, rest = line.partition("\t")
            pairs = []
            for item in rest.split(" "):
                headword, _, position = item.rpartition(":")
                if headword and position.isdigit():
                    pairs.append((headword, int(position)))
            if WORD.fullmatch(word) and pairs:
                result[word] = tuple(pairs)
    except (OSError, UnicodeDecodeError):
        return {}  # Translations are optional; conversion must not fail.
    return result


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
