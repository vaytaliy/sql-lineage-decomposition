# SQL AST Tree Requirements Specification

## 1. Overview

This document defines the formal specification for the SQL Abstract Syntax Tree (AST) parser. The AST represents SQL queries as structured JSON trees, capturing relationships between tables, columns, functions, subqueries, and set operations.

## 2. Node Schema

Every node in the AST must implement the following properties:

| Property | Type | Description |
|----------|------|-------------|
| `id` | Integer | Unique identifier for the node |
| `parent_id` | Integer \| null | ID of the immediate parent node; root is always `null` |
| `node_type` | String | Structural or operational role (see Section 3) |
| `value` | String \| null | Literal SQL fragment evaluated at this node |
| `alias` | String \| null | Target name assigned via the `AS` keyword |
| `table_alias` | String \| null | Immediate structural origin of an attribute |
| `inner_attributes` | Array[String] | Present on FUNCTION nodes; lists bare column dependencies |
| `children` | Array[Node] | Nested array containing child expressions |

## 3. Node Type Definitions

### 3.1 STATEMENT
- **Purpose**: Root wrapper for a single SELECT query block
- **Value**: Always `null`
- **Parent**: Can be root, or child of SET_OPERATOR
- **Children**: Column projections, TABLE nodes, SUBSELECT nodes, JOIN nodes
- **Rule**: Never a child of SUBSELECT

### 3.2 SET_OPERATOR
- **Purpose**: Horizontal set-combining operations (UNION, UNION ALL, INTERSECT, EXCEPT)
- **Value**: The operator string (e.g., `"UNION"`, `"UNION ALL"`, `"INTERSECT"`, `"EXCEPT"`)
- **Parent**: Always root
- **Children**: Exactly 2 STATEMENT nodes (left and right branches)
- **Schema Contract**: Output column names/aliases are bound strictly to the left-most query block

### 3.3 SUBSELECT
- **Purpose**: Inline view or named subquery wrapper
- **Value**: Always `null`
- **Alias**: The subquery name (e.g., `"sub"`)
- **Children**: Inner query's column projections directly (no STATEMENT wrapper)
- **Rule**: Acts as a transparent container exposing its inner columns

### 3.4 FUNCTION
- **Purpose**: Scalar functions, operators, or conditional logic (LENGTH, CONCAT, CASE, +, =, etc.)
- **Value**: SQL fragment without table aliases (e.g., `"LENGTH(target_city)"`)
- **Alias**: Present if the function expression has an AS alias
- **table_alias**: Always `null`
- **inner_attributes**: Sorted list of column references used inside the
  function, in `alias.column` form when the source is aliased, bare column
  name otherwise (e.g., `["sub.order_amount", "sub.target_city"]`)
- **Children**: Nested argument expressions (functions, columns)

### 3.5 JOIN
- **Purpose**: Explicit join conditions and table associations
- **Value**: Full join SQL string (e.g., `"INNER JOIN customers AS c ON sub.customer_id = c.id"`)
- **table_alias**: Always `null` (the joined table is a child node instead)
- **Children**: The joined TABLE node (carrying its own ROOT_ATTRIBUTE
  children); empty when the join source is a subquery

### 3.6 ATTRIBUTE
- **Purpose**: Literal column or field name (always a leaf node)
- **Value**: Column name without table prefix (e.g., `"customer_id"`)
- **table_alias**: Source table or subquery alias
- **Children**: Always empty array
- **Rule**: Literal values and data types are NEVER represented as ATTRIBUTE nodes

### 3.7 TABLE
- **Purpose**: Physical table referenced in a FROM or JOIN clause
- **Value**: Table name (e.g., `"orders"`)
- **Alias**: Table alias if present (empty string otherwise)
- **Children**: ROOT_ATTRIBUTE nodes — the columns this table contributes to
  the enclosing SELECT (matched from projections and FUNCTION
  `inner_attributes` via `table_alias`, sorted by column name)
- **Rule**: Subqueries in FROM/JOIN produce SUBSELECT nodes, not TABLE nodes

### 3.8 ROOT_ATTRIBUTE
- **Purpose**: A source-table column that feeds the query (lineage origin)
- **Value**: Column name without table prefix
- **Parent**: Always a TABLE node
- **Children**: Always empty array
- **Rule**: Only projections actually consumed by the enclosing SELECT are
  collected — it is not a full table schema dump

## 4. Structural Rules

### 4.1 Root Determination
```
IF parsed SQL is Union/Intersect/Except:
    root = SET_OPERATOR
ELSE IF parsed SQL is Select:
    root = STATEMENT
```

### 4.2 STATEMENT Node Hierarchy
```
STATEMENT can be:
- Root node
- Child of SET_OPERATOR

STATEMENT CANNOT be:
- Child of SUBSELECT
- Child of another STATEMENT
```

