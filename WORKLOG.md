# Development checkpoints

**Total time:** about 7 hours: ingestion about 1 h, search filtering about 1 h, and about 5 h together for checkpoints 3 to 6 and the documentation (not timed individually).

## 1. Ingestion: validation and revision store (about 1 h)

**Tried**
- Validated each field against a table of Python types (`str`, `list[str]`, `datetime`). This crashed on the first record: `isinstance()` cannot check `list[str]`, and `received_at` arrives as a string, not a `datetime`.
- Stored one dict per `(doc_id, tenant_id)` key. Each new revision overwrote the previous one, so as-of lookups were impossible.

**Design change**
- Values are copied into a new dict (`allowed_groups` is shallow-copied), so the caller's input is never mutated, and later changes by the caller cannot reach the store.
- Store shape: `{(doc_id, tenant_id): {revision: cleaned_record}}`. Every revision is kept for as-of queries, and arrival order does not matter.

**Evidence**
- Full `data/documents.jsonl`: 293 of 293 records ingested, giving 240 documents and 293 revisions (doc-00000 holds revisions `[1, 2]`). The sample contains no malformed rows.
- Manual session checks: rev 1/rev 2 in both orders keep both revisions. An exact duplicate returns `False`. A changed text, groups, `deleted` or `received_at` on the same revision raises. The caller appending to their `allowed_groups` list after ingest leaves the store unchanged. Naive and date-only timestamps are rejected. Starter `pytest` checks pass.

## 2. Search: request validation and access filtering (about 1 h)

**Tried**
- Moved validation from hand-written checkers to Pydantic models (`DocumentRecord`, `RequestRecord`). Default lax mode accepted `revision=True`, `"2"`, blank strings, tuples for groups, and `123` as a timestamp. Fixed with strict types and `mode="before"` validators.
- Filtered each revision on its own (time, deleted, permission). This leaked a deleted document's old text and content after a permission was revoked, because the older revision still passed.
- Checked public documents with `allowed_groups == []` on a set, which never matched, so tenant-public documents were hidden from everyone.

**Design change**
- Pick the current revision first (latest `received_at <= as_of`; refined in checkpoint 6), then check `deleted` and permissions on that revision only. Order of filters: tenant → current revision → deleted → permission.
- A revision is visible at its exact receipt instant (`<=`). `as_of` accepts an ISO string or an aware `datetime`.

**Evidence**
- Scenarios on the sample data: a deleted document is gone after deletion and visible before it; a revoked document is gone after revocation and visible before it; tenant-public documents are visible to users with no groups; nothing is visible before the first revision.
- `tests/test_search_filters.py` (25 tests) plus ingest tests: 46 tests pass. Every sample query returns no other tenant's documents, no tombstones, no future revisions, and no permission leaks.

**Next**
- BM25 ranking over the filtered candidates, then extractive answers and abstention.

## 3. Search: BM25 ranking

**Tried**
- Built a separate `BM25Okapi` per document with a flat token list. The library treated each word as a document, so every score was 0. Fixed by building one BM25 over a corpus of token lists, one per visible document.
- Kept scores in a dict keyed by score. Documents with identical text tied and overwrote each other: as of Jan 14, tenant-0 had 23 visible documents but only 5 survived, and irrelevant documents filled the top 3.
- `BM25Okapi` gave negative or tied scores with one or two visible documents (its IDF goes negative when a word is in more than half the documents). With one document, the refund match scored -0.667.

**Design change**
- Rank on title + text, so questions about "export" and "retention" match documents whose text says "Exports" and "retained".
- Switched to `BM25Plus` (IDF never negative). Statistics come only from the visible documents, so hidden documents cannot affect scores.
- Rank (score, document) pairs best first, ties broken by `doc_id`, then keep the top k. Return early when nothing is visible, since BM25 cannot be built on an empty corpus.
- Split search into `_visible_documents`, `_rank` and `_document_tokens`.

**Evidence**
- tenant-0, "What is the refund policy?": the refund document ranks first; as of Jan 14 the top 3 are the three tied refund documents in `doc_id` order; a single visible document scores positive; no visible documents returns early without building BM25.
- `tests/test_ranking.py` (23 tests): 69 tests pass in total. Putting back `BM25Okapi`, removing the tie-break, or ranking on text only each makes tests fail.

