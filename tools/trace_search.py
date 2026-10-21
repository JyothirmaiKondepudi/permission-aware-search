"""Dev helper: trace one ingest + search, printing what goes into and out of each step.

Usage (from the project root):
    .venv/bin/python tools/trace_search.py

Edit DOCUMENTS and REQUEST below to trace your own case. The engine's methods are
wrapped from the outside, so solution.py contains no debug or logging code (and no
document text is written to logs by the library itself).
"""
import functools
import json

from secure_search.solution import SearchEngine

# ---- the case to trace (Case 6: punctuation-only text) ---------------------------
DOCUMENTS = [
    {
        "tenant_id": "t1", "doc_id": "d6", "revision": 1,
        "received_at": "2026-01-01T00:00:00Z",
        "title": "refund policy", "text": "!!! ??? ...",
        "allowed_groups": [], "deleted": False,
    },
]
REQUEST = {
    "query_id": "q6", "tenant_id": "t1", "groups": [],
    "question": "What is the refund policy?", "as_of": "2026-01-10T00:00:00Z",
}
K = 3
# -----------------------------------------------------------------------------------

TRACED = [
    "ingest", "_apply", "_index_changed",
    "search", "_cache_key", "_search_uncached",
    "_visible_documents", "get_latest_revision",
    "tokenization", "_document_tokens", "_rank", "get_top_k_documents",
    "_decide", "_passes_rules", "_best_sentence", "_build_result",
]
depth = 0


def show(value, limit=400):
    """Readable, shortened view of an object."""
    if isinstance(value, dict) and "doc_id" in value and "revision" in value:
        value = {k: value[k] for k in ("doc_id", "revision", "received_at", "deleted",
                                       "allowed_groups", "title", "text") if k in value}
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + " …"


def wrap(name, method, is_static):
    @functools.wraps(method)
    def traced(*args, **kwargs):
        global depth
        shown_args = args if is_static else args[1:]  # drop self
        pad = "  " * depth
        print(f"{pad}→ {name}(" + ", ".join(show(a, 200) for a in shown_args) + ")")
        depth += 1
        try:
            result = method(*args, **kwargs)
        except Exception as exc:
            depth -= 1
            print(f"{pad}✗ {name} raised {type(exc).__name__}: {str(exc).splitlines()[0]}")
            raise
        depth -= 1
        print(f"{pad}← {name} returned {show(result)}")
        return result
    return traced


for name in TRACED:
    raw = SearchEngine.__dict__[name]
    is_static = isinstance(raw, staticmethod)
    function = raw.__func__ if is_static else raw
    wrapped = wrap(name, function, is_static)
    setattr(SearchEngine, name, staticmethod(wrapped) if is_static else wrapped)


def main():
    engine = SearchEngine()
    print("=" * 30, "INGEST", "=" * 30)
    for document in DOCUMENTS:
        engine.ingest(document)
    print("\nstore after ingest:",
          {key: {rev: show(record, 160) for rev, record in revisions.items()}
           for key, revisions in engine._store.items()})

    print("\n" + "=" * 30, "SEARCH", "=" * 30)
    result = engine.search(REQUEST, K)

    print("\n" + "=" * 30, "RESULT", "=" * 30)
    print(json.dumps(json.loads(result.to_wire()), indent=2))


if __name__ == "__main__":
    main()