### 4.3 SUBSELECT Content
```
SUBSELECT.children = [
    <inner column projections>,     # ATTRIBUTE, FUNCTION nodes
    <inner tables>,                 # TABLE nodes
    <nested subqueries>,            # SUBSELECT nodes
    <inner joins>                   # JOIN nodes
]
```

### 4.4 Set Operation Branching
```
SET_OPERATOR (root)
├── STATEMENT (left branch)
│   └── <column projections>
└── STATEMENT (right branch)
    └── <column projections>
```

## 5. Scope Resolution (table_alias)

### 5.1 Scope Building
For each SELECT block, build a mapping of `alias -> source` from:
- FROM clause tables
- JOIN clause tables

### 5.2 Column Resolution
```
IF column has explicit table prefix:
    table_alias = prefix
ELIF scope has exactly one table:
    table_alias = that table's alias
ELSE:
    table_alias = null
```

### 5.3 Subquery Enclosure
When an outer query selects from a named subquery:
```sql
SELECT sub.col FROM (SELECT col FROM orders) sub
```
The outer attribute must declare `"table_alias": "sub"`, regardless of the inner physical table.

### 5.4 Computed Nodes
FUNCTION, SET_OPERATOR, STATEMENT, and JOIN nodes always have `table_alias: null`.

## 6. Literal and Data Type Filtering

The following sqlglot expression types are NEVER added to the AST tree:
- `Literal` - string, numeric, boolean literals
- `Null` - NULL keyword
- `DataType` - type annotations (INT, VARCHAR, etc.)

These values appear only in parent FUNCTION nodes' `value` strings.

## 7. SQL Reconstruction Rules

### 7.1 Function Values
```
func_name(arg1, arg2, ...)
```
Arguments are reconstructed without table aliases.

### 7.2 CASE Values
```
CASE expr WHEN cond1 THEN result1 [WHEN ...] [ELSE default] END
```
Conditions and results are reconstructed without table aliases.

### 7.3 Operator Values
```
left_expr OP right_expr
```
Supported operators: +, -, =, <>, >, >=, <, <=, AND, OR

## 8. inner_attributes Computation

For FUNCTION nodes, `inner_attributes` is a sorted, deduplicated list of all
column references used inside the function. References keep their qualifier:
`alias.column` when the column carries a table/subquery prefix, bare column
name otherwise.

Example:
```sql
UPPER(CONCAT(sub.target_city, ABS(sub.order_amount)))
```
Yields: `["sub.order_amount", "sub.target_city"]`

## 9. Concrete Examples

### 9.1 Basic Query
```sql
SELECT customer_id, total_amount AS order_amount FROM orders
```

```json
{
    "id": 0,
    "parent_id": null,
    "node_type": "STATEMENT",
    "children": [
        {
            "id": 1,
            "parent_id": 0,
            "node_type": "ATTRIBUTE",
            "value": "customer_id",
            "table_alias": "orders"
        },
        {
            "id": 2,
            "parent_id": 0,
            "node_type": "ATTRIBUTE",
            "value": "total_amount",
            "alias": "order_amount",
            "table_alias": "orders"
        },
        {
            "id": 3,
            "parent_id": 0,
            "node_type": "TABLE",
            "value": "orders",
            "alias": "",
            "children": [
                {
                    "id": 4,
                    "parent_id": 3,
                    "node_type": "ROOT_ATTRIBUTE",
                    "value": "customer_id"
                },
                {
                    "id": 5,
                    "parent_id": 3,
                    "node_type": "ROOT_ATTRIBUTE",
                    "value": "total_amount"
                }
            ]
        }
    ]
}
```

### 9.2 Union Query
```sql
SELECT customer_id, total_amount AS order_amount FROM orders
UNION
SELECT customer_id, historical_amount AS order_amount FROM archived_orders
```

