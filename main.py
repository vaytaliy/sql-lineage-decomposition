"""Main module demonstrating SQL AST tree parsing."""

from sql_tree import SqlTree
from ast_node import AstNode
from root_hier import RootHierarchy
from lineage import Lineage
import diagrammer as d

def main() -> None:
    """Parse example SQL and save AST tree to JSON file."""

    #Example_out1
    sql = """
    SELECT 
        all_tx.customer_id,
        all_tx.amount AS revenue,
        c.customer_name
    FROM (
        SELECT customer_id, total_amount AS amount FROM active_orders
        UNION ALL
        SELECT customer_id, historical_amount AS amount FROM archived_orders
    ) all_tx
    INNER JOIN customers c 
        ON all_tx.customer_id = c.id

    UNION ALL

    SELECT 
        b2b.corporate_id AS customer_id,
        b2b.invoice_total AS revenue,
        b2b.company_name AS customer_name
    FROM b2b_invoices b2b
    """

    #create SqlTree instance specify SQL dialect (example uses pyspark)
    #queries with "with" statement get converted to subqueries by sqlglot engine. recursive queries aren't tested
    #lineage only considers attributes that are part of select
    #unions are supported 

    tree = SqlTree(sql=sql, dialect="spark")

    #query decomposed to tree representation 
    print(tree.to_json())

    #if there are unions - they create complex hierarchy
    #what the hierarchy is: left branch node - things before union keyword (but within same scope), right branch node - everything after this union keyword
    hier = tree.get_statement_hierarchy()
    for h in hier:
        print(h.to_json())

    #usage (returns csv):
    lineage_csv = Lineage.to_csv(tree.get_statement_hierarchy().collapse_lineage())

    #Use this tool to build simple image diagram
    img = d.build_diagram(lineage_csv)
    d.save(img, "example_out1.png")

    # Example out 2 
    # This is how multiple unions are represented
    sql = """
    SELECT a, b
    FROM tst1
    UNION ALL 
    SELECT c,d FROM tst1
    UNION ALL
    SELECT k,x FROM tst2
    UNION ALL
    SELECT s,l FROM tst3    
    """
    img = d.build_diagram(
        Lineage.to_csv(
            SqlTree(sql=sql, dialect="spark")
            .get_statement_hierarchy()
            .collapse_lineage()
        )
    )
    d.save(img, "example_out2.png")

    # Example out 3
    sql = """
        SELECT
        LENGTH(sub.target_city) AS city_len,
        sub.order_amount,
        UPPER(CONCAT(sub.target_city, ABS(sub.order_amount))) AS formatted_mix,
        CASE sub.target_city
            WHEN 'New York' THEN sub.customer_id
            ELSE sub.order_amount
        END AS fallback_val,
        c.customer_name
    FROM (
        SELECT
            customer_id,
            total_amount AS raw_amount,
            total_amount AS order_amount,
            CASE
                WHEN CAST(FLOOR(total_amount) AS INT) != 500 THEN UPPER(shipping_city)
                ELSE billing_city
            END AS target_city
        FROM orders
    ) sub
    INNER JOIN customers c
        ON sub.customer_id = c.id
    """
    img = d.build_diagram(
        Lineage.to_csv(
            SqlTree(sql=sql, dialect="spark")
            .get_statement_hierarchy()
            .collapse_lineage()
        )
    )

    
    d.save(img, "example_out3.png")


if __name__ == "__main__":
    main()