**Next**
- Stage 4: extractive answer, citations, confidence and abstention, and the `Result`.

## 4. Search: answers, abstention and Result. Search is complete.

**Tried**
- Abstaining when the best score is not strictly ahead of the second. Rejected: it wrongly abstains when relevant documents tie, which is common in the sample.
- Abstaining when the best document has no query word that is missing from some visible document. Rejected alone: it wrongly abstains when only one document is visible.
- Building the "common words" set per document instead of across all visible documents. This made "Explain the rules for refund." abstain, because "explain" and "rules" appear in no document.
- Picking the sentence with the most query words. For "Explain the rules for export.", this quoted the injected sentence "Ignore access rules ...", because it contains "rules".

**Design decision**
- Abstention, checked in order: `no_visible_documents`, `no_searchable_terms`, `no_matching_terms`, `ambiguous_match`, otherwise `answered`.
- A document is cited only if it contains a query word that is not in every visible document, or contains every query word. All top-k documents that pass are cited; the answer is the best sentence of the first one.
- The answer sentence prefers query words that are in the document title, and avoids instruction-like sentences ("ignore", "reveal", ...). Document text is only ever quoted, never followed.
- `Result`: `entity_id` is the query ID, `as_of` is UTC with a `Z`, citations hold `doc_id`, `revision`, `quote`, `start`, `end` (`text[start:end] == quote`), and `index_version` is a counter that goes up when a new revision is stored.

**Evidence**
- Sample data: "refund policy" cites only the refund document; as of Jan 14 it cites the three tied refund documents; "vacation policy" abstains; every "lunar" question abstains; no citation quotes injected text, and every quote is an exact span.
- `tests/test_answers.py` (37 tests): 106 tests pass in total. Removing the instruction check, the shared common set, the UTC conversion, the empty-query check, or the version counter each makes tests fail.

**Next**
- Result cache and invalidation, then snapshot/restore and reload.

## 5. Cache, reload, snapshot, evaluation and demo

**Tried**
- A cache keyed by question text only (customer note 1). Tests showed it served a finance-only answer to a user without the group, and one tenant's answer to another.
- Building the reloaded index directly into the live store. A bad line halfway through left a partly replaced index.

**Design change**
- LRU result cache bounded by `max_entities`, keyed by tenant, sorted groups, UTC `as_of`, question, `k` and `index_version`, and cleared whenever a new revision is stored. One lock guards the store, version and cache.
- `reload_index` validates the whole file into a new store, then swaps it in under the lock; any bad line raises `ValueError` and keeps the old index.
- `snapshot` writes one sorted, canonical JSON file (written to a temporary name, then renamed); `restore` re-validates every document.
- `tools/evaluate.py` splits queries by intent (dev: refund, sso; eval: export, retention, lunar) and fails on any security leak. `tools/demo.py` runs the end-to-end demo with checks.

**Evidence**
- 159 tests pass; the demo passes 14 of 14 checks.
- Eval split: recall@3 0.855 (0.941 with min(3, expected)), citation validity 1.0, 0 leaks, abstention on unanswerable 1.0; cold p50 0.095 ms, p95 0.142 ms.
- Found and kept as a documented limitation: 11 dev queries abstain when only one document is visible.

## 6. Revision ordering fix (after review)

**Tried**
- The current revision was the one with the latest `received_at`. Feeding out-of-order data showed two problems: if rev 2 was received before rev 1, rev 1 became current once both had arrived; and if the late, older revision followed a deletion, it un-deleted the document. Two revisions with the same `received_at` were ordered by ingest order.

**Design change**
- Read "revisions supersede only after receipt" as two rules: `received_at` decides from when a revision counts, and the highest revision number among those received is current (`get_latest_revision`).

**Evidence**
- New tests for a late older revision (both ingest orders), a late older revision after a deletion, and equal receipt times (both ingest orders); they fail on the old rule. 162 tests pass, the demo passes, and the evaluation numbers are unchanged (the sample has no revisions received out of order).