```json
{
    "id": 0,
    "parent_id": null,
    "node_type": "SET_OPERATOR",
    "value": "UNION",
    "children": [
        {
            "id": 1,
            "parent_id": 0,
            "node_type": "STATEMENT",
            "children": [
                {
                    "id": 2,
                    "parent_id": 1,
                    "node_type": "ATTRIBUTE",
                    "value": "customer_id",
                    "table_alias": "orders"
                },
                {
                    "id": 3,
                    "parent_id": 1,
                    "node_type": "ATTRIBUTE",
                    "value": "total_amount",
                    "alias": "order_amount",
                    "table_alias": "orders"
                },
                {
                    "id": 4,
                    "parent_id": 1,
                    "node_type": "TABLE",
                    "value": "orders",
                    "alias": "",
                    "children": [
                        {
                            "id": 5,
                            "parent_id": 4,
                            "node_type": "ROOT_ATTRIBUTE",
                            "value": "customer_id"
                        },
                        {
                            "id": 6,
                            "parent_id": 4,
                            "node_type": "ROOT_ATTRIBUTE",
                            "value": "total_amount"
                        }
                    ]
                }
            ]
        },
        {
            "id": 7,
            "parent_id": 0,
            "node_type": "STATEMENT",
            "children": [
                {
                    "id": 8,
                    "parent_id": 7,
                    "node_type": "ATTRIBUTE",
                    "value": "customer_id",
                    "table_alias": "archived_orders"
                },
                {
                    "id": 9,
                    "parent_id": 7,
                    "node_type": "ATTRIBUTE",
                    "value": "historical_amount",
                    "alias": "order_amount",
                    "table_alias": "archived_orders"
                },
                {
                    "id": 10,
                    "parent_id": 7,
                    "node_type": "TABLE",
                    "value": "archived_orders",
                    "alias": "",
                    "children": [
                        {
                            "id": 11,
                            "parent_id": 10,
                            "node_type": "ROOT_ATTRIBUTE",
                            "value": "customer_id"
                        },
                        {
                            "id": 12,
                            "parent_id": 10,
                            "node_type": "ROOT_ATTRIBUTE",
                            "value": "historical_amount"
                        }
                    ]
                }
            ]
        }
    ]
}
```

### 9.3 Complex Query with Subquery
```sql
SELECT LENGTH(sub.target_city) AS city_len, c.customer_name
FROM (SELECT shipping_city AS target_city FROM orders) sub
INNER JOIN customers c ON sub.customer_id = c.id
```

```json
{
    "id": 0,
    "parent_id": null,
    "node_type": "STATEMENT",
    "children": [
        {
            "id": 1,
            "parent_id": 0,
            "node_type": "FUNCTION",
            "value": "LENGTH(target_city)",
            "alias": "city_len",
            "inner_attributes": ["sub.target_city"],
            "children": [
                {
                    "id": 2,
                    "parent_id": 1,
                    "node_type": "ATTRIBUTE",
                    "value": "target_city",
                    "table_alias": "sub"
                }
            ]
        },
        {
            "id": 3,
            "parent_id": 0,
            "node_type": "ATTRIBUTE",
            "value": "customer_name",
            "table_alias": "c"
        },
        {
            "id": 4,
            "parent_id": 0,
            "node_type": "SUBSELECT",
            "alias": "sub",
            "children": [
                {
                    "id": 5,
                    "parent_id": 4,
                    "node_type": "ATTRIBUTE",
                    "value": "shipping_city",
                    "alias": "target_city",
                    "table_alias": "orders"
                },
                {
                    "id": 6,
                    "parent_id": 4,
                    "node_type": "TABLE",
                    "value": "orders",
                    "alias": "",
                    "children": [
                        {
                            "id": 7,
                            "parent_id": 6,
                            "node_type": "ROOT_ATTRIBUTE",
                            "value": "shipping_city"
                        }
                    ]
                }
            ]
        },
        {
            "id": 8,
            "parent_id": 0,
            "node_type": "JOIN",
            "value": "INNER JOIN customers AS c ON sub.customer_id = c.id",
            "children": [
                {
                    "id": 9,
                    "parent_id": 8,
                    "node_type": "TABLE",
                    "value": "customers",
                    "alias": "c",
                    "children": [
                        {
                            "id": 10,
                            "parent_id": 9,
                            "node_type": "ROOT_ATTRIBUTE",
                            "value": "customer_name"
                        }
                    ]
                }
            ]
        }
    ]
}
```

## 10. Test Case Format

Tests are stored as TOML files in `tests/test_queries/`. A single file pairs
one `[in]` query with any number of expectation sections:

```toml
[in]
sql = """
SELECT customer_id, total_amount AS order_amount FROM orders
"""

[test_1]
description = "Basic SELECT with single table"
expected = """
{
    "node_type": "STATEMENT",
    ...
}
"""

[roots]
expected = ["orders.customer_id", "orders.total_amount"]

[lineage_1]
description = "Column lineage chains"
expected = """
[ ... ]
"""

[collapse_1]
description = "Collapsed union lineage"
expected = """
[ ... ]
"""
```

### 10.1 Section Types
| Section prefix | Runner | Validates |
|---|---|---|
| `test_N` | `tests/test_ast_tree.py` | Full AST JSON (`SqlTree.to_json()`) |
| `roots` | `tests/test_root_hierarchy_flat.py` | Extracted root attributes (`table.column`) |
| `lineage_*` | `tests/test_lineage.py` | `Lineage.to_json()` chains before collapse |
| `collapse_*` | `tests/test_collapse_lineage.py` | Lineage after `RootHierarchy.collapse_lineage()` |

### 10.2 Test Validation Rules
- Node `id` and `parent_id` are excluded from comparison (dynamic)
- Tests compare normalized trees/chains (dynamic IDs removed)
- See `docs/lineage_pipeline.md` for the lineage and collapse semantics
