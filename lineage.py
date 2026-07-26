"""Lineage tracking for SQL AST trees."""

from __future__ import annotations

import csv
import io
from enum import Enum
from typing import Any, Optional, Sequence, Tuple

from ast_node import AstNode


CSV_COLUMNS = [
    "attribute_value",
    "container_name",
    "container_type",
    "function_str",
    "next_attribute_value",
    "next_container_name",
]


class LineageStep(Enum):
    """Enumeration of lineage container step types."""

    ROOT = 1
    SUBQUERY = 2
    STATEMENT = 3
    SET_OPERATOR = 4


class Lineage:
    """Represents a single step in a column lineage chain.

    Attributes:
        node: The AST node representing this lineage container.
        next: List of subsequent lineage steps.
        container_type: The type of container (ROOT, SUBQUERY, etc.).
        function_str: SQL string of the function if this step is a function.
        container_name: Formatted name including ID and container type.
        attribute_value: The attribute name at this lineage step.
    """

    def __init__(
        self,
        node: AstNode,
        container_node: AstNode,
        container_type: LineageStep,
        attribute_value: str,
        function_str: Optional[str] = None,
    ) -> None:
        """Initialize a lineage step.

        Args:
            node: The AST container node for this step.
            container_type: The lineage step type enum value.
            attribute_value: The column/attribute name at this step.
            function_str: Function SQL string if applicable.
        """
        self.node = node
        self.container_node = container_node
        self.next: list[Lineage] = []
        self.container_type = container_type
        self.function_str = function_str or ""
        self.attribute_value = attribute_value
        self.container_name = self._build_container_name()

    def _build_container_name(self) -> str:
        """Build the formatted container name string.

        Returns:
            String in format "{id}__{LineageStep}__{name}".
        """
        name = self.container_node.value or self.container_node.alias or "#none#"
        return f"{self.container_node.id}__{self.container_type.name}__{name}"

    @staticmethod
    def _find_subselect_with_set_operator(node: AstNode) -> Optional[AstNode]:
        """Find a SUBSELECT child that contains a SET_OPERATOR.

    Args:
            node: The AST node to search within.

        Returns:
            The SUBSELECT node if found, otherwise None.
        """
        for child in node.children:
            if child.node_type == "SUBSELECT":
                for sub_child in child.children:
                    if sub_child.node_type == "SET_OPERATOR":
                        return child
        return None

    @staticmethod
    def _collect_subquery_column_values(subselect: AstNode) -> set[str]:
        """Collect real column values from inner STATEMENTs of a subquery.

        Args:
            subselect: The SUBSELECT node containing a SET_OPERATOR.

        Returns:
            Set of column value strings from inner STATEMENT projections.
        """
        values: set[str] = set()
        for child in subselect.children:
            if child.node_type != "SET_OPERATOR":
                continue
            for stmt in child.children:
                if stmt.node_type != "STATEMENT":
                    continue
                for proj in stmt.children:
                    if proj.node_type not in ("ATTRIBUTE", "FUNCTION"):
                        continue
                    if proj.alias:
                        values.add(proj.alias)
                    elif proj.value:
                        values.add(proj.value)
        return values

    @staticmethod
    def _prepare_subquery_lineage(
        statement: AstNode, subselect: AstNode
    ) -> list[Lineage]:
        """Prepare lineages starting from SUBQUERY level for edge case.

        Args:
            statement: The outer STATEMENT node.
            subselect: The SUBSELECT node containing a SET_OPERATOR.

        Returns:
            List of Lineage objects starting from subquery projections.
        """
        lineages: list[Lineage] = []
        column_values = Lineage._collect_subquery_column_values(subselect)
        processed_values: set[str] = set()
        for child in statement.children:
            if child.node_type not in ("ATTRIBUTE", "FUNCTION"):
                continue
            attr_value = child.value or ""
            if attr_value not in column_values:
                continue
            if attr_value in processed_values:
                continue
            processed_values.add(attr_value)
            lineage = Lineage(
                node=child,   #TODO was subselect, changed to child
                container_node=subselect,
                container_type=LineageStep.SUBQUERY,
                attribute_value=attr_value,
            )
            Lineage._trace_upward(lineage, statement, attr_value, subselect)
            lineages.append(lineage)
        return lineages

    @staticmethod
    def _is_descendant_of(node: AstNode, ancestor: AstNode) -> bool:
        """Check if a node is a direct or indirect descendant of ancestor."""
        current = node.parent
        while current is not None:
            if current is ancestor:
                return True
            current = current.parent
        return False

    @staticmethod
    def prepare_lineage(root_node: AstNode) -> list[Lineage]:
        """Prepare lineage chains for all root attributes in the subtree.

        Args:
            root_node: The AST node whose subtree contains root attributes.

        Returns:
            List of Lineage objects, one per root attribute.
        """
        lineages: list[Lineage] = []

        # Edge case: STATEMENT with SUBSELECT containing SET_OPERATOR
        # Collect subquery-level lineages first, then continue with remaining roots.
        subselect_to_skip: Optional[AstNode] = None
        if root_node.node_type == "STATEMENT":
            subselect_to_skip = Lineage._find_subselect_with_set_operator(root_node)
            if subselect_to_skip:
                lineages.extend(
                    Lineage._prepare_subquery_lineage(root_node, subselect_to_skip)
                )

        for root_attr in root_node.iter_roots():
            if subselect_to_skip and Lineage._is_descendant_of(
                root_attr, subselect_to_skip
            ):
                continue

            table_node = root_attr.parent
            if not table_node:
                continue

            if table_node.node_type == "JOIN":
                table_children = [
                    c for c in table_node.children if c.node_type == "TABLE"
                ]
                if not table_children:
                    continue
                table_node = table_children[0]

            if table_node.node_type != "TABLE":
                continue

            root_lineage = Lineage(
                node=root_attr, #TODO was table_node, changed to root_attr
                container_node = table_node,
                container_type=LineageStep.ROOT,
                attribute_value=root_attr.value or "",
            )

            Lineage._trace_upward(
                root_lineage, table_node.parent, root_attr.value or "", table_node
            )
            lineages.append(root_lineage)

        return lineages

    @staticmethod
    def _trace_upward(
        current: Lineage,
        container: Optional[AstNode],
        attr_name: str,
        source_node: AstNode,
    ) -> None:
        """Trace lineage upward from the current step.

        Args:
            current: The current lineage step.
            container: The parent container to search next.
            attr_name: The attribute name to match at this level.
            source_node: The source node (TABLE or SUBSELECT) providing the attribute.
        """
        if not container:
            return

        if container.node_type == "JOIN":
            Lineage._trace_upward(current, container.parent, attr_name, source_node)
            return

        if container.node_type not in ("SUBSELECT", "STATEMENT", "SET_OPERATOR"):
            Lineage._trace_upward(current, container.parent, attr_name, source_node)
            return

        step_map = {
            "SUBSELECT": LineageStep.SUBQUERY,
            "STATEMENT": LineageStep.STATEMENT,
            "SET_OPERATOR": LineageStep.SET_OPERATOR,
        }
        step = step_map.get(container.node_type, LineageStep.STATEMENT)

        matches = Lineage._find_matches(container, attr_name, source_node)

        if not matches:
            if container.node_type == "SET_OPERATOR":
                return  # Stop at SET_OPERATOR boundary
            Lineage._trace_upward(current, container.parent, attr_name, source_node)
            return

        # Deduplicate matches that would produce identical next steps
        seen_keys: set[tuple[str, Optional[str]]] = set()
        for child, next_attr_name, func_str in matches:
            key = (next_attr_name, func_str)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            next_lineage = Lineage(
                node=child, #TODO changed from container to child
                container_node=container,
                container_type=step,
                attribute_value=next_attr_name,
                function_str=func_str,
            )
            current.next.append(next_lineage)
            Lineage._trace_upward(
                next_lineage, container.parent, next_attr_name, container
            )

    @staticmethod
    def _find_matches(
        container: AstNode, attr_name: str, source_node: AstNode
    ) -> list[Tuple[AstNode, str, Optional[str]]]:
        """Find matching ATTRIBUTE or FUNCTION children in a container.

        Args:
            container: The AST container node to search within.
            attr_name: The attribute name to match.
            source_node: The source node providing the attribute.

        Returns:
            List of tuples (matched_node, next_attr_name, function_str).
        """
        matches: list[Tuple[AstNode, str, Optional[str]]] = []
        source_id = Lineage._get_source_identifier(source_node)

        for child in container.children:
            if child.node_type == "ATTRIBUTE":
                if Lineage._attribute_matches(child, attr_name, source_id):
                    next_name = child.alias or child.value or ""
                    matches.append((child, next_name, None))
            elif child.node_type == "FUNCTION":
                if Lineage._function_matches(child, attr_name, source_id):
                    # TBD to clarify: handling when function has no alias
                    next_name = child.alias or child.value or ""
                    matches.append((child, next_name, child.value))

        return matches

    @staticmethod
    def _get_source_identifier(node: AstNode) -> Optional[str]:
        """Get the identifier for a source node.

        Args:
            node: The source AST node (TABLE or SUBSELECT).

        Returns:
            The alias or name used to reference this source.
        """
        if node.node_type == "TABLE":
            return node.alias or node.value
        if node.node_type == "SUBSELECT":
            return node.alias
        return None

    @staticmethod
    def _attribute_matches(
        attr: AstNode, attr_name: str, source_id: Optional[str]
    ) -> bool:
        """Check if an ATTRIBUTE node matches the target attribute and source.

        Args:
            attr: The ATTRIBUTE node to check.
            attr_name: The target attribute name.
            source_id: The expected source identifier.

        Returns:
            True if the attribute matches both name and source.
        """
        if attr_name not in (attr.value, attr.alias):
            return False

        if source_id is None or attr.table_alias is None:
            return True
        return attr.table_alias == source_id

    @staticmethod
    def _function_matches(
        func: AstNode, attr_name: str, source_id: Optional[str]
    ) -> bool:
        """Check if a FUNCTION node references the target attribute.

        Args:
            func: The FUNCTION node to check.
            attr_name: The target attribute name.
            source_id: The expected source identifier.

        Returns:
            True if the function references the attribute from the source.
        """
        for inner_attr in func.inner_attributes or []:
            parts = inner_attr.split(".")
            if len(parts) == 1:
                name = parts[0]
                table = None
            else:
                table = parts[0]
                name = parts[1]

            if name != attr_name:
                continue

            if source_id is None or table is None:
                return True
            if table == source_id:
                return True

        return False

    def get_heads(self) -> list[Lineage]:
        """Return terminal lineage steps with no further next steps.

        Traverses the lineage chain and collects all leaf nodes
        (steps where next is empty) for this attribute.

        Returns:
            List of terminal Lineage steps.
        """
        if not self.next:
            return [self]

        heads: list[Lineage] = []
        for nxt in self.next:
            heads.extend(nxt.get_heads())
        return heads

    def to_dict(self) -> dict[str, Any]:
        """Serialize this lineage step to a dictionary.

        Returns:
            Dictionary with container_name, container_type, attribute_value,
            function_str, and next steps.
        """
        return {
            "container_name": self.container_name,
            "container_type": self.container_type.name,
            "attribute_value": self.attribute_value,
            "function_str": self.function_str if self.function_str else None,
            "next": [n.to_dict() for n in self.next],
        }

    def to_json(self) -> dict[str, Any]:
        """Serialize this lineage step to a dictionary (alias for to_dict).

        Returns:
            Dictionary representation of this lineage step.
        """
        return self.to_dict()

    @staticmethod
    def to_csv(lineages: Sequence[Lineage], delimiter: str = ",") -> str:
        """Flatten a list of lineage chains into a CSV/TSV edge table.

        Each step is emitted as a single row. Rows are deduplicated by the
        key (attribute_value, container_name, next_attribute_value,
        next_container_name): the first occurrence wins and later duplicates
        are dropped, regardless of whether they originate from shared step
        objects or distinct but logically identical steps. Terminal steps
        have empty next columns.

        Args:
            lineages: Root Lineage objects to flatten.
            delimiter: Column delimiter. Defaults to comma.

        Returns:
            String containing the comma-separated edge table with a header.
        """
        output = io.StringIO()
        writer = csv.writer(output, delimiter=delimiter, lineterminator="\n")
        writer.writerow(CSV_COLUMNS)

        visited: set[int] = set()
        emitted_keys: set[tuple[str, str, str, str]] = set()
        rows: list[list[str]] = []

        def emit_row(step: Lineage, next_attr: str, next_cont: str) -> None:
            key = (step.attribute_value, step.container_name, next_attr, next_cont)
            if key in emitted_keys:
                return
            emitted_keys.add(key)
            rows.append(
                [
                    step.attribute_value,
                    step.container_name,
                    step.container_type.name,
                    step.function_str or "",
                    next_attr,
                    next_cont,
                ]
            )

        def collect(step: Lineage) -> None:
            if id(step) in visited:
                return
            visited.add(id(step))
            if not step.next:
                emit_row(step, "", "")
                return
            for nxt in step.next:
                emit_row(step, nxt.attribute_value, nxt.container_name)
                collect(nxt)

        for lineage in lineages:
            collect(lineage)

        writer.writerows(rows)
        return output.getvalue()
