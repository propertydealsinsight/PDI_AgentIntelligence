# Agent Branch-Wise Listings Export

Export raw property listings and a **Birds Eye View** branch summary for an agent brand. The script connects to the PDI MySQL database, resolves all branch (`agent_master_id`) records for a given agent name, runs the listing query once per branch, and writes results incrementally to Excel.

---

## What it does

1. **Branch lookup** — finds distinct `agent_master_id` values from `PDI_PortalsData.pdi_agent_performance` where `agent_name` matches your search pattern (joined to `pdi_agent_master` for branch details).
2. **Optional branch review** — with `--select-branches`, writes a `branches.csv` for manual editing before export starts.
3. **Per-branch export** — runs the listing query against `pdi_agent_master` for each branch id (Rightmove + Zoopla, sale/new-homes, last 12 months).
4. **Excel output** — appends rows to a `listings` sheet as each branch completes.
5. **Birds Eye View** — after all branches finish, adds a summary tab with per-branch metrics (active, sold/under offer, withdrawn, pricing, geography, and so on).

The export supports **resume**, **retries**, **graceful shutdown** (Ctrl+C / SIGTERM), and **rotating log files** so long-running brand exports can be restarted safely.

---

## Prerequisites

- Python 3.10 or later
- Network access to the PDI MySQL read-only host
- Database credentials with read access to `PDI_PortalsData` (including `pdi_agent_performance` and `pdi_agent_master`)

---

## Setup

From this directory:

```powershell
python -m venv env
.\env\Scripts\Activate.ps1
pip install -r requirements.txt
```

On macOS/Linux, activate with `source env/bin/activate` instead.

Copy the example config and fill in credentials:

```powershell
copy example.config.py config.py
```

Edit `config.py`:

| Setting | Description |
|---|---|
| `HOST` | MySQL host |
| `USER` | Database username |
| `PASSWORD` | Database password |
| `DATABASE` | Default: `PDI_PortalsData` |
| `PORT` | Default: `3306` |

`config.py` is gitignored — do not commit credentials.

### Environment variable overrides

These override `config.py` when set:

| Variable | Maps to |
|---|---|
| `PDI_RO_HOST` | `HOST` |
| `PDI_RO_PORT` | `PORT` |
| `PDI_RO_USER` | `USER` |
| `PDI_RO_PASSWORD` | `PASSWORD` |
| `PDI_RO_DB` | `DATABASE` |

---

## Usage

Basic export (creates a new timestamped run folder under `output/`):

```powershell
python get_branch_wise_listings.py --agent-name "Fine & Country"
```

Review and filter branches before export:

```powershell
python get_branch_wise_listings.py --agent-name "Fine & Country" --select-branches
```

SQL `LIKE` pattern (pass `%` explicitly when needed):

```powershell
python get_branch_wise_listings.py --agent-name "Fine & Country%"
```

Custom output path (bypasses the default timestamp folder):

```powershell
python get_branch_wise_listings.py --agent-name "Fine & Country" --output output/my-run/Fine_and_Country.xlsx
python get_branch_wise_listings.py --agent-name "Fine & Country" --output output/my-run/
```

Resume after interruption or failure:

```powershell
python get_branch_wise_listings.py --agent-name "Fine & Country" --resume
```

Run in the background (Linux/macOS):

```bash
nohup python get_branch_wise_listings.py --agent-name "Fine & Country" > export.log 2>&1 &
```

### CLI options

| Option | Default | Description |
|---|---|---|
| `--agent-name` | *(required)* | Agent name search pattern (`LIKE`; `%` appended if omitted) |
| `--output` | `output/<timestamp>_<AgentName>/<AgentName>_raw_listings.xlsx` | Excel file or output directory |
| `--select-branches` | off | Write `branches.csv`, wait for confirmation, export only remaining rows |
| `--resume` | off | Skip branch ids already completed; reuses latest run folder when `--output` is omitted |
| `--skip-failed` | off | With `--resume`, do not retry previously failed ids |
| `--limit` | `0` (all) | Process only the first N pending branch ids |
| `--max-retries` | `3` | Retries per branch on transient errors |
| `--query-timeout` | `180` | Per-branch query timeout in seconds |
| `--sleep` | `0` | Pause in seconds between branch ids |
| `--skip-birds-eye` | off | Export listings only; skip summary tab |

---

## Branch selection (`--select-branches`)

When enabled, the script pauses before processing listings:

