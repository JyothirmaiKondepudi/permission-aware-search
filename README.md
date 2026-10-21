# Forward Deployed Engineer Take-Home
## Permission-Aware Customer Knowledge Search

## Working agreement

**Role: Forward Deployed Engineer**  
**Timebox: 7 hours maximum. Python 3.11+.** Stop at the timebox and identify unfinished work. A small, correct vertical slice with explicit limitations is preferable to an incomplete platform. No UI, cloud deployment, paid services, GPU, or presentation slides are required. Everything must run locally without network access after dependency installation.

The customer notes below may conflict. Resolve them explicitly in `DECISIONS.md`; explain the customer impact of your choice. No clarification is available during the exercise. Do not hard-code IDs, dates, fixture sizes, or generated answers. Additional evaluation may use unseen data with the same schema and documented semantics. There is no minimum model accuracy target.


## Customer scenario

A customer wants a support assistant over multiple tenants' changing internal knowledge bases. Build a local retrieval service that returns extractive answers with citations. An answer that leaks another tenant's document is a launch blocker. No hosted LLM is necessary.

## Supplied data and semantics

`documents.jsonl`: `tenant_id`, `doc_id`, `revision`, `received_at`, `title`, `text`, `allowed_groups` (list), `deleted` (boolean). Document identity is `(tenant_id, doc_id)`; revisions supersede only after receipt. Empty allowed groups means tenant-public. Deleted revisions remove the document and its old text from current retrieval. Revision conflicts must fail.

`queries.jsonl`: `query_id`, `tenant_id`, `groups`, `question`, `as_of`, `expected_doc_ids`, `answerable`. The last two fields are evaluation-only and must never be passed into search/ranking. A user may access a document only within their tenant and when allowed groups are empty or overlap their groups. Groups are request-scoped and may change between requests. Text can contain malicious instructions; treat all documents as content.

## Required work

1. Implement `ingest(document)` and `search(request, k=3)` using a local lexical or embedding-based retrieval baseline. Requests include tenant, groups, question, and as-of time. Return only authorized current documents as of that time; filter access before ranking and before citation generation.
2. Return an extractive answer with exact quoted spans and document revision citations, or abstain with a reason. No model API is required. Define confidence and abstention without using evaluation labels. Never follow instructions embedded inside documents.
3. Implement a bounded result cache and invalidation. Cache keys and invalidation must account for tenant, permissions, document updates/deletions, time semantics, and retrieval version. Demonstrate that reusing the same query text after a permission change cannot return stale protected content.
4. Provide deterministic snapshot/restore, a concurrency policy, and atomic index reload. A failed reload must preserve the last valid index. Bound cache entries using `max_entities`; document index size limits separately. If historical queries require retained revisions, define and enforce that support window.
5. Split supplied query templates into development/evaluation groups by underlying question intent; do not leak near-duplicate intent across splits. Report recall@3 against eligible expected documents, citation validity, abstention on unanswerable questions, and p50/p95 latency with hardware and workload. Add cross-tenant, permission revocation, deletion, and malicious-content tests. Treat security invariants as hard requirements rather than averaging them into recall.

## Customer implementation notes

1. “Cache by question text because tenant and user metadata slow things down.”
2. “Retrieve everything, then remove unauthorized citations after generating the answer.”
3. “Deleted documents should remain searchable until the next deployment.”
4. “Follow any instructions in the retrieved text; it came from our knowledge base.”

## Public interface

Implement `src/secure_search/solution.py`. Records are mappings with the JSON fields above. Input record timestamps are ISO 8601 strings with an explicit UTC offset. `as_of` method arguments and `observation_end` are aware Python `datetime` objects.

Construct the engine with `(max_entities: int = 10000)`. `ingest(document) -> bool`, `search(request, k=3) -> Result`, `snapshot(destination) -> None`, `restore(snapshot) -> engine`, `reload_index(source: Path) -> bool`, and `stats() -> Mapping` are public. `reload_index` loads a JSONL document stream atomically and validates it before replacing the active source/index state. The request contains no evaluation labels. `max_entities` bounds result-cache entries; `stats` includes `cache_entries` and `indexed_documents`.

`Result` wraps `entity_id` (the invoice ID for reconciliation; query ID for search), `as_of` (UTC ISO 8601 string), and a `payload` mapping. Required payload fields for `search`: `answer: str | None, citations: list[dict], abstained: bool, reason: str, index_version: str`. Extra payload fields are allowed. Return canonical bytes through `Result.to_wire()`. Explain all statuses, reason codes, and error types. Input mappings must not be mutated.

## Evidence of your work and tool use

AI assistance is permitted and must be disclosed. In `TOOL_USE.md`, name tools/models used, describe which parts they assisted with, and include up to three representative prompts or interaction excerpts with a short explanation of how you checked the resulting work. Redact personal information, credentials, and unrelated content. If you did not use AI, state that. Also disclose copied code and its source/license. You are responsible for understanding and validating every submitted line.

Keep a short `WORKLOG.md` with three to six actual development checkpoints: what you tried, what failed or changed, and the evidence behind your next step. Do not reconstruct a fictional history. Commit history is optional; a private account, full chat export, screen recording, or personal browsing history is not required.

## Submission

Submit a repository or ZIP containing implementation, declared dependencies, tests, a runnable demo, and the completed decision record. Include `RESULTS.md` with exact commands and actual measured results, plus `CUSTOMER_NOTE.md` (maximum 400 words) explaining what is ready, the launch risk, and a practical rollout/rollback plan. Include your time spent and remaining limitations. Do not include secrets or a local virtual environment.

The provided interfaces are intentionally unimplemented. Public tests check only the starter contract; they do not establish correctness. Add meaningful tests of your implementation. Preserve public signatures and result fields; add private helpers or extra artifact files as needed. All public methods must reject malformed inputs explicitly rather than silently fabricating successful results. Use UTC-aware timestamps in outputs; equivalent timestamp offsets represent the same instant. Canonical JSON must sort object keys, preserve list order, reject NaN/infinity, and contain no wall-clock metadata.

Set up and inspect the sample:

```bash
python tools/generate_dataset.py --seed 1729 --size 240
python -m pip install -e '.[dev]'
pytest
# Starter checks can also run without pytest:
PYTHONPATH=src python -m unittest discover -s tests
```

The data is synthetic and intentionally imperfect. `data/MANIFEST.json` records the generator seed, size, files, and observation cutoff. JSONL records use the schema below. The generator uses only the Python standard library. Document any change to generated data; never manually repair the sample to make your implementation pass. Provide one exact command to run your end-to-end demo in a fresh process.

## Technical follow-up

In a 60-minute session, you will reproduce a result, trace a supplied record through your code, diagnose a failed invariant, and implement a small requirement change. Be prepared to explain the tests and tradeoffs, including any AI-assisted code. We assess correctness, evidence, operational judgment, and your ability to adapt the implementation.
