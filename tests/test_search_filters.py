"""Stage 2 tests: tenant isolation, as-of revision selection, deletion and permissions.

These are security invariants: each must hold on its own, never averaged into recall.
"""
import json
import unittest
from datetime import datetime
from pathlib import Path

from secure_search.models.request import RequestRecord
from secure_search.solution import SearchEngine

DATA = Path(__file__).resolve().parents[1] / "data" / "documents.jsonl"


def make_doc(**overrides):
    doc = {
        "tenant_id": "tenant-a",
        "doc_id": "doc-1",
        "revision": 1,
        "received_at": "2026-01-01T00:00:00Z",
        "title": "refund policy",
        "text": "Refund requests must be submitted within 30 days.",
        "allowed_groups": [],
        "deleted": False,
    }
    doc.update(overrides)
    return doc


def visible(engine, tenant_id="tenant-a", groups=(), as_of="2026-01-10T00:00:00Z"):
    """Return {doc_id: revision} visible to this request."""
    request = {
        "query_id": "q",
        "tenant_id": tenant_id,
        "groups": list(groups),
        "question": "refund policy",
        "as_of": as_of,
    }
    validated = RequestRecord.model_validate(request).model_dump()
    return {d["doc_id"]: d["revision"] for d in engine._visible_documents(validated)}


class TenantIsolationTests(unittest.TestCase):
    def test_other_tenant_documents_are_never_visible(self):
        engine = SearchEngine()
        engine.ingest(make_doc(tenant_id="tenant-a", doc_id="a-doc"))
        engine.ingest(make_doc(tenant_id="tenant-b", doc_id="b-doc"))
        self.assertEqual(visible(engine, "tenant-a"), {"a-doc": 1})
        self.assertEqual(visible(engine, "tenant-b"), {"b-doc": 1})

    def test_same_doc_id_in_two_tenants_stays_separate(self):
        engine = SearchEngine()
        engine.ingest(make_doc(tenant_id="tenant-a", text="Tenant A text."))
        engine.ingest(make_doc(tenant_id="tenant-b", text="Tenant B text."))
        results = engine._visible_documents(
            RequestRecord.model_validate(
                {"query_id": "q", "tenant_id": "tenant-a", "groups": [],
                 "question": "x", "as_of": "2026-01-10T00:00:00Z"}
            ).model_dump()
        )
        self.assertEqual([d["text"] for d in results], ["Tenant A text."])

    def test_group_match_does_not_cross_tenants(self):
        engine = SearchEngine()
        engine.ingest(make_doc(tenant_id="tenant-b", allowed_groups=["finance"]))
        self.assertEqual(visible(engine, "tenant-a", groups=["finance"]), {})


class PermissionTests(unittest.TestCase):
    def setUp(self):
        self.engine = SearchEngine()
        self.engine.ingest(make_doc(doc_id="public", allowed_groups=[]))
        self.engine.ingest(make_doc(doc_id="finance-only", allowed_groups=["finance"]))
        self.engine.ingest(make_doc(doc_id="finance-or-hr", allowed_groups=["finance", "hr"]))

    def test_user_without_groups_sees_only_public(self):
        self.assertEqual(visible(self.engine, groups=[]), {"public": 1})

    def test_one_shared_group_is_enough(self):
        self.assertEqual(
            visible(self.engine, groups=["hr"]), {"public": 1, "finance-or-hr": 1}
        )

    def test_matching_group_sees_restricted_and_public(self):
        self.assertEqual(
            visible(self.engine, groups=["finance"]),
            {"public": 1, "finance-only": 1, "finance-or-hr": 1},
        )

    def test_unrelated_group_sees_only_public(self):
        self.assertEqual(visible(self.engine, groups=["admin"]), {"public": 1})


