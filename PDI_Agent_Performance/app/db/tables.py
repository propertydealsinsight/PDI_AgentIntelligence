"""Staging and swap helpers for pdi_agent_performance."""

from __future__ import annotations

import logging
from datetime import datetime

from app.db.database import Database

logger = logging.getLogger(__name__)


def staging_suffix() -> str:
    return datetime.now().strftime("%d%m%Y%H%M%S")


def staging_table_name(main_table: str, suffix: str) -> str:
    return f"{main_table}_{suffix}"


def backup_table_name(main_table: str) -> str:
    return f"{main_table}_bkp"


def qualified_table(database: str, table: str) -> str:
    return f"`{database}`.`{table}`"


def table_exists(db: Database, database: str, table: str) -> bool:
    row = db.execute(
        """
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema = %(schema)s
          AND table_name = %(table)s
        LIMIT 1
        """,
        {"schema": database, "table": table},
        fetchone=True,
    )
    return row is not None


def create_staging_table(
    db: Database,
    database: str,
    main_table: str,
    suffix: str,
) -> str:
    staging = staging_table_name(main_table, suffix)
    main_qualified = qualified_table(database, main_table)
    staging_qualified = qualified_table(database, staging)

    db.execute(f"CREATE TABLE {staging_qualified} LIKE {main_qualified}")
    logger.info("Created staging table %s", staging_qualified)
    return staging


def atomic_replace_main_table(
    db: Database,
    database: str,
    main_table: str,
    staging_table: str,
) -> None:
    main_qualified = qualified_table(database, main_table)
    staging_qualified = qualified_table(database, staging_table)
    backup = backup_table_name(main_table)
    backup_qualified = qualified_table(database, backup)

    if table_exists(db, database, backup):
        logger.warning(
            "Dropping existing backup table %s before atomic swap",
            backup_qualified,
        )
        db.execute(f"DROP TABLE {backup_qualified}")

    db.execute(
        f"RENAME TABLE {main_qualified} TO {backup_qualified}, "
        f"{staging_qualified} TO {main_qualified}"
    )
    logger.info(
        "Atomic swap complete | main=%s | backup=%s | previous_main=%s",
        main_qualified,
        backup_qualified,
        staging_qualified,
    )
