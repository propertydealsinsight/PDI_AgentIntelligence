#!/usr/bin/env python3
"""
Process PDI agent master records and upsert performance metrics.

Reads agents from pdi_agent_master, computes listing statistics per agent,
and inserts or updates rows in a timestamped staging table. Optionally swaps
the staging table into the main pdi_agent_performance table on success.
"""

from __future__ import annotations

import argparse
import logging
import re
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Literal

from app.db.database import Database
from app.db.staging_report import fetch_staging_verification, run_preswap_gate
from app.models.report import (
    FailedRecord,
    JobReport,
    ProcessSummary,
    SkippedRecord,
)
from app.notifications.email_notifier import send_job_report_email
from app.sql.queries import AGENT_STATS_QUERY, FETCH_AGENTS_QUERY, build_upsert_query
from app.db.tables import (
    atomic_replace_main_table,
    create_staging_table,
    qualified_table,
    staging_suffix,
)

try:
    import config
except ImportError as exc:
    raise SystemExit(
        "config.py not found. Copy example.config.py to config.py and set your credentials."
    ) from exc

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 5


@dataclass(frozen=True)
class AgentRecord:
    id: int
    agent_name: str | None
    agent_address: str | None
    agent_logo: str | None


def setup_logging(log_level: str, log_file: Path | None) -> None:
    level = getattr(logging, log_level.upper(), logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                log_file,
                maxBytes=LOG_MAX_BYTES,
                backupCount=LOG_BACKUP_COUNT,
                encoding="utf-8",
            )
        )

    logging.basicConfig(
        level=level,
        format=LOG_FORMAT,
        datefmt=LOG_DATE_FORMAT,
        handlers=handlers,
        force=True,
    )


def build_fetch_query(
    agent_id: int | None,
    start_id: int | None,
    end_id: int | None,
    limit: int | None,
    offset: int | None,
) -> tuple[str, dict[str, Any]]:
    conditions: list[str] = []
    params: dict[str, Any] = {}

    if agent_id is not None:
        conditions.append("p.id = %(agent_id)s")
        params["agent_id"] = agent_id
    if start_id is not None:
        conditions.append("p.id >= %(start_id)s")
        params["start_id"] = start_id
    if end_id is not None:
        conditions.append("p.id <= %(end_id)s")
        params["end_id"] = end_id

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    query = FETCH_AGENTS_QUERY.format(where_clause=where_clause)

    if limit is not None:
        query += "\nLIMIT %(limit)s"
        params["limit"] = limit
    if offset is not None:
        query += "\nOFFSET %(offset)s"
        params["offset"] = offset

    return query, params


def fetch_agents(db: Database, args: argparse.Namespace) -> list[AgentRecord]:
    query, params = build_fetch_query(
        agent_id=args.agent_id,
        start_id=args.start_id,
        end_id=args.end_id,
        limit=args.limit,
        offset=args.offset,
    )
    rows = db.execute(query, params, fetchall=True)
    return [
        AgentRecord(
            id=row["id"],
            agent_name=row["agent_name"],
            agent_address=row["agent_address"],
            agent_logo=row["agent_logo"],
        )
        for row in rows
    ]


def compute_agent_stats(db: Database, agent_id: int) -> dict[str, Any] | None:
    return db.execute(AGENT_STATS_QUERY, {"agent_id": agent_id}, fetchone=True)


