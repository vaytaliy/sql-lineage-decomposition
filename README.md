# SQL column-lineage builder
## Requirements

- Python 3.11+
- `pip install sqlglot pillow`

## Quick start

```python
from sql_tree import SqlTree
from lineage import Lineage
import diagrammer as d

sql = """
SELECT
    all_tx.customer_id,
    all_tx.amount AS revenue
FROM (
    SELECT customer_id, total_amount AS amount FROM active_orders
    UNION ALL
    SELECT customer_id, historical_amount AS amount FROM archived_orders
) all_tx
"""

tree = SqlTree(sql=sql, dialect="spark")
lineage_csv = Lineage.to_csv(tree.get_statement_hierarchy().collapse_lineage())

img = d.build_diagram(lineage_csv)
d.save(img, "lineage.png")
```

The intermediate CSV is a plain edge table — inspect it, diff it, or feed it
from anywhere else:

```csv
attribute_value,container_name,container_type,function_str,next_attribute_value,next_container_name
customer_id,10__ROOT__active_orders,ROOT,,customer_id,7__STATEMENT__#none#
total_amount,10__ROOT__active_orders,ROOT,,amount,7__STATEMENT__#none#
amount,7__STATEMENT__#none#,STATEMENT,,revenue,1__STATEMENT__#none#
revenue,1__STATEMENT__#none#,STATEMENT,,,
```

Rows are unique by `(attribute_value, container_name, next_attribute_value,
next_container_name)`; terminal steps leave the `next_*` columns empty.

## CLI

Render an existing lineage CSV without writing code:

```bash
python diagrammer.py -i lineage.csv -o lineage.png
```

## Inspecting the AST

```python
tree = SqlTree(sql="SELECT o.customer_id FROM orders o", dialect="spark")
print(tree.to_json())
```

```json
{
    "id": 0,
    "parent_id": null,
    "node_type": "STATEMENT",
    "children": [
        {"id": 1, "parent_id": 0, "node_type": "ATTRIBUTE",
         "value": "customer_id", "table_alias": "o"},
        {"id": 2, "parent_id": 0, "node_type": "TABLE",
         "value": "orders", "alias": "o", "children": [
            {"id": 3, "parent_id": 2, "node_type": "ROOT_ATTRIBUTE",
             "value": "customer_id"}
        ]}
    ]
}
```

## SQL support & limitations

- Dialects: anything sqlglot supports (`spark`, `postgres`, `snowflake`, ...)
- `UNION` / `UNION ALL` (including chained) — each branch becomes a separate
  container in the diagram
- Subqueries, joins, aliases, `CASE`, casts and function expressions are traced
- `WITH` (CTEs) are converted to subqueries by sqlglot; recursive CTEs are
  not tested
- Only attributes appearing in the `SELECT` list are traced

## Development

```bash
python -m unittest discover -s tests   # run the test suite
python -m pylint <module>              # lint gate: 10/10 expected
```

Tests are data-driven: SQL cases live in `tests/test_queries/*.toml` with
expected lineage/collapse JSON.