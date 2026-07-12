"""Fetch staging table data for post-run verification and the pre-swap gate."""

from __future__ import annotations

from typing import Any

from app.db.database import Database
from app.db.tables import qualified_table

# Pre-swap gate thresholds. Baselines from the 2026-07-10 validated run:
# 23,155 rows, 16 clamped (auction agents), 34% NULL diff%, 0 invariant breaks.
GATE_MIN_ROW_RATIO = 0.90        # staging must have >= 90% of live-table rows
GATE_MAX_CLAMPED = 50            # rows pinned at +/-99.99
GATE_MAX_NULL_DIFF_SHARE = 0.50  # share of agents with NULL diff%


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
            SUM(no_of_excluded_outliers) AS total_excluded_outliers,
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


def run_preswap_gate(
    db: Database,
    database: str,
    staging_table: str,
    main_table: str,
) -> tuple[bool, list[str]]:
    """Anomaly checks that must pass before the staging table may be swapped
    into production. Returns (passed, [failure reasons]). Read-only.

    Rationale per check is in docs/agent_performance_design_and_decisions.md
    (gotchas G4, G10; decision D5).
    """
    staging = qualified_table(database, staging_table)
    main = qualified_table(database, main_table)
    failures: list[str] = []
    try:
        return _run_preswap_checks(db, database, staging_table, staging, main, failures)
    except Exception as exc:  # fail CLOSED: any error blocks the swap
        failures.append(f"gate check errored: {type(exc).__name__}: {exc}")
        return False, failures


def _run_preswap_checks(
    db: Database,
    database: str,
    staging_table: str,
    staging: str,
    main: str,
    failures: list[str],
) -> tuple[bool, list[str]]:

    # 1) both unique keys present (LIKE-copied; losing them silently allows
    #    duplicate (name, addr) rows to reach the consumer JOIN — G10)
    idx = db.execute(
        """
        SELECT DISTINCT INDEX_NAME AS idx_name
        FROM information_schema.statistics
        WHERE table_schema = %(schema)s AND table_name = %(table)s
          AND non_unique = 0
        """,
        {"schema": database, "table": staging_table},
        fetchall=True,
    )
    unique_names = {r["idx_name"] for r in (idx or [])}
    for required in ("idx_unique_id", "idx_pdi_agent_performance_agent_name_agent_address"):
        if required not in unique_names:
            failures.append(f"missing unique index {required} on staging table")

    stats = db.execute(
        f"""
        SELECT
            COUNT(*) AS row_count,
            COUNT(DISTINCT agent_name, agent_address) AS distinct_name_addr,
            SUM(ABS(avg_difference_in_percentage) >= 99.99) AS clamped,
            SUM(avg_difference_in_percentage IS NULL) AS null_diff,
            SUM(COALESCE(no_of_live_listings, 0)
                + COALESCE(no_of_sold_listings, 0)
                + COALESCE(no_of_withdrawn_listing, 0)
                > COALESCE(no_of_listings, 0)) AS invariant_violations
        FROM {staging}
        """,
        fetchone=True,
    )
    live_count_row = db.execute(f"SELECT COUNT(*) AS c FROM {main}", fetchone=True)
    live_count = (live_count_row or {}).get("c") or 0

    row_count = stats["row_count"] or 0

    # 2) row count sanity vs current live table
    if live_count and row_count < GATE_MIN_ROW_RATIO * live_count:
        failures.append(
            f"row count {row_count:,} < {GATE_MIN_ROW_RATIO:.0%} of live {live_count:,}"
        )

    # 3) no duplicate (name, addr) keys
    if row_count and stats["distinct_name_addr"] != row_count:
        failures.append(
            f"duplicate (agent_name, agent_address) keys: "
            f"{row_count - stats['distinct_name_addr']} rows collide"
        )

    # 4) clamping not exploding (a few auction agents are expected)
    if (stats["clamped"] or 0) > GATE_MAX_CLAMPED:
        failures.append(f"{stats['clamped']} rows clamped at +/-99.99 (max {GATE_MAX_CLAMPED})")

    # 5) NULL diff% share within normal range
    if row_count and (stats["null_diff"] or 0) / row_count > GATE_MAX_NULL_DIFF_SHARE:
        failures.append(
            f"NULL diff% share {(stats['null_diff'] or 0) / row_count:.0%} "
            f"> {GATE_MAX_NULL_DIFF_SHARE:.0%}"
        )

    # 6) count invariant
    if (stats["invariant_violations"] or 0) > 0:
        failures.append(
            f"{stats['invariant_violations']} rows violate live+sold+withdrawn <= total"
        )

    return (not failures), failures