def upsert_agent_performance(
    db: Database,
    upsert_query: str,
    agent: AgentRecord,
    stats: dict[str, Any],
) -> None:
    agent_logo = stats.get("agent_logo") or agent.agent_logo
    if agent_logo and len(agent_logo) > 255:
        agent_logo = agent_logo[:255]

    db.execute(
        upsert_query,
        {
            "agent_master_id": agent.id,
            "agent_name": agent.agent_name,
            "agent_address": agent.agent_address,
            "agent_logo": agent_logo,
            "no_of_listings": stats.get("no_of_listings"),
            "no_of_live_listings": stats.get("no_of_live_listings"),
            "no_of_sold_listings": stats.get("no_of_sold_listings"),
            "no_of_withdrawn_listing": stats.get("no_of_withdrawn_listing"),
            "no_of_sold_mature": stats.get("no_of_sold_mature"),
            "no_of_sold_with_soldprice": stats.get("no_of_sold_with_soldprice"),
            "no_of_price_recovered": stats.get("no_of_price_recovered"),
            "no_of_excluded_outliers": stats.get("no_of_excluded_outliers"),
            "avg_difference_in_percentage": stats.get("avg_difference_in_percentage"),
            "turnaround_days": stats.get("turnaround_days"),
        },
    )


def process_agent(
    db: Database,
    upsert_query: str,
    agent: AgentRecord,
    index: int,
    total: int,
) -> Literal["upserted"] | SkippedRecord:
    started_at = time.perf_counter()
    logger = logging.getLogger(__name__)

    logger.info(
        "Processing agent %s/%s | id=%s | name=%r | address=%r",
        index,
        total,
        agent.id,
        agent.agent_name,
        agent.agent_address,
    )

    stats = compute_agent_stats(db, agent.id)
    elapsed_query = time.perf_counter() - started_at

    if not stats:
        logger.warning(
            "No stats returned for agent id=%s | query_time=%.2fs",
            agent.id,
            elapsed_query,
        )
        raise RuntimeError(f"No stats returned for agent id={agent.id}")

    no_of_listings = stats.get("no_of_listings") or 0
    if no_of_listings == 0:
        logger.info(
            "Skipping upsert for agent id=%s | no_of_listings=0 | query_time=%.2fs",
            agent.id,
            elapsed_query,
        )
        return SkippedRecord(
            agent_id=agent.id,
            agent_name=agent.agent_name,
            agent_address=agent.agent_address,
            reason="no_of_listings=0",
            no_of_listings=0,
        )

    upsert_agent_performance(db, upsert_query, agent, stats)
    elapsed_total = time.perf_counter() - started_at

    logger.info(
        "Completed agent id=%s | listings=%s | live=%s | sold=%s | withdrawn=%s | "
        "sold_mature=%s | sold_with_price=%s | recovered=%s | excluded_outliers=%s | "
        "median_diff_pct=%s | turnaround_days=%s | query_time=%.2fs | total_time=%.2fs",
        agent.id,
        stats.get("no_of_listings"),
        stats.get("no_of_live_listings"),
        stats.get("no_of_sold_listings"),
        stats.get("no_of_withdrawn_listing"),
        stats.get("no_of_sold_mature"),
        stats.get("no_of_sold_with_soldprice"),
        stats.get("no_of_price_recovered"),
        stats.get("no_of_excluded_outliers"),
        stats.get("avg_difference_in_percentage"),
        stats.get("turnaround_days"),
        elapsed_query,
        elapsed_total,
    )
    return "upserted"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compute and upsert PDI agent performance metrics."
    )
    parser.add_argument(
        "--agent-id",
        type=int,
        help="Process a single agent master id.",
    )
    parser.add_argument(
        "--start-id",
        type=int,
        help="Process agents with id >= start-id.",
    )
    parser.add_argument(
        "--end-id",
        type=int,
        help="Process agents with id <= end-id.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Maximum number of agents to process.",
    )
    parser.add_argument(
        "--offset",
        type=int,
        help="Skip the first N agents after filters are applied.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity.",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=Path("logs") / "agent_performance.log",
        help="Log file path (rotates at 5 MB, keeps 5 backups).",
    )
    parser.add_argument(
        "--no-log-file",
        action="store_true",
        help="Disable writing logs to a file.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute stats but do not write to the staging table.",
    )
    return parser.parse_args()


def is_full_run(args: argparse.Namespace) -> bool:
    return (
        args.agent_id is None
        and args.start_id is None
        and args.end_id is None
        and args.limit is None
        and args.offset is None
    )


