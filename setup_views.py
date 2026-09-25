"""Create the curated views the MCP layer reads.

Design note: with DAB the domain semantics live in SQL, not in application code.
That is deliberate and matches Microsoft's own guidance for the SQL MCP Server -
"we recommend using a view instead of a table" for anything needing joins or
computed logic. It also puts the logic somewhere a bank's data team can review,
version and own, rather than inside a Python file.

The maturity ladder is expressed as a computed `days_to_maturity` column rather
than a parameterised window, so a caller can ask for any horizon with an ordinary
filter (days_to_maturity le 90) instead of needing a bespoke tool.

Idempotent - safe to re-run.

    python setup_views.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from fabric_sql import FabricSqlClient  # noqa: E402

VIEWS: list[tuple[str, str]] = [
    (
        "dbo.vw_maturity_ladder",
        """
CREATE VIEW dbo.vw_maturity_ladder AS
SELECT
    account_number,
    customer_id,
    customer_name,
    relationship_manager,
    branch_code,
    branch_name,
    city,
    country,
    product_code,
    product_name,
    currency,
    balance,
    rate,
    as_of_date,
    matures_on,
    DATEDIFF(day, as_of_date, matures_on) AS days_to_maturity
FROM dbo.vw_deposits
WHERE matures_on IS NOT NULL
  AND matures_on >= as_of_date
""",
    ),
    (
        "dbo.vw_customer_exposure",
        """
CREATE VIEW dbo.vw_customer_exposure AS
SELECT
    customer_id,
    customer_name,
    parent_customer_id,
    segment,
    industry,
    relationship_manager,
    currency,
    product_code,
    product_name,
    SUM(balance)  AS total_balance,
    COUNT_BIG(*)  AS account_count,
    AVG(rate)     AS avg_rate
FROM dbo.vw_deposits
GROUP BY
    customer_id,
    customer_name,
    parent_customer_id,
    segment,
    industry,
    relationship_manager,
    currency,
    product_code,
    product_name
""",
    ),
]


def main() -> None:
    client = FabricSqlClient.from_env()
    conn = client.connect()
    cursor = conn.cursor()
    try:
        for name, ddl in VIEWS:
            bare = name.split(".")[-1]
            cursor.execute(
                "SELECT COUNT(*) FROM sys.views WHERE name = ?", (bare,)
            )
            if cursor.fetchone()[0]:
                cursor.execute(f"DROP VIEW {name}")
                conn.commit()
                print(f"  dropped existing {name}")

            cursor.execute(ddl)
            conn.commit()
            cursor.execute(f"SELECT COUNT(*) FROM {name}")
            print(f"  created {name}  ({cursor.fetchone()[0]:,} rows)")
    finally:
        cursor.close()
        client.close()

    print("\nviews ready")


if __name__ == "__main__":
    main()
