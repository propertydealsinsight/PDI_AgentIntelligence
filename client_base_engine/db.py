"""Read-only database access for the client-base engine.

The login comes only from Nitin's approved runner (~/.claude/tools/rodb.py, which reads the approved AI access
file); this module never sees or prints a credential and never imports a config module. Every statement must be a
SELECT carrying /*+ MAX_EXECUTION_TIME(n) */ immediately after SELECT, with n no higher than MAX_CAP_MS (under 60s).
Queries run one at a time on one connection, with a short pause between them, and each one is timed and logged.

A query that hits its time cap is never retried as-is. Callers that pass a list of keys (fetch_chunked) get the chunk
split in half and re-run as two smaller queries, down to a minimum chunk size; below that the chunk is skipped and
recorded in the run log, so a slow moment on production never escalates into a heavier query.
"""
from __future__ import annotations

import logging
import os
import re
import sys
import time
from typing import Any, Callable, Iterable, Sequence

import pymysql

log = logging.getLogger("client_base_engine.db")

MAX_CAP_MS = 55_000
DEFAULT_CAP_MS = 30_000
_CAPPED = re.compile(r"^\s*SELECT\s*/\*\+\s*MAX_EXECUTION_TIME\((\d+)\)\s*\*/", re.I)
_WRITE = re.compile(r"\b(INSERT|UPDATE|DELETE|REPLACE|CREATE|ALTER|DROP|TRUNCATE|GRANT|REVOKE|CALL|LOAD|SET)\b", re.I)
_TIMEOUT_CODES = {3024, 1317, 2013}   # max execution time exceeded, query interrupted, lost connection mid-query


def hint(ms: int = DEFAULT_CAP_MS) -> str:
    """The optimizer hint to put straight after SELECT."""
    return f"/*+ MAX_EXECUTION_TIME({min(int(ms), MAX_CAP_MS)}) */"


def check_sql(sql: str) -> None:
    """Refuse anything that is not a time-capped SELECT."""
    m = _CAPPED.match(sql)
    if not m:
        raise ValueError("query must start with SELECT /*+ MAX_EXECUTION_TIME(n) */")
    if int(m.group(1)) > MAX_CAP_MS:
        raise ValueError(f"MAX_EXECUTION_TIME above {MAX_CAP_MS} ms is not allowed")
    body = re.sub(r"'[^']*'", "''", sql)          # ignore words inside string literals
    if _WRITE.search(body):
        raise ValueError("only read statements are allowed")


class QueryTimeout(Exception):
    pass


class ReadOnlyDB:
    """One sequential read-only connection with timing, pacing and a query log."""

    def __init__(self, sleep_s: float = 0.3, connect: Callable[[], Any] | None = None):
        self.sleep_s = sleep_s
        self._connect = connect or _approved_connect
        self._conn = None
        self.timings: list[dict] = []

    def _connection(self):
        if self._conn is None:
            self._conn = self._connect()
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    def fetch(self, sql: str, params: Sequence | None = None, label: str = "") -> list[dict]:
        check_sql(sql)
        if self.timings and self.sleep_s:
            time.sleep(self.sleep_s)
        t0 = time.time()
        try:
            with self._connection().cursor(pymysql.cursors.DictCursor) as cur:
                cur.execute(sql, tuple(params or ()))
                rows = list(cur.fetchall())
        except pymysql.err.OperationalError as e:
            secs = round(time.time() - t0, 2)
            code = e.args[0] if e.args else None
            self.timings.append({"label": label, "seconds": secs, "rows": None, "error": f"OperationalError {code}"})
            log.warning("query %s failed after %.1fs: %s", label, secs, code)
            self.close()
            if code in _TIMEOUT_CODES:
                raise QueryTimeout(label) from e
            raise
        secs = round(time.time() - t0, 2)
        self.timings.append({"label": label, "seconds": secs, "rows": len(rows)})
        log.info("query %-38s %6.2fs %7d rows", label, secs, len(rows))
        return rows

    def fetch_chunked(self, build: Callable[[list], tuple[str, Sequence]], keys: Iterable, chunk: int,
                      label: str, min_chunk: int = 10) -> tuple[list[dict], list]:
        """Run build(chunk_keys) -> (sql, params) over the keys in chunks. Returns (rows, skipped_keys)."""
        keys = list(keys)
        rows: list[dict] = []
        skipped: list = []
        queue = [keys[i:i + chunk] for i in range(0, len(keys), chunk)]
        n = 0
        while queue:
            part = queue.pop(0)
            n += 1
            sql, params = build(part)
            try:
                rows += self.fetch(sql, params, label=f"{label}[{n}:{len(part)}]")
            except QueryTimeout:
                if len(part) > min_chunk:
                    half = len(part) // 2
                    queue[:0] = [part[:half], part[half:]]
                    log.warning("%s: chunk of %d hit its cap, splitting in two", label, len(part))
                else:
                    skipped += part
                    log.warning("%s: chunk of %d hit its cap at minimum size, skipped", label, len(part))
        return rows, skipped


def _approved_connect():
    """Connection from the approved read-only runner; the credential never passes through this code."""
    tools = os.path.expanduser(os.path.join("~", ".claude", "tools"))
    if tools not in sys.path:
        sys.path.insert(0, tools)
    from rodb import conn  # noqa: E402  (approved runner; reads only the approved AI access file)
    return conn()
