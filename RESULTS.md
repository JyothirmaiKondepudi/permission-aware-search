# Results

All numbers below were measured on the sample data with the commands shown.

## Commands

```bash
python -m pip install -e '.[dev]'
PYTHONPATH=src python -m pytest
PYTHONPATH=src python tools/demo.py
PYTHONPATH=src python tools/evaluate.py --json results.json
```

## Environment

| | |
|---|---|
| Hardware | Apple arm64, 8 CPUs |
| OS | macOS (Darwin 25.5.0) |
| Python | 3.14.7 |
| Libraries | pydantic 2.14.1, rank-bm25 0.2.2, numpy 2.5.4 |
| Data | seed 1729, size 240: 293 document records (240 documents), 504 queries; not modified |

## Tests and demo

- `pytest`: **162 passed**.
- `tools/demo.py`: **14 of 14 checks passed**. These cover an answer with citations, an abstention, a permission change through the cache, a deletion, an injected document, snapshot/restore, and a failed reload.

## Evaluation (`tools/evaluate.py`, k = 3)

Queries are split by intent. **Dev** is refund and sso. **Eval** is export, retention and "office lunar evacuation procedure" (unanswerable). Evaluation labels are read only after search, never passed to it.

| Metric | Dev | Eval |
|---|---|---|
| Queries (answerable / unanswerable) | 256 (256 / 0) | 248 (224 / 24) |
| Recall@3, found ÷ expected | 0.815 | **0.855** |
| Recall@3, found ÷ min(3, expected) | 0.939 | **0.941** |
| Citation validity (quote is the exact span of the cited revision) | 1.0 (604 citations) | **1.0** (520 citations) |
| **Security leaks** (citation not visible to the request) | **0** | **0** |
| Abstention on unanswerable questions | n/a | **1.0** |
| Wrong abstention on answerable questions | 0.043 (11 queries) | **0.0** |

Recall is reported with both denominators. Many queries have more than 3 expected documents (identical documents within a tenant), and then found ÷ expected cannot reach 1.0 with k = 3.

The 11 wrong abstentions in dev are the single-visible-document case described in `DECISIONS.md` (Known limitations).

## Latency

Workload: one thread, one `search()` call per query, sequentially, over all 504 sample queries.

| | p50 | p95 |
|---|---|---|
| Cold (cache cleared before each split) | 0.095 ms | 0.142 ms |
| Warm (eval queries answered from the cache) | 0.008 ms | 0.009 ms |

Ingesting all 293 records took about 2 ms. Latency is low because each tenant has at most a few dozen visible documents. It grows with the number of visible documents per request.
