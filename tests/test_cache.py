"""Result cache tests: keys, invalidation, bounds, isolation, stats and concurrency."""
import threading
import unittest

from secure_search.solution import SearchEngine

TEXTS = {
    "refund": ("refund policy", "Refund requests must be submitted within 30 days."),
    "sso": ("sso policy", "SSO setup requires a verified domain and an administrator."),
    "retention": ("retention policy", "Audit logs are retained for 180 days."),
}


def make_doc(doc_id, topic, **overrides):
    title, text = TEXTS[topic]
    doc = {"tenant_id": "tenant-a", "doc_id": doc_id, "revision": 1,
           "received_at": "2026-01-01T00:00:00Z", "title": title, "text": text,
           "allowed_groups": [], "deleted": False}
    doc.update(overrides)
    return doc


def request(question="What is the refund policy?", query_id="q-1", tenant_id="tenant-a",
            groups=(), as_of="2026-01-10T00:00:00Z"):
    return {"query_id": query_id, "tenant_id": tenant_id, "groups": list(groups),
            "question": question, "as_of": as_of}


def cited(result):
    return [c["doc_id"] for c in result.payload["citations"]]


class PermissionChangeTests(unittest.TestCase):
    """README: reusing the same query text after a permission change must not return
    stale protected content."""

    def setUp(self):
        self.engine = SearchEngine()
        self.engine.ingest(make_doc("d-secret", "refund", allowed_groups=["finance"]))
        self.engine.ingest(make_doc("d-sso", "sso"))
        self.engine.ingest(make_doc("d-retention", "retention"))

    def test_same_question_after_losing_a_group(self):
        with_group = self.engine.search(request(groups=["finance"]))
        self.assertEqual(cited(with_group), ["d-secret"])
        self.assertEqual(self.engine.stats()["cache_entries"], 1)

        without_group = self.engine.search(request(groups=[]))
        self.assertNotIn("d-secret", cited(without_group))
        self.assertNotIn("30 days", without_group.to_wire().decode())

    def test_same_question_from_another_tenant(self):
        self.engine.search(request(groups=["finance"]))
        other = self.engine.search(request(tenant_id="tenant-b", groups=["finance"]))
        self.assertEqual(other.payload["reason"], "no_visible_documents")

    def test_document_revoked_after_answer_was_cached(self):
        self.assertEqual(cited(self.engine.search(request(groups=["finance"]))), ["d-secret"])
        self.engine.ingest(make_doc("d-secret", "refund", revision=2,
                                    received_at="2026-01-05T00:00:00Z", allowed_groups=["admin"]))
        self.assertEqual(self.engine.stats()["cache_entries"], 0)
        self.assertNotIn("d-secret", cited(self.engine.search(request(groups=["finance"]))))

    def test_document_deleted_after_answer_was_cached(self):
        self.engine.search(request(groups=["finance"]))
        self.engine.ingest(make_doc("d-secret", "refund", revision=2, received_at="2026-01-05T00:00:00Z",
                                    deleted=True, text="This policy has been withdrawn."))
        after = self.engine.search(request(groups=["finance"]))
        self.assertNotIn("d-secret", cited(after))
        self.assertNotIn("30 days", after.to_wire().decode())


class CacheKeyTests(unittest.TestCase):
    def setUp(self):
        self.engine = SearchEngine()
        self.engine.ingest(make_doc("d-refund", "refund"))
        self.engine.ingest(make_doc("d-sso", "sso"))

    def test_repeat_is_a_hit_with_the_new_query_id(self):
        first = self.engine.search(request(query_id="q-1"))
        second = self.engine.search(request(query_id="q-2"))
        self.assertEqual(self.engine.stats()["cache_entries"], 1)
        self.assertEqual(second.entity_id, "q-2")
        self.assertEqual(first.payload, second.payload)

    def test_group_order_and_duplicates_share_an_entry(self):
        self.engine.search(request(groups=["hr", "finance"]))
        self.engine.search(request(groups=["finance", "hr", "finance"]))
        self.assertEqual(self.engine.stats()["cache_entries"], 1)

    def test_equivalent_as_of_offsets_share_an_entry(self):
        self.engine.search(request(as_of="2026-01-02T12:00:00Z"))
        self.engine.search(request(as_of="2026-01-02T17:30:00+05:30"))
        self.assertEqual(self.engine.stats()["cache_entries"], 1)

    def test_different_as_of_k_or_question_are_separate_entries(self):
        self.engine.search(request())
        self.engine.search(request(as_of="2026-01-11T00:00:00Z"))
        self.engine.search(request(question="What is the sso policy?"))
        self.engine.search(request(), 1)
        self.assertEqual(self.engine.stats()["cache_entries"], 4)

    def test_as_of_before_and_after_an_update_give_different_answers(self):
        self.engine.ingest(make_doc("d-refund", "refund", revision=2, received_at="2026-01-05T00:00:00Z",
                                    text="Refund requests must be submitted within 14 days."))
        old = self.engine.search(request(as_of="2026-01-03T00:00:00Z")).payload["answer"]
        new = self.engine.search(request(as_of="2026-01-06T00:00:00Z")).payload["answer"]
        self.assertIn("30 days", old)
        self.assertIn("14 days", new)

    def test_cached_answer_reports_the_current_index_version(self):
        result = self.engine.search(request())
        self.assertEqual(result.payload["index_version"], self.engine.stats()["index_version"])


