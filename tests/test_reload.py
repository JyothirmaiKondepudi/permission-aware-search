"""reload_index tests: atomic replacement, and a failed reload keeps the last valid index."""
import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from secure_search.solution import SearchEngine

SAMPLE = Path(__file__).resolve().parents[1] / "data" / "documents.jsonl"


def make_doc(doc_id, text, **overrides):
    doc = {"tenant_id": "tenant-a", "doc_id": doc_id, "revision": 1,
           "received_at": "2026-01-01T00:00:00Z", "title": f"{doc_id} policy", "text": text,
           "allowed_groups": [], "deleted": False}
    doc.update(overrides)
    return doc


def request(question):
    return {"query_id": "q", "tenant_id": "tenant-a", "groups": [], "question": question,
            "as_of": "2026-01-10T00:00:00Z"}


class ReloadTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.engine = SearchEngine()
        self.engine.ingest(make_doc("refund", "Refund requests must be submitted within 30 days."))
        self.engine.ingest(make_doc("sso", "SSO setup requires a verified domain."))

    def write(self, name, lines):
        path = self.dir / name
        path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
        return path

    def jsonl(self, name, docs):
        return self.write(name, [json.dumps(d) for d in docs])

    def snapshot_state(self):
        # search first, so the cache entry it adds is already counted in stats
        wire = self.engine.search(request("refund policy")).to_wire()
        return (self.engine.stats(), wire)


class SuccessfulReloadTests(ReloadTestCase):
    def test_new_documents_replace_the_old_ones(self):
        path = self.jsonl("new.jsonl", [
            make_doc("export", "Exports include the last 90 days."),
            make_doc("retention", "Audit logs are retained for 180 days."),
        ])
        self.assertTrue(self.engine.reload_index(path))
        self.assertEqual(self.engine.stats()["indexed_documents"], 2)
        self.assertEqual(self.engine.search(request("export policy")).payload["citations"][0]["doc_id"],
                         "export")
        # the old refund document is gone, so nothing about refunds can be answered
        self.assertTrue(self.engine.search(request("refund policy")).payload["abstained"])

    def test_reload_bumps_version_and_clears_cache(self):
        self.engine.search(request("refund policy"))
        before = self.engine.stats()["index_version"]
        self.engine.reload_index(self.jsonl("new.jsonl", [make_doc("export", "Exports use CSV.")]))
        self.assertNotEqual(self.engine.stats()["index_version"], before)
        self.assertEqual(self.engine.stats()["cache_entries"], 0)

    def test_reloading_identical_content_changes_nothing(self):
        path = self.jsonl("same.jsonl", [
            make_doc("refund", "Refund requests must be submitted within 30 days."),
            make_doc("sso", "SSO setup requires a verified domain."),
        ])
        self.engine.search(request("refund policy"))
        before = self.engine.stats()
        self.assertFalse(self.engine.reload_index(path))
        self.assertEqual(self.engine.stats(), before)

    def test_blank_lines_and_exact_duplicates_are_accepted(self):
        doc = json.dumps(make_doc("export", "Exports use CSV."))
        self.assertTrue(self.engine.reload_index(self.write("dup.jsonl", [doc, "", "   ", doc])))
        self.assertEqual(self.engine.stats()["indexed_revisions"], 1)

    def test_accepts_a_string_path(self):
        path = self.jsonl("new.jsonl", [make_doc("export", "Exports use CSV.")])
        self.assertTrue(self.engine.reload_index(str(path)))

    def test_sample_file_reloads(self):
        self.assertTrue(self.engine.reload_index(SAMPLE))
        self.assertEqual(self.engine.stats()["indexed_documents"], 240)
        self.assertEqual(self.engine.stats()["indexed_revisions"], 293)


class FailedReloadTests(ReloadTestCase):
    """Every failure raises ValueError and leaves the previous index, version and cache intact."""

    def assertFailsAndKeepsIndex(self, source, message_part):
        before = self.snapshot_state()
        with self.assertRaises(ValueError) as ctx:
            self.engine.reload_index(source)
        self.assertIn(message_part, str(ctx.exception))
        self.assertEqual(self.snapshot_state(), before)

    def test_invalid_json_line(self):
        good = json.dumps(make_doc("export", "Exports use CSV."))
        self.assertFailsAndKeepsIndex(self.write("bad.jsonl", [good, '{"tenant_id": "t1", ']),
                                      "line 2: invalid JSON")

    def test_line_that_is_not_an_object(self):
        self.assertFailsAndKeepsIndex(self.write("bad.jsonl", ["[1, 2, 3]"]), "line 1: expected a JSON object")

    def test_invalid_document_in_the_middle(self):
        docs = [make_doc("a", "Text a."), make_doc("b", "Text b.", revision=0), make_doc("c", "Text c.")]
        self.assertFailsAndKeepsIndex(self.jsonl("bad.jsonl", docs), "line 2")

    def test_timestamp_without_offset(self):
        docs = [make_doc("a", "Text a.", received_at="2026-01-01T00:00:00")]
        self.assertFailsAndKeepsIndex(self.jsonl("bad.jsonl", docs), "explicit UTC offset")

    def test_revision_conflict_inside_the_file(self):
        docs = [make_doc("a", "Original text."), make_doc("a", "Different text.")]
        self.assertFailsAndKeepsIndex(self.jsonl("bad.jsonl", docs), "revision conflict")

    def test_empty_file(self):
        self.assertFailsAndKeepsIndex(self.write("empty.jsonl", ["", "  "]), "contains no documents")

    def test_missing_file(self):
        self.assertFailsAndKeepsIndex(self.dir / "does-not-exist.jsonl", "cannot read")

    def test_not_a_path(self):
        for bad in (None, 42, ["a.jsonl"]):
            with self.subTest(bad=bad):
                self.assertFailsAndKeepsIndex(bad, "expected a path")

    def test_not_utf8(self):
        path = self.dir / "latin1.jsonl"
        path.write_bytes(b'{"title": "caf\xe9"}\n')
        self.assertFailsAndKeepsIndex(path, "cannot read")


class ConcurrentReloadTests(ReloadTestCase):
    def test_searches_see_either_the_old_or_the_new_index(self):
        path = self.jsonl("new.jsonl", [make_doc("export", "Exports use CSV.")])
        seen, errors = set(), []

        def searcher():
            try:
                for _ in range(200):
                    stats = self.engine.stats()
                    seen.add(stats["indexed_documents"])
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=searcher) for _ in range(3)]
        for thread in threads:
            thread.start()
        self.engine.reload_index(path)
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertTrue(seen <= {1, 2})


if __name__ == "__main__":
    unittest.main()
