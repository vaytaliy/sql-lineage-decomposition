# Lineage Pipeline

How a parsed AST (see `ast_requirements_spec.md`) becomes column lineage and,
ultimately, a rendered diagram.

```
SqlTree ──▶ RootHierarchy ──▶ prepare_lineage() ──▶ collapse_lineage()
  AST       union-aware         per-statement         connected chains
            statement tree      chains                (one per root column)
                                                              │
                    Lineage.to_csv() ◀────────────────────────┘
                          │ deduplicated edge-table CSV string
                          ▼
              diagrammer.build_diagram(csv_text) ──▶ PIL Image ──▶ PNG
```

## 1. RootHierarchy (`root_hier.py`)

Wraps the AST STATEMENT/SET_OPERATOR nodes into a binary tree:

- **SET_OPERATOR** nodes get `left`/`right` children — the query blocks
  before and after the operator keyword (within the same scope). Chained
  unions nest on the right.
- **STATEMENT** nodes link through `left` to a nested hierarchy only when
  they contain a SUBSELECT wrapping a SET_OPERATOR; plain subqueries are
  handled inline.
- `get_statement_hierarchy()` on `SqlTree` returns the root `RootHierarchy`.

## 2. Lineage Steps (`lineage.py`)

A `Lineage` is one step in a column's journey, with `next` pointers forming
a chain. Each step has:

| Property | Content |
|---|---|
| `container_name` | `"{node_id}__{STEP_TYPE}__{name}"`, e.g. `5__SUBQUERY__all_tx` |
| `container_type` | `LineageStep`: `ROOT`, `SUBQUERY`, `STATEMENT`, `SET_OPERATOR` |
| `attribute_value` | Column name at this step |
| `function_str` | SQL of the function expression, if the step is a function |

`prepare_lineage()` builds chains per statement: ROOT steps originate from
`ROOT_ATTRIBUTE` leaves, then flow through SUBQUERY/STATEMENT containers up
to the selected output columns.

## 3. Collapse (`RootHierarchy.collapse_lineage()`)

Walks the hierarchy bottom-up and merges branches:

- A **SET_OPERATOR** unions both branches' chains. Right-branch terminal
  heads connect to left-branch heads **by output column position** (SQL
  UNION semantics — names may differ). The left branch defines the union's
  output column names.
- A **STATEMENT above a union-subquery** *stitches*: child chain heads
  connect to this statement's SUBQUERY steps whose attribute matches.
- Result: one connected chain per root column, `ROOT ... -> final output`.

## 4. CSV Export (`Lineage.to_csv`)

Flattens chains to an edge-table string:

```csv
attribute_value,container_name,container_type,function_str,next_attribute_value,next_container_name
customer_id,10__ROOT__active_orders,ROOT,,customer_id,7__STATEMENT__#none#
customer_id,7__STATEMENT__#none#,STATEMENT,,,
```

- One row per step-to-step edge; terminal steps have empty `next_*` columns.
- Rows are **deduplicated by key** `(attribute_value, container_name,
  next_attribute_value, next_container_name)` — first occurrence wins.
- `delimiter` parameter switches to TSV etc.

## 5. Rendering (`diagrammer.py`)

Layered; each stage is independently callable:

| Stage | Function | Output |
|---|---|---|
| Parse | `parse_lineage_csv(csv_text)` | `DiagramData` (typed dataclasses) |
| Layout | `compute_layout(data)` | `Layout` — containers in depth columns by longest-path relaxation |
| Routing | `route_edges(data, layout)` | Collision-free waypoint skeletons (two-pass: reserved corridors, obstacle detours, track allocation) |
| Render | `render_diagram(...)` | PIL `Image` — jump arches over line crossings, rounded corners, arrowheads, one stable color per source container |

Entry points:

```python
img = diagrammer.build_diagram(csv_text)                 # in-memory
diagrammer.build_diagram_from_file("in.csv", "out.png")  # file to file
diagrammer.save(img, "out.png")                          # save existing image
```

CLI: `python diagrammer.py -i lineage.csv -o lineage.png`
