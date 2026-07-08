# Agent Performance — Gaps, Fixes & Validation

**Status:** the new Python pipeline is **NOT approved for activation.** Keep the
live MySQL event/procedure (`p_generate_agent_performance`) running until the
items below are fixed and the numbers are validated with the harness in
[`validation/validate_agent_performance.py`](../validation/validate_agent_performance.py).

**Audience:** the developer owning `process_agent_performance.py` /
`app/sql/queries.py` and the `p_generate_agent_performance` procedure.

---

## 0. TL;DR

We have **two pipelines** writing an agent-performance table with the **same
column names but different definitions and different bugs**:

| | Old (live prod) | New (this Python project) |
|---|---|---|
| Table | `PDI_PortalsData.pdi_agent_performance` | `..._<ddmmyyyy>` |
| Trigger | MySQL event → `p_generate_agent_performance` | `process_agent_performance.py` |
| Agents | **40,217** | **25,813** |
| `avg_difference_in_percentage` | bounded `[-30, +28]` by a hard filter | `[-99.99, +43]`, **clamped** by column type |
| Mean turnaround | 108 days | 69 days |

**They disagree on the same 15,205 shared agents:** sold count differs for **67%
of them** (avg gap 20.6, up to 10×), turnaround by ~30 days on average. At least
one is badly wrong; in practice **both are**, and today **nothing proves either.**
That lack of provability is the real problem — everything is collapsed into an
average with no supporting counts.

**Decision (agreed):** `avg_difference_in_percentage` means **"sold vs asking",
using the Land-Registry sold price** — `(sold_price − asking) / asking`. Anomalies
(the "£3,000 → £1" price-hiding trick and statistical outliers) must be **excluded
from the calculation but the listing must still be counted.** Use a **robust,
relative** rule, not an absolute ±30 band.

---

## 1. Decided metric definitions (pin these down first)

Everything downstream depends on these. Document them in code.

- **`avg_difference_in_percentage` = sold-vs-ORIGINAL-asking:**
  `sold_price = COALESCE(pdi_ppd_last_transaction_soldPrice, last_transaction_soldPrice)` (Land Registry),
  `asking = listed_price` (the **original** asking; recovered from `price` **only**
  when `listed_price` is missing/a placeholder — the £1 trick),
  `pct = (sold_price − asking) / asking * 100`.
  **Only counted when the sale was registered after the listing went live**
  (`transaction_date >= first_published_date`) — otherwise you are comparing the
  original asking to an unrelated historical sale. Report the **median** as the
  headline (robust), not the mean. Note: even the old proc used `price` (current
  asking) here, not `listed_price` — so the corrected baseline is stronger than
  both prior pipelines.
- **`turnaround_days`** — decide and document **which event** it measures:
  time-to-**under-offer** (status-change date, available immediately) or
  time-to-**completion** (Land-Registry date, lags ~2 months). The two tables
  currently use different ones — hence 108 vs 69 days. Pick one.
- **`no_of_sold_listings` / live / withdrawn / total** — one agreed dedup key and
  one set of status definitions, shared by both counting and the average.

---

## 2. Gaps in the NEW Python pipeline (`app/sql/queries.py`)

| # | Gap | Where | Impact | Fix |
|---|---|---|---|---|
| N1 | **Wrong metric** — measures in-listing *asking reduction* (`price` vs `listed_price`), not sold-vs-asking | `queries.py:241,261` | Not the metric we want; irreconcilable with prod | Rewrite to the Land-Registry sold-vs-asking definition (§1) |
| N2 | **Wrong denominator** — divides by `price` (current), not asking | `queries.py:241,261` | £1 price → ÷1 → ~‑299,900% | Divide by `asking` (recovered) |
| N3 | **No anomaly handling** — £1 "hidden" prices flow straight in | whole `T_RAW_STATS` | Poisons the average | Price-recovery + robust outlier exclusion (§4) |
| N4 | **Column clamps** — `avg_difference_in_percentage decimal(4,2)` (±99.99) | table DDL | 8 agents already pinned at ‑99.99 | Widen to `decimal(6,2)` and/or store median |
| N5 | **Fabricated zeros** — `COALESCE(listed_price, price)` makes diff = 0 when the reference is missing | `queries.py:241,261` | Mean pulled toward 0; fake "sold at asking" | Never coalesce a missing value into 0 — exclude it (keep the listing counted) |
| N6 | **No transaction-recency guard** | `T_RAW_STATS` | Would compare asking to unrelated old sales | Require `txn_date >= first_published_date` (the proc already does this) |
| N7 | **Sold-count divergence** from independent recompute (~9 avg on mid-tier, e.g. **EweMove 10 vs 110**) | `T_dedup*` | Counts not trustworthy | Nail one dedup key; validate with harness |
| N8 | **Opaque average** — one number, no supporting counts | `T_STATS` | Unprovable | Add ingredient columns (§5) |
| N9 | **Post-swap verification bug** — verifies the staging table by its pre-swap name after the atomic rename | `process_agent_performance.py:497` | Benign `1146` error, 3× retry, missing report block | Verify against `main_table` when `job_report.atomic_swap_performed` |

## 3. Gaps in the OLD procedure (`p_generate_agent_performance`)

