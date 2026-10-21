"""End-to-end demo on the sample data, in a fresh process.

Usage (from the project root):
    PYTHONPATH=src .venv/bin/python tools/demo.py

Shows: ingest, an answer with citations, an abstention, a permission change through
the cache, a deletion, an injected document, snapshot/restore, and a failed reload
that keeps the last valid index. Exits with status 1 if any check fails.
"""
import json
import sys
import tempfile
from pathlib import Path

from secure_search.solution import SearchEngine

DATA = Path(__file__).resolve().parents[1] / "data" / "documents.jsonl"
failures = []


def section(title):
    print(f"\n=== {title} ===")


def check(condition, description):
    print(f"  [{'ok' if condition else 'FAIL'}] {description}")
    if not condition:
        failures.append(description)


def ask(engine, tenant, groups, question, as_of, k=3):
    result = engine.search({"query_id": "demo", "tenant_id": tenant, "groups": groups,
                            "question": question, "as_of": as_of}, k)
    p = result.payload
    cited = [f"{c['doc_id']}@r{c['revision']}" for c in p["citations"]]
    print(f"  {tenant} groups={groups} as_of={result.as_of}  {question!r}")
    print(f"    -> {p['reason']}; answer={p['answer']!r}; citations={cited}")
    return result


def cited_ids(result):
    return [c["doc_id"] for c in result.payload["citations"]]


def main() -> int:
    section("1. Ingest the sample documents")
    engine = SearchEngine()
    lines = [line for line in DATA.read_text().splitlines() if line.strip()]
    stored = sum(engine.ingest(json.loads(line)) for line in lines)
    print(f"  {stored} of {len(lines)} records stored; stats: {dict(engine.stats())}")
    check(engine.stats()["indexed_revisions"] == len(lines), "every sample record stored")

    section("2. Answer with citations")
    r = ask(engine, "tenant-0", ["finance"], "What is the refund policy?", "2026-01-14T00:00:00Z")
    check(r.payload["answer"] == "Refund requests must be submitted within 30 days.",
          "answer is an exact sentence from the document")
    check(len(r.payload["citations"]) == 3, "three tied refund documents cited")

    section("3. Abstain when the knowledge base has no answer")
    r = ask(engine, "tenant-0", ["finance"], "What is the office lunar evacuation procedure?",
            "2026-01-14T00:00:00Z")
    check(r.payload["abstained"] and r.payload["reason"] == "no_matching_terms", "abstains with a reason")

    section("4. Same question text after a permission change (through the cache)")
    question, as_of = "What is the retention policy?", "2026-01-02T12:00:00Z"
    with_group = ask(engine, "tenant-0", ["finance"], question, as_of)
    print(f"  cache entries: {engine.stats()['cache_entries']}")
    without_group = ask(engine, "tenant-0", [], question, as_of)
    check("doc-00024" in cited_ids(with_group), "finance user sees the finance-only document")
    check("doc-00024" not in cited_ids(without_group), "same question without the group gets no stale content")

    section("5. Deletion removes the document and its old text")
    before = ask(engine, "tenant-0", ["finance"], "What is the refund policy?", "2026-01-01T10:00:00Z")
    after = ask(engine, "tenant-0", ["finance"], "What is the refund policy?", "2026-01-01T21:00:00Z")
    check("doc-00000" in cited_ids(before), "doc-00000 visible before its deletion (Jan 1 20:00)")
    check("doc-00000" not in cited_ids(after), "doc-00000 gone after its deletion")

    section("6. Injected instructions are content, never followed")
    doc = next(json.loads(l) for l in lines if '"doc-00017"' in l)
    print(f"  doc-00017 text: {doc['text']!r}")
    r = ask(engine, "tenant-1", [], "Explain the rules for export.", "2026-01-02T00:00:00Z")
    check(r.payload["answer"] and "Ignore" not in r.payload["answer"], "injected sentence is not the answer")
    tenant_1_public = {json.loads(l)["doc_id"] for l in lines
                       if json.loads(l)["tenant_id"] == "tenant-1" and not json.loads(l)["allowed_groups"]}
    check(set(cited_ids(r)) <= tenant_1_public,
          "only tenant-1 documents this user may see are cited, despite the instruction")

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        section("7. Snapshot and restore")
        engine.snapshot(tmp / "index.json")
        restored = SearchEngine.restore(tmp / "index.json")
        request = {"query_id": "demo", "tenant_id": "tenant-0", "groups": ["finance"],
                   "question": "What is the refund policy?", "as_of": "2026-01-14T00:00:00Z"}
        print(f"  snapshot: {(tmp / 'index.json').stat().st_size} bytes; restored stats: {dict(restored.stats())}")
        check(engine.search(request).to_wire() == restored.search(request).to_wire(),
              "restored engine gives byte-identical results")
        restored.snapshot(tmp / "again.json")
        check((tmp / "index.json").read_bytes() == (tmp / "again.json").read_bytes(),
              "snapshot of the restored engine is byte-identical")

        section("8. A failed reload keeps the last valid index")
        bad = tmp / "bad.jsonl"
        bad.write_text(lines[0] + "\n" + '{"tenant_id": "t1", "doc_id": \n')
        stats_before, answer_before = dict(engine.stats()), engine.search(request).to_wire()
        try:
            engine.reload_index(bad)
            check(False, "bad reload is rejected")
        except ValueError as exc:
            print(f"  rejected: {exc}")
            check(True, "bad reload is rejected with the failing line")
        check(dict(engine.stats()) == stats_before and engine.search(request).to_wire() == answer_before,
              "index, version and answers unchanged")

    section("9. Canonical wire format")
    print("  " + engine.search(request).to_wire().decode())

    print(f"\n{'ALL CHECKS PASSED' if not failures else f'{len(failures)} CHECK(S) FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
