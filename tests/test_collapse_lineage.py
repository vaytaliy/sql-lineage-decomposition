"""Test runner for collapsed SQL lineage tests.

Discovers and executes collapse test cases from TOML files in
tests/test_queries/. Each TOML file contains an [in] section with SQL and
[collapse_*] sections with expected collapsed lineage JSON output produced by
RootHierarchy.collapse_lineage().
"""

from __future__ import annotations

import json
import tomllib
import unittest
from pathlib import Path
from typing import Any, Callable

from tests.test_lineage import normalize_lineage

from ast_node import AstNode
from lineage import CSV_COLUMNS, Lineage, LineageStep
from sql_tree import SqlTree


TEST_QUERIES_DIR = Path(__file__).parent / "test_queries"


def load_collapse_test_cases() -> list[tuple[str, str, str, str, list[dict[str, Any]]]]:
    """Load all collapse test cases from TOML files.

    Returns:
        List of tuples (file_name, test_name, description, sql,
        expected_lineage).
    """
    cases: list[
        tuple[str, str, str, str, list[dict[str, Any]]]
    ] = []
    for toml_file in sorted(TEST_QUERIES_DIR.glob("*.toml")):
        with open(toml_file, "rb") as f:
            data = tomllib.load(f)

        test_sql = data["in"]["sql"]

        for section_name, section_data in data.items():
            if not section_name.startswith("collapse"):
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


class TestCollapseLineage(unittest.TestCase):
    """Test cases for collapsed SQL lineage generation."""

    def test_to_csv_deduplication(self) -> None:
        """CSV flattening emits one row per step and deduplicates shared edges."""
        with open(TEST_QUERIES_DIR / "test_collapse_complex_union.toml", "rb") as f:
            data = tomllib.load(f)
        tree = SqlTree(sql=data["in"]["sql"], dialect="spark")
        result = tree.get_statement_hierarchy().collapse_lineage()
        csv = Lineage.to_csv(result)
        lines = csv.strip().split("\n")
        self.assertEqual(lines[0], ",".join(CSV_COLUMNS))
        self.assertEqual(len(lines), 21)
        rows = [line.split(",") for line in lines[1:]]
        subquery_sources = [
            row for row in rows
            if row[0] == "customer_id" and row[1].endswith("__SUBQUERY__all_tx")
        ]
        self.assertEqual(len(subquery_sources), 1)

    def test_to_csv_deduplicates_by_row_key(self) -> None:
        """Distinct step objects with identical row keys emit a single row."""
        # Arrange: two logically identical chains built from distinct objects
        def build_chain() -> Lineage:
            container_node = AstNode(id=1, parent_id=None,
                                     node_type="STATEMENT", alias="orders")
            next_node = AstNode(id=2, parent_id=None,
                                node_type="ROOT", alias="out")
            step = Lineage(node=container_node, container_node=container_node,
                           container_type=LineageStep.STATEMENT,
                           attribute_value="customer_id")
            step.next.append(Lineage(node=next_node, container_node=next_node,
                                     container_type=LineageStep.ROOT,
                                     attribute_value="customer_id"))
            return step

        # Act
        csv = Lineage.to_csv([build_chain(), build_chain()])

        # Assert: header + one edge row + one terminal row, no duplicates
        rows = csv.strip().split("\n")[1:]
        keys = [(row.split(",")[0], row.split(",")[1],
                 row.split(",")[4], row.split(",")[5]) for row in rows]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(len(rows), 2)


def make_test_method(
    test_sql: str, expected_lineage: list[dict[str, Any]]
) -> Callable[[Any], None]:
    """Create a test method that verifies collapsed lineage output."""

    def test_method(self: unittest.TestCase) -> None:
        tree = SqlTree(sql=test_sql, dialect="spark")
        actual_lineage = tree.get_statement_hierarchy().collapse_lineage()
        actual_json = [lineage.to_json() for lineage in actual_lineage]

        actual_normalized = normalize_lineage(actual_json)
        expected_normalized = normalize_lineage(expected_lineage)

        self.assertEqual(
            actual_normalized,
            expected_normalized,
            f"Collapsed lineage mismatch for SQL:\n{test_sql}\n\n"
            f"Expected:\n{json.dumps(expected_normalized, indent=2)}\n\n"
            f"Actual:\n{json.dumps(actual_normalized, indent=2)}",
        )

    return test_method


# Dynamically add test methods for each collapse TOML test case
for (
    _file_name,
    _test_name,
    _description,
    _sql,
    _expected_lineage,
) in load_collapse_test_cases():
    _method_name = f"test_{_file_name.replace('.toml', '')}_{_test_name}"
    _test_method = make_test_method(_sql, _expected_lineage)
    _test_method.__name__ = _method_name
    _test_method.__doc__ = f"{_file_name} - {_test_name}: {_description}"
    setattr(TestCollapseLineage, _method_name, _test_method)


if __name__ == "__main__":
    unittest.main()
