"""SQL AST tree builder using sqlglot."""

# from __future__ import annotations

import json
from typing import Any , Dict, List, Optional

import sqlglot
from sqlglot import exp

from ast_node import AstNode
from root_hier import RootHierarchy


class SqlTree:
    """Parses SQL into a structured Abstract Syntax Tree.

    Attributes:
        sql: The SQL string to parse.
        dialect: The SQL dialect (e.g., 'spark', 'postgres').
    """

    def __init__(self, sql: str, dialect: str = "spark") -> None:
        """Initialize and parse the SQL into an AST.

        Args:
            sql: SQL query string.
            dialect: SQL dialect for parsing.
        """
        self.sql = sql
        self.dialect = dialect
        self._id_counter = 0
        self._head = self._parse(sql)

    def _next_id(self) -> int:
        """Generate the next unique node identifier."""
        id_val = self._id_counter
        self._id_counter += 1
        return id_val

    def _parse(self, sql: str) -> AstNode:
        """Parse SQL, eliminate CTEs, and build the AST root node."""
        parsed = sqlglot.parse_one(sql, dialect=self.dialect)
        parsed = self._eliminate_ctes(parsed)

        return self._build_node(parsed, parent=None)

    def _eliminate_ctes(self, node: exp.Expr) -> exp.Expr:
        """Replace CTE definitions by inlining them as subqueries.

        Walks the parsed expression, extracts any WITH clause, and replaces
        every reference to a CTE name in FROM/JOIN positions with a Subquery
        wrapping the CTE body. This keeps downstream logic free of CTE-specific
        handling.

        Recursive CTEs are supported by leaving self-references inside a CTE
        body as plain table references, which avoids cycles in the AST.

        Args:
            node: The root sqlglot expression.

        Returns:
            The expression with CTEs inlined and the WITH clause removed.
        """
        if not isinstance(node, exp.Select):
            return node

        with_clause = node.args.get("with_")
        if not with_clause:
            return node

        with_clause.set("recursive", False)

        cte_map: dict[str, exp.Expr] = {
            cte.alias: cte.this for cte in with_clause.expressions if cte.alias
        }

        self._inline_cte_references(node, cte_map)
        node.set("with_", None)
        return node

    def _inline_cte_references(
        self,
        node: exp.Expr | list[Any] | None,
        cte_map: dict[str, exp.Expr],
        inside: Optional[set[str]] = None,
        processed: Optional[set[int]] = None,
    ) -> None:
        """Recursively replace CTE table references with subqueries.

        Args:
            node: Current sqlglot expression node or list of nodes.
            cte_map: Mapping from CTE alias to its body expression.
            inside: Set of CTE aliases currently being inlined to detect
                recursive self-references.
            processed: Set of node ids already processed to avoid
                re-traversing shared CTE bodies.
        """
        if node is None:
            return

        inside = inside or set()
        processed = processed or set()

        if isinstance(node, list):
            for index, item in enumerate(node):
                if (
                    isinstance(item, exp.Table)
                    and item.name in cte_map
                    and item.name not in inside
                ):
                    node[index] = self._build_cte_subquery(
                        item.name, cte_map, inside, processed
                    )
                else:
                    self._inline_cte_references(
                        item, cte_map, inside, processed
                    )
            return

        if not isinstance(node, exp.Expr):
            return

        node_id = id(node)
        if node_id in processed:
            return
        processed.add(node_id)

        for key, val in list(node.args.items()):
            if (
                isinstance(val, exp.Table)
                and val.name in cte_map
                and val.name not in inside
            ):
                node.set(
                    key,
                    self._build_cte_subquery(val.name, cte_map, inside, processed),
                )
            elif isinstance(val, list):
                self._inline_cte_references(val, cte_map, inside, processed)
            elif isinstance(val, exp.Expr):
                self._inline_cte_references(val, cte_map, inside, processed)

    def _build_cte_subquery(
        self,
        name: str,
        cte_map: dict[str, exp.Expr],
        inside: set[str],
        processed: set[int],
    ) -> exp.Subquery:
        """Create a Subquery for a CTE reference and inline nested references.

        Args:
            name: The CTE alias being referenced.
            cte_map: Mapping from CTE alias to its body expression.
            inside: Set of CTE aliases currently being inlined.
            processed: Set of node ids already processed.

        Returns:
            A Subquery wrapping the CTE body, with nested CTE references
            inlined in a cycle-safe manner.
        """
        body = cte_map[name]
        self._inline_cte_references(body, cte_map, inside | {name}, processed)
        return exp.Subquery(this=body, alias=name)

    def _build_node(
        self, node: exp.Expr, parent: Optional[AstNode]
    ) -> AstNode:
        """Build an AST node from a sqlglot expression."""
        if isinstance(node, (exp.Union, exp.Intersect, exp.Except)):
            return self._build_set_operator(node, parent)
        if isinstance(node, exp.Select):
            return self._build_select(node, parent)
        # Fallback for unexpected root types
        parent_id = parent.id if parent else None
        return AstNode(
            id=self._next_id(),
            parent_id=parent_id,
            parent=parent,
            node_type="STATEMENT",
        )

    def _build_set_operator(
        self, node: exp.Expr, parent: Optional[AstNode]
    ) -> AstNode:
        """Build a SET_OPERATOR node for UNION, INTERSECT, EXCEPT."""
        if isinstance(node, exp.Union):
            op_value = "UNION ALL" if not node.args.get("distinct", True) else "UNION"
        elif isinstance(node, exp.Intersect):
            op_value = "INTERSECT"
        else:
            op_value = "EXCEPT"

        parent_id = parent.id if parent else None
        set_op = AstNode(
            id=self._next_id(),
            parent_id=parent_id,
            parent=parent,
            node_type="SET_OPERATOR",
            value=op_value,
        )

        left = self._build_node(node.args["this"], parent=set_op)
        right = self._build_node(node.args["expression"], parent=set_op)
        set_op.children = [left, right]

        return set_op

    def is_attribute_matched_table(self, attr_node: AstNode, table_node: AstNode):
        if table_node.alias:
            return attr_node.table_alias == table_node.alias
        
        # table has no alias
        if attr_node.table_alias:
            return attr_node.table_alias == table_node.value
        
        # no alias in both, assume match
        return True

    def _assign_lowest_level_attributes(self, node: AstNode):
        if node.node_type not in ['JOIN', 'TABLE']: return
        if node.node_type == 'JOIN':
            table_child_nodes = [c for c in node.children if c.node_type == 'TABLE']
            if not table_child_nodes: return #check in case of subquery 
            
            table_node = table_child_nodes[0]

        if node.node_type == 'TABLE':
            table_node = node
        
        if not node.parent: return

        attr_children = set([AstNode(id=self._next_id(),
                                        parent_id= table_node.id,
                                        parent= table_node,
                                        node_type='ROOT_ATTRIBUTE',
                                        value=c.value
                                        ) 
                                for c in node.parent.children if c.node_type == 'ATTRIBUTE' 
                                and self.is_attribute_matched_table(c, table_node)])
        
        fn_attr_children = set()
        for n in node.parent.children:
            if n.node_type == 'FUNCTION':
                if not n.inner_attributes:
                    continue
                for attr_name in n.inner_attributes:
                    split_val = attr_name.split('.') #must check if it belongs to table
                    table_alias_n = ""
                    value_n = ""
                    if len(split_val) == 1: value_n = split_val[0]
                    if len(split_val) == 2: 
                        table_alias_n = split_val[0]
                        value_n = split_val[1] 

                    matched_node = AstNode(
                        id=self._next_id(),
                        parent_id=table_node.id,
                        parent=table_node,
                        node_type='ROOT_ATTRIBUTE',
                        value=value_n,
                        table_alias=table_alias_n
                    )
                    if self.is_attribute_matched_table(matched_node, table_node): fn_attr_children.add(matched_node)

        combined_nodes = fn_attr_children | attr_children
        sorted_nodes = sorted(combined_nodes, key=lambda n: n.value or "")
        table_node.children.extend(sorted_nodes)
                

    def _build_from(self, node: exp.From | exp.Join, parent: AstNode) -> AstNode | None:
        if isinstance(node, exp.From):
            if not isinstance(node.this, exp.Subquery):
                stmt = AstNode(
                    id=self._next_id(),
                    parent_id=parent.id if parent else None,
                    parent=parent,
                    node_type="TABLE",
                    value=node.this.name,  #??
                    alias=node.this.alias  #??
                )

                return stmt
        elif isinstance(node, exp.Join):
            if isinstance(node.this, exp.Table):
                stmt = AstNode(
                    id=self._next_id(),
                    parent_id=parent.id if parent else None,
                    parent=parent,
                    node_type='TABLE',
                    value=node.this.name,
                    alias=node.this.alias
                )

                return stmt
        return None

    def _build_select(
        self, node: exp.Select, parent: Optional[AstNode]
    ) -> AstNode:
        """Build a STATEMENT node for a SELECT query."""
        scope = self._build_scope(node)

        parent_id = parent.id if parent else None
        stmt = AstNode(
            id=self._next_id(),
            parent_id=parent_id,
            parent=parent,
            node_type="STATEMENT",
            value=None,
            scope=scope,
        )

        # Process column projections
        for expr in node.args.get("expressions", []):
            child = self._build_expression(expr, parent=stmt, scope=scope)
            if child:
                stmt.children.append(child)

        # Process FROM for subqueries
        from_node = node.args.get("from_")
        if from_node:
            from_ast_node = self._build_from(from_node, parent=stmt)
            subqueries = self._extract_subqueries(from_node, parent=stmt)
            stmt.children.extend(subqueries)
            if from_ast_node: 
                stmt.children.append(from_ast_node)
                if from_ast_node.parent: self._assign_lowest_level_attributes(from_ast_node)            

        # Process JOINs
        for join in node.args.get("joins", []):
            join_node = self._build_join(join, parent=stmt)
            stmt.children.append(join_node)

        return stmt

    def _build_scope(self, node: exp.Select) -> Dict[str, str]:
        """Build a mapping of table aliases to source names from FROM and JOINs."""
        scope: Dict[str, str] = {}

        from_node = node.args.get("from_")
        if from_node:          
            self._add_to_scope(from_node.this, scope)

        for join in node.args.get("joins", []):
            self._add_to_scope(join.this, scope)

        return scope

    def _add_to_scope(
        self, node: exp.Expr, scope: Dict[str, str]
    ) -> None:
        """Add a table or subquery to the scope mapping."""
        if isinstance(node, exp.Table):
            alias = node.alias or node.name
            scope[alias] = node.name
        elif isinstance(node, exp.Subquery):
            alias = node.alias
            if alias:
                scope[alias] = alias

    def _extract_subqueries(
        self, from_node: exp.From, parent: AstNode
    ) -> List[AstNode]:
        """Extract subqueries from FROM clause and return SUBSELECT nodes."""
        subqueries: List[AstNode] = []
        self._collect_subqueries(from_node.this, parent, subqueries)
        return subqueries

    def _collect_subqueries(
        self, node: exp.Expr, parent: AstNode, result: List[AstNode]
    ) -> None:
        """Recursively collect subqueries from a FROM expression."""
        if isinstance(node, exp.Subquery):
            subselect = self._build_subselect(node, parent)
            result.append(subselect)
            return

        for _, val in node.args.items():
            if val is None:
                continue
            if isinstance(val, list):
                for item in val:
                    if isinstance(item, exp.Expr):
                        self._collect_subqueries(item, parent, result)
            elif isinstance(val, exp.Expr):
                self._collect_subqueries(val, parent, result)

    def _build_subselect(
        self, node: exp.Subquery, parent: AstNode
    ) -> AstNode:
        """Build a SUBSELECT node from a sqlglot Subquery."""
        subselect = AstNode(
            id=self._next_id(),
            parent_id=parent.id,
            parent=parent,
            node_type="SUBSELECT",
            alias=self._extract_alias(node),
        )

        inner_select = node.this
        if isinstance(inner_select, exp.Select):
            inner_scope = self._build_scope(inner_select)

            # Process inner projections directly (no STATEMENT wrapper)
            for expr in inner_select.args.get("expressions", []):
                child = self._build_expression(
                    expr, parent=subselect, scope=inner_scope
                )
                if child:
                    subselect.children.append(child)

            # Process inner FROM for nested subqueries
            inner_from = inner_select.args.get("from_")
            if inner_from:
                from_ast_node = self._build_from(inner_from, parent=subselect)

                nested = self._extract_subqueries(
                    inner_from, parent=subselect
                )
                subselect.children.extend(nested)
                if from_ast_node: 
                    subselect.children.append(from_ast_node) #TODO check
                    if from_ast_node.parent: self._assign_lowest_level_attributes(from_ast_node)
                    #if from_ast_node.parent: self._assign_lowest_level_attributes(from_ast_node.parent)
            # Process inner JOINs
            for join in inner_select.args.get("joins", []):
                join_node = self._build_join(join, parent=subselect)
                subselect.children.append(join_node)
        elif isinstance(
            inner_select, (exp.Union, exp.Intersect, exp.Except)
        ):
            set_op = self._build_set_operator(
                inner_select, parent=subselect
            )
            subselect.children.append(set_op)

        return subselect

    def _extract_alias(self, node: exp.Expr) -> Optional[str]:
        """Extract the alias string from a sqlglot expression node."""
        if hasattr(node, "alias") and node.alias:
            if isinstance(node.alias, str):
                return node.alias
        # alias_node = node.args.get("alias")  #TODO May need?
        # if alias_node:
        #     if hasattr(alias_node, "name"):
        #         return alias_node.name
        return None

    def _is_literal_or_datatype(self, node: exp.Expr) -> bool:
        """Check if a node is a literal value or data type.

        These nodes should not be added to the AST tree.
        """
        return isinstance(node, (exp.Literal, exp.Boolean, exp.Null, exp.DataType))

    def _build_expression(
        self, node: exp.Expr, parent: AstNode, scope: Dict[str, str]
    ) -> Optional[AstNode]:
        """Build an AST node for a projection expression.

        Returns None for literal values and data types that should be
        excluded from the tree structure.
        """
        result: Optional[AstNode] = None

        # Skip literal values and data types
        if self._is_literal_or_datatype(node):
            return None

        # Handle Alias wrapper
        if isinstance(node, exp.Alias):
            alias = self._extract_alias(node)
            result = self._build_expression(node.this, parent, scope)
            if result and alias:
                result.alias = alias
        elif isinstance(node, exp.Column):
            result = self._build_attribute(node, parent, scope)
        elif isinstance(node, exp.Case):
            result = self._build_case(node, parent, scope)
        elif isinstance(node, exp.Func):
            result = self._build_function(node, parent, scope)
        # elif isinstance(node, exp.From):
        #     print(node)
        else:
            # Fallback for other expression types (operators, etc.)
            result = self._build_generic_expression(node, parent, scope)

        return result

    def _build_attribute(
        self, node: exp.Column, parent: AstNode, scope: Dict[str, str]
    ) -> AstNode:
        """Build an ATTRIBUTE node from a sqlglot Column."""
        table_alias = self._get_column_table_alias(node, scope)
        return AstNode(
            id=self._next_id(),
            parent_id=parent.id,
            parent=parent,
            node_type="ATTRIBUTE",
            value=node.name,
            table_alias=table_alias,
        )

    def _get_column_table_alias(
        self, node: exp.Column, scope: Dict[str, str]
    ) -> Optional[str]:
        """Resolve the table alias for a column reference."""
        if node.table:
            return node.table
        # If no explicit table and exactly one table in scope, use that
        if len(scope) == 1:
            return list(scope.keys())[0]
        return None

    def _build_case(
        self, node: exp.Case, parent: AstNode, scope: Dict[str, str]
    ) -> AstNode:
        """Build a FUNCTION node from a sqlglot Case expression."""
        func = AstNode(
            id=self._next_id(),
            parent_id=parent.id,
            parent=parent,
            node_type="FUNCTION",
            value=self._reconstruct_case_sql(node),
        )

        columns = self._collect_columns(node)
        func.inner_attributes = sorted(list(set(columns)))

        # Add CASE expression (the value being compared)
        if node.args.get("this"):
            child = self._build_expression(
                node.args["this"], parent=func, scope=scope
            )
            if child:
                func.children.append(child)

        # Add WHEN/THEN pairs
        for if_node in node.args.get("ifs", []):
            # WHEN condition
            when_expr = if_node.this
            if when_expr:
                child = self._build_expression(
                    when_expr, parent=func, scope=scope
                )
                if child:
                    func.children.append(child)
            # THEN result
            then_expr = if_node.args.get("true")
            if then_expr:
                child = self._build_expression(
                    then_expr, parent=func, scope=scope
                )
                if child:
                    func.children.append(child)

        # Add ELSE
        if node.args.get("default"):
            child = self._build_expression(
                node.args["default"], parent=func, scope=scope
            )
            if child:
                func.children.append(child)

        return func

    def _reconstruct_case_sql(self, node: exp.Case) -> str:
        """Reconstruct CASE SQL without table aliases."""
        parts = ["CASE"]
        if node.args.get("this"):
            parts.append(self._expr_to_sql_no_aliases(node.args["this"]))
        for if_node in node.args.get("ifs", []):
            when_sql = self._expr_to_sql_no_aliases(if_node.this)
            then_sql = self._expr_to_sql_no_aliases(if_node.args.get("true"))
            parts.append(f"WHEN {when_sql} THEN {then_sql}")
        if node.args.get("default"):
            parts.append(
                f"ELSE {self._expr_to_sql_no_aliases(node.args['default'])}"
            )
        parts.append("END")
        return " ".join(parts)

    def _build_function(
        self, node: exp.Func, parent: AstNode, scope: Dict[str, str]
    ) -> AstNode:
        """Build a FUNCTION node from a sqlglot Func expression."""
        func = AstNode(
            id=self._next_id(),
            parent_id=parent.id,
            parent=parent,
            node_type="FUNCTION",
            value=self._reconstruct_func_sql(node),
        )

        columns = self._collect_columns(node)
        func.inner_attributes = sorted(list(set(columns)))

        # Process function arguments
        for _, val in node.args.items():
            if val is None or isinstance(val, (bool, str, int, float)):
                continue
            if isinstance(val, list):
                for item in val:
                    if isinstance(item, exp.Expr):
                        child = self._build_expression(
                            item, parent=func, scope=scope
                        )
                        if child:
                            func.children.append(child)
            elif isinstance(val, exp.Expr):
                child = self._build_expression(
                    val, parent=func, scope=scope
                )
                if child:
                    func.children.append(child)

        return func

    def _reconstruct_func_sql(self, node: exp.Func) -> str:
        """Reconstruct function SQL without table aliases."""
        if isinstance(node, exp.Anonymous):
            func_name = node.name
        else:
            func_name = node.sql_name()
        args = []
        for _, val in node.args.items():
            if val is None or isinstance(val, (bool, str, int, float)):
                continue
            if isinstance(val, list):
                for item in val:
                    if isinstance(item, exp.Expr):
                        args.append(self._expr_to_sql_no_aliases(item))
            elif isinstance(val, exp.Expr):
                args.append(self._expr_to_sql_no_aliases(val))
        return f"{func_name}({', '.join(args)})"

    def _build_generic_expression(
        self, node: exp.Expr, parent: AstNode, scope: Dict[str, str]
    ) -> AstNode:
        """Build a FUNCTION node for generic expressions (operators, etc.)."""
        func = AstNode(
            id=self._next_id(),
            parent_id=parent.id,
            parent=parent,
            node_type="FUNCTION",
            value=self._expr_to_sql_no_aliases(node),
        )

        columns = self._collect_columns(node)
        func.inner_attributes = sorted(list(set(columns)))

        for _, val in node.args.items():
            if val is None or isinstance(val, (bool, str, int, float)):
                continue
            if isinstance(val, list):
                for item in val:
                    if isinstance(item, exp.Expr):
                        child = self._build_expression(
                            item, parent=func, scope=scope
                        )
                        if child:
                            func.children.append(child)
            elif isinstance(val, exp.Expr):
                child = self._build_expression(
                    val, parent=func, scope=scope
                )
                if child:
                    func.children.append(child)

        return func

    def _expr_to_sql_no_aliases(self, node: exp.Expr) -> str:
        """Convert a sqlglot expression to SQL string without table aliases."""
        result = ""

        # Simple types
        if isinstance(node, exp.Column):
            result = node.name
        elif isinstance(node, exp.Alias):
            result = self._expr_to_sql_no_aliases(node.this)
        elif isinstance(node, exp.Func):
            result = self._reconstruct_func_sql(node)
        elif isinstance(node, exp.Case):
            result = self._reconstruct_case_sql(node)
        elif isinstance(node, exp.Literal):
            result = node.sql(dialect=self.dialect)
        elif isinstance(node, exp.Star):
            result = "*"
        else:
            # Binary operators and fallback
            result = self._format_binary_op(node)

        return result

    def _format_binary_op(self, node: exp.Expr) -> str:
        """Format a binary operator expression without table aliases."""
        op_mapping = {
            exp.Add: "+",
            exp.Sub: "-",
            exp.EQ: "=",
            exp.NEQ: "<>",
            exp.GT: ">",
            exp.GTE: ">=",
            exp.LT: "<",
            exp.LTE: "<=",
            exp.And: "AND",
            exp.Or: "OR",
        }

        for op_type, op_symbol in op_mapping.items():
            if isinstance(node, op_type):
                left = self._expr_to_sql_no_aliases(node.this)
                right = self._expr_to_sql_no_aliases(node.expression)
                return f"{left} {op_symbol} {right}"

        # Fallback
        return node.sql(dialect=self.dialect)

    def _collect_columns(self, node: exp.Expr) -> List[str]:
        """Collect all column references (with optional table alias) from an expression tree."""
        columns: List[str] = []
        self._collect_columns_recursive(node, columns)
        return columns

    def _collect_columns_recursive(
        self, node: exp.Expr, result: List[str]
    ) -> None:
        """Recursively collect column references."""
        if isinstance(node, exp.Column):
            if node.table:
                result.append(f"{node.table}.{node.name}")
            else:
                result.append(node.name)
            return
        for _, val in node.args.items():
            if val is None or isinstance(val, (bool, str, int, float)):
                continue
            if isinstance(val, list):
                for item in val:
                    if isinstance(item, exp.Expr):
                        self._collect_columns_recursive(item, result)
            elif isinstance(val, exp.Expr):
                self._collect_columns_recursive(val, result)

    def _build_join(self, node: exp.Join, parent: AstNode) -> AstNode:
        """Build a JOIN node from a sqlglot Join expression."""
        join_sql = node.sql(dialect=self.dialect)
        # table_alias = None
        # if isinstance(node.this, (exp.Table, exp.Subquery)):
        #     table_alias = self._extract_alias(node.this)



        join_node = AstNode(  #Or instead parent the FROM to "parent" (statement)  
            id=self._next_id(),
            parent_id=parent.id,
            parent=parent,
            node_type="JOIN",
            value=join_sql,
            # table_alias=table_alias,
        )

        table_node = node.this
        from_ast_node = None
        if isinstance(table_node, exp.Table):
            from_ast_node = self._build_from(node, join_node)

        if from_ast_node: 
            join_node.children.append(from_ast_node)
            if from_ast_node.parent: self._assign_lowest_level_attributes(from_ast_node.parent)
        return join_node

    def get_statement_hierarchy(self):
        hier_head_node = RootHierarchy(self.head, None)
        RootHierarchy.build_hierarchy(self.head, hier_head_node)
        return hier_head_node

    # def get_roots_hierarchy(self) -> RootHierarchy:
    #     """Build a RootHierarchy from the AST tree.

    #     Returns:
    #         RootHierarchy representing the root structure of the query.

    #     Raises:
    #         NotImplementedError: If the tree root is not a STATEMENT.
    #     """
    #     return build_flat_roots_hierarchy(self._head)

    def to_json(self) -> str:
        """Serialize the AST to a JSON string."""
        return json.dumps(self._head.to_dict(), indent=4)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the AST to a dictionary."""
        return self._head.to_dict()

    @property
    def head(self) -> AstNode:
        """Return the head AST node."""
        return self._head
