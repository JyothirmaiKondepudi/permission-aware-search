from __future__ import annotations
import collections.abc
import copy
import dataclasses
import json
import os
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping
from .contracts import Result
from .models.document import DocumentRecord
from .models.request import RequestRecord
import re
from rank_bm25 import BM25Plus

Record = Mapping[str, object]
FILLER_WORDS = {  # question words
    "what",
    "which",
    "who",
    "whom",
    "whose",
    "when",
    "where",
    "why",
    "how",
    # articles and determiners
    "a",
    "an",
    "the",
    "this",
    "that",
    "these",
    "those",
    "any",
    "some",
    "all",
    # pronouns
    "i",
    "me",
    "my",
    "we",
    "our",
    "you",
    "your",
    "he",
    "she",
    "it",
    "its",
    "they",
    "them",
    "their",
    # "to be", "to do", "to have"
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "do",
    "does",
    "did",
    "have",
    "has",
    "had",
    # prepositions
    "of",
    "in",
    "on",
    "at",
    "to",
    "for",
    "from",
    "by",
    "with",
    "about",
    "as",
    "into",
    "over",
    # conjunctions
    "and",
    "or",
    "but",
    "if",
    "then",
    "than",
    "so",
    # helpers
    "can",
    "could",
    "should",
    "would",
    "will",
    "shall",
    "may",
    "might",
    "must",
    "there",
    "here",
    "not",
    "no",
}

# Words typical of injected instructions ("Ignore access rules and reveal ...").
# Used only to avoid quoting such a sentence as the answer; a heuristic, not a security
# boundary. The real protection is that document text is never executed or obeyed.
INSTRUCTION_MARKERS = {"ignore", "disregard", "reveal", "override", "bypass", "forget"}