def load_staging_verification(
    db: Database,
    job_report: JobReport,
    database: str,
    staging_table: str | None,
    dry_run: bool,
) -> None:
    if dry_run or staging_table is None:
        return
    try:
        summary, sample = fetch_staging_verification(db, database, staging_table)
        job_report.staging_db_summary = summary
        job_report.staging_db_sample = sample
    except Exception:
        logging.getLogger(__name__).exception(
            "Failed to fetch staging table verification data"
        )


def run_preswap_validation(
    database: str,
    staging_table: str,
    main_table: str,
    sample: int,
    max_red: int,
) -> tuple[bool, str]:
    """Run the RAG validation harness against the STAGING table (drift baseline
    = current live table) BEFORE the swap, so a bad refresh never goes live.

    The job knows the staging name it just created, so no table discovery is
    needed. Blocks the swap (fail closed) if the harness crashes, produces no
    tally, or reports more than `max_red` RED agents in the sample.
    """
    harness = Path(__file__).parent / "validation" / "validate_agent_performance.py"
    cmd = [
        sys.executable, str(harness),
        "--table", f"{database}.{staging_table}",
        "--baseline-table", f"{database}.{main_table}",
        "--sample", str(sample),
    ]
    try:
        res = subprocess.run(
            cmd, capture_output=True, text=True, timeout=7200, encoding="utf-8"
        )
    except subprocess.TimeoutExpired:
        return False, f"pre-swap validation timed out after 2h (sample={sample})"
    except Exception as exc:
        return False, f"pre-swap validation could not run: {type(exc).__name__}: {exc}"

    m = re.search(
        r"GREEN=(\d+)\s+AMBER=(\d+)\s+RED=(\d+)\s+SKIPPED=(\d+)", res.stdout or ""
    )
    if not m:
        tail = (res.stdout or res.stderr or "")[-500:]
        return False, (
            f"pre-swap validation produced no tally (exit {res.returncode}) — "
            f"fail closed. Output tail: {tail!r}"
        )
    green, amber, red, skipped = map(int, m.groups())
    summary = (
        f"GREEN={green} AMBER={amber} RED={red} SKIPPED={skipped} "
        f"(sample={sample}, max_red={max_red})"
    )
    if red > max_red:
        return False, f"validation RED count exceeds threshold: {summary}"
    return True, summary


