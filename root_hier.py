"""Root hierarchy builder for SQL AST trees."""

from __future__ import annotations

import json
from typing import Any, Iterator, List, Optional

from ast_node import AstNode
from lineage import Lineage, LineageStep


class HierId:
    """Process-wide sequential identifier generator for hierarchy nodes."""

    current_id = 0

    @staticmethod
    def _next_id() -> int:
        id_val = HierId.current_id
        HierId.current_id += 1
        return id_val

class RootHierarchy:
    """Hierarchical representation of root nodes in an SQL AST.

    Attributes:
        node: The AST node this hierarchy wraps (STATEMENT or SET_OPERATOR).
        parent: Parent RootHierarchy node, None for the top level.
        lineage: Lineage chains prepared for this node (STATEMENT nodes) or
            merged chains produced by collapse (SET_OPERATOR nodes).
        left: Left child hierarchy (for SET_OPERATOR branches).
        right: Right child hierarchy (for SET_OPERATOR branches).
    """

    # Internal members are accessed across instances of this same class
    # during recursive hierarchy traversal and collapse.
    # pylint: disable=protected-access

    def __init__(self, node: AstNode, parent: RootHierarchy | None) -> None:
        self._id_counter = HierId._next_id()
        self.node = node
        self.parent = parent
        self.lineage: list[Lineage] = []
        self.left: Optional[RootHierarchy] = None
        self.right: Optional[RootHierarchy] = None
        self._lineage_prepared = False

    @staticmethod
    def build_hierarchy(next_node: AstNode, next_hier: RootHierarchy) -> None:
        """Recursively build the root hierarchy from an AST subtree.

        Args:
            next_node: The AST node currently being wrapped.
            next_hier: The hierarchy node the AST subtree attaches to.
        """
        if next_node.node_type == 'SET_OPERATOR':
            next_hier.left = RootHierarchy(next_node.children[0], next_hier)
            next_hier.right = RootHierarchy(next_node.children[1], next_hier)
            RootHierarchy.build_hierarchy(next_node.children[0], next_hier.left)
            RootHierarchy.build_hierarchy(next_node.children[1], next_hier.right)
            return
        if next_node.node_type == 'STATEMENT':
            # A STATEMENT's direct children are projections, FROM, SUBSELECTs,
            # and JOINs.  Nested query bodies live inside SUBSELECT children.
            for child in next_node.children:
                if child.node_type == 'SUBSELECT':
                    RootHierarchy.build_hierarchy(child, next_hier)
                    break
            return
        if next_node.node_type == 'SUBSELECT':
            # Skip SUBSELECT wrapper and link its contents directly to parent
            for child in next_node.children:
                if child.node_type == 'SET_OPERATOR':
                    set_op_hier = RootHierarchy(child, next_hier)
                    next_hier.left = set_op_hier
                    RootHierarchy.build_hierarchy(child, set_op_hier)
                    break
                if child.node_type == 'SUBSELECT':
                    RootHierarchy.build_hierarchy(child, next_hier)
                    break
            return

    def __iter__(self) -> Iterator[RootHierarchy]:
        """Iterate over the hierarchy tree using BFS."""
        queue: List[RootHierarchy] = [self]
        while queue:
            current = queue.pop(0)
            yield current
            if current.left:
                queue.append(current.left)
            if current.right:
                queue.append(current.right)

    def prepare_lineage(self) -> list[Lineage]:
        """Prepare lineage chains for all root attributes across the hierarchy.

        Returns:
            List of Lineage objects for all root attributes.
        """
        all_lineages: list[Lineage] = []
        for hier in self:
            if hier.node.node_type != "STATEMENT":
                continue
            hier.lineage = Lineage.prepare_lineage(hier.node)
            all_lineages.extend(hier.lineage)
        self._lineage_prepared = True
        return all_lineages

    def get_root(self) -> RootHierarchy:
        """Return the top-level hierarchy node by walking up parents."""
        current = self
        while current.parent is not None:
            current = current.parent
        return current

    def collapse_lineage(self) -> list[Lineage]:
        """Collapse the hierarchy into a single node with connected lineage.

        Walks the hierarchy bottom-up. Every SET_OPERATOR node unions the
        lineage chains of both branches into one list - each branch keeps its
        own independent root chains, since a root never has lineage before
        itself. Branches converge where the union output is consumed: at an
        outer statement reading subquery alias columns (stitch), or - when
        there is no consumer above - at the left branch's terminal steps,
        which represent the union's output columns. Right children are
        dropped as they are absorbed, so in the end the root remains a
        single RootHierarchy object with only ``left`` nodes.

        Returns:
            The final list of connected Lineage chains, one per root.
        """
        hier_root = self.get_root()
        if not hier_root._lineage_prepared:
            hier_root.prepare_lineage()
        return hier_root._collapse()

    def _collapse(self) -> list[Lineage]:
        """Recursively collapse child hierarchies and union set branches.

        Returns:
            Lineage chains representing this node after collapsing. For
            SET_OPERATOR nodes this is both branches' chains concatenated.
        """
        if self.node.node_type == "SET_OPERATOR":
            left_lineage = self.left._collapse() if self.left else []
            right_lineage = self.right._collapse() if self.right else []
            if self.parent is None or self.parent.node.node_type != "STATEMENT":
                self._converge_branches(left_lineage, right_lineage)
            self.lineage = left_lineage + right_lineage
            self.right = None
            return self.lineage

        if self.left is not None:
            child_lineage = self.left._collapse()
            self.lineage = self._stitch_child_lineage(child_lineage)
        return self.lineage

    @staticmethod
    def _get_output_projections(container: AstNode) -> list[AstNode]:
        """Return the ordered list of output projections for a container.

        For a SET_OPERATOR this is the left branch's projections, because the
        left branch defines the union's output column names and positions.
        For a SUBSELECT wrapping a SET_OPERATOR the same left-branch rule
        applies. For a STATEMENT or plain SUBSELECT the direct projections are
        used.

        Args:
            container: The AST container node whose output projections are
                needed.

        Returns:
            List of ATTRIBUTE/FUNCTION child nodes that define output columns.
        """
        if container.node_type == "SET_OPERATOR":
            return RootHierarchy._get_output_projections(container.children[0])

        if container.node_type == "SUBSELECT":
            for child in container.children:
                if child.node_type == "SET_OPERATOR":
                    return RootHierarchy._get_output_projections(child)
            return [
                c
                for c in container.children
                if c.node_type in ("ATTRIBUTE", "FUNCTION")
            ]

        return [
            c
            for c in container.children
            if c.node_type in ("ATTRIBUTE", "FUNCTION")
        ]

    @staticmethod
    def _output_position(head: Lineage) -> int:
        """Return the output column position of a lineage head.

        The position is the index of the matching projection within the
        container's ordered output projection list. This follows SQL UNION
        semantics where output columns are determined by position.

        Args:
            head: A terminal lineage step.

        Returns:
            Zero-based output column index, or -1 if not found.
        """
        projections = RootHierarchy._get_output_projections(head.container_node)
        for index, projection in enumerate(projections):
            if projection.value == head.attribute_value:
                return index
            if projection.alias == head.attribute_value:
                return index
        return -1

    @staticmethod
    def _converge_branches(
        left_lineage: list[Lineage], right_lineage: list[Lineage]
    ) -> None:
        """Connect right branch heads to matching left branch heads by position.

        The left branch defines the set operator's output columns. Under SQL
        UNION semantics each right branch output column maps to the left
        branch output column at the same ordinal position, regardless of name.

        Args:
            left_lineage: Collapsed chains of the left (column-naming) branch.
            right_lineage: Collapsed chains of the right branch.
        """
        left_heads_by_position: dict[int, list[Lineage]] = {}
        seen: set[int] = set()
        for chain in left_lineage:
            for head in chain.get_heads():
                if id(head) in seen:
                    continue
                seen.add(id(head))
                position = RootHierarchy._output_position(head)
                if position < 0:
                    continue
                left_heads_by_position.setdefault(position, []).append(head)

        for chain in right_lineage:
            for head in chain.get_heads():
                position = RootHierarchy._output_position(head)
                if position < 0:
                    continue
                for target in left_heads_by_position.get(position, []):
                    head.next.append(target)

    def _stitch_child_lineage(
        self, child_lineage: list[Lineage]
    ) -> list[Lineage]:
        """Stitch a collapsed child hierarchy into this statement's lineage.

        A child hierarchy is always a subquery containing a set operator, so
        its collapsed chains flow into this statement through the subquery
        alias columns. Each child chain head is connected to the subquery
        lineage step whose attribute matches the head's attribute value.

        Args:
            child_lineage: Collapsed lineage chains from the child hierarchy.

        Returns:
            The child chains extended through this statement, followed by
            this statement's own chains that were not consumed by stitching.
        """
        sub_starts: dict[str, list[Lineage]] = {}
        for lin in self.lineage:
            if lin.container_type == LineageStep.SUBQUERY:
                sub_starts.setdefault(lin.attribute_value, []).append(lin)

        consumed: set[int] = set()
        for child in child_lineage:
            for head in child.get_heads():
                for start in sub_starts.get(head.attribute_value, []):
                    head.next.append(start)
                    consumed.add(id(start))

        merged = list(child_lineage)
        merged.extend(
            lin for lin in self.lineage if id(lin) not in consumed
        )
        return merged

    def to_dict(self) -> dict[str, Any]:
        """Serialize the hierarchy to a dictionary.

        Returns:
            Dictionary representation excluding parent references.
        """
        result: dict[str, Any] = {"node": self.node.node_type}
        result['id'] = self._id_counter
        if self.left:
            result["left"] = self.left.to_dict()

        if self.right:
            result["right"] = self.right.to_dict()

        return result

    def to_json(self) -> str:
        """Serialize the hierarchy to a JSON string."""
        return json.dumps(self.to_dict(), indent=4)