class InvalidationTests(unittest.TestCase):
    def setUp(self):
        self.engine = SearchEngine()
        self.engine.ingest(make_doc("d-refund", "refund"))

    def test_new_revision_clears_the_cache(self):
        self.engine.search(request())
        self.engine.ingest(make_doc("d-sso", "sso"))
        self.assertEqual(self.engine.stats()["cache_entries"], 0)

    def test_duplicate_or_rejected_ingest_keeps_the_cache(self):
        self.engine.search(request())
        self.engine.ingest(make_doc("d-refund", "refund"))  # exact duplicate
        with self.assertRaises(ValueError):
            self.engine.ingest(make_doc("d-refund", "refund", text="conflicting text"))
        with self.assertRaises(ValueError):
            self.engine.ingest({"not": "a document"})
        self.assertEqual(self.engine.stats()["cache_entries"], 1)

    def test_abstentions_are_cached_and_invalidated_too(self):
        self.engine.search(request(question="What is the sso policy?"))
        self.assertEqual(self.engine.stats()["cache_entries"], 1)
        self.engine.ingest(make_doc("d-sso", "sso"))
        self.assertEqual(self.engine.search(request(question="What is the sso policy?"))
                         .payload["reason"], "answered")


class BoundTests(unittest.TestCase):
    def setUp(self):
        self.engine = SearchEngine(max_entities=2)
        self.engine.ingest(make_doc("d-refund", "refund"))

    def test_cache_never_exceeds_max_entities(self):
        for i in range(10):
            self.engine.search(request(question=f"refund policy {i}"))
            self.assertLessEqual(self.engine.stats()["cache_entries"], 2)

    def test_least_recently_used_entry_is_evicted(self):
        a, b, c = (request(question=f"refund {x}") for x in "abc")
        self.engine.search(a)
        self.engine.search(b)
        self.engine.search(a)  # a becomes most recently used
        self.engine.search(c)  # evicts b
        keys = [key[3] for key in self.engine._cache]
        self.assertEqual(keys, ["refund a", "refund c"])

    def test_max_entities_is_validated(self):
        for bad in (0, -1, True, "10", 2.5):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    SearchEngine(max_entities=bad)


class IsolationTests(unittest.TestCase):
    def test_changing_a_returned_payload_does_not_change_the_cache(self):
        engine = SearchEngine()
        engine.ingest(make_doc("d-refund", "refund"))
        first = engine.search(request())
        first.payload["citations"].clear()
        first.payload["answer"] = "tampered"
        second = engine.search(request())
        self.assertEqual(cited(second), ["d-refund"])
        self.assertEqual(second.payload["answer"], TEXTS["refund"][1])


class StatsTests(unittest.TestCase):
    def test_required_fields(self):
        engine = SearchEngine()
        engine.ingest(make_doc("d-refund", "refund"))
        engine.ingest(make_doc("d-refund", "refund", revision=2, received_at="2026-01-02T00:00:00Z"))
        engine.ingest(make_doc("d-sso", "sso"))
        engine.search(request())
        stats = engine.stats()
        self.assertEqual(stats["indexed_documents"], 2)
        self.assertEqual(stats["indexed_revisions"], 3)
        self.assertEqual(stats["cache_entries"], 1)


class ConcurrencyTests(unittest.TestCase):
    def test_parallel_ingest_and_search_stay_consistent(self):
        engine = SearchEngine(max_entities=50)
        engine.ingest(make_doc("d-secret", "refund", allowed_groups=["finance"]))
        errors, leaks = [], []

        def writer(start):
            try:
                for i in range(start, start + 50):
                    engine.ingest(make_doc(f"d-{i}", "sso"))
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)

        def reader():
            try:
                for _ in range(100):
                    result = engine.search(request(groups=[]))
                    if "d-secret" in cited(result):
                        leaks.append(result)
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(n * 100,)) for n in range(3)]
        threads += [threading.Thread(target=reader) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(leaks, [])
        self.assertEqual(engine.stats()["indexed_documents"], 151)
        self.assertEqual(engine.stats()["index_version"], "v151")


if __name__ == "__main__":
    unittest.main()
