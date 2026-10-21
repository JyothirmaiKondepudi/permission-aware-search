# Tool and source disclosure

**Tool:** Claude Opus 5.5, used through Claude Code.

**Used for:**
- **Design:** pros and cons of options and review of my approach: the store and access filter, retrieval (BM25 library vs own code), validation (hand-written vs Pydantic), the abstention rule, the cache key, reload and snapshot.
- **Cleanup and modularising:** splitting search into separate methods (`_visible_documents`, `_rank`, `_decide`, `_best_sentence`, `_build_result`), shared helpers for ingest, reload and restore, renaming, and readability cleanups with no change in behaviour.
- **Code it wrote at my request:** the answer and decision steps, the result cache and lock, `stats()`, `reload_index`, `snapshot`/`restore`, the revision-ordering fix, and the scripts in `tools/` (`evaluate.py`, `demo.py`, `trace_search.py`). I reviewed each piece with it line by line.
- **Tests:** all files in `tests/`. I reviewed them and checked that they fail when bugs are put in on purpose.
- **Documentation:** drafts of `DECISIONS.md`, `RESULTS.md`, `CUSTOMER_NOTE.md` and the worklog, based on my decisions and the measured results.

**What I did:** the ingestion design and validation rules, the revision store, the access filter and BM25 wiring, the citation rule (`_passes_rules`), and the design decisions recorded in `DECISIONS.md`.

**Representative prompts:**
1. *"what are the design options i have? what are the pros and cons of it."* (retrieval choice). I checked the recommendation against the sample data.
2. *"tenant_id + doc_id :{revision:document}, how about this?"* I verified it by ingesting the full sample (293 records → 293 stored revisions).
3. *"give me the scenarios where the failures are happening. i will figure out the fix."* I fixed the bugs myself and re-ran each scenario. All tests pass.

**How I checked the work:** every change was run against the sample data and the test suite (162 tests), and `tools/demo.py` and `tools/evaluate.py` report the results in `RESULTS.md`.

**Copied code:** none.

**Third-party libraries:** Pydantic (MIT license) for input validation; rank-bm25 (Apache-2.0) for BM25 scoring, which also installs NumPy (BSD).
