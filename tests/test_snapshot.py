"""snapshot/restore tests: deterministic bytes, identical behaviour, strict restore."""
import json
import random
import shutil
import tempfile
import unittest
from pathlib import Path

from secure_search.solution import SearchEngine

SAMPLE = Path(__file__).resolve().parents[1] / "data" / "documents.jsonl"
QUERIES = Path(__file__).resolve().parents[1] / "data" / "queries.jsonl"


def records():
    return [json.loads(line) for line in SAMPLE.read_text().splitlines() if line.strip()]


def engine_from(docs, **kwargs):
    engine = SearchEngine(**kwargs)
    for doc in docs:
        engine.ingest(doc)
    return engine


class SnapshotTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)


class DeterminismTests(SnapshotTestCase):
    def test_same_state_gives_identical_bytes(self):
        engine = engine_from(records())
        engine.snapshot(self.dir / "a.json")
        engine.snapshot(self.dir / "b.json")
        self.assertEqual((self.dir / "a.json").read_bytes(), (self.dir / "b.json").read_bytes())

    def test_ingest_order_does_not_change_the_bytes(self):
        docs = records()
        shuffled = docs[:]
        random.Random(7).shuffle(shuffled)
        engine_from(docs).snapshot(self.dir / "a.json")
        engine_from(shuffled).snapshot(self.dir / "b.json")
        self.assertEqual((self.dir / "a.json").read_bytes(), (self.dir / "b.json").read_bytes())

    def test_cache_and_searches_do_not_change_the_bytes(self):
        engine = engine_from(records())
        engine.snapshot(self.dir / "before.json")
        engine.search({"query_id": "q", "tenant_id": "tenant-0", "groups": [],
                       "question": "refund policy", "as_of": "2026-01-14T00:00:00Z"})
        engine.snapshot(self.dir / "after.json")
        self.assertEqual((self.dir / "before.json").read_bytes(), (self.dir / "after.json").read_bytes())

    def test_snapshot_contents(self):
        engine = engine_from(records(), max_entities=50)
        engine.snapshot(self.dir / "s.json")
        state = json.loads((self.dir / "s.json").read_text())
        self.assertEqual(set(state), {"format", "index_version", "max_entities", "documents"})
        self.assertEqual(state["max_entities"], 50)
        self.assertEqual(len(state["documents"]), 293)
        self.assertTrue(all(d["received_at"].endswith("Z") for d in state["documents"]))


class RestoreTests(SnapshotTestCase):
    def test_restored_engine_answers_every_query_identically(self):
        original = engine_from(records())
        original.snapshot(self.dir / "s.json")
        restored = SearchEngine.restore(self.dir / "s.json")
        self.assertEqual(restored.stats(), original.stats() | {"cache_entries": 0})
        for line in QUERIES.read_text().splitlines():
            query = json.loads(line)
            request = {k: query[k] for k in ("query_id", "tenant_id", "groups", "question", "as_of")}
            with self.subTest(query_id=query["query_id"]):
                self.assertEqual(original.search(request).to_wire(), restored.search(request).to_wire())

    def test_restore_is_a_classmethod_returning_a_new_engine(self):
        original = engine_from(records()[:5])
        original.snapshot(self.dir / "s.json")
        restored = SearchEngine.restore(self.dir / "s.json")
        self.assertIsInstance(restored, SearchEngine)
        self.assertIsNot(restored, original)

    def test_restored_engine_keeps_working(self):
        engine = engine_from(records()[:5])
        engine.snapshot(self.dir / "s.json")
        restored = SearchEngine.restore(self.dir / "s.json")
        before = restored.stats()["index_version"]
        self.assertTrue(restored.ingest(records()[5]))
        self.assertNotEqual(restored.stats()["index_version"], before)

    def test_round_trip_of_a_restored_engine_is_byte_identical(self):
        engine_from(records()).snapshot(self.dir / "a.json")
        SearchEngine.restore(self.dir / "a.json").snapshot(self.dir / "b.json")
        self.assertEqual((self.dir / "a.json").read_bytes(), (self.dir / "b.json").read_bytes())

    def test_empty_engine_round_trips(self):
        SearchEngine().snapshot(self.dir / "empty.json")
        self.assertEqual(SearchEngine.restore(self.dir / "empty.json").stats()["indexed_documents"], 0)


class StrictRestoreTests(SnapshotTestCase):
    def good_state(self):
        engine_from(records()[:3]).snapshot(self.dir / "good.json")
        return json.loads((self.dir / "good.json").read_text())

    def assertRejected(self, content, message_part):
        path = self.dir / "bad.json"
        path.write_text(content if isinstance(content, str) else json.dumps(content))
        with self.assertRaises(ValueError) as ctx:
            SearchEngine.restore(path)
        self.assertIn(message_part, str(ctx.exception))

    def test_not_json(self):
        self.assertRejected("{not json", "not valid JSON")

    def test_wrong_format(self):
        self.assertRejected(dict(self.good_state(), format=99), "unsupported format")

    def test_invalid_document(self):
        state = self.good_state()
        state["documents"][1]["revision"] = 0
        self.assertRejected(state, "document 2")

    def test_conflicting_documents(self):
        state = self.good_state()
        state["documents"].append(dict(state["documents"][0], text="tampered text"))
        self.assertRejected(state, "revision conflict")

    def test_duplicate_documents(self):
        state = self.good_state()
        state["documents"].append(state["documents"][0])
        self.assertRejected(state, "duplicate")

    def test_invalid_index_version_or_max_entities(self):
        for field, value, message in [("index_version", -1, "index_version"),
                                      ("index_version", True, "index_version"),
                                      ("index_version", 1, "lower than"),
                                      ("max_entities", 0, "max_entities")]:
            with self.subTest(field=field, value=value):
                self.assertRejected(dict(self.good_state(), **{field: value}), message)

    def test_missing_file_and_bad_argument(self):
        with self.assertRaises(ValueError):
            SearchEngine.restore(self.dir / "missing.json")
        with self.assertRaises(ValueError):
            SearchEngine.restore(None)


class SnapshotArgumentTests(SnapshotTestCase):
    def test_bad_destination(self):
        engine = SearchEngine()
        with self.assertRaises(ValueError):
            engine.snapshot(None)
        with self.assertRaises(ValueError):
            engine.snapshot(self.dir / "no-such-folder" / "s.json")

    def test_no_temporary_file_left_behind(self):
        engine_from(records()[:3]).snapshot(self.dir / "s.json")
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["s.json"])


if __name__ == "__main__":
    unittest.main()
