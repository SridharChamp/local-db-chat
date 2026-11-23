"""Verified examples: questions whose SQL a person confirmed was right.

They are fed back to the model as worked examples for similar questions, which
is the main way answers get more precise over time. Stored as a small JSON file
with a hard size cap, written atomically so a crash never corrupts it.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

from .retrieval import tokens


def normalise(question: str) -> str:
    return " ".join((question or "").lower().split()).rstrip("?.! ")


class ExampleStore:
    def __init__(self, path: str | os.PathLike, *, static: list[dict] | None = None,
                 max_items: int = 500):
        self.path = Path(path)
        self.max_items = max(1, max_items)
        self._lock = threading.Lock()
        self.static = [e for e in (static or []) if e.get("question") and e.get("sql")]
        self.learned: list[dict] = self._load()

    def _load(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # Corrupt or unreadable: keep a copy, start fresh, never crash.
            try:
                self.path.replace(self.path.with_suffix(".corrupt.json"))
            except OSError:
                pass
            return []
        if not isinstance(data, list):
            return []
        return [e for e in data if isinstance(e, dict) and e.get("question") and e.get("sql")][-self.max_items:]

    def add(self, question: str, sql: str) -> None:
        key = normalise(question)
        if not key or not sql:
            return
        with self._lock:
            self.learned = [e for e in self.learned if normalise(e["question"]) != key]
            self.learned.append({"question": question.strip(), "sql": sql.strip()})
            if len(self.learned) > self.max_items:
                self.learned = self.learned[-self.max_items:]
            self._save()

    def remove(self, question: str) -> bool:
        key = normalise(question)
        with self._lock:
            before = len(self.learned)
            self.learned = [e for e in self.learned if normalise(e["question"]) != key]
            if len(self.learned) != before:
                self._save()
                return True
            return False

    def exact(self, question: str) -> dict | None:
        key = normalise(question)
        with self._lock:
            for e in reversed(self.learned):
                if normalise(e["question"]) == key:
                    return e
        for e in self.static:
            if normalise(e["question"]) == key:
                return e
        return None

    def similar(self, question: str, k: int = 3) -> list[dict]:
        q = tokens(question)
        if not q:
            return []
        with self._lock:
            pool = self.static + list(self.learned)
        scored = []
        for e in pool:
            et = tokens(e["question"])
            overlap = len(q & et) / len(q | et) if et else 0.0
            if overlap > 0.15:
                scored.append((overlap, e))
        scored.sort(key=lambda x: -x[0])
        return [e for _, e in scored[:k]]

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".learned-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.learned, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
