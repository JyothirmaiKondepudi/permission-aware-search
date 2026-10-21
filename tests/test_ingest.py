"""Behaviour tests for SearchEngine.ingest: validation, revisions, duplicates, conflicts."""
import json
import unittest
from pathlib import Path
from types import MappingProxyType

from secure_search.solution import SearchEngine

DATA = Path(__file__).resolve().parents[1] / "data" / "documents.jsonl"


def make_doc(**overrides):
    doc = {
        "tenant_id": "tenant-a",
        "doc_id": "doc-1",
        "revision": 1,
        "received_at": "2026-01-01T03:00:00Z",
        "title": "refund policy",
        "text": "Refund requests must be submitted within 30 days.",
        "allowed_groups": ["finance"],
        "deleted": False,
    }
    doc.update(overrides)
    return doc


def stored_revisions(engine, tenant_id="tenant-a", doc_id="doc-1"):
    # Reads internal state; update this helper if the store shape changes.
    return engine._store.get((doc_id, tenant_id), {})


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.engine = SearchEngine()

    def assertRejected(self, document, message_part):
        with self.assertRaises(ValueError) as ctx:
            self.engine.ingest(document)
        self.assertIn(message_part, str(ctx.exception))

    def test_non_mapping_is_rejected(self):
        for bad in ("hello", None, ["tenant_id"], 42):
            with self.subTest(bad=bad):
                self.assertRejected(bad, "expected a mapping")

    def test_read_only_mapping_is_accepted(self):
        self.assertTrue(self.engine.ingest(MappingProxyType(make_doc())))

    def test_missing_field_is_rejected(self):
        for field in make_doc():
            with self.subTest(field=field):
                doc = make_doc()
                del doc[field]
                self.assertRejected(doc, field)

    def test_wrong_types_are_rejected(self):
        cases = {
            "tenant_id": [None, 42, "", "   "],
            "doc_id": [None, ""],
            "title": [None, ""],
            "text": [None, ""],
            "revision": [True, False, "2", 2.0, None],
            "deleted": ["no", 0, None],
            "allowed_groups": ["finance", None, [1], ("finance",)],
        }
        for field, values in cases.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    self.assertRejected(make_doc(**{field: value}), field)

    def test_revision_must_be_at_least_one(self):
        for value in (0, -5):
            with self.subTest(value=value):
                self.assertRejected(make_doc(revision=value), "greater_than_equal")

    def test_timestamp_without_offset_is_rejected(self):
        for value in ("2026-01-05T10:00:00", "2026-01-05"):
            with self.subTest(value=value):
                self.assertRejected(make_doc(received_at=value), "explicit UTC offset")

    def test_unparseable_timestamp_is_rejected(self):
        for value in ("banana", 123, 0, ""):
            with self.subTest(value=value):
                self.assertRejected(make_doc(received_at=value), "received_at")

    def test_timestamps_with_offsets_are_accepted(self):
        for i, value in enumerate(("2026-01-05T10:00:00Z", "2026-01-05T15:30:00+05:30")):
            with self.subTest(value=value):
                self.assertTrue(self.engine.ingest(make_doc(doc_id=f"d{i}", received_at=value)))

    def test_rejected_document_is_not_stored(self):
        with self.assertRaises(ValueError):
            self.engine.ingest(make_doc(revision=0))
        self.assertEqual(stored_revisions(self.engine), {})


class RevisionTests(unittest.TestCase):
    def setUp(self):
        self.engine = SearchEngine()
        self.rev1 = make_doc()
        self.rev2 = make_doc(revision=2, received_at="2026-01-02T00:00:00Z")

    def test_new_revision_keeps_previous_revisions(self):
        self.assertTrue(self.engine.ingest(self.rev1))
        self.assertTrue(self.engine.ingest(self.rev2))
        self.assertEqual(sorted(stored_revisions(self.engine)), [1, 2])

    def test_out_of_order_arrival_keeps_both_revisions(self):
        self.assertTrue(self.engine.ingest(self.rev2))
        self.assertTrue(self.engine.ingest(self.rev1))
        self.assertEqual(sorted(stored_revisions(self.engine)), [1, 2])

    def test_same_doc_id_in_different_tenants_is_separate(self):
        self.engine.ingest(make_doc(tenant_id="tenant-a"))
        self.engine.ingest(make_doc(tenant_id="tenant-b", text="Different tenant text."))
        self.assertEqual(list(stored_revisions(self.engine, "tenant-a")), [1])
        self.assertEqual(list(stored_revisions(self.engine, "tenant-b")), [1])

    def test_exact_duplicate_is_a_no_op(self):
        self.assertTrue(self.engine.ingest(self.rev1))
        self.assertFalse(self.engine.ingest(dict(self.rev1)))
        self.assertEqual(list(stored_revisions(self.engine)), [1])

    def test_same_instant_with_different_offset_is_a_duplicate(self):
        self.engine.ingest(self.rev1)
        same_instant = make_doc(received_at="2026-01-01T08:30:00+05:30")
        self.assertFalse(self.engine.ingest(same_instant))

    def test_same_revision_with_different_content_is_a_conflict(self):
        changes = {
            "text": "Refunds within 14 days.",
            "title": "refund rules",
            "allowed_groups": ["admin"],
            "deleted": True,
            "received_at": "2026-01-09T00:00:00Z",
        }
        for field, value in changes.items():
            with self.subTest(field=field):
                engine = SearchEngine()
                engine.ingest(make_doc())
                with self.assertRaises(ValueError) as ctx:
                    engine.ingest(make_doc(**{field: value}))
                self.assertIn("revision conflict", str(ctx.exception))
                self.assertEqual(stored_revisions(engine)[1]["text"], make_doc()["text"])
                self.assertEqual(stored_revisions(engine)[1]["allowed_groups"], ["finance"])


class InputIsolationTests(unittest.TestCase):
    def test_input_is_not_mutated(self):
        doc = make_doc()
        before = json.dumps(doc, sort_keys=True)
        SearchEngine().ingest(doc)
        self.assertEqual(json.dumps(doc, sort_keys=True), before)

    def test_caller_changes_after_ingest_do_not_reach_store(self):
        engine = SearchEngine()
        doc = make_doc()
        engine.ingest(doc)
        doc["text"] = "changed by caller"
        doc["allowed_groups"].append("everyone")
        stored = stored_revisions(engine)[1]
        self.assertEqual(stored["text"], make_doc()["text"])
        self.assertEqual(stored["allowed_groups"], ["finance"])


class SampleDataTests(unittest.TestCase):
    def test_every_sample_record_is_ingested(self):
        records = [json.loads(line) for line in DATA.read_text().splitlines() if line.strip()]
        engine = SearchEngine()
        for record in records:
            engine.ingest(record)
        expected = {(r["doc_id"], r["tenant_id"], r["revision"]) for r in records}
        stored = {
            (doc_id, tenant_id, rev)
            for (doc_id, tenant_id), revisions in engine._store.items()
            for rev in revisions
        }
        self.assertEqual(stored, expected)


if __name__ == "__main__":
    unittest.main()
