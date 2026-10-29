"""Next-word suggestions learned only from the user's own typing.

Two local sources, no dictionary statistics:

* consecutive commits recorded by the native frontend ("我" then "哋"), stored
  in a small JSON file under both the whole previous text and its last
  character, so "我哋" then "去" also helps after a later "哋";
* phrases the user already chose in learning memory (choosing 我哋 implies
  我 -> 哋), derived on the fly and never written here.

The file holds what the user typed in sequence, so it stays local, is bounded,
and is only written when the frontend sends records (the feature is off by
default). Deleting the file clears it: the store notices the missing file and
drops its in-memory copy.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Iterable, Mapping


SCHEMA_VERSION = 1
MAX_TEXT_LENGTH = 8
MAX_FOLLOWERS = 32
MAX_CONTEXTS = 5000
MAX_FILE_BYTES = 2_000_000
SAVE_INTERVAL_SECONDS = 3.0
_EXACT_WEIGHT = 2.0
_LAST_CHARACTER_WEIGHT = 1.0
_PHRASE_WEIGHT = 0.5


def _valid_text(value: object) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= MAX_TEXT_LENGTH
            and not any(c.isspace() or ord(c) < 32 for c in value))


def phrase_pairs(selections: Iterable[tuple[tuple[str, str, str, str], int]]) -> dict[str, dict[str, float]]:
    """First character -> rest, from learned multi-character selections."""
    pairs: dict[str, dict[str, float]] = {}
    for (_mode, _raw, text, _reading), count in selections:
        if isinstance(text, str) and 2 <= len(text) <= MAX_TEXT_LENGTH + 1 and _valid_text(text[1:]):
            followers = pairs.setdefault(text[0], {})
            followers[text[1:]] = followers.get(text[1:], 0.0) + float(count)
    return pairs


class PredictionStore:
    """Thread-safe counts of which text the user typed after which."""

    def __init__(self, path: str | Path | None) -> None:
        self.path = Path(path).resolve() if path is not None else None
        self._lock = threading.RLock()
        self._pairs: dict[str, dict[str, float]] = {}
        self._stamp: tuple[int, int] | None = None
        self._dirty = False
        self._last_save = 0.0
        self._load()

    # ---- persistence ----
    def _file_stamp(self) -> tuple[int, int] | None:
        try:
            stat = self.path.stat()
            return stat.st_mtime_ns, stat.st_size
        except (OSError, AttributeError):
            return None

    def _load(self) -> None:
        self._pairs = {}
        self._stamp = self._file_stamp() if self.path is not None else None
        if self._stamp is None:
            return
        try:
            if self._stamp[1] > MAX_FILE_BYTES:
                raise ValueError("too large")
            content = json.loads(self.path.read_text(encoding="utf-8"))
            if content.get("schema_version") != SCHEMA_VERSION or not isinstance(content.get("pairs"), dict):
                raise ValueError("unsupported")
            for context, followers in list(content["pairs"].items())[:MAX_CONTEXTS]:
                if not _valid_text(context) or not isinstance(followers, dict):
                    continue
                kept = {text: float(count) for text, count in followers.items()
                        if _valid_text(text) and isinstance(count, (int, float)) and 0 < count < 1e9}
                if kept:
                    self._pairs[context] = dict(sorted(kept.items(), key=lambda item: -item[1])[:MAX_FOLLOWERS])
        except (OSError, ValueError, TypeError, AttributeError):
            # A damaged file is ignored, not overwritten until the next record.
            self._pairs = {}

    def _refresh(self) -> None:
        """Pick up an outside change, e.g. the settings window deleting the file."""
        if self.path is not None and self._file_stamp() != self._stamp:
            self._load()
            self._dirty = False

    def save(self, *, force: bool = False) -> bool:
        with self._lock:
            if self.path is None or not self._dirty:
                return False
            if not force and time.monotonic() - self._last_save < SAVE_INTERVAL_SECONDS:
                return False
            payload = json.dumps({"schema_version": SCHEMA_VERSION, "pairs": self._pairs},
                                 ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(payload) > MAX_FILE_BYTES:
                return False
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle, temporary = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent)
            try:
                with os.fdopen(handle, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            except OSError:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
                return False
            self._stamp = self._file_stamp()
            self._dirty = False
            self._last_save = time.monotonic()
            return True

    # ---- learning ----
    def _bump(self, context: str, following: str, amount: float) -> None:
        followers = self._pairs.setdefault(context, {})
        followers[following] = followers.get(following, 0.0) + amount
        if len(followers) > MAX_FOLLOWERS:
            del followers[min(followers, key=lambda text: (followers[text], text))]
        if len(self._pairs) > MAX_CONTEXTS:
            # Forget the context with the least total evidence.
            weakest = min(self._pairs, key=lambda key: (sum(self._pairs[key].values()), key))
            del self._pairs[weakest]

    def record(self, previous: str, following: str) -> None:
        """Count one occurrence of `following` typed right after `previous`."""
        previous = previous[-MAX_TEXT_LENGTH:] if isinstance(previous, str) else ""
        if not _valid_text(previous) or not _valid_text(following):
            raise ValueError("Prediction records need 1-8 non-space characters on each side.")
        with self._lock:
            self._refresh()
            self._bump(previous, following, 1.0)
            if len(previous) > 1:
                self._bump(previous[-1], following, 1.0)
            self._dirty = True
            self.save()

    def suggest(self, context: str, *, limit: int = 9,
                selections: Mapping[tuple[str, str, str, str], int] | None = None) -> list[str]:
        """Most likely next texts after `context`, best first."""
        context = context[-MAX_TEXT_LENGTH:] if isinstance(context, str) else ""
        if not _valid_text(context) or not 1 <= limit <= 9:
            return []
        with self._lock:
            self._refresh()
            scores: dict[str, float] = {}
            def add(followers: Mapping[str, float] | None, weight: float) -> None:
                for text, count in (followers or {}).items():
                    scores[text] = scores.get(text, 0.0) + weight * count
            add(self._pairs.get(context), _EXACT_WEIGHT)
            if len(context) > 1:
                add(self._pairs.get(context[-1]), _LAST_CHARACTER_WEIGHT)
            if selections:
                add(phrase_pairs(selections.items()).get(context[-1]), _PHRASE_WEIGHT)
        return [text for text, _ in sorted(scores.items(), key=lambda item: (-item[1], item[0]))][:limit]
