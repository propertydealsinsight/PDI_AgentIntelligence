# Agent Performance — Design, Decisions, Gotchas & TODO

**Status (2026-07-10): LIVE.** The Python pipeline is the production source of
truth. `PDI_PortalsData.pdi_agent_performance` is the 2026-07-10 run
(23,155 agents), validated GREEN=78 / AMBER=18 / RED=6-free (4 REDs, all
sold-count dedup boundaries with matching price metrics). The old MySQL
event/procedure output is parked at `pdi_agent_performance_legacy_DND`
(do not touch, restore-only). `ATOMIC_REPLACE_MAIN_TABLE=True` — each
successful full run now swaps itself into production automatically.

This document replaces `agent_performance_gaps_and_fixes.md` (deleted
2026-07-10; all N1–N9 Python gaps were fixed, all O1–O7 procedure gaps became
moot when the procedure was decommissioned). Git history has the original.

---

## 1. Key architectural decisions (do not re-litigate without data)

### D1. Two-window ("two-clock") metric
- **Activity counts** (`no_of_listings` / live / sold / withdrawn /
  `turnaround_days`): RECENT window = last **365 days** of
  `first_published_date`.
- **Realised price metric** (`avg_difference_in_percentage`): MATURE window =
  last **24 months**. Land Registry completions lag Sold STC by months;
  a 365-day window yields <1% price coverage, 24 months yields ~60% for
  mature cohorts. Two separate dedup chains prevent a recently-relisted
  property from pointing at an old unregistered sale.

### D2. Sold-vs-ORIGINAL-asking definition
- `sold_price = COALESCE(pdi_ppd_last_transaction_soldPrice, last_transaction_soldPrice)` (Land Registry).
- `asking = listed_price` (ORIGINAL asking). Recovered from `price` **only**
  when `listed_price` is NULL or ≤ £1,000 (the "£1 price-hiding trick").
  If both are placeholders → price is *missing*: the listing stays in every
  count but contributes no value.
- Guard: `txn_date >= first_published_date` (this listing's sale, not a stale
  historical one).
- Headline = **MEDIAN**, never mean.

### D3. Robust MAD outlier fence
Points outside `median ± 3.5 × 1.4826 × MAD` are excluded from the median —
**the value is dropped, the listing stays counted** (`no_of_excluded_outliers`
records how many). Fence only applies with ≥ 5 valid points and MAD > 0.
Tunables: `MAD_K`, `MIN_POINTS_FOR_OUTLIER_FENCE` in `app/sql/queries.py`.

### D4. Naming convention = the consumer join key (critical)
The output table stores `agent_name = COALESCE(agent_name_zl, agent_name_rm)`
and `agent_address = COALESCE(address_zl, address_rm)` — **ZL-first, exactly
matching the join key used by the customer-facing API**
(`PropertyDataDBAPIs/routers/Agent/agentPerformance.py`).

**Do NOT switch to `agent_master_name`**: it is RM-first (see
`p_generate_agent_master` — `COALESCE(rm.agent_name, zl.agent_name)`), differs
from the ZL-first key for ~16k of ~63k master rows, and storing it dropped
API join coverage from 26,491 to 16,901 master-row matches when we measured it
(2026-07-10). `agent_master_name` remains the display label for the
listings-feed world (MarketIntelligenceFeed, dedup procs) — different concern,
unaffected by this pipeline.

### D5. Dual unique keys + last-writer-wins collisions
The table carries `UNIQUE(agent_master_id)` **and**
`UNIQUE(agent_name, agent_address)`. Under ZL-first naming, ~2.6k master rows
collapse onto a shared (name, addr) key (duplicate master entries for the same
branch); the upsert's `ON DUPLICATE KEY UPDATE` resolves them
last-writer-wins — the same semantics the legacy prod table had. Staging tables
are `CREATE TABLE ... LIKE pdi_agent_performance`, so both keys self-propagate
**as long as the live table keeps them**.

### D6. Staging + atomic swap, guarded by TWO pre-swap checks (validate-then-swap)
Runs never write to the live table: the build happens in a timestamped staging
table (~4.5–5h) while live serves traffic untouched; the swap is one atomic
`RENAME TABLE` (milliseconds — there is never an outage or a blank table).
`live → pdi_agent_performance_bkp` (previous `_bkp` is **dropped**),
`staging → live`. Swap is skipped on any failure, partial filter, dry-run, or
interrupt, and additionally blocked (fail-closed) by, in order:

1. **Structural gate** (`run_preswap_gate`, `app/db/staging_report.py`): both
   unique indexes present, row count ≥ 90% of live, zero duplicate (name, addr)
   keys, ≤ 50 clamped rows, NULL-diff% ≤ 50%, zero count-invariant violations;
   any gate error blocks.
