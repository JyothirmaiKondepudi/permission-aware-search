"""Stage 3 tests: tokenization and BM25 ranking over visible documents.

Ranking is tested through _rank and _visible_documents rather than search(),
because search() will return a Result once stage 4 is built.
"""
import unittest

from secure_search.models.request import RequestRecord
from secure_search.solution import SearchEngine

TEXTS = {
    "refund": ("refund policy", "Refund requests must be submitted within 30 days."),
    "sso": ("sso policy", "SSO setup requires a verified domain and an administrator."),
    "export": ("export policy", "Exports include the last 90 days and use CSV format."),
    "retention": ("retention policy", "Audit logs are retained for 180 days."),
}


def make_doc(doc_id, topic, **overrides):
    title, text = TEXTS[topic]
    doc = {
        "tenant_id": "tenant-a",
        "doc_id": doc_id,
        "revision": 1,
        "received_at": "2026-01-01T00:00:00Z",
        "title": title,
        "text": text,
        "allowed_groups": [],
        "deleted": False,
    }
    doc.update(overrides)
    return doc


def ranked(engine, question, k=3, tenant_id="tenant-a", groups=()):
    """Run stages 2 and 3 and return [(doc_id, score), ...]."""
    query = RequestRecord.model_validate(
        {"query_id": "q", "tenant_id": tenant_id, "groups": list(groups),
         "question": question, "as_of": "2026-01-10T00:00:00Z"}
    ).model_dump()
    visible = engine._visible_documents(query)
    if not visible:
        return []
    top = engine._rank(engine.tokenization(question), visible, k)
    return [(doc["doc_id"], score) for score, doc in top]


def engine_with(*docs):
    engine = SearchEngine()
    for doc in docs:
        engine.ingest(doc)
    return engine


class TokenizationTests(unittest.TestCase):
    def setUp(self):
        self.tokenize = SearchEngine().tokenization

    def test_lowercases_and_drops_punctuation(self):
        self.assertEqual(self.tokenize("Refund Policy?!"), ["refund", "policy"])

    def test_drops_filler_words(self):
        self.assertEqual(self.tokenize("What is the refund policy?"), ["refund", "policy"])

    def test_keeps_numbers(self):
        self.assertIn("30", self.tokenize("within 30 days."))

    def test_keeps_accented_letters(self):
        self.assertEqual(self.tokenize("Política de reembolso"), ["política", "de", "reembolso"])

    def test_handles_non_ascii_punctuation(self):
        self.assertEqual(self.tokenize("The “refund” policy — 30 days…"),
                         ["refund", "policy", "30", "days"])

    def test_only_filler_or_empty_gives_no_tokens(self):
        self.assertEqual(self.tokenize("What is it?"), [])
        self.assertEqual(self.tokenize(""), [])

    def test_document_tokens_separate_title_and_text(self):
        engine = SearchEngine()
        tokens = engine._document_tokens({"title": "export policy", "text": "CSV format"})
        self.assertEqual(tokens, ["export", "policy", "csv", "format"])
        tokens = engine._document_tokens({"title": "policy", "text": "format"})
        self.assertNotIn("policyformat", tokens)


class RankingTests(unittest.TestCase):
    def setUp(self):
        self.engine = engine_with(
            make_doc("d-refund", "refund"),
            make_doc("d-sso", "sso"),
            make_doc("d-export", "export"),
            make_doc("d-retention", "retention"),
        )

    def test_best_match_is_first(self):
        self.assertEqual(ranked(self.engine, "What is the refund policy?")[0][0], "d-refund")

    def test_title_only_match_still_ranks_first(self):
        # the texts say "Exports" and "retained"; only the titles say "export" and "retention"
        for question, expected in [("What is the export policy?", "d-export"),
                                   ("Explain the rules for retention.", "d-retention")]:
            with self.subTest(question=question):
                results = ranked(self.engine, question, k=4)
                self.assertEqual(results[0][0], expected)
                # strictly ahead, so a doc_id tie-break on equal scores cannot pass this by luck
                self.assertGreater(results[0][1], results[1][1])

    def test_best_score_is_strictly_higher_than_the_rest(self):
        results = ranked(self.engine, "What is the refund policy?", k=4)
        self.assertGreater(results[0][1], max(score for _, score in results[1:]))

    def test_k_limits_the_number_of_results(self):
        self.assertEqual(len(ranked(self.engine, "refund policy", k=1)), 1)
        self.assertEqual(len(ranked(self.engine, "refund policy", k=2)), 2)

    def test_k_larger_than_visible_returns_all(self):
        self.assertEqual(len(ranked(self.engine, "refund policy", k=10)), 4)

    def test_scores_are_plain_floats_sorted_best_first(self):
        results = ranked(self.engine, "refund policy", k=4)
        scores = [score for _, score in results]
        self.assertTrue(all(type(score) is float for score in scores))
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_no_matching_words_gives_zero_scores(self):
        results = ranked(self.engine, "What is the office lunar evacuation procedure?", k=4)
        self.assertEqual([score for _, score in results], [0.0, 0.0, 0.0, 0.0])

    def test_query_of_only_filler_words_gives_zero_scores(self):
        results = ranked(self.engine, "What is it?", k=4)
        self.assertEqual([score for _, score in results], [0.0, 0.0, 0.0, 0.0])


