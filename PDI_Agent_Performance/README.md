# PDI Agent Performance

Python job that reads agents from `pdi_agent_master`, computes listing performance metrics for each agent, and inserts or updates rows in `pdi_agent_performance`.

Agents are processed **one by one**. Each record is logged with timing and result details.

## Requirements

- Python 3.10+
- MySQL access to `PDI_PortalsData`

## Setup

1. Create and activate a virtual environment:

```powershell
python -m venv env
.\env\Scripts\activate
```

2. Install dependencies:

```powershell
pip install -r requirements.txt
```

3. Create your config file:

```powershell
copy example.config.py config.py
```

4. Edit `config.py` with your database credentials and table settings:

```python
HOST = "your-mysql-host"
USER = "your-user"
PASSWORD = "your-password"
DATABASE = "PDI_PortalsData"
PORT = 3306
POOL_SIZE = 5

# Source schema for CREATE TABLE ... LIKE
MAIN_PERFORMANCE_TABLE = "pdi_agent_performance"

# When True, swap staging table into main on a successful full run
ATOMIC_REPLACE_MAIN_TABLE = False
```

`config.py` is gitignored and should not be committed.

## How to Run

From the project root:

```powershell
python process_agent_performance.py
```

This processes all agents from `pdi_agent_master` in ascending `id` order.

### Common Examples

Process a single agent:

```powershell
python process_agent_performance.py --agent-id 20
```

Process a range of agent IDs:

```powershell
python process_agent_performance.py --start-id 1 --end-id 1000
```

Process in batches:

```powershell
python process_agent_performance.py --limit 500 --offset 0
python process_agent_performance.py --limit 500 --offset 500
```

Compute stats without writing to the database:

```powershell
python process_agent_performance.py --agent-id 20 --dry-run
```

Run a full refresh and atomically promote staging data to the main table:

```powershell
# Set ATOMIC_REPLACE_MAIN_TABLE = True in config.py first
python process_agent_performance.py
```

Log to console only:

```powershell
python process_agent_performance.py --no-log-file
```

Write logs to a custom file:

```powershell
python process_agent_performance.py --log-file logs\my_run.log
```

## CLI Options

| Option | Description |
|--------|-------------|
| `--agent-id ID` | Process only the given `pdi_agent_master.id`. |
| `--start-id ID` | Process agents with `id >= start-id`. |
| `--end-id ID` | Process agents with `id <= end-id`. |
| `--limit N` | Maximum number of agents to process after filters are applied. |
| `--offset N` | Skip the first N agents after filters are applied. |
| `--log-level LEVEL` | Logging verbosity: `DEBUG`, `INFO`, `WARNING`, or `ERROR`. Default: `INFO`. |
| `--log-file PATH` | Log file path. Default: `logs/agent_performance.log`. Rotates at 5 MB, keeps 5 backups. |
| `--no-log-file` | Disable file logging; output goes to console only. |
| `--dry-run` | Run the stats query for each agent but do not create a staging table or write results. |

Options can be combined. For example:

```powershell
python process_agent_performance.py --start-id 100 --end-id 500 --limit 50 --log-level DEBUG
```

## Config Options

| Setting | Default | Description |
|---------|---------|-------------|
| `MAIN_PERFORMANCE_TABLE` | `pdi_agent_performance` | Main table name. Used as the template for staging tables and as the swap target. |
| `ATOMIC_REPLACE_MAIN_TABLE` | `False` | When `True`, atomically swap the staging table into the main table after a successful **full run** (no agent filters). |

### Email notifications

| Setting | Description |
|---------|-------------|
| `EnableMailNotification` | Set `True` to send an HTML email report after every run. |
| `MailTO` | List of recipient email addresses. |
| `MAIL_FROM` | Sender address. |
| `MAIL_HOST` | SMTP server host. |
| `MAIL_PORT` | SMTP port (`465` for SSL, `587` for STARTTLS). |
| `MAIL_USER` | SMTP username. |
| `MAIL_PASS` | SMTP password. |
| `MAIL_SSL` | Use `SMTP_SSL` (typical for port 465). |
| `MAIL_TLS` | Use STARTTLS (typical for port 587). |

The email includes:

- Run status (SUCCESS / FAILED / PARTIAL / DRY RUN)
- Summary counts and elapsed time (totals only — not every record)
- Staging table verification totals queried from the database
- Sample of top 20 agents by listing count (for spot-checking inserted data)
- Sample of up to 20 skipped agents (with total skip count)
- Sample of up to 20 errors (with total error count; all errors are always in the log file)

