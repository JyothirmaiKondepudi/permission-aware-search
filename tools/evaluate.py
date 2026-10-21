"""Evaluate the search engine on the sample data.

Usage (from the project root):
    PYTHONPATH=src .venv/bin/python tools/evaluate.py
    PYTHONPATH=src .venv/bin/python tools/evaluate.py --dev-intents refund,sso --json results.json

Queries are split into dev and eval groups by underlying intent (the topic of the
question), so both wordings of the same question always land in the same split.
Evaluation labels (expected_doc_ids, answerable) are read here only, after search;
they are never passed to the engine.

Security invariants are hard checks: any cited document that the request may not
see as of its time is a leak, and the script exits with status 1.
"""
import argparse
import json
import os
import platform
import statistics
import sys
import time
from pathlib import Path

from secure_search.models.request import RequestRecord
from secure_search.solution import SearchEngine

DATA = Path(__file__).resolve().parents[1] / "data"
REQUEST_FIELDS = ("query_id", "tenant_id", "groups", "question", "as_of")
# question-template words, removed to find the topic a question is about
TEMPLATE_WORDS = {"explain", "rules", "policy"}


def read_jsonl(path: Path) -> list:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def intent_of(engine: SearchEngine, question: str) -> str:
    """'What is the refund policy?' and 'Explain the rules for refund.' -> 'refund'."""
    words = [w for w in engine.tokenization(question) if w not in TEMPLATE_WORDS]
    return " ".join(words) or "(none)"


def percentile(values: list, pct: int) -> float:
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[pct - 1]


def evaluate(engine: SearchEngine, queries: list, texts: dict, k: int) -> dict:
    recall_by_expected, recall_by_min_k = [], []
    citations_total = citations_valid = leaks = 0
    unanswerable = abstained_on_unanswerable = 0
    answerable = abstained_on_answerable = 0
    latencies_ms = []

    for q in queries:
        request = {f: q[f] for f in REQUEST_FIELDS}  # labels are never sent to search
        start = time.perf_counter()
        payload = engine.search(request, k).payload
        latencies_ms.append((time.perf_counter() - start) * 1000)

        # what this request may see as of its time, recomputed independently of the answer
        query = RequestRecord.model_validate(request).model_dump()
        allowed = {(d["doc_id"], d["revision"]) for d in engine._visible_documents(query)}

        for c in payload["citations"]:
            citations_total += 1
            if (c["doc_id"], c["revision"]) not in allowed:
                leaks += 1
                continue
            text = texts[(q["tenant_id"], c["doc_id"], c["revision"])]
            if text[c["start"]:c["end"]] == c["quote"]:
                citations_valid += 1

        cited = [c["doc_id"] for c in payload["citations"]]
        expected = set(q["expected_doc_ids"])
        if q["answerable"]:
            answerable += 1
            abstained_on_answerable += payload["abstained"]
            found = len(expected & set(cited[:k]))
            recall_by_expected.append(found / len(expected))
            recall_by_min_k.append(found / min(k, len(expected)))
        else:
            unanswerable += 1
            abstained_on_unanswerable += payload["abstained"]

    def ratio(a, b):
        return round(a / b, 4) if b else None

    return {
        "queries": len(queries),
        "answerable": answerable,
        "unanswerable": unanswerable,
        f"recall@{k} (found / expected)": ratio(sum(recall_by_expected), len(recall_by_expected)),
        f"recall@{k} (found / min({k}, expected))": ratio(sum(recall_by_min_k), len(recall_by_min_k)),
        "citation_validity": ratio(citations_valid, citations_total),
        "citations": citations_total,
        "security_leaks": leaks,
        "abstention_on_unanswerable": ratio(abstained_on_unanswerable, unanswerable),
        "abstention_on_answerable": ratio(abstained_on_answerable, answerable),
        "latency_ms_p50": round(percentile(latencies_ms, 50), 3),
        "latency_ms_p95": round(percentile(latencies_ms, 95), 3),
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dev-intents", default="refund,sso",
                   help="comma-separated intents for the dev split; all others are eval")
    p.add_argument("-k", type=int, default=3)
    p.add_argument("--json", type=Path, help="also write the results to this JSON file")
    a = p.parse_args()

    documents = read_jsonl(DATA / "documents.jsonl")
    queries = read_jsonl(DATA / "queries.jsonl")
    manifest = json.loads((DATA / "MANIFEST.json").read_text())

    engine = SearchEngine()
    start = time.perf_counter()
    for record in documents:
        engine.ingest(record)
    ingest_ms = (time.perf_counter() - start) * 1000
    texts = {(d["tenant_id"], d["doc_id"], d["revision"]): d["text"] for d in documents}

    dev_intents = {i.strip() for i in a.dev_intents.split(",") if i.strip()}
    splits = {"dev": [], "eval": []}
    for q in queries:
        splits["dev" if intent_of(engine, q["question"]) in dev_intents else "eval"].append(q)
    intents = {name: sorted({intent_of(engine, q["question"]) for q in qs}) for name, qs in splits.items()}

    # cold: every request computed (each split gets a fresh engine state for its cache);
    # warm: the same requests again, answered from the cache
    results = {}
    for name, qs in splits.items():
        engine._cache.clear()
        results[name] = evaluate(engine, qs, texts, a.k)
    warm = evaluate(engine, splits["eval"], texts, a.k)

    report = {
        "dataset": {"seed": manifest.get("seed"), "size": manifest.get("size"),
                    "documents": len(documents), "queries": len(queries)},
        "hardware": {"machine": platform.machine(), "processor": platform.processor() or "unknown",
                     "cpu_count": os.cpu_count(), "os": f"{platform.system()} {platform.release()}",
                     "python": platform.python_version()},
        "workload": {"ingest_ms_total": round(ingest_ms, 1), "k": a.k,
                     "latency": "single thread, one search() call per query, cache cleared per split (cold)"},
        "intents": intents,
        "dev": results["dev"],
        "eval": results["eval"],
        "eval_warm_cache_latency_ms": {"p50": warm["latency_ms_p50"], "p95": warm["latency_ms_p95"]},
        "engine_stats": dict(engine.stats()),
    }

    print(json.dumps(report, indent=2))
    if a.json:
        a.json.write_text(json.dumps(report, indent=2) + "\n")

    leaks = results["dev"]["security_leaks"] + results["eval"]["security_leaks"]
    if leaks:
        print(f"SECURITY FAILURE: {leaks} citation(s) not visible to the request", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