class SmallCandidateSetTests(unittest.TestCase):
    """BM25Okapi gave negative or tied scores here; these guard against that regression."""

    def test_single_visible_document_scores_positive(self):
        engine = engine_with(make_doc("d-refund", "refund"))
        results = ranked(engine, "What is the refund policy?")
        self.assertEqual(results[0][0], "d-refund")
        self.assertGreater(results[0][1], 0)

    def test_two_visible_documents_rank_the_match_first(self):
        engine = engine_with(make_doc("d-refund", "refund"), make_doc("d-sso", "sso"))
        results = ranked(engine, "What is the refund policy?")
        self.assertEqual(results[0][0], "d-refund")
        self.assertGreater(results[0][1], results[1][1])
        self.assertTrue(all(score >= 0 for _, score in results))


class TieTests(unittest.TestCase):
    def test_tied_documents_are_all_kept_and_ordered_by_doc_id(self):
        engine = engine_with(
            make_doc("d-3", "refund"),
            make_doc("d-1", "refund"),
            make_doc("d-2", "refund"),
            make_doc("d-sso", "sso"),
        )
        results = ranked(engine, "What is the refund policy?", k=3)
        self.assertEqual([doc_id for doc_id, _ in results], ["d-1", "d-2", "d-3"])
        self.assertEqual(len({score for _, score in results}), 1)

    def test_tie_order_does_not_depend_on_ingest_order(self):
        docs = [make_doc("d-b", "refund"), make_doc("d-a", "refund"), make_doc("d-c", "refund")]
        forward = ranked(engine_with(*docs), "refund policy", k=3)
        backward = ranked(engine_with(*reversed(docs)), "refund policy", k=3)
        self.assertEqual(forward, backward)


class IsolationTests(unittest.TestCase):
    """Scores come only from visible documents, so hidden documents cannot influence them."""

    def test_other_tenant_documents_do_not_change_scores(self):
        own = [make_doc("d-refund", "refund"), make_doc("d-sso", "sso")]
        alone = ranked(engine_with(*own), "What is the refund policy?")
        other = [make_doc(f"b-{i}", "refund", tenant_id="tenant-b") for i in range(5)]
        crowded = ranked(engine_with(*own, *other), "What is the refund policy?")
        self.assertEqual(alone, crowded)

    def test_forbidden_documents_do_not_change_scores(self):
        own = [make_doc("d-refund", "refund"), make_doc("d-sso", "sso")]
        alone = ranked(engine_with(*own), "What is the refund policy?")
        hidden = [make_doc(f"f-{i}", "refund", allowed_groups=["finance"]) for i in range(5)]
        with_hidden = ranked(engine_with(*own, *hidden), "What is the refund policy?", groups=[])
        self.assertEqual(alone, with_hidden)

    def test_forbidden_document_is_never_ranked(self):
        engine = engine_with(
            make_doc("d-public", "sso"),
            make_doc("d-secret", "refund", allowed_groups=["finance"]),
        )
        results = ranked(engine, "What is the refund policy?", k=5, groups=[])
        self.assertNotIn("d-secret", [doc_id for doc_id, _ in results])

    def test_nothing_visible_ranks_nothing(self):
        engine = engine_with(make_doc("d-refund", "refund", tenant_id="tenant-b"))
        self.assertEqual(ranked(engine, "refund policy"), [])
        # search() must not crash building BM25 on an empty corpus
        engine.search({"query_id": "q", "tenant_id": "tenant-a", "groups": [],
                       "question": "refund policy", "as_of": "2026-01-10T00:00:00Z"})


if __name__ == "__main__":
    unittest.main()