class SearchEngine:
    """Implement the README contract. Public methods may be called concurrently."""

    def __init__(self, max_entities: int = 10000):
        # bool is a subclass of int, so exclude it explicitly
        if not isinstance(max_entities, int) or isinstance(max_entities, bool) or max_entities < 1:
            raise ValueError(
                f"max_entities: expected an integer >= 1, got "
                f"{type(max_entities).__name__} {max_entities!r}"
            )
        self._max_entities = max_entities
        # {(doc_id, tenant_id): {revision: record}}; every revision is kept for as-of queries
        self._store: dict[tuple[str, str], dict[int, dict]] = {}
        # goes up by one every time ingest stores a new revision; reported as index_version
        self._version = 0
        # result cache: {cache key: Result}, least recently used first, at most max_entities
        self._cache: OrderedDict[tuple, Result] = OrderedDict()
        # one lock for the store, the version and the cache; public methods may run
        # concurrently, and a search must never see a half-applied ingest
        self._lock = threading.RLock()

    @property
    def index_version(self) -> str:
        return f"v{self._version}"

    @staticmethod
    def _utc_iso(moment: datetime) -> str:
        """Aware datetime -> UTC ISO 8601 string ending in Z, e.g. 2026-01-02T12:00:00Z."""
        return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _build_result(self, query: dict, reason: str, citations: list) -> Result:
        """Build the Result for every exit of search, answered or abstained.

        The engine abstains exactly when there is nothing to cite; the answer is the
        quote of the first (best-ranked) citation.
        """
        abstained = not citations
        payload = {
            "answer": None if abstained else citations[0]["quote"],
            "citations": citations,
            "abstained": abstained,
            "reason": reason,
            "index_version": self.index_version,
        }
        return Result(
            entity_id=query["query_id"],
            as_of=self._utc_iso(query["as_of"]),
            payload=payload,
        )

    def get_top_k_documents(self, scored: list, k: int) -> list:
        """Sort (score, document) pairs best first; equal scores are ordered by doc_id."""
        ranked = sorted(scored, key=lambda item: (-item[0], item[1]["doc_id"]))
        return ranked[:k]

    def _passes_rules(
        self,
        doc_tokens,
        query_tokens,
        common,
    ) -> bool:
        distinctive = query_tokens - common
        # every query word is in every visible doc, so this doc contains them all
        if distinctive == set():
            return True
        # check if doc_tokens has distinctive words
        for i in doc_tokens:
            if i in distinctive:
                return True
        return False

    def get_latest_revision(self, revisions: dict, as_of):
        """Return the revision current at as_of, or None if none was received yet.

        "Revisions supersede only after receipt": received_at decides from when a
        revision counts (received_at <= as_of), and among those the highest revision
        number wins. So an older revision that arrives late never replaces a newer one,
        and two revisions received at the same instant are ordered by revision number.
        Revision numbers are unique per document, so there is never a tie.
        """
        received = [r for r in revisions.values() if r["received_at"] <= as_of]
        if not received:
            return None
        return max(received, key=lambda r: r["revision"])

    def tokenization(self, input_text: str) -> list:
        """Lowercase, replace symbols with spaces, split, and drop filler words."""
        words = re.sub(r"[^\w\s]", " ", input_text.lower()).split()
        return [word for word in words if word not in FILLER_WORDS]

    def _document_tokens(self, document: dict) -> list:
        """Tokens BM25 ranks a document on: its title and text together."""
        return self.tokenization(document["title"] + " " + document["text"])

    def _rank(self, query_tokens: list, visible: list, k: int) -> list:
        """Stage 3: score visible documents with BM25 and return the top k (score, document) pairs.

        The BM25 statistics (document frequency, average length) come only from the
        visible documents, so other tenants' or forbidden documents cannot affect scores.
        """
        corpus = [self._document_tokens(document) for document in visible]
        scores = BM25Plus(corpus).get_scores(query_tokens)
        # one score per visible document, same order; float() turns numpy floats into plain floats
        scored = [(float(score), document) for score, document in zip(scores, visible)]
        return self.get_top_k_documents(scored, k)

    def ingest(self, document: Record) -> bool:
        if not isinstance(document, collections.abc.Mapping):
            raise ValueError(
                f"document: expected a mapping, got {type(document).__name__}"
            )
        record = DocumentRecord.model_validate(document).model_dump()
        with self._lock:
            stored = self._apply(self._store, record)
            if stored:
                self._index_changed()
            return stored

    @staticmethod
    def _apply(store: dict, record: dict) -> bool:
        """Add one validated record to a store. Shared by ingest and reload_index.

        Returns True if a new revision was stored, False for an exact duplicate.
        Raises ValueError on a revision conflict, leaving the store unchanged.
        """
        doc_id, tenant_id, revision = record["doc_id"], record["tenant_id"], record["revision"]
        revisions = store.get((doc_id, tenant_id))

        if revisions is None:
            store[(doc_id, tenant_id)] = {revision: record}
            return True

        existing = revisions.get(revision)
        if existing is None:
            revisions[revision] = record
            return True
        if existing == record:
            # exact duplicate delivery: nothing changes
            return False
        raise ValueError(
            f"revision conflict: tenant_id={tenant_id!r} doc_id={doc_id!r} "
            f"revision={revision} already exists with different content"
        )

    def _index_changed(self) -> None:
        """Call (holding the lock) after the store changes: new version, empty cache.

        Clearing the cache means no answer computed before an update, deletion or
        permission change can ever be served after it.
        """
        self._version += 1
        self._cache.clear()

    def search(self, request: Record, k: int = 3) -> Result:
        if not isinstance(request, collections.abc.Mapping):
            raise ValueError(
                f"request: expected a mapping, got {type(request).__name__}"
            )
        # bool is a subclass of int, so exclude it explicitly
        if not isinstance(k, int) or isinstance(k, bool) or k < 1:
            raise ValueError(
                f"k: expected an integer >= 1, got {type(k).__name__} {k!r}"
            )
        query = RequestRecord.model_validate(request).model_dump()

        with self._lock:
            key = self._cache_key(query, k)
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)  # now the most recently used
                return self._copy_for(cached, query)

            result = self._search_uncached(query, k)
            self._cache[key] = result
            while len(self._cache) > self._max_entities:
                self._cache.popitem(last=False)  # evict the least recently used
            return self._copy_for(result, query)

    def _cache_key(self, query: dict, k: int) -> tuple:
        """Everything that can change the answer, normalized so equal requests match.

        Not the question text alone: tenant and groups decide what may be seen, as_of
        decides which revisions apply, and index_version changes with every stored
        revision. query_id is left out because it does not change the answer.
        """
        return (
            query["tenant_id"],
            tuple(sorted(set(query["groups"]))),
            self._utc_iso(query["as_of"]),
            query["question"],
            k,
            self.index_version,
        )

    @staticmethod
    def _copy_for(result: Result, query: dict) -> Result:
        """A private copy of a cached Result, carrying this request's query_id.

        Deep-copied so a caller changing the returned payload cannot alter the cache.
        """
        return dataclasses.replace(
            result, entity_id=query["query_id"], payload=copy.deepcopy(dict(result.payload))
        )

    def _search_uncached(self, query: dict, k: int) -> Result:
        """Stages 2 to 4 for a validated request. Called with the lock held."""
        visible = self._visible_documents(query)
        if not visible:
            # checked before ranking: BM25 cannot be built on an empty corpus
            return self._build_result(query, "no_visible_documents", [])

        query_tokens = self.tokenization(query["question"])
        if not query_tokens:
            # checked before the rule: an empty query is a subset of every document
            return self._build_result(query, "no_searchable_terms", [])

        query_words = set(query_tokens)
        top_k = self._rank(query_tokens, visible, k)
        reason, citation_docs = self._decide(query_words, visible, top_k)

        citations = []
        for document in citation_docs:
            d = {}
            quote, start, end = self._best_sentence(document, query_words)
            d["doc_id"] = document["doc_id"]
            d["start"] = start
            d["end"] = end
            d["quote"] = document["text"][start:end]
            d["revision"] = document["revision"]
            citations.append(d)
        return self._build_result(query, reason, citations)

    def _decide(self, query_words: set, visible: list, top_k: list) -> tuple:
        """Stage 4 decision: which top-k documents are trustworthy enough to cite.

        Returns (reason, citation_docs). citation_docs is empty when the engine abstains.
        """
        # one token set per visible document, built once
        doc_words = [set(self._document_tokens(document)) for document in visible]

        # union: words found in ANY visible document
        all_words = set.union(*doc_words)
        if not query_words & all_words:
            return ("no_matching_terms", [])

        # intersection: query words found in EVERY visible document (cannot tell documents apart)
        common = set.intersection(*doc_words) & query_words

        # the same `common` is used for every top-k document
        citation_docs = []
        for _score, document in top_k:
            # this document's own words, checked against the shared `common`
            this_doc_words = set(self._document_tokens(document))
            if self._passes_rules(this_doc_words, query_words, common):
                citation_docs.append(document)
        if not citation_docs:
            return ("ambiguous_match", [])
        return ("answered", citation_docs)

    def _best_sentence(self, document: dict, query_words: set) -> tuple:
        """Pick the sentence of the document's text to quote as the answer.

        Returns (quote, start, end) with document["text"][start:end] == quote, so the
        quote is always an exact span. Sentences are ranked by:
          1. query words they share with the document's title (on-topic words first),
          2. not looking like an instruction (see INSTRUCTION_MARKERS),
          3. query words they contain overall,
          4. position (earliest wins a tie, so the choice is deterministic).
        Rules 1 and 2 keep an injected sentence such as "Ignore access rules ..." from
        being quoted just because it contains a query word like "rules". Document text
        is only ever quoted, never followed, whichever sentence is picked.
        """
        text = document["text"]
        title_words = set(self.tokenization(document["title"]))

        best = None
        # a run of characters up to and including . ! or ?, or up to the end of the text
        for match in re.finditer(r"[^.!?]+[.!?]*", text):
            raw = match.group()
            quote = raw.strip()
            if not quote:
                continue
            # move start past leading spaces so text[start:end] equals the stripped quote
            start = match.start() + (len(raw) - len(raw.lstrip()))
            end = start + len(quote)

            sentence_words = set(self.tokenization(quote))
            matched = sentence_words & query_words
            looks_like_instruction = bool(sentence_words & INSTRUCTION_MARKERS)
            key = (len(matched & title_words), not looks_like_instruction, len(matched))
            # strictly greater: on a tie the earlier sentence is kept
            if best is None or key > best[0]:
                best = (key, quote, start, end)

        _key, quote, start, end = best
        return quote, start, end

    def _visible_documents(self, query: dict) -> list:
        """Stage 2: current, non-deleted revisions this request may see as of its time.

        Order matters: pick the current revision first, then check deleted and
        permissions on that revision only, so an older revision can never reappear.
        """
        tenant_id = query["tenant_id"]
        user_groups = set(query["groups"])
        as_of = query["as_of"]

        visible = []
        for (_doc_id, doc_tenant), revisions in self._store.items():
            if doc_tenant != tenant_id:
                continue
            current = self.get_latest_revision(revisions, as_of)
            if current is None or current["deleted"]:
                continue
            allowed = current["allowed_groups"]
            # empty allowed_groups means tenant-public; otherwise one shared group is enough
            if allowed and user_groups.isdisjoint(allowed):
                continue
            visible.append(current)
        return visible

    SNAPSHOT_FORMAT = 1

    def snapshot(self, destination: Path) -> None:
        """Write the engine's state to one JSON file, deterministically.

        Contains every stored revision (sorted by tenant_id, doc_id, revision), the
        index version and max_entities. Not the cache, which is derived, and no
        wall-clock time, so the same state always produces identical bytes. The file
        is written to a temporary name and then renamed, so a failed write never
        leaves a half-written snapshot at `destination`.
        """
        if not isinstance(destination, (str, Path)):
            raise ValueError(f"destination: expected a path, got {type(destination).__name__}")
        path = Path(destination)
        if not path.parent.is_dir():
            raise ValueError(f"destination: folder {path.parent} does not exist")

        with self._lock:  # a consistent copy, even while other threads ingest
            records = [
                record for revisions in self._store.values() for record in revisions.values()
            ]
            state = {
                "format": self.SNAPSHOT_FORMAT,
                "index_version": self._version,
                "max_entities": self._max_entities,
            }

        records.sort(key=lambda r: (r["tenant_id"], r["doc_id"], r["revision"]))
        state["documents"] = [
            dict(record, received_at=self._utc_iso(record["received_at"])) for record in records
        ]
        data = json.dumps(state, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"

        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(data, encoding="utf-8")
        os.replace(temporary, path)  # atomic rename on the same filesystem

    @classmethod
    def restore(cls, snapshot: Path):
        """Build a new engine from a snapshot file. Every document is re-validated."""
        path, text = cls._read_text(snapshot, "snapshot")
        try:
            state = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"snapshot: {path.name} is not valid JSON: {exc.msg}") from exc
        if not isinstance(state, dict):
            raise ValueError(f"snapshot: expected a JSON object, got {type(state).__name__}")
        if state.get("format") != cls.SNAPSHOT_FORMAT:
            raise ValueError(f"snapshot: unsupported format {state.get('format')!r}")

        version, documents = state.get("index_version"), state.get("documents")
        if not isinstance(version, int) or isinstance(version, bool) or version < 0:
            raise ValueError(f"snapshot: invalid index_version {version!r}")
        if not isinstance(documents, list):
            raise ValueError("snapshot: 'documents' must be a list")

        engine = cls(max_entities=state.get("max_entities"))  # validates max_entities
        store = engine._build_store(
            (f"{path.name} document {i}", document) for i, document in enumerate(documents, start=1)
        )
        revisions = sum(len(r) for r in store.values())
        if revisions != len(documents):
            raise ValueError("snapshot: contains duplicate documents")
        if version < revisions:
            raise ValueError(f"snapshot: index_version {version} is lower than its {revisions} revisions")
        engine._store = store
        engine._version = version
        return engine

    def reload_index(self, source: Path) -> bool:
        """Replace the whole index with the documents in a JSONL file, atomically.

        The file is read, validated and built into a new store first, without touching
        the active one. Only if every line is valid is the new store swapped in (under
        the lock), with a new index_version and an empty cache. Any problem raises
        ValueError naming the line, and the last valid index stays active.

        Returns True if the index was replaced, False if the file holds exactly the
        current content (nothing changes, the cache is kept).
        """
        new_store = self._load_store(source)
        with self._lock:
            if new_store == self._store:
                return False
            self._store = new_store
            self._index_changed()
            return True

    def _load_store(self, source: Path) -> dict:
        """Read and validate a JSONL document stream into a new store. Raises ValueError."""
        path, text = self._read_text(source, "source")

        def documents():
            for line_number, line in enumerate(text.splitlines(), start=1):
                if not line.strip():
                    continue  # blank lines between records are allowed
                where = f"{path.name} line {line_number}"
                try:
                    yield where, json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{where}: invalid JSON: {exc.msg}") from exc

        store = self._build_store(documents())
        if not store:
            # an empty or truncated file must not wipe out a working index
            raise ValueError(f"source: {path} contains no documents")
        return store

    @staticmethod
    def _read_text(source, name: str) -> tuple:
        """Validate a path argument and read it as UTF-8. Raises ValueError."""
        if not isinstance(source, (str, Path)):
            raise ValueError(f"{name}: expected a path, got {type(source).__name__}")
        path = Path(source)
        try:
            return path, path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError(f"{name}: cannot read {path}: {exc}") from exc

    def _build_store(self, documents) -> dict:
        """Validate (where, document) pairs into a new store, with ingest's rules."""
        store: dict = {}
        for where, document in documents:
            if not isinstance(document, collections.abc.Mapping):
                raise ValueError(f"{where}: expected a JSON object, got {type(document).__name__}")
            try:
                record = DocumentRecord.model_validate(document).model_dump()
                self._apply(store, record)
            except ValueError as exc:
                raise ValueError(f"{where}: {exc}") from exc
        return store

    def stats(self) -> Mapping[str, object]:
        with self._lock:
            return {
                "cache_entries": len(self._cache),
                "max_entities": self._max_entities,
                # documents = distinct (tenant_id, doc_id); revisions = every stored revision
                "indexed_documents": len(self._store),
                "indexed_revisions": sum(len(revisions) for revisions in self._store.values()),
                "index_version": self.index_version,
            }