| # | Gap | Impact | Fix |
|---|---|---|---|
| O1 | **Absolute outlier cutoff** `difference BETWEEN -30 AND 30 AND turnaround_days > 45` **drops the whole listing** (out of the sold count too) | Undercounts sold; hard-coded band | Robust relative exclusion that removes only the *value*, keeps the listing counted (§4) |
| O2 | **`turnaround_days > 45` excludes fast sales** from the average | Turnaround biased upward (mean 108) | Remove the arbitrary floor; use robust filtering |
| O3 | Turnaround anchored on **completion date** (`last_transaction_date`) | Lags reality | Decide event per §1 |
| O4 | Same **`decimal(4,2)`** clamp risk (masked only because of the ±30 filter) | Fragile | Widen column |
| O5 | Does **not populate** `no_of_listings` / `no_of_withdrawn_listing` | Schema/behaviour drift vs new table | Align columns |
| O6 | Denominator is `price` (current) → same £1 vulnerability (hidden by the ±30 filter, at the cost of O1) | Anomalies silently dropped | Price recovery (§4) |
| O7 | **40,217 agents** vs 25,813 — likely stale agents accumulated via `ON DUPLICATE KEY` without clearing de-listed agents | Inflated agent universe | Confirm and prune |

## 4. Robust anomaly & outlier handling (the core ask)

Two **separate** problems — handle them differently, and in both cases **keep the
listing in every count; exclude only the value from the average.**

1. **Bad price (data error / the £1 hiding trick).** The original asking survives in
   `listed_price`; the true sale survives in the Land-Registry `sold_price`.
   - Baseline is `listed_price` (original asking). If `listed_price` is missing or
     ≤ £1,000, **recover** it from `price`.
   - If both are ≤ £1,000 (or asking ≤ 0), mark the price **missing** → keep the
     listing counted but exclude it from the % average.
2. **Genuine statistical outlier.** Replace the absolute ±30 with a **robust,
   relative** rule computed per peer group (e.g. outcode × property type):
   keep points within **median ± 3.5 × (1.4826 × MAD)** (median absolute
   deviation) *or* winsorize to `[p5, p95]`. MAD/percentiles are immune to the
   extreme values that break a mean; a fixed ±30 is not.

Headline = **median**. The harness implements exactly this recipe as the
reference recomputation (`recompute()` in the harness).

**Coverage caveat (found during validation):** most sold listings have **no
usable Land-Registry sold price** with `txn_date >= first_published` (the harness
reported e.g. 59 of 65, 129 of 161 excluded for that reason — completion data
lags and is sparse). So sold-vs-asking is only computable for a **minority** of
sales. The metric **must publish its coverage** (`n_used_in_diff / n_sold`) or a
tiny, unrepresentative sample will masquerade as the agent's whole performance.

## 5. Make it provable (stop shipping bare averages)

Store the **ingredients**, not just the answer, so any number can be audited:

```
n_listings, n_live, n_sold, n_withdrawn,
n_used_in_diff,        -- how many sold listings actually had a usable price (coverage)
n_price_recovered,     -- placeholder/£1 prices recovered from listed_price
n_excluded_outliers,   -- statistical outliers removed from the average
median_diff_pct,       -- headline (robust)
mean_diff_pct, p25_diff_pct, p75_diff_pct,
turnaround_median_days
```

Then a reader can see *"this −3.0% is the median of 6 clean sales out of 65 sold,
2 outliers excluded"* — instead of an opaque, un-checkable average.

**Invariants** the pipeline should self-assert (and the harness checks):
`live + sold + withdrawn ≤ total`; `n_used_in_diff ≤ n_sold`;
`|median| ≤ p95`; **no clamped values** (`|value| < column max`); and the two
tables reconcile (or the difference is explained).

## 6. Validation harness

[`validation/validate_agent_performance.py`](../validation/validate_agent_performance.py)
— strictly **read-only** (SELECT only, read-only DB account). It:

- **Section A** — table health & cross-table divergence (clamping, nulls,
  invariant violations, prod-vs-new gaps).
- **Section B** — **independent recomputation** from raw `property_details` /
  `property_details_zoopla` per the decided definition (with the §4 robust
  handling), shown side-by-side: `P` (prod) / `N` (new) / `RE` (recompute).

Run:

```bash
set PDI_RO_PASSWORD=********           # read-only account; nothing secret is committed
python validation/validate_agent_performance.py --sample 40      # mid-tier agents
python validation/validate_agent_performance.py --agent-id 132   # one agent, verbose
python validation/validate_agent_performance.py --top            # largest agents (heavy)
```

Needs read-only `SELECT` on `property_details`, `property_details_zoopla`,
`pdi_agent_master`, and both result tables. For an **exact** turnaround
recompute it also needs `property_details_history` /
`property_details_zoopla_history` (currently no read-only grant — turnaround `RE`
is approximated from the transaction date until then; treat it as directional).

## 7. Deployment sequencing (do not skip)

1. **Do not** activate the Python pipeline yet. Keep the event enabled.
2. Fix N1–N9 and O1–O7; agree §1 definitions; add §5 columns.
3. Re-run the harness until Section B gaps are small and explained, and Section A
   shows no clamping and no invariant violations.
4. Only then swap the source of truth — and when you do, mind the **schema drift**
   (the new table adds `agent_master_id` unique key, `no_of_listings`,
   `no_of_withdrawn_listing`; the old one lacks them) and disable the event:
   `ALTER EVENT event_generate_agent_performance DISABLE;`