class AsOfTests(unittest.TestCase):
    def setUp(self):
        self.engine = SearchEngine()
        self.engine.ingest(make_doc(revision=1, received_at="2026-01-01T00:00:00Z",
                                    text="Refunds within 30 days."))
        self.engine.ingest(make_doc(revision=2, received_at="2026-01-05T00:00:00Z",
                                    text="Refunds within 14 days."))

    def test_nothing_visible_before_first_revision(self):
        self.assertEqual(visible(self.engine, as_of="2025-12-31T00:00:00Z"), {})

    def test_first_revision_visible_before_update(self):
        self.assertEqual(visible(self.engine, as_of="2026-01-03T00:00:00Z"), {"doc-1": 1})

    def test_update_visible_after_receipt(self):
        self.assertEqual(visible(self.engine, as_of="2026-01-06T00:00:00Z"), {"doc-1": 2})

    def test_revision_visible_at_exact_receipt_instant(self):
        # Decision: received_at <= as_of, so a revision is visible at its receipt instant.
        self.assertEqual(visible(self.engine, as_of="2026-01-05T00:00:00Z"), {"doc-1": 2})

    def test_equivalent_offset_is_the_same_instant(self):
        self.assertEqual(visible(self.engine, as_of="2026-01-05T05:30:00+05:30"), {"doc-1": 2})

    def test_out_of_order_arrival_uses_received_at(self):
        engine = SearchEngine()
        engine.ingest(make_doc(revision=2, received_at="2026-01-05T00:00:00Z"))
        engine.ingest(make_doc(revision=1, received_at="2026-01-01T00:00:00Z"))
        self.assertEqual(visible(engine, as_of="2026-01-03T00:00:00Z"), {"doc-1": 1})
        self.assertEqual(visible(engine, as_of="2026-01-06T00:00:00Z"), {"doc-1": 2})

    def test_older_revision_received_later_does_not_supersede_a_newer_one(self):
        # rev 2 is received first (Jan 2), rev 1 arrives late (Jan 5)
        for order in ("rev 2 first", "rev 1 first"):
            with self.subTest(ingest_order=order):
                rev1 = make_doc(revision=1, received_at="2026-01-05T00:00:00Z", text="Thirty days.")
                rev2 = make_doc(revision=2, received_at="2026-01-02T00:00:00Z", text="Fourteen days.")
                engine = SearchEngine()
                for doc in ((rev2, rev1) if order == "rev 2 first" else (rev1, rev2)):
                    engine.ingest(doc)
                self.assertEqual(visible(engine, as_of="2026-01-01T00:00:00Z"), {})
                self.assertEqual(visible(engine, as_of="2026-01-03T00:00:00Z"), {"doc-1": 2})
                # both received: the higher revision wins, not the later receipt
                self.assertEqual(visible(engine, as_of="2026-01-10T00:00:00Z"), {"doc-1": 2})

    def test_older_revision_received_later_cannot_undo_a_deletion(self):
        engine = SearchEngine()
        engine.ingest(make_doc(revision=2, received_at="2026-01-02T00:00:00Z",
                               deleted=True, text="This policy has been withdrawn."))
        engine.ingest(make_doc(revision=1, received_at="2026-01-05T00:00:00Z"))
        self.assertEqual(visible(engine, as_of="2026-01-10T00:00:00Z"), {})

    def test_same_received_at_is_broken_by_revision_number_in_any_ingest_order(self):
        for order in ("rev 1 first", "rev 2 first"):
            with self.subTest(ingest_order=order):
                rev1 = make_doc(revision=1, received_at="2026-01-05T00:00:00Z", text="Thirty days.")
                rev2 = make_doc(revision=2, received_at="2026-01-05T00:00:00Z", text="Fourteen days.")
                engine = SearchEngine()
                for doc in ((rev1, rev2) if order == "rev 1 first" else (rev2, rev1)):
                    engine.ingest(doc)
                self.assertEqual(visible(engine, as_of="2026-01-10T00:00:00Z"), {"doc-1": 2})

    def test_at_most_one_revision_per_document(self):
        results = self.engine._visible_documents(
            RequestRecord.model_validate(
                {"query_id": "q", "tenant_id": "tenant-a", "groups": [],
                 "question": "x", "as_of": "2026-01-10T00:00:00Z"}
            ).model_dump()
        )
        self.assertEqual(len(results), 1)

    def test_as_of_accepts_aware_datetime_object(self):
        validated = RequestRecord.model_validate(
            {"query_id": "q", "tenant_id": "tenant-a", "groups": [], "question": "x",
             "as_of": datetime.fromisoformat("2026-01-03T00:00:00+00:00")}
        ).model_dump()
        self.assertEqual([d["revision"] for d in self.engine._visible_documents(validated)], [1])