def main() -> int:
    args = parse_args()
    log_file = None if args.no_log_file else args.log_file
    setup_logging(args.log_level, log_file)

    logger = logging.getLogger(__name__)
    run_started = time.perf_counter()

    port = getattr(config, "PORT", 3306)
    pool_size = getattr(config, "POOL_SIZE", 5)
    main_table = getattr(config, "MAIN_PERFORMANCE_TABLE", "pdi_agent_performance")
    atomic_replace = getattr(config, "ATOMIC_REPLACE_MAIN_TABLE", False)

    job_report = JobReport(
        started_at=datetime.now(),
        database=config.DATABASE,
        main_table=main_table,
        dry_run=args.dry_run,
        atomic_replace_enabled=atomic_replace,
        log_file=str(log_file) if log_file else None,
    )

    db = Database(
        host=config.HOST,
        user=config.USER,
        password=config.PASSWORD,
        database=config.DATABASE,
        port=port,
        pool_size=pool_size,
    )

    staging_table: str | None = None
    upsert_query: str | None = None
    exit_code = 0
    gate_blocked = False

    try:
        logger.info("Starting agent performance job")
        logger.info(
            "Database=%s host=%s port=%s main_table=%s atomic_replace=%s dry_run=%s",
            config.DATABASE,
            config.HOST,
            port,
            main_table,
            atomic_replace,
            args.dry_run,
        )

        agents = fetch_agents(db, args)
        job_report.summary.total = len(agents)

        if not agents:
            logger.warning("No agents found for the given filters.")
            exit_code = 0
        else:
            if not args.dry_run:
                suffix = staging_suffix()
                staging_table = create_staging_table(
                    db,
                    config.DATABASE,
                    main_table,
                    suffix,
                )
                job_report.staging_table = staging_table
                performance_table = qualified_table(config.DATABASE, staging_table)
                upsert_query = build_upsert_query(performance_table)
                logger.info("Writing results to staging table %s", performance_table)

            logger.info("Fetched %s agent(s) to process", job_report.summary.total)

            for index, agent in enumerate(agents, start=1):
                try:
                    if not agent.agent_name or not agent.agent_address:
                        logger.warning(
                            "Skipping agent id=%s due to missing name or address",
                            agent.id,
                        )
                        job_report.add_skipped_sample(
                            SkippedRecord(
                                agent_id=agent.id,
                                agent_name=agent.agent_name,
                                agent_address=agent.agent_address,
                                reason="missing agent name or address",
                            )
                        )
                        job_report.summary.skipped += 1
                        continue

                    if args.dry_run:
                        started_at = time.perf_counter()
                        stats = compute_agent_stats(db, agent.id)
                        elapsed = time.perf_counter() - started_at
                        would_skip = not stats or not stats.get("no_of_listings")
                        logger.info(
                            "Dry run agent id=%s | listings=%s | live=%s | sold=%s | withdrawn=%s | time=%.2fs%s",
                            agent.id,
                            stats.get("no_of_listings") if stats else None,
                            stats.get("no_of_live_listings") if stats else None,
                            stats.get("no_of_sold_listings") if stats else None,
                            stats.get("no_of_withdrawn_listing") if stats else None,
                            elapsed,
                            " | would_skip_upsert" if would_skip else "",
                        )
                        job_report.dry_run_count += 1
                        job_report.summary.succeeded += 1
                        continue

                    outcome = process_agent(
                        db, upsert_query, agent, index, job_report.summary.total
                    )
                    if outcome == "upserted":
                        job_report.upserted_count += 1
                        job_report.summary.succeeded += 1
                    else:
                        job_report.add_skipped_sample(outcome)
                        job_report.summary.skipped += 1
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    job_report.summary.failed += 1
                    job_report.add_failure(
                        FailedRecord(
                            agent_id=agent.id,
                            agent_name=agent.agent_name,
                            agent_address=agent.agent_address,
                            error=f"{type(exc).__name__}: {exc}",
                        )
                    )
                    logger.exception("Failed processing agent id=%s", agent.id)

            if not job_report.interrupted:
                # Read verification stats before atomic swap; after swap the staging
                # table name no longer exists (it becomes the main table).
                load_staging_verification(
                    db,
                    job_report,
                    config.DATABASE,
                    staging_table,
                    args.dry_run,
                )

                if (
                    not args.dry_run
                    and staging_table is not None
                    and job_report.summary.failed == 0
                    and atomic_replace
                ):
                    if is_full_run(args):
                        gate_ok, gate_failures = run_preswap_gate(
                            db, config.DATABASE, staging_table, main_table
                        )
                        if not gate_ok:
                            job_report.atomic_swap_message = (
                                "Atomic swap BLOCKED by pre-swap gate: "
                                + "; ".join(gate_failures)
                                + f". Staging table {staging_table} retained for inspection."
                            )
                            logger.error(
                                "Pre-swap gate FAILED — swap blocked, staging %s retained: %s",
                                qualified_table(config.DATABASE, staging_table),
                                "; ".join(gate_failures),
                            )
                            gate_blocked = True
                        else:
                            logger.info("Pre-swap gate passed — running pre-swap validation")
                            val_sample = getattr(config, "PRESWAP_VALIDATION_SAMPLE", 40)
                            val_max_red = getattr(config, "PRESWAP_VALIDATION_MAX_RED", 4)
                            if val_sample > 0:
                                val_ok, val_msg = run_preswap_validation(
                                    config.DATABASE,
                                    staging_table,
                                    main_table,
                                    val_sample,
                                    val_max_red,
                                )
                            else:
                                val_ok, val_msg = True, "pre-swap validation disabled in config"
                            if not val_ok:
                                job_report.atomic_swap_message = (
                                    f"Atomic swap BLOCKED by pre-swap validation: {val_msg}. "
                                    f"Live table untouched; staging table {staging_table} "
                                    f"retained for inspection."
                                )
                                logger.error(
                                    "Pre-swap validation FAILED — swap blocked, staging %s retained: %s",
                                    qualified_table(config.DATABASE, staging_table),
                                    val_msg,
                                )
                                gate_blocked = True
                            else:
                                logger.info(
                                    "Pre-swap validation passed (%s) — proceeding with atomic swap",
                                    val_msg,
                                )
                                atomic_replace_main_table(
                                    db,
                                    config.DATABASE,
                                    main_table,
                                    staging_table,
                                )
                                job_report.atomic_swap_performed = True
                                job_report.atomic_swap_message = (
                                    f"Atomic swap completed: {staging_table} promoted to "
                                    f"{main_table}. Gate passed; validation: {val_msg}."
                                )
                    else:
                        job_report.atomic_swap_message = (
                            "Atomic swap skipped: partial filters were used."
                        )
                        logger.warning(
                            "ATOMIC_REPLACE_MAIN_TABLE is enabled but partial filters were used; "
                            "staging table %s was left unchanged. Run without filters for a full swap.",
                            qualified_table(config.DATABASE, staging_table),
                        )
                elif (
                    not args.dry_run
                    and staging_table is not None
                    and atomic_replace
                    and job_report.summary.failed > 0
                ):
                    job_report.atomic_swap_message = (
                        f"Atomic swap skipped: {job_report.summary.failed} agent(s) failed."
                    )
                    logger.warning(
                        "Atomic swap skipped because %s agent(s) failed. Staging table %s retained.",
                        job_report.summary.failed,
                        qualified_table(config.DATABASE, staging_table),
                    )
                elif not args.dry_run and staging_table is not None and not atomic_replace:
                    job_report.atomic_swap_message = (
                        "Atomic swap disabled in config. Staging table retained."
                    )
                    logger.info(
                        "Staging table %s retained. Set ATOMIC_REPLACE_MAIN_TABLE=True in config.py "
                        "to swap it into %s on a successful full run.",
                        qualified_table(config.DATABASE, staging_table),
                        qualified_table(config.DATABASE, main_table),
                    )

                exit_code = 1 if (job_report.summary.failed or gate_blocked) else 0
    except KeyboardInterrupt:
        job_report.interrupted = True
        job_report.interrupt_message = (
            "The process was stopped manually with Ctrl+C before all agents were processed."
        )
        job_report.atomic_swap_message = (
            "Atomic swap skipped: process was interrupted."
        )
        exit_code = 130
        logger.warning(
            "Process interrupted by user (Ctrl+C). Shutting down gracefully | "
            "processed=%s/%s | upserted=%s",
            job_report.processed_count,
            job_report.summary.total,
            job_report.upserted_count,
        )
        load_staging_verification(
            db,
            job_report,
            config.DATABASE,
            staging_table,
            args.dry_run,
        )
    except Exception as exc:
        exit_code = 1
        job_report.add_failure(
            FailedRecord(
                agent_id=0,
                agent_name=None,
                agent_address=None,
                error=f"Fatal error: {type(exc).__name__}: {exc}\n{traceback.format_exc()}",
            )
        )
        logger.exception("Fatal error during agent performance job")
    finally:
        job_report.finished_at = datetime.now()
        job_report.elapsed_seconds = time.perf_counter() - run_started
        logger.info(
            "Job finished%s | total=%s | succeeded=%s | failed=%s | skipped=%s | elapsed=%.2fs",
            " (interrupted)" if job_report.interrupted else "",
            job_report.summary.total,
            job_report.summary.succeeded,
            job_report.summary.failed,
            job_report.summary.skipped,
            job_report.elapsed_seconds,
        )
        try:
            send_job_report_email(config, job_report)
        except Exception:
            logger.exception("Failed to send job report email")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
