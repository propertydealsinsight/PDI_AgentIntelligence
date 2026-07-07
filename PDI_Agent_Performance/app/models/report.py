"""Data structures for job run reporting."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class ProcessSummary:
    total: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0


@dataclass
class UpsertedRecord:
    agent_id: int
    agent_name: str | None
    agent_address: str | None
    no_of_listings: int | None
    no_of_live_listings: int | None
    no_of_sold_listings: int | None
    no_of_withdrawn_listing: int | None
    avg_difference_in_percentage: float | None
    turnaround_days: int | None
    query_time: float
    total_time: float


@dataclass
class SkippedRecord:
    agent_id: int
    agent_name: str | None
    agent_address: str | None
    reason: str
    no_of_listings: int | None = None


@dataclass
class FailedRecord:
    agent_id: int
    agent_name: str | None
    agent_address: str | None
    error: str


@dataclass
class DryRunRecord:
    agent_id: int
    agent_name: str | None
    agent_address: str | None
    no_of_listings: int | None
    no_of_live_listings: int | None
    no_of_sold_listings: int | None
    no_of_withdrawn_listing: int | None
    would_skip_upsert: bool
    elapsed: float


@dataclass
class JobReport:
    started_at: datetime
    database: str
    main_table: str
    dry_run: bool
    atomic_replace_enabled: bool
    finished_at: datetime | None = None
    elapsed_seconds: float = 0.0
    staging_table: str | None = None
    atomic_swap_performed: bool = False
    atomic_swap_message: str | None = None
    summary: ProcessSummary = field(default_factory=ProcessSummary)
    upserted_count: int = 0
    dry_run_count: int = 0
    skipped_sample: list[SkippedRecord] = field(default_factory=list)
    failures: list[FailedRecord] = field(default_factory=list)
    staging_db_summary: dict[str, Any] | None = None
    staging_db_sample: list[dict[str, Any]] = field(default_factory=list)
    log_file: str | None = None
    interrupted: bool = False
    interrupt_message: str | None = None

    # Caps for in-memory samples stored during the run (email shows subsets of these)
    MAX_SKIPPED_SAMPLE = 50
    MAX_FAILURE_SAMPLE = 100

    @property
    def processed_count(self) -> int:
        return self.summary.succeeded + self.summary.skipped + self.summary.failed

    @property
    def status_label(self) -> str:
        if self.interrupted:
            return "INTERRUPTED"
        if self.dry_run:
            return "DRY RUN"
        if self.summary.failed > 0:
            return "FAILED" if self.summary.succeeded == 0 else "PARTIAL"
        if self.summary.succeeded == 0 and self.summary.skipped == self.summary.total:
            return "COMPLETED (ALL SKIPPED)"
        return "SUCCESS"

    def add_skipped_sample(self, record: SkippedRecord) -> None:
        if len(self.skipped_sample) < self.MAX_SKIPPED_SAMPLE:
            self.skipped_sample.append(record)

    def add_failure(self, record: FailedRecord) -> None:
        if len(self.failures) < self.MAX_FAILURE_SAMPLE:
            self.failures.append(record)