2. **Pre-swap RAG validation** (`run_preswap_validation`,
   `process_agent_performance.py`): the job runs the validation harness against
   the STAGING table it just built (it knows the name — no discovery needed),
   with the current live table as drift baseline, on a rotating random sample.
   Blocks if RED agents exceed `PRESWAP_VALIDATION_MAX_RED` (default 4 of a
   40-agent sample), if the harness crashes, or if it produces no tally.
   `PRESWAP_VALIDATION_SAMPLE = 0` disables (gate still runs).

A blocked swap leaves live untouched, retains staging for inspection, emails
the reasons, and exits non-zero. Standalone harness runs (no arguments) default
to validating live vs `_bkp` — for ad-hoc review, not part of the swap flow.

### D7. Provable ingredients, not bare averages
Published per agent: `no_of_sold_mature`, `no_of_sold_with_soldprice`
(coverage), `no_of_price_recovered`, `no_of_excluded_outliers`. A reader can
see "this −3.0% is the median of 6 clean sales out of 65 sold, 2 outliers
excluded".

### D8. Consumers of `pdi_agent_performance` (the full list, verified 2026-07-10)
1. **`PropertyDataDBAPIs/routers/Agent/agentPerformance.py`** — ONE handler,
   TWO routes: public `POST /agent/agent-performance` (external client
   agentchecker.co.uk) and hidden `/internal` (called by the Java backend
   `AgentPerformanceController` as a credit-gate proxy for the PDI UI).
   Joins on (agent_name, agent_address), filters `no_of_sold_listings > 5`,
   excludes `%auction%` names, orders by `avg_difference_in_percentage DESC`.
2. **`PDI4Institutions/PortfolioDashboard/process/p_searchAgentNames.php`** —
   agent autocomplete; only shows rows with `update_dt/create_dt ≤ 15 days`.
3. Angular UI / Java backend consume via the API only — no direct SQL.

Any schema or naming change must keep 1 and 2 working unmodified: agentchecker
has no developers to adapt.

---

## 2. Gotchas (open risks — each needs closing or a conscious "accept")