1. Writes `branches.csv` in the run output folder with one row per branch:
   - `agent_master_id`, `agent_name`, `agent_name_rm`, `agent_name_zl`, `address_rm`, `address_zl`
2. Prompts you to open the file, **delete rows** for branches you do not want, and save.
3. Press **Y** to continue (exports only the remaining rows) or **N** to cancel.

Example prompt:

```
Review the branch list before export:
  C:\...\output\2026-07-16_18-38-04_Fine_and_Country\branches.csv

Remove rows for branches you do NOT want to export, save the file,
then press Y to continue or N to cancel.

Continue? [Y/N]:
```

On **`--resume --select-branches`**, the script reads the existing `branches.csv` in that run folder without prompting. Edit the file first if you want to change the selection.

---

## Logging

Logs are written to `logs/get_branch_wise_listings.log` with rotation:

- **5 MB** per file
- **3** backup files (`.log.1`, `.log.2`, `.log.3`)

The same messages also appear on the console. The log file path is printed at the start of each run.

---

## Output

Each new run creates a timestamped folder under `output/` named `<YYYY-MM-DD_HH-MM-SS>_<AgentName>/`.

Example:

```
output/
  2026-07-16_18-38-04_Fine_and_Country/
    branches.csv                                          # with --select-branches
    Fine_and_Country_raw_listings.xlsx
    Fine_and_Country_raw_listings.xlsx.progress
    Fine_and_Country_raw_listings.xlsx.errors.csv
    Fine_and_Country_raw_listings.xlsx.failed_ids.txt
```

With `--resume` and no `--output`, the script continues in the **latest matching run folder** for that agent name.

### Excel workbook

| Sheet | Contents |
|---|---|
| `listings` | One row per listing, with `agent_master_id` as the first column |
| `Birds Eye View` | Per-branch summary plus a **TOTAL** row (regenerated on each successful run) |

Listing columns include portal, address, price, status flags (active / sold-under-offer / withdrawn), sold/withdrawn history dates, and price-paid evidence fields.

### Sidecar files

All sidecar files live alongside the Excel file in the same run folder:

| File | Purpose |
|---|---|
| `<AgentName>_raw_listings.xlsx.progress` | Branch completion log (`id,status,row_count,timestamp`) |
| `<AgentName>_raw_listings.xlsx.errors.csv` | Failed branch details |
| `<AgentName>_raw_listings.xlsx.failed_ids.txt` | One failed `agent_master_id` per line |

### Resume behaviour

With `--resume`, the script skips branch ids that:

- appear in the progress file with status `ok` or `no_match`, or
- already exist in the `listings` sheet of the output workbook.

Failed ids (`error`, `timeout`, `connection_error`, etc.) are retried unless `--skip-failed` is set.

When using `--select-branches`, only branch ids present in `branches.csv` are considered for export.

---

## Birds Eye View summary

The summary tab aggregates listings by `agent_master_id` and includes:

- Branch identity (single agent name and address per branch, with a Portals column showing Rightmove / Zoopla / Both)
- Counts: total, active, sold/under offer, withdrawn (with percentages)
- Recent listings (365-day window), portal split, sale vs new-homes
- Median asking price, listed days, and sold/under-offer averages
- Land Registry and price-paid evidence counts
- Postcode/outcode coverage and publish date range

---

## Project layout

```
Agent_Branch_Wise_Listings/
├── get_branch_wise_listings.py   # Main export script
├── example.config.py             # Config template (copy to config.py)
├── requirements.txt
├── logs/                         # Rotating log files (created at runtime)
├── output/                       # Timestamped run folders (created at runtime)
└── README.md
```

---

## Troubleshooting

**`config.py not found`** — copy `example.config.py` to `config.py` and set credentials.

**`SELECT command denied ... for table 'pdi_agent_performance'`** — the script queries `PDI_PortalsData.pdi_agent_performance`. Confirm your MySQL user has read access to that table from your current IP (Workbench may use a different user or connect via VPN).

**`No agent_master_id found`** — check the agent name pattern.

**`Branch selection requires interactive input`** — `--select-branches` needs a terminal for the Y/N prompt. Run from a shell, not a non-interactive job runner.

**Query timeouts** — increase `--query-timeout` or use `--resume` to continue from where the run stopped.

**Connection drops** — the script reconnects automatically after connection errors; re-run with `--resume` if needed.

**Partial Birds Eye View** — if the run is interrupted, the summary tab is not updated. Re-run with `--resume` to finish remaining branches, or use `--skip-birds-eye` during partial runs and regenerate the summary later once all branches are complete.
