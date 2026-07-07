"""MySQL connection helpers for PDI agent performance processing."""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Any, Generator, Mapping

import mysql.connector
from mysql.connector import MySQLConnection, pooling

logger = logging.getLogger(__name__)


class Database:
    """Thin wrapper around a MySQL connection pool."""

    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        database: str,
        port: int = 3306,
        pool_size: int = 5,
    ) -> None:
        self._pool = pooling.MySQLConnectionPool(
            pool_name="pdi_agent_performance_pool",
            pool_size=pool_size,
            pool_reset_session=True,
            host=host,
            user=user,
            password=password,
            database=database,
            port=port,
            autocommit=False,
            connection_timeout=30,
        )

    @contextmanager
    def connection(self) -> Generator[MySQLConnection, None, None]:
        conn = self._pool.get_connection()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def cursor(self, dictionary: bool = True) -> Generator[Any, None, None]:
        with self.connection() as conn:
            cursor = conn.cursor(dictionary=dictionary)
            try:
                yield cursor
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                cursor.close()

    def execute(
        self,
        query: str,
        params: Mapping[str, Any] | tuple[Any, ...] | None = None,
        *,
        fetchone: bool = False,
        fetchall: bool = False,
        max_retries: int = 3,
    ) -> Any:
        last_error: Exception | None = None

        for attempt in range(1, max_retries + 1):
            try:
                with self.cursor() as cursor:
                    cursor.execute(query, params or ())
                    if fetchone:
                        return cursor.fetchone()
                    if fetchall:
                        return cursor.fetchall()
                    return cursor.rowcount
            except mysql.connector.Error as exc:
                last_error = exc
                logger.warning(
                    "Database operation failed (attempt %s/%s): %s",
                    attempt,
                    max_retries,
                    exc,
                )
                if attempt < max_retries:
                    time.sleep(min(2 ** attempt, 10))

        assert last_error is not None
        raise last_error
