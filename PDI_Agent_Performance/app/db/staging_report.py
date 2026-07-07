"""Fetch staging table data for post-run verification."""

from __future__ import annotations

from typing import Any

from app.db.database import Database
from app.db.tables import qualified_table


def fetch_staging_verification(
    db: Database,
    database: str,
    staging_table: str,
    sample_limit: int = 20,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    table = qualified_table(database, staging_table)

    summary = db.execute(
        f"""
        SELECT
            COUNT(*) AS row_count,
            SUM(no_of_listings) AS total_listings,
            SUM(no_of_live_listings) AS total_live,
            SUM(no_of_sold_listings) AS total_sold,
            SUM(no_of_withdrawn_listing) AS total_withdrawn,
            ROUND(AVG(avg_difference_in_percentage), 2) AS avg_price_diff,
            ROUND(AVG(turnaround_days)) AS avg_turnaround
        FROM {table}
        """,
        fetchone=True,
    )

    sample = db.execute(
        f"""
        SELECT
            agent_master_id,
            agent_name,
            agent_address,
            no_of_listings,
            no_of_live_listings,
            no_of_sold_listings,
            no_of_withdrawn_listing,
            avg_difference_in_percentage,
            turnaround_days
        FROM {table}
        ORDER BY no_of_listings DESC, agent_master_id
        LIMIT %(limit)s
        """,
        {"limit": sample_limit},
        fetchall=True,
    )

    return summary, sample or []