class DeletionTests(unittest.TestCase):
    def setUp(self):
        self.engine = SearchEngine()
        self.engine.ingest(make_doc(revision=1, received_at="2026-01-01T00:00:00Z"))
        self.engine.ingest(make_doc(revision=2, received_at="2026-01-05T00:00:00Z",
                                    deleted=True, text="This policy has been withdrawn."))

    def test_visible_before_deletion(self):
        self.assertEqual(visible(self.engine, as_of="2026-01-03T00:00:00Z"), {"doc-1": 1})

    def test_deleted_document_and_old_text_are_gone_after_deletion(self):
        self.assertEqual(visible(self.engine, as_of="2026-01-06T00:00:00Z"), {})

    def test_deleted_at_exact_deletion_instant(self):
        self.assertEqual(visible(self.engine, as_of="2026-01-05T00:00:00Z"), {})

    def test_deletion_arriving_before_original_still_wins(self):
        engine = SearchEngine()
        engine.ingest(make_doc(revision=2, received_at="2026-01-05T00:00:00Z",
                               deleted=True, text="withdrawn"))
        engine.ingest(make_doc(revision=1, received_at="2026-01-01T00:00:00Z"))
        self.assertEqual(visible(engine, as_of="2026-01-06T00:00:00Z"), {})
        self.assertEqual(visible(engine, as_of="2026-01-03T00:00:00Z"), {"doc-1": 1})

    def test_new_revision_after_deletion_restores_document(self):
        self.engine.ingest(make_doc(revision=3, received_at="2026-01-08T00:00:00Z",
                                    text="Refunds within 60 days."))
        self.assertEqual(visible(self.engine, as_of="2026-01-09T00:00:00Z"), {"doc-1": 3})


class PermissionRevocationTests(unittest.TestCase):
    def setUp(self):
        self.engine = SearchEngine()
        self.engine.ingest(make_doc(revision=1, received_at="2026-01-01T00:00:00Z",
                                    allowed_groups=["finance"]))
        self.engine.ingest(make_doc(revision=2, received_at="2026-01-05T00:00:00Z",
                                    allowed_groups=["admin"]))

    def test_access_before_revocation(self):
        self.assertEqual(
            visible(self.engine, groups=["finance"], as_of="2026-01-03T00:00:00Z"),
            {"doc-1": 1},
        )

    def test_no_fallback_to_older_revision_after_revocation(self):
        self.assertEqual(
            visible(self.engine, groups=["finance"], as_of="2026-01-06T00:00:00Z"), {}
        )

    def test_new_group_holder_gains_access_after_change(self):
        self.assertEqual(
            visible(self.engine, groups=["admin"], as_of="2026-01-06T00:00:00Z"),
            {"doc-1": 2},
        )

    def test_user_losing_a_group_between_requests(self):
        before = visible(self.engine, groups=["finance"], as_of="2026-01-03T00:00:00Z")
        after = visible(self.engine, groups=[], as_of="2026-01-03T00:00:00Z")
        self.assertEqual(before, {"doc-1": 1})
        self.assertEqual(after, {})


class SampleDataInvariantTests(unittest.TestCase):
    """Run every sample query through stage 2 and check invariants hold for all of them."""

    @classmethod
    def setUpClass(cls):
        cls.records = [json.loads(l) for l in DATA.read_text().splitlines() if l.strip()]
        cls.engine = SearchEngine()
        for record in cls.records:
            cls.engine.ingest(record)
        queries = DATA.with_name("queries.jsonl").read_text().splitlines()
        cls.queries = [json.loads(l) for l in queries if l.strip()]

    def _visible_for(self, query):
        request = {k: query[k] for k in ("query_id", "tenant_id", "groups", "question", "as_of")}
        return self.engine._visible_documents(RequestRecord.model_validate(request).model_dump())

    def test_invariants_hold_for_every_sample_query(self):
        for query in self.queries:
            with self.subTest(query_id=query["query_id"]):
                results = self._visible_for(query)
                as_of = datetime.fromisoformat(query["as_of"])
                doc_ids = [d["doc_id"] for d in results]
                self.assertEqual(len(doc_ids), len(set(doc_ids)), "duplicate document")
                for d in results:
                    self.assertEqual(d["tenant_id"], query["tenant_id"], "cross-tenant leak")
                    self.assertFalse(d["deleted"], "tombstone returned")
                    self.assertLessEqual(d["received_at"], as_of, "future revision returned")
                    self.assertTrue(
                        not d["allowed_groups"] or set(d["allowed_groups"]) & set(query["groups"]),
                        "permission leak",
                    )


if __name__ == "__main__":
    unittest.main()
