"""Starter checks only. Add behavior tests for your implementation."""
import inspect
import json
import unittest
from secure_search.contracts import Result
from secure_search.solution import SearchEngine

class PublicContractTests(unittest.TestCase):
    def test_canonical_result(self):
        a=Result("x", "2026-01-01T00:00:00Z", {"z":1,"a":2})
        b=Result("x", "2026-01-01T00:00:00Z", {"a":2,"z":1})
        self.assertEqual(a.to_wire(), b.to_wire())
        self.assertEqual(json.loads(a.to_wire())["entity_id"], "x")

    def test_nonfinite_output_is_rejected(self):
        with self.assertRaises(ValueError):
            Result("x", "2026-01-01T00:00:00Z", {"bad":float("nan")}).to_wire()

    def test_restore_remains_a_classmethod(self):
        self.assertIsInstance(inspect.getattr_static(SearchEngine,"restore"),classmethod)