| # | Gotcha | Why it bites | Close by |
|---|--------|--------------|----------|
| G1 | **Old event must never fire again.** `event_generate_agent_performance` is only DISABLED. If re-enabled it upserts old-definition numbers straight into the new live table — schema-compatible, so it corrupts silently. | Silent data corruption | `DROP EVENT` (and archive `p_generate_agent_performance`) once confident |
| G2 | **`agent_master_id` is NOT stable.** `p_generate_agent_master` rebuilds its table from scratch (INSERT without id + RENAME swap), reassigning ids every regeneration. Performance-table ids are only valid against the master snapshot they were built from. | Any id-based join/reporting silently mismatches after a master rebuild | Never join consumers on `agent_master_id` across snapshots; re-run this pipeline right after any master rebuild |
| G3 | **PHP autocomplete goes blind after 15 days.** It filters `update_dt ≤ 15 days`; the table only refreshes when this job runs. | Feature silently returns zero results | Schedule the job (weekly recommended) — TODO T1 |
| G4 | **Auction agents break the diff% metric.** Guide-price vs hammer-price is structurally shifted (+100–200% is normal, e.g. Bond Wolfe median +171% on 448 points). MAD cannot fix a wholesale-shifted distribution; 16 rows sit clamped at +99.99. The API's `%auction%` name filter misses agents like "Bond Wolfe", which therefore top the DESC ranking. | Misleading customer-facing ranking | Decision pending — TODO T2 |
| G5 | **The bkp slot is rolling.** Every auto-swap DROPS the previous `pdi_agent_performance_bkp`. Only `_legacy_DND` is a durable backup. | Assumed backup may be one run old / gone | Accept, or snapshot before risky changes |
| G6 | **`p_generate_agent_master` loop-1 bug**: `temp_rm_property_signatures` joins RM listings against the master's **ZL** columns (`p.agent_name = ptmp.agent_name_zl` + `WHERE ptmp.agent_name_rm IS NULL`) — the already-matched exclusion is wrong on the RM side of loop 1 (loop 2 has it right). | Wasted work; edge-case duplicate matches in the master | Fix the join columns in the procedure |
| G7 | **Metric ≠ ground truth — now MEASURED** (`validation/calibrate_agent_performance.py`, first run 2026-07-10 over 8 outcodes): (C1) we attribute **137%** of the PPD transaction universe as "sold" — i.e. Sold-STC counts exceed the real market itself (fall-throughs + multi-agency double-credit + window drift); **45%** of real transactions feed diff% with a usable price point. (C2) **47%** of Sold-STC listings 18–30 months old never registered with Land Registry → sold counts ≈ **2× actual completions** (upper bound; includes match failures). (C3) baseline-asking flattery is small — only 1.3% of sampled price points had a history-confirmed earlier higher asking (median 9.9pp when present); tracking-lag blind spot exists (41% of listings first tracked >30d after publish). Also: 34% of agents have no diff%; 16% of those that do rest on <5 points. **Bottom line: "sold" must be presented as "sales agreed", never completions; diff% is defensible for relative ranking, uncalibrated in absolute terms.** | Overclaiming accuracy to customers | Rightmove panel (T3), then decide customer-facing wording |
| G8 | **Validation is self-referential on methodology.** The harness recomputes from the same raw tables with the same definitions — GREEN proves faithful implementation, not truth (see G7). | False confidence | Keep G7 experiments as the external check |
| G9 | **Withdrawn counts are the least trustworthy column** — 'removed'/'archived'/'EXPIRED' conflate real withdrawals with scraper losses and portal cleanups. Turnaround resets on relisting (inherits agents' days-on-market gaming). | Weak columns quietly treated as strong | Document in any customer-facing use; no fix planned |
| G10 | **Two config landmines**: `config.py` must keep `MAIN_PERFORMANCE_TABLE="pdi_agent_performance"` (staging schema is LIKE-copied from it — pointing elsewhere loses the unique keys, resurrecting ~2.6k duplicate rows); and MySQL time-hints must sit immediately after SELECT. | Duplicate rows reach customers via join fan-out | Keep config comments; index check is in the harness's Section A |

---

## 3. TODO actions

| # | Action | Trigger/when |
|---|--------|--------------|
| T1 | **Schedule the pipeline** (weekly; Windows Task Scheduler or cron on the runner box). Without it the table ages out of the PHP autocomplete in 15 days (G3) and metrics go stale. | ASAP — hard deadline ~15 days after 2026-07-10 |
| T2 | **Auction-agent decision** (G4): exclude auction listings from diff%, detect & flag auction agents (e.g. a column consumers can filter on), or accept. Then fix the API's `%auction%` filter accordingly. | Next methodology session |
| T3 | **Calibration** (G7): C1 PPD recall, C2 fall-through bound and C3 asking audit are IMPLEMENTED and first-run 2026-07-10 (`python validation/calibrate_agent_performance.py`; results in G7 and `validation/calibration_*.txt`). REMAINING: (a) fill the Rightmove panel CSV (`validation/rightmove_panel_*.csv`, 20 agents) from public branch pages; (b) decide customer-facing wording for "sold" (agreed vs completed) given the 2× finding; (c) re-run quarterly. | Panel: next manual session; wording: before any accuracy claims |
| T4 | **Drop the old event + archive the old procedure** (G1). Also decide the fate of `db/p_generate_agent_performance_corrected.sql` (parked; still has MEAN-not-MEDIAN, no MAD fence, and the `pdi_procedure_logs` control-key collision — see header comment in that file) and the empty `pdi_agent_performance_proc_v2` table left in the DB. | After 2–3 clean scheduled runs |
| T5 | **Fix `p_generate_agent_master` loop-1 join bug** (G6). | Next master-procedure maintenance |
| T6 | **Investigate exact-0.00 medians**: 1,111 agents (7.3% of those with a diff%) sit at exactly 0.00. Plausible (sold at asking) for solid samples, suspicious for thin ones (original Leese and Gordon case: 0.00 on 11/60 usable points). | With T3 |
| T7 | **Optional richer ingredients**: mean/p25/p75 diff and median turnaround were designed (old §5) but never implemented. Add only if a consumer needs them. | On demand |
| T8 | **If `agent_master_name` should ever become the public display name**, do it as a deliberate API-side change (join key stays ZL-first, add a display column) — never by changing the pipeline's stored `agent_name`. | Only if product asks |

---

## 4. Validation harness (how to re-verify after any change)

`validation/validate_agent_performance.py` — strictly read-only, credentials
from `config.py` (override via `PDI_RO_*` env vars). **No table names needed**:
target defaults to live `pdi_agent_performance`, drift baseline defaults to
`pdi_agent_performance_bkp` (both maintained by the atomic swap). The sample
seed defaults to today's date, so scheduled runs rotate through different
agents/areas/sizes over time and anomalies surface cumulatively.

```bash
python validation/validate_agent_performance.py --sample 100 --email   # scheduled weekly run
python validation/validate_agent_performance.py --agent-id 6209        # one agent, deep-dive
python validation/validate_agent_performance.py --seed 20260710 ...    # reproduce a past sample
python validation/validate_agent_performance.py --table PDI_PortalsData.<t>  # ad-hoc table
```

- **Section A**: whole-table health (clamping, nulls, invariants,
  live+sold+withdrawn ≤ total) + week-over-week drift vs the `_bkp` baseline.
- **Section B**: independent per-agent recompute from raw
  `property_details`/`property_details_zoopla` via exact `pdi_agent_master`
  pairs, RAG-scored (exit code 0/1/2 = GREEN/AMBER/RED; any RED ⇒ 2).
- Every run prints its seed — rerun with `--seed <n>` to reproduce exactly.
- HTML report auto-written to `validation/report_*.html`; keep only the latest
  few.

Remember G8: a GREEN harness validates implementation, not truth.
