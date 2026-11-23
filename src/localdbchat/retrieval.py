"""Pick the few tables a question is about, so the prompt stays small and precise."""

from __future__ import annotations

import math
import re
import threading
from collections import OrderedDict

from .llm import LLMError, OllamaClient
from .schema import SchemaInfo, Table

_SPLIT = re.compile(r"[^a-z0-9]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_STOP = {
    "a", "an", "the", "of", "in", "on", "for", "to", "by", "with", "and", "or", "is", "are",
    "was", "were", "be", "how", "many", "much", "what", "which", "who", "whom", "when", "where",
    "show", "me", "list", "give", "get", "find", "all", "any", "each", "per", "from", "that",
    "this", "these", "those", "do", "does", "did", "have", "has", "had", "there", "their",
    "it", "its", "as", "at", "than", "then", "last", "first", "top", "number", "count", "total",
    "please", "can", "you", "i", "we", "our", "my", "tell", "about", "between", "into",
}


def stem(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith(("ses", "xes", "ches", "shes")):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def tokens(text: str) -> set[str]:
    text = _CAMEL.sub(" ", text or "").lower()
    return {stem(t) for t in _SPLIT.split(text) if t and t not in _STOP and len(t) > 1}


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return 0.0  # vectors from different embedding models are not comparable
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class TableRetriever:
    def __init__(self, schema: SchemaInfo, glossary: dict[str, str] | None = None,
                 llm: OllamaClient | None = None, question_cache_size: int = 512):
        self.schema = schema
        self.glossary = {k.lower(): v for k, v in (glossary or {}).items()}
        self.llm = llm if (llm and llm.embed_model) else None
        self._table_tokens = {t.key: self._index(t) for t in schema.tables.values()}
        self._table_vecs: dict[str, list[float]] | None = None
        self._q_cache: OrderedDict[str, list[float]] = OrderedDict()
        self._q_cache_size = question_cache_size
        self._lock = threading.Lock()
        self.embedding_warning: str | None = None

    @staticmethod
    def _index(table: Table) -> tuple[set[str], set[str], set[str]]:
        name = tokens(table.name)
        cols = set()
        for c in table.visible_columns:
            cols |= tokens(c.name)
        notes = tokens(table.comment) | {t for c in table.visible_columns for t in tokens(c.comment)}
        return name, cols, notes

    def expand_question(self, question: str) -> set[str]:
        q = tokens(question)
        lower = question.lower()
        for term, meaning in self.glossary.items():
            if term in lower:
                q |= tokens(meaning)
        return q

    def select(self, question: str, k: int) -> list[Table]:
        tables = list(self.schema.tables.values())
        if len(tables) <= k:
            return tables
        q = self.expand_question(question)
        lower = question.lower()
        scores: dict[str, float] = {}
        for table in tables:
            name_t, col_t, note_t = self._table_tokens[table.key]
            score = 5.0 * len(q & name_t) + 2.0 * len(q & col_t) + 1.0 * len(q & note_t)
            if table.name.lower() in lower or table.name.lower().replace("_", " ") in lower:
                score += 8.0
            scores[table.key] = score

        semantic = self._semantic_scores(question)
        if semantic:
            top = max(scores.values()) or 1.0
            for key, sim in semantic.items():
                scores[key] = scores.get(key, 0.0) / top * 5.0 + 6.0 * max(sim, 0.0)

        ranked = sorted(tables, key=lambda t: (-scores[t.key], t.key))
        chosen = [t for t in ranked if scores[t.key] > 0][:k]
        if not chosen:
            # Nothing matched: offer the most connected tables, the model may still map terms.
            chosen = sorted(tables, key=lambda t: (-len(self.schema.neighbours(t)), t.key))[:k]

        # Joins need the tables in between: add foreign-key neighbours of the best matches.
        for table in list(chosen[:3]):
            for n in self.schema.neighbours(table):
                if len(chosen) >= k:
                    break
                if all(n.key != c.key for c in chosen):
                    chosen.append(n)
        return chosen[:k]

    def _semantic_scores(self, question: str) -> dict[str, float] | None:
        if self.llm is None:
            return None
        try:
            with self._lock:
                if self._table_vecs is None:
                    tables = list(self.schema.tables.values())
                    docs = [self._doc(t) for t in tables]
                    vecs: list[list[float]] = []
                    for i in range(0, len(docs), 64):  # batches keep memory and request size bounded
                        vecs.extend(self.llm.embed(docs[i:i + 64]))
                    self._table_vecs = {t.key: v for t, v in zip(tables, vecs, strict=True)}
                qv = self._q_cache.get(question)
            if qv is None:
                qv = self.llm.embed([question])[0]
                with self._lock:
                    self._q_cache[question] = qv
                    while len(self._q_cache) > self._q_cache_size:
                        self._q_cache.popitem(last=False)
            return {key: _cosine(qv, vec) for key, vec in self._table_vecs.items()}
        except LLMError as exc:
            self.embedding_warning = f"Embeddings unavailable, using keyword matching: {exc}"
            self.llm = None
            return None

    @staticmethod
    def _doc(table: Table) -> str:
        cols = ", ".join(c.name + (f" ({c.comment})" if c.comment else "") for c in table.visible_columns)
        return f"{table.full_name}: {table.comment}. Columns: {cols}"
