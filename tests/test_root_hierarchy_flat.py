"""Tests for flat statement root hierarchy extraction."""

from __future__ import annotations

import tomllib
import unittest
from pathlib import Path
from typing import Any, Callable, List, Tuple

from sql_tree import SqlTree


TEST_QUERIES_DIR = Path(__file__).parent / "test_queries"


def load_flat_root_test_cases() -> List[Tuple[str, str, str, List[str]]]:
    """Load test cases with root expectations from TOML files.

    Returns:
        List of tuples (file_name, test_name, sql, expected_roots).
    """
    cases: List[Tuple[str, str, str, List[str]]] = []

    for toml_file in sorted(TEST_QUERIES_DIR.glob("*.toml")):
        with open(toml_file, "rb") as f:
            data = tomllib.load(f)

        if "roots" not in data:
            continue

        test_sql = data["in"]["sql"]
        expected_roots = data["roots"]["expected"]

        cases.append((
            toml_file.name,
            "roots",
            test_sql,
            expected_roots,
        ))

    return cases


class TestFlatRootHierarchy(unittest.TestCase):
    """Test cases for flat statement root hierarchy extraction."""


def make_test_method(
    test_sql: str, expected_roots: List[str]
) -> Callable[[Any], None]:
    """Create a test method verifying root extraction."""
    def test_method(self: unittest.TestCase) -> None:
        tree = SqlTree(sql=test_sql, dialect="spark")
        roots = tree.head.iter_roots()

        actual_roots: set[str] = set()
        for root_attr in roots:
            table_node = root_attr.parent
            if table_node and table_node.node_type == "TABLE":
                table_name = table_node.value
                column_name = root_attr.value
                if table_name and column_name:
                    actual_roots.add(f"{table_name}.{column_name}")

        self.assertEqual(
            sorted(actual_roots),
            sorted(expected_roots),
            f"Root mismatch for SQL:\n{test_sql}\n\n"
            f"Expected: {sorted(expected_roots)}\n"
            f"Actual: {sorted(actual_roots)}",
        )

    return test_method


# Dynamically add test methods for each TOML test case with [roots]
for _file_name, _test_name, _sql, _expected_roots in load_flat_root_test_cases():
    _method_name = f"test_{_file_name.replace('.toml', '')}_{_test_name}"
    _test_method = make_test_method(_sql, _expected_roots)
    _test_method.__name__ = _method_name
    _test_method.__doc__ = f"{_file_name} - {_test_name}: root extraction"
    setattr(TestFlatRootHierarchy, _method_name, _test_method)


if __name__ == "__main__":
    unittest.main()
