"""AST node data models for SQL tree representation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from typing import Iterator


@dataclass
class AstNode:  # pylint: disable=too-many-instance-attributes
    """Represents a node in the SQL Abstract Syntax Tree.

    Attributes:
        id: Unique identifier for the node.
        parent_id: ID of the immediate parent node, null for root.
        node_type: Structural or operational role of the node.
        value: Literal SQL fragment evaluated at this node.
        alias: Target name assigned via the AS keyword.
        table_alias: Immediate structural origin of an attribute.
        inner_attributes: Distinct structural dependencies inside FUNCTION nodes.
        children: Nested array containing child expressions.
    """

    id: int
    parent_id: Optional[int]
    node_type: str
    value: Optional[str] = None
    alias: Optional[str] = None
    table_alias: Optional[str] = None
    inner_attributes: Optional[List[str]] = None
    children: List[AstNode] = field(default_factory=list)
    parent: Optional[AstNode] = field(default=None, repr=False)
    scope: Optional[Dict[str, str]] = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        """Convert the node to a dictionary representation."""
        result = {
            "id": self.id,
            "parent_id": self.parent_id,
            "node_type": self.node_type,
            # "scope": self.scope
        }

        if self.value is not None:
            result["value"] = self.value

        if self.alias is not None:
            result["alias"] = self.alias

        if self.table_alias is not None:
            result["table_alias"] = self.table_alias

        if self.inner_attributes is not None:
            result["inner_attributes"] = self.inner_attributes

        if self.children:
            result["children"] = [child.to_dict() for child in self.children]

        return result

    def iter_roots(self) -> Iterator[AstNode]:
        """Yield all ROOT_ATTRIBUTE nodes in the subtree."""
        nodes: list[AstNode] = []

        def get_to_root(node: AstNode, nodes: list[AstNode]) -> None:
            if node.node_type == 'ROOT_ATTRIBUTE':
                nodes.append(node)
            else:
                for child in node.children:
                    get_to_root(child, nodes)

        get_to_root(self, nodes)

        while nodes:
            yield nodes.pop(0)


    def __hash__(self):
        return hash((self.value or "", self.table_alias or ""))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, AstNode):
            return NotImplemented
        return (
            (self.value or "") == (other.value or "")
            and (self.table_alias or "") == (other.table_alias or "")
        )
