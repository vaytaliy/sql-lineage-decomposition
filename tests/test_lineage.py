"""Test runner for SQL lineage tests.

Discovers and executes lineage test cases from TOML files in tests/test_queries/.
Each TOML file contains an [in] section with SQL and [lineage_*] sections
with expected lineage JSON output.
"""

from __future__ import annotations

import json
import tomllib
import unittest
from pathlib import Path
from typing import Any, Callable

from sql_tree import SqlTree


TEST_QUERIES_DIR = Path(__file__).parent / "test_queries"


def load_lineage_test_cases() -> list[tuple[str, str, str, str, list[dict[str, Any]]]]:
    """Load all lineage test cases from TOML files.

    Returns:
        List of tuples (file_name, test_name, description, sql, expected_lineage).
    """
    cases: list[
        tuple[str, str, str, str, list[dict[str, Any]]]
    ] = []
    for toml_file in sorted(TEST_QUERIES_DIR.glob("*.toml")):
        with open(toml_file, "rb") as f:
            data = tomllib.load(f)

        test_sql = data["in"]["sql"]

        for section_name, section_data in data.items():
            if not section_name.startswith("lineage"):
                continue

            test_description = section_data.get("description", "")
            expected_json = section_data["expected"].strip()
            expected_lineage = json.loads(expected_json)
            if not isinstance(expected_lineage, list):
                expected_lineage = [expected_lineage]

            cases.append((
                toml_file.name,
                section_name,
                test_description,
                test_sql,
                expected_lineage,
            ))

    return cases


def normalize_lineage(lineage: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize lineage dict for comparison by removing dynamic id fields."""

    def _normalize_node(node: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in node.items():
            if key == "container_name":
                # Remove dynamic ID prefix, keep only __TYPE__name part
                parts = str(value).split("__", 1)
                if len(parts) == 2:
                    result[key] = f"ID__{parts[1]}"
                else:
                    result[key] = value
            elif key == "next":
                result[key] = [_normalize_node(n) for n in value]
            else:
                result[key] = value
        return result

    return [_normalize_node(item) for item in lineage]


class TestLineage(unittest.TestCase):
    """Test cases for SQL lineage generation."""


def make_test_method(
    test_sql: str, expected_lineage: list[dict[str, Any]]
) -> Callable[[Any], None]:
    """Create a test method that verifies lineage output matches expected."""

    def test_method(self: unittest.TestCase) -> None:
        tree = SqlTree(sql=test_sql, dialect="spark")
        actual_lineage = tree.get_statement_hierarchy().prepare_lineage()
        actual_json = [lineage.to_json() for lineage in actual_lineage]

        actual_normalized = normalize_lineage(actual_json)
        expected_normalized = normalize_lineage(expected_lineage)

        self.assertEqual(
            actual_normalized,
            expected_normalized,
            f"Lineage mismatch for SQL:\n{test_sql}\n\n"
            f"Expected:\n{json.dumps(expected_normalized, indent=2)}\n\n"
            f"Actual:\n{json.dumps(actual_normalized, indent=2)}",
        )

    return test_method


# Dynamically add test methods for each lineage TOML test case
for (
    _file_name,
    _test_name,
    _description,
    _sql,
    _expected_lineage,
) in load_lineage_test_cases():
    _method_name = f"test_{_file_name.replace('.toml', '')}_{_test_name}"
    _test_method = make_test_method(_sql, _expected_lineage)
    _test_method.__name__ = _method_name
    _test_method.__doc__ = f"{_file_name} - {_test_name}: {_description}"
    setattr(TestLineage, _method_name, _test_method)


if __name__ == "__main__":
    unittest.main()
