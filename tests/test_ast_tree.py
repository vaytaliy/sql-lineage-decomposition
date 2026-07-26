"""Test runner for SQL AST tree tests.

Discovers and executes test cases from TOML files in tests/test_queries/.
Each TOML file contains an [in] section with SQL and [test_*] sections
with expected JSON tree output.
"""

from __future__ import annotations

import json
import tomllib
import unittest
from pathlib import Path
from typing import Any, Callable

from sql_tree import SqlTree


TEST_QUERIES_DIR = Path(__file__).parent / "test_queries"


def load_toml_test_cases() -> list[tuple[str, str, str, str, dict[str, Any]]]:
    """Load all test cases from TOML files in the test_queries directory.

    Returns:
        List of tuples (file_name, test_name, description, sql, expected_dict).
    """
    cases: list[tuple[str, str, str, str, dict[str, Any]]] = []
    for toml_file in sorted(TEST_QUERIES_DIR.glob("*.toml")):
        with open(toml_file, "rb") as f:
            data = tomllib.load(f)

        test_sql = data["in"]["sql"]

        for section_name, section_data in data.items():
            if section_name in ("in", "roots"):
                continue
            test_description = section_data.get("description", "")
            expected_json = section_data["expected"].strip()
            expected_dict = json.loads(expected_json)

            # Skip non-AST test formats (e.g., lineage tests)
            if "node_type" not in expected_dict:
                continue

            cases.append((
                toml_file.name,
                section_name,
                test_description,
                test_sql,
                expected_dict,
            ))

    return cases


def normalize_tree(tree: dict[str, Any]) -> dict[str, Any]:
    """Normalize tree dict for comparison by removing dynamic id fields."""
    if not isinstance(tree, dict):
        return tree

    result = {}
    for key, value in tree.items():
        if key in ("id", "parent_id"):
            continue
        if key == "children":
            result[key] = [normalize_tree(child) for child in value]
        else:
            result[key] = value

    return result


class TestAstTree(unittest.TestCase):
    """Test cases for SQL AST tree generation."""


def make_test_method(
    test_sql: str, expected_dict: dict[str, Any]
) -> Callable[[Any], None]:
    """Create a test method that verifies AST output matches expected."""
    def test_method(self: unittest.TestCase) -> None:
        tree = SqlTree(sql=test_sql, dialect="spark")
        actual_dict = tree.to_dict()

        actual_normalized = normalize_tree(actual_dict)
        expected_normalized = normalize_tree(expected_dict)

        self.assertEqual(
            actual_normalized,
            expected_normalized,
            f"AST tree mismatch for SQL:\n{test_sql}\n\n"
            f"Expected:\n{json.dumps(expected_normalized, indent=2)}\n\n"
            f"Actual:\n{json.dumps(actual_normalized, indent=2)}",
        )

    return test_method


# Dynamically add test methods for each TOML test case
for _file_name, _test_name, _description, _sql, _expected_dict in load_toml_test_cases():
    _method_name = f"test_{_file_name.replace('.toml', '')}_{_test_name}"
    _test_method = make_test_method(_sql, _expected_dict)
    _test_method.__name__ = _method_name
    _test_method.__doc__ = f"{_file_name} - {_test_name}: {_description}"
    setattr(TestAstTree, _method_name, _test_method)


if __name__ == "__main__":
    unittest.main()
