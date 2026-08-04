from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy.dialects import postgresql

from app.services import tm_lookup


class _FakeSession:
    def __init__(self, entries, exact=None):
        self.entries = entries
        self.exact = exact
        self.statements = []
        self.closed = False

    def scalar(self, statement):
        self.statements.append(statement)
        return self.exact

    def scalars(self, statement):
        self.statements.append(statement)
        return self.entries

    def close(self):
        self.closed = True


class TranslationMemoryLookupTests(unittest.TestCase):
    def test_exact_match_uses_normalized_unique_index_before_trigram(self):
        candidate = SimpleNamespace(source_text="  LEGAL CLAUSE  ", target_text="法律条款")
        session = _FakeSession([], exact=candidate)

        with patch.object(tm_lookup, "SessionLocal", return_value=session):
            result = tm_lookup.lookup_tm("legal clause", "en", "zh")

        self.assertEqual(result["target_text"], "法律条款")
        self.assertTrue(result["exact"])
        self.assertEqual(result["similarity"], 1.0)
        self.assertTrue(session.closed)

        sql = str(session.statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        ))
        self.assertIn("source_normalized", sql)
        self.assertIn("= 'legal clause'", sql)
        self.assertNotIn("similarity(", sql)

    def test_postgres_limits_indexed_trigram_candidates_then_confirms_dice(self):
        candidate = SimpleNamespace(source_text="legal clauses", target_text="法律条款")
        session = _FakeSession([candidate])

        with patch.object(tm_lookup, "SessionLocal", return_value=session):
            result = tm_lookup.lookup_tm("legal clause", "en", "zh")

        self.assertEqual(result["target_text"], "法律条款")
        sql = str(session.statements[1].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        ))
        self.assertIn("similarity(", sql)
        self.assertIn("ORDER BY", sql)
        self.assertIn("LIMIT 50", sql)
        self.assertIn(" % ", sql.replace("%%", "%"))

    def test_database_candidate_below_existing_threshold_is_rejected(self):
        session = _FakeSession([
            SimpleNamespace(source_text="unrelated memory", target_text="不应复用"),
        ])

        with patch.object(tm_lookup, "SessionLocal", return_value=session):
            result = tm_lookup.lookup_tm("legal clause", "en", "zh", threshold=0.8)

        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
