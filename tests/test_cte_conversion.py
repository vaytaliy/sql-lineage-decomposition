"""Tests for CTE-to-subquery conversion."""

from __future__ import annotations

import json
import unittest

from sql_tree import SqlTree


class TestCteConversion(unittest.TestCase):
    """Tests verifying recursive CTE handling."""

    def test_recursive_cte_converted_to_subselect(self) -> None:
        """Recursive CTE is inlined as SUBSELECT without infinite loops."""
        sql = """
        WITH RECURSIVE cte_paths AS (
            SELECT 1 AS level, 'root' AS path FROM dual
            UNION ALL
            SELECT c.level + 1, c.path || '.' || n.name
            FROM cte_paths c
            JOIN nodes n ON n.parent_id = c.level
            WHERE c.level < 5
        )
        SELECT * FROM cte_paths
        """
        tree = SqlTree(sql=sql, dialect="spark")
        ast = tree.to_dict()

        self.assertEqual(ast["node_type"], "STATEMENT")
        subselects = [
            c for c in ast["children"] if c["node_type"] == "SUBSELECT"
        ]
        self.assertEqual(len(subselects), 1)
        self.assertEqual(subselects[0]["alias"], "cte_paths")

        # Verify JSON serialization succeeds (would loop on cycles)
        json_str = tree.to_json()
        self.assertIn("cte_paths", json_str)

    def test_mutually_recursive_ctes_converted_to_subselect(self) -> None:
        """Mutually recursive CTE references do not hang."""
        sql = """
        WITH RECURSIVE cte_a AS (SELECT id FROM cte_b),
             cte_b AS (SELECT id FROM cte_a)
        SELECT * FROM cte_a
        """
        tree = SqlTree(sql=sql, dialect="spark")
        ast = tree.to_dict()

        self.assertEqual(ast["node_type"], "STATEMENT")
        subselects = [
            c for c in ast["children"] if c["node_type"] == "SUBSELECT"
        ]
        self.assertEqual(len(subselects), 1)
        self.assertEqual(subselects[0]["alias"], "cte_a")

        # Ensure no cycle broke serialization
        self.assertTrue(json.loads(tree.to_json()))


if __name__ == "__main__":
    unittest.main()