Full per-agent details for large runs (~65k+ records) are in the **log file** and **staging table**, not in the email.

### Staging table workflow

The script **never writes directly** to `pdi_agent_performance` during processing.

1. Creates a timestamped staging table, for example:
   `pdi_agent_performance_07072026171255`
   using:
   ```sql
   CREATE TABLE PDI_PortalsData.pdi_agent_performance_07072026171255
   LIKE PDI_PortalsData.pdi_agent_performance;
   ```
2. Upserts all computed results into the staging table.
3. On success:
   - If `ATOMIC_REPLACE_MAIN_TABLE = False` (default), the staging table is kept for review.
   - If `ATOMIC_REPLACE_MAIN_TABLE = True` and the run processed **all agents** with no failures, MySQL performs an atomic rename:
     ```sql
     RENAME TABLE
       pdi_agent_performance      TO pdi_agent_performance_bkp,
       pdi_agent_performance_<ts> TO pdi_agent_performance;
     ```
     Any existing `pdi_agent_performance_bkp` table is dropped before the swap.

Atomic swap is **skipped** when:

- `ATOMIC_REPLACE_MAIN_TABLE` is `False`
- Any agent failed during the run
- Partial filters were used (`--agent-id`, `--start-id`, `--end-id`, `--limit`, `--offset`)
- The run was a `--dry-run`

## What the Script Does

For each agent in `pdi_agent_master`:

1. Reads agent details:

```sql
SELECT
    p.id,
    COALESCE(p.agent_master_name, p.agent_name_zl, p.agent_name_rm) AS agent_name,
    COALESCE(p.address_zl, p.address_rm) AS agent_address,
    p.agent_logo
FROM PDI_PortalsData.pdi_agent_master p
```

2. Runs the performance calculation query using Rightmove and Zoopla listing data from the last 365 days.

3. Upserts the result into the timestamped staging table using `agent_master_id` as the unique key.

Updated fields on conflict:

- `agent_name`
- `agent_address`
- `agent_logo`
- `no_of_listings`
- `no_of_live_listings`
- `no_of_sold_listings`
- `no_of_withdrawn_listing`
- `avg_difference_in_percentage`
- `turnaround_days`
- `update_dt`

Agents missing both name and address are skipped. Agents with `no_of_listings = 0` are computed but not inserted into the staging table.

## Logging

Each agent log includes:

- Agent id, name, and address
- Listing counts (total, live, sold, withdrawn)
- Average price difference percentage
- Turnaround days
- Query time and total processing time

At the end of the run, a summary is logged:

```
Job finished | total=100 | succeeded=98 | failed=1 | skipped=1 | elapsed=245.32s
```

The script exits with code `0` on success, `1` if any agent failed, and `130` if stopped with Ctrl+C.

Pressing **Ctrl+C** stops the job gracefully: partial progress is kept in the staging table, atomic swap is skipped, and an email is sent with status **INTERRUPTED**.

## Project Structure

```
PDI_Agent_Performance/
├── process_agent_performance.py   # Entry point — run this script
├── config.py                      # Local credentials (not committed)
├── example.config.py              # Config template
├── requirements.txt
├── README.md
├── logs/                          # Runtime log files
└── app/
    ├── db/
    │   ├── database.py            # MySQL connection pool
    │   ├── staging_report.py      # Staging table verification queries
    │   └── tables.py              # Staging table creation and atomic swap
    ├── models/
    │   └── report.py              # Job report data structures
    ├── notifications/
    │   └── email_notifier.py      # HTML email report builder and sender
    └── sql/
        └── queries.py             # SQL queries for fetch, stats, and upsert
```

## Project Files

| File | Purpose |
|------|---------|
| `process_agent_performance.py` | Main entry point |
| `app/db/database.py` | MySQL connection pool and query execution |
| `app/sql/queries.py` | SQL queries for fetch, stats, and upsert |
| `app/db/tables.py` | Staging table creation and atomic swap |
| `config.py` | Local database credentials (not committed) |
| `example.config.py` | Config template |

## Notes

- Processing is sequential. Large full-table runs can take a long time; use `--start-id`, `--end-id`, `--limit`, and `--offset` to split work across runs. Partial runs write to a staging table but will **not** trigger an atomic swap.
- Use `--dry-run` first when testing changes or validating a single agent.
- Set `ATOMIC_REPLACE_MAIN_TABLE = True` only for full production refreshes with no agent filters.
- Default log file: `logs/agent_performance.log` (rotates at 5 MB, keeps 5 backups: `.log.1` … `.log.5`).
