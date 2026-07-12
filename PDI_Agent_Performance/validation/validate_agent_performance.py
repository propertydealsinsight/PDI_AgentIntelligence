"""
Read-only validation harness for the agent-performance tables (RAG mailshot).

WHY THIS EXISTS
---------------
Ongoing quality watch on the LIVE pdi_agent_performance table (populated weekly
by process_agent_performance.py, which has its own pre-swap gate for structural
checks). This harness does the deeper, sampled work:

  Section A: whole-table health + week-over-week drift vs the _bkp table that
             the atomic swap maintains (no table names needed).
  Section B: independent recompute of a rotating random sample of agents from
             the raw listing tables, RAG-scored against the stored values.

It exits with code 0=GREEN, 1=AMBER, 2=RED so it integrates cleanly with cron.

METRIC DEFINITION (two-window / two-clock)
------------------------------------------
Activity counts (no_of_listings / live / sold / withdrawn / turnaround):
    RECENT window — listings with first_published_date in last COUNT_LOOKBACK_DAYS.

Realised sold-vs-asking (avg_difference_in_percentage):
    MATURE window — listings with first_published_date in last PRICE_LOOKBACK_MONTHS.
    Land-Registry data lags Sold STC by months; a 365d window yields <1% coverage
    while 24mo yields ~60% for mature cohorts.  Two separate dedup chains prevent
    a recently-relisted property from pointing at an old unregistered sale.

    asking     = listed_price (ORIGINAL asking); recovered from price only if
                 listed_price is NULL or a placeholder (<=£1,000).
    sold_price = COALESCE(pdi_ppd_last_transaction_soldPrice, last_transaction_soldPrice)
    pct        = (sold_price - asking) / asking * 100
    Only when txn_date >= first_published_date  (this listing's sale, not a stale one).
    Anomalies excluded from the median via robust MAD fence; listing still counted.
    Headline = MEDIAN of valid points (robust to £1 price-hiding trick).

    Coverage columns published:
        n_sold_mature     -- sold in the 24-month window (denominator)
        n_pricepoints     -- of those, how many had a usable Land-Registry price
        n_price_recovered -- of those, how many had asking recovered from placeholder

USAGE
-----
    python validation/validate_agent_performance.py --sample 40
    python validation/validate_agent_performance.py --agent-id 132
    python validation/validate_agent_performance.py --sample 20 --email
    python validation/validate_agent_performance.py --sample 50 --seed 99 --region SW

HTML report is always written to validation/report_YYYYMMDD_HHMMSS_<table>.html automatically.

Credentials are read from config.py (HOST/USER/PASSWORD/DATABASE) by default.
Override any value with env vars: PDI_RO_HOST, PDI_RO_PORT, PDI_RO_USER, PDI_RO_PASSWORD, PDI_RO_DB.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import statistics
import sys
from collections import Counter
from datetime import date, datetime
from typing import Any

# Force UTF-8 output on Windows so Unicode chars in output don't crash
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import mysql.connector

# --------------------------------------------------------------------------- #
# Two-window constants (must match app/sql/queries.py tunables)
# --------------------------------------------------------------------------- #
COUNT_LOOKBACK_DAYS   = 365    # recent window: activity counts + turnaround
PRICE_LOOKBACK_MONTHS = 24     # mature window: realised sold-vs-asking metric
PLACEHOLDER_PRICE     = 1_000  # listing asking ≤ this is treated as hidden/placeholder

MAD_K = 3.5   # robust outlier fence: median ± MAD_K × 1.4826 × MAD

# --------------------------------------------------------------------------- #
# Stratified sampling bands  (no_of_listings from the new table)
# --------------------------------------------------------------------------- #
SIZE_BANDS: list[tuple[str, int, int]] = [
    ("small",  5,    30),
    ("medium", 30,   150),
    ("large",  150,  1_000),
    ("mega",   1_000, 10 ** 9),
]

# --------------------------------------------------------------------------- #
# RAG thresholds
# --------------------------------------------------------------------------- #
SOLD_AMBER_ABS, SOLD_AMBER_PCT = 5,   0.15   # absolute OR percentage of recomputed sold
SOLD_RED_ABS,   SOLD_RED_PCT   = 10,  0.30
DIFF_AMBER_PP,  DIFF_RED_PP    = 4.0, 8.0    # percentage-point gap in diff%
MIN_COVERAGE = 5                              # fewer valid price-points → AMBER note

SOLD_STATUSES      = {
    "Sold STC", "Under offer", "Reserved", "Sold STCM",
    "Sold subject to", "Sold subject to contract",
}
LIVE_STATUSES      = {"for_sale", "to_rent"}
WITHDRAWN_STATUSES = {"removed", "archived", "EXPIRED"}

# TARGET  = the table being validated. Defaults to the LIVE production table,
#           so scheduled runs never need a table name.
# BASELINE = comparison table for week-over-week drift. Defaults to the _bkp
#           table that the pipeline's atomic swap maintains automatically
#           (last successful run). If it doesn't exist, drift is skipped.
TARGET_TABLE_DEFAULT   = "PDI_PortalsData.pdi_agent_performance"
BASELINE_TABLE_DEFAULT = "PDI_PortalsData.pdi_agent_performance_bkp"
TIME_CAP_MS            = 60_000

# Optional overrides from config.py (leave unset/empty for the defaults above)
try:
    import sys as _sys, pathlib as _pl
    _sys.path.insert(0, str(_pl.Path(__file__).parent.parent))
    import config as _cfg
    if getattr(_cfg, "VALIDATION_TARGET_TABLE", ""):
        TARGET_TABLE_DEFAULT = _cfg.VALIDATION_TARGET_TABLE
    if getattr(_cfg, "VALIDATION_BASELINE_TABLE", ""):
        BASELINE_TABLE_DEFAULT = _cfg.VALIDATION_BASELINE_TABLE
    del _cfg, _sys, _pl
except ModuleNotFoundError:
    pass

PORTAL_SRC: dict[str, tuple[str, str]] = {
    "Rightmove": ("property_details",       "p.residential='YES' AND p.commercial='NO'"),
    "Zoopla":    ("property_details_zoopla", "p.category='residential'"),
}

RAG_COLORS = {"GREEN": "#2e7d32", "AMBER": "#e65100", "RED": "#b71c1c"}
ROW_BG     = {"GREEN": "#e8f5e9", "AMBER": "#fff3e0", "RED": "#ffebee", "SKIPPED": "#f5f5f5"}


# --------------------------------------------------------------------------- #
# DB connection (read-only)
# --------------------------------------------------------------------------- #
def connect():
    try:
        sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
        import config as _cfg
        _cfg_host = getattr(_cfg, "HOST", "insight.c1d82vlu7pgj.eu-west-2.rds.amazonaws.com")
        _cfg_user = getattr(_cfg, "USER", "pdi_prospect_readonly")
        _cfg_pass = getattr(_cfg, "PASSWORD", "")
        _cfg_db   = getattr(_cfg, "DATABASE", "PDI_PortalsData")
    except ModuleNotFoundError:
        _cfg_host = "insight.c1d82vlu7pgj.eu-west-2.rds.amazonaws.com"
        _cfg_user = "pdi_prospect_readonly"
        _cfg_pass = ""
        _cfg_db   = "PDI_PortalsData"

    host     = os.environ.get("PDI_RO_HOST",     _cfg_host)
    port     = int(os.environ.get("PDI_RO_PORT", "3306"))
    user     = os.environ.get("PDI_RO_USER",     _cfg_user)
    password = os.environ.get("PDI_RO_PASSWORD", _cfg_pass)
    db       = os.environ.get("PDI_RO_DB",       _cfg_db)

    if not password:
        sys.exit(
            "ERROR: set the read-only DB password first, e.g.\n"
            "    set PDI_RO_PASSWORD=********   (Windows)\n"
            "    export PDI_RO_PASSWORD=...     (bash)\n"
        )
    cx = mysql.connector.connect(
        host=host, port=port, user=user, password=password,
        database=db, charset="utf8mb4", autocommit=True,
    )
    who = cx.cursor()
    who.execute("SELECT CURRENT_USER()")
    print(f"Connected as {who.fetchone()[0]}  (read-only expected)\n")
    who.close()
    return cx


def cap(sql: str) -> str:
    """Insert MAX_EXECUTION_TIME hint immediately after the SELECT keyword."""
    s = sql.lstrip()
    if not s.upper().startswith("SELECT"):
        return s
    return s.replace("SELECT", f"SELECT /*+ MAX_EXECUTION_TIME({TIME_CAP_MS}) */", 1)


def table_accessible(cur, table: str) -> bool:
    try:
        cur.execute(f"SELECT 1 FROM {table} LIMIT 1")
        cur.fetchall()
        return True
    except mysql.connector.Error:
        return False


# --------------------------------------------------------------------------- #
# Stratified sample
# --------------------------------------------------------------------------- #
def stratified_sample(
    cur, new_table: str, n: int, seed: int, region: str | None
) -> list[dict]:
    """
    Allocate n agents proportionally across SIZE_BANDS.
    Optional --region prefix is applied as a LIKE filter on agent_address
    (e.g. 'SW%' for South-West postcodes).
    """
    region_clause = "AND agent_address LIKE %s" if region else ""
    region_param  = [f"{region}%"] if region else []

    band_counts: dict[str, int] = {}
    for band, lo, hi in SIZE_BANDS:
        params = [lo, hi - 1] + region_param
        cur.execute(
            f"SELECT COUNT(*) cnt FROM {new_table} "
            f"WHERE no_of_listings BETWEEN %s AND %s "
            f"AND agent_name IS NOT NULL AND agent_address IS NOT NULL "
            f"{region_clause}",
            params,
        )
        band_counts[band] = cur.fetchone()["cnt"]

    total_pop = sum(band_counts.values()) or 1

    # proportional allocation, min 1 per non-empty band
    alloc: dict[str, int] = {}
    for band, lo, hi in SIZE_BANDS:
        cnt = band_counts[band]
        alloc[band] = max(1, round(n * cnt / total_pop)) if cnt else 0

    # trim/pad to exactly n
    diff = n - sum(alloc.values())
    nonempty = [b for b, _, _ in SIZE_BANDS if alloc[b] > 0]
    for i in range(abs(diff)):
        band = nonempty[i % len(nonempty)]
        alloc[band] += 1 if diff > 0 else max(-alloc[band] + 1, -1)

    rows: list[dict] = []
    for band, lo, hi in SIZE_BANDS:
        want = alloc[band]
        if want <= 0:
            continue
        params = [lo, hi - 1] + region_param + [seed, want]
        cur.execute(
            f"SELECT agent_master_id, agent_name, agent_address, no_of_listings "
            f"FROM {new_table} "
            f"WHERE no_of_listings BETWEEN %s AND %s "
            f"AND agent_name IS NOT NULL AND agent_address IS NOT NULL "
            f"{region_clause} "
            f"ORDER BY RAND(%s) LIMIT %s",
            params,
        )
        rows.extend(cur.fetchall())

    return rows


# --------------------------------------------------------------------------- #
# Raw listing fetch (24-month window, is_recent flag embedded)
# --------------------------------------------------------------------------- #
_RAW_SQL = """
    SELECT '{portal}' AS portal,
           p.listing_id, p.uprn, p.postcode, p.outcode,
           p.num_bedrooms, p.property_type,
           p.price, p.listed_price,
           COALESCE(p.pdi_ppd_last_transaction_soldPrice,
                    p.last_transaction_soldPrice)           AS sold_price,
           COALESCE(p.pdi_ppd_last_transaction_date,
                    p.last_transaction_date)                AS txn_date,
           p.first_published_date, p.display_status, p.status, p.listing_status,
           IF(p.first_published_date >= DATE_SUB(CURDATE(),
               INTERVAL {count_days} DAY), 1, 0)           AS is_recent
    FROM PDI_PortalsData.{table} p
    WHERE p.agent_name = %(name)s AND p.agent_address = %(addr)s
      AND p.listing_status IN ('sale', 'new-homes')
      AND p.first_published_date >= DATE_SUB(CURDATE(),
              INTERVAL {price_months} MONTH)
      AND {residential_filter}
"""


def agent_master_pairs(cur, agent_master_id: int) -> list[tuple[str, str, str]] | None:
    """Exact (portal, name, addr) pairs from pdi_agent_master. Returns None if inaccessible."""
    try:
        cur.execute(cap(
            "SELECT agent_name_rm, address_rm, agent_name_zl, address_zl "
            "FROM PDI_PortalsData.pdi_agent_master WHERE id = %s"
        ), (agent_master_id,))
        m = cur.fetchone()
    except mysql.connector.Error:
        return None
    if not m:
        return []
    pairs: list[tuple[str, str, str]] = []
    if m["agent_name_rm"] and m["address_rm"]:
        pairs.append(("Rightmove", m["agent_name_rm"], m["address_rm"]))
    if m["agent_name_zl"] and m["address_zl"]:
        pairs.append(("Zoopla", m["agent_name_zl"], m["address_zl"]))
    return pairs


def fetch_agent_raw(cur, agent: dict, use_master: bool) -> tuple[list[dict], bool, bool]:
    """
    Returns (rows, exact_match, timed_out).
    exact_match=True means rows came from pdi_agent_master name/addr (same as pipeline).
    """
    pairs: list[tuple[str, str, str]] | None = None
    exact = False
    if use_master:
        pairs = agent_master_pairs(cur, agent["agent_master_id"])
        exact = pairs is not None
    if not pairs:
        pairs = [
            ("Rightmove", agent["agent_name"], agent["agent_address"]),
            ("Zoopla",    agent["agent_name"], agent["agent_address"]),
        ]

    rows: list[dict] = []
    timed_out = False
    for portal, name, addr in pairs:
        table, resfilter = PORTAL_SRC[portal]
        sql = _RAW_SQL.format(
            portal=portal, table=table,
            count_days=COUNT_LOOKBACK_DAYS,
            price_months=PRICE_LOOKBACK_MONTHS,
            residential_filter=resfilter,
        )
        try:
            cur.execute(cap(sql), {"name": name, "addr": addr})
            rows.extend(cur.fetchall())
        except mysql.connector.Error as exc:
            if exc.errno == 3024:
                timed_out = True
            else:
                raise
    return rows, exact, timed_out


# --------------------------------------------------------------------------- #
# Dedup + two-window recompute
# --------------------------------------------------------------------------- #
def _dedup(rows: list[dict]) -> list[dict]:
    best: dict[Any, tuple] = {}
    for r in rows:
        key = r["uprn"] or f"{r['postcode']}|{r['property_type']}|{r['num_bedrooms']}"
        sold_flag = 1 if r["display_status"] in SOLD_STATUSES else 0
        rank = (sold_flag, r["first_published_date"] or date(1900, 1, 1))
        if key not in best or rank > best[key][0]:
            best[key] = (rank, r)
    return [v[1] for v in best.values()]


def recompute(rows: list[dict]) -> dict:
    """Independent two-window recompute matching the pipeline definition.

    recent = rows where is_recent=1  → activity counts + turnaround
    mature = all 24-month rows       → realised price median + coverage
    """
    recent = _dedup([r for r in rows if r.get("is_recent")])
    mature = _dedup(rows)

    # ---- RECENT activity counts ----
    n_listings = len(recent)
    n_live = n_sold_recent = n_withdrawn = 0
    for r in recent:
        ds = r["display_status"]
        if ds in SOLD_STATUSES:
            n_sold_recent += 1
        elif r["status"] in LIVE_STATUSES:
            n_live += 1
        elif r["status"] in WITHDRAWN_STATUSES:
            n_withdrawn += 1

    # ---- MATURE realised sold-vs-asking ----
    n_sold_mature = sum(1 for r in mature if r["display_status"] in SOLD_STATUSES)

    pct_vals: list[float] = []
    n_price_recovered = 0
    n_no_valid_price  = 0

    for r in mature:
        if r["display_status"] not in SOLD_STATUSES:
            continue

        # listed_price is the ORIGINAL asking; recover from price only if placeholder
        lp = r.get("listed_price")
        p  = r.get("price")
        if lp is not None and lp > PLACEHOLDER_PRICE:
            asking   = lp
            recovered = False
        elif p is not None and p > PLACEHOLDER_PRICE:
            asking   = p
            recovered = True
        else:
            asking   = None
            recovered = False

        sold_price = r.get("sold_price")
        txn_ok = (
            r.get("txn_date") is not None
            and r.get("first_published_date") is not None
            and r["txn_date"] >= r["first_published_date"]
        )
        if asking and asking > 0 and sold_price and sold_price > 0 and txn_ok:
            pct_vals.append(round((sold_price - asking) / asking * 100, 2))
            if recovered:
                n_price_recovered += 1
        else:
            n_no_valid_price += 1

    # Robust MAD outlier exclusion — excluded from median, listing still counted
    kept    = pct_vals[:]
    n_excl  = 0
    if len(pct_vals) >= 5:
        med = statistics.median(pct_vals)
        mad = statistics.median([abs(v - med) for v in pct_vals]) or 0.0
        if mad > 0:
            lo   = med - MAD_K * 1.4826 * mad
            hi   = med + MAD_K * 1.4826 * mad
            kept = [v for v in pct_vals if lo <= v <= hi]
            n_excl = len(pct_vals) - len(kept)

    # Modal outcode alpha prefix (area) from recent listings
    area_ctr: Counter = Counter()
    for r in recent:
        oc = (r.get("outcode") or "")
        prefix = "".join(c for c in oc if c.isalpha()).upper()
        if prefix:
            area_ctr[prefix] += 1
    area = area_ctr.most_common(1)[0][0] if area_ctr else None

    return {
        "n_listings":          n_listings,
        "n_live":              n_live,
        "n_sold":              n_sold_recent,
        "n_withdrawn":         n_withdrawn,
        "n_sold_mature":       n_sold_mature,
        "n_pricepoints":       len(pct_vals),
        "n_price_recovered":   n_price_recovered,
        "n_no_valid_price":    n_no_valid_price,
        "n_excluded_outliers": n_excl,
        "median_diff":         round(statistics.median(kept), 2) if kept else None,
        "area":                area,
    }


# --------------------------------------------------------------------------- #
# RAG verdict
# --------------------------------------------------------------------------- #
def rag(stored: dict | None, rc: dict) -> tuple[str, list[str]]:
    """
    Compare stored table row vs independent recompute.
    Returns (verdict, [notes]) where verdict ∈ {GREEN, AMBER, RED}.
    """
    if stored is None:
        return "AMBER", ["not in table"]

    notes: list[str] = []
    worst = "GREEN"

    def escalate(level: str) -> None:
        nonlocal worst
        if level == "RED" or (level == "AMBER" and worst == "GREEN"):
            worst = level

    # sold-count delta
    stored_sold  = stored.get("sold") or 0
    sold_delta   = abs(stored_sold - rc["n_sold"])
    red_thresh   = max(SOLD_RED_ABS,   round(SOLD_RED_PCT   * rc["n_sold"]))
    amber_thresh = max(SOLD_AMBER_ABS, round(SOLD_AMBER_PCT * rc["n_sold"]))
    if sold_delta >= red_thresh:
        escalate("RED");   notes.append(f"sold delta={sold_delta} (≥{red_thresh})")
    elif sold_delta >= amber_thresh:
        escalate("AMBER"); notes.append(f"sold delta={sold_delta} (≥{amber_thresh})")

    # diff% delta
    stored_diff = stored.get("diff")
    if stored_diff is not None and rc["median_diff"] is not None:
        diff_delta = abs(float(stored_diff) - rc["median_diff"])
        if diff_delta >= DIFF_RED_PP:
            escalate("RED");   notes.append(f"diff delta={diff_delta:.2f}pp (≥{DIFF_RED_PP})")
        elif diff_delta >= DIFF_AMBER_PP:
            escalate("AMBER"); notes.append(f"diff delta={diff_delta:.2f}pp (≥{DIFF_AMBER_PP})")

    # clamped value — column was decimal(4,2), fixed by migration to decimal(6,2)
    if stored_diff is not None and float(stored_diff) == -99.99:
        escalate("RED"); notes.append("diff% is exactly -99.99 — likely clamped by old decimal(4,2) column")

    # low PPD coverage
    if rc["n_pricepoints"] < MIN_COVERAGE and rc["n_sold_mature"] >= MIN_COVERAGE:
        escalate("AMBER"); notes.append(
            f"only {rc['n_pricepoints']} usable price-points of {rc['n_sold_mature']} mature sold"
        )

    if not notes:
        notes.append("ok")
    return worst, notes


# --------------------------------------------------------------------------- #
# Stored stats reader
# --------------------------------------------------------------------------- #
def stored_stats(cur, table: str, name: str, addr: str) -> dict | None:
    cur.execute(cap(
        f"SELECT no_of_sold_listings sold, avg_difference_in_percentage diff, "
        f"turnaround_days ta "
        f"FROM {table} WHERE agent_name=%s AND agent_address=%s LIMIT 1"
    ), (name, addr))
    return cur.fetchone()


# --------------------------------------------------------------------------- #
# Section A — whole-table health
# --------------------------------------------------------------------------- #
def section_a(cur, target_table: str, baseline_table: str) -> list[str]:
    lines: list[str] = []
    lines.append("SECTION A — table health & week-over-week drift")
    lines.append("=" * 60)

    baseline_ok = table_accessible(cur, baseline_table)
    tables = [("TARGET  (validating)", target_table, True)]
    if baseline_ok:
        tables.append(("BASELINE (previous run)", baseline_table, True))
    else:
        lines.append(f"\n[baseline {baseline_table} not accessible — drift comparison skipped]")

    for label, t, has_extra in tables:
        cur.execute(cap(f"""
            SELECT COUNT(*) n_rows,
                   MIN(avg_difference_in_percentage) mn,
                   MAX(avg_difference_in_percentage) mx,
                   ROUND(AVG(avg_difference_in_percentage), 2) mean_diff,
                   SUM(avg_difference_in_percentage IS NULL) null_diff,
                   SUM(avg_difference_in_percentage <= -99.99) at_neg_floor,
                   SUM(avg_difference_in_percentage >=  99.99) at_pos_floor,
                   ROUND(AVG(turnaround_days)) mean_ta,
                   SUM(turnaround_days IS NULL) null_ta
            FROM {t}
        """))
        r = cur.fetchone()
        lines.append(f"\n{label}: {r['n_rows']:,} rows")
        lines.append(
            f"  diff%  min={r['mn']}  max={r['mx']}  mean={r['mean_diff']}  null={r['null_diff']:,}"
        )
        clamp = "  <- CLAMPING" if (r["at_neg_floor"] or r["at_pos_floor"]) else ""
        lines.append(
            f"  CLAMP  at -99.99={r['at_neg_floor']:,}  at +99.99={r['at_pos_floor']:,}{clamp}"
        )
        lines.append(f"  turnaround mean={r['mean_ta']}  null={r['null_ta']:,}")

        if has_extra:
            try:
                cur.execute(cap(f"""
                    SELECT SUM(no_of_listings IS NULL) null_total,
                           SUM(COALESCE(no_of_live_listings,0)
                               + COALESCE(no_of_sold_listings,0)
                               + COALESCE(no_of_withdrawn_listing,0)
                               > COALESCE(no_of_listings,0)) invariant_violations,
                           ROUND(AVG(no_of_sold_mature),1) avg_sold_mature,
                           ROUND(AVG(no_of_sold_with_soldprice),1) avg_with_price,
                           ROUND(100*SUM(no_of_sold_with_soldprice)
                                 /NULLIF(SUM(no_of_sold_mature),0),1) coverage_pct
                    FROM {t}
                """))
                r2 = cur.fetchone()
                inv = "  <- VIOLATION" if r2["invariant_violations"] else ""
                lines.append(
                    f"  live+sold+withdrawn ≤ total violations={r2['invariant_violations']:,}{inv}"
                )
                lines.append(
                    f"  avg sold_mature={r2['avg_sold_mature']}  "
                    f"avg with_price={r2['avg_with_price']}  "
                    f"coverage={r2['coverage_pct']}%"
                )
            except mysql.connector.Error as exc:
                if exc.errno == 1054:   # Unknown column — migration not yet applied
                    cur.fetchall()
                    lines.append("  [coverage columns absent — run db/migrations/*.sql first]")
                else:
                    raise

    if baseline_ok:
        cur.execute(cap(f"""
            SELECT COUNT(*) matched,
                   SUM(p.no_of_sold_listings <> n.no_of_sold_listings) sold_differs,
                   ROUND(AVG(ABS(CAST(p.no_of_sold_listings AS SIGNED)
                                 - CAST(n.no_of_sold_listings AS SIGNED))), 1) avg_abs_sold_gap,
                   ROUND(AVG(ABS(COALESCE(p.turnaround_days,0)
                                 - COALESCE(n.turnaround_days,0))), 1) avg_abs_ta_gap,
                   ROUND(AVG(ABS(COALESCE(p.avg_difference_in_percentage,0)
                                 - COALESCE(n.avg_difference_in_percentage,0))), 2) avg_abs_diff_gap
            FROM {baseline_table} p
            JOIN {target_table} n
              ON p.agent_name=n.agent_name AND p.agent_address=n.agent_address
        """))
        r = cur.fetchone()
        pct = (r["sold_differs"] / r["matched"] * 100) if r["matched"] else 0
        lines.append(f"\nDRIFT vs previous run (agents in both, n={r['matched']:,}):")
        lines.append(
            f"  sold count differs for {r['sold_differs']:,} agents ({pct:.0f}%),  "
            f"avg abs gap {r['avg_abs_sold_gap']}"
        )
        lines.append(f"  turnaround avg abs gap {r['avg_abs_ta_gap']} days")
        lines.append(f"  diff% avg abs gap {r['avg_abs_diff_gap']} pp")
        lines.append("  (large jumps week-over-week = investigate before trusting the refresh)")
    return lines


# --------------------------------------------------------------------------- #
# Section B — per-agent RAG
# --------------------------------------------------------------------------- #
def _size_band(n_listings: int) -> str:
    for band, lo, hi in SIZE_BANDS:
        if lo <= n_listings < hi:
            return band
    return "?"


def section_b(
    cur, new_table: str, agents: list[dict], use_master: bool
) -> tuple[list[dict], dict[str, int]]:
    results: list[dict] = []
    tally = {"GREEN": 0, "AMBER": 0, "RED": 0, "SKIPPED": 0}

    for a in agents:
        name = a.get("agent_name") or ""
        addr = a.get("agent_address") or ""

        raw, exact, timed_out = fetch_agent_raw(cur, a, use_master)

        if timed_out:
            results.append(_skipped(a, f"raw pull exceeded {TIME_CAP_MS/1000:.0f}s"))
            tally["SKIPPED"] += 1
            continue
        if not raw:
            msg = "no raw listings matched" + ("" if exact else " (display name/addr)")
            results.append(_skipped(a, msg))
            tally["SKIPPED"] += 1
            continue

        rc      = recompute(raw)
        row_n   = stored_stats(cur, new_table, name, addr)
        verdict, notes = rag(row_n, rc)
        tally[verdict] += 1

        coverage = (
            f"{rc['n_pricepoints']}/{rc['n_sold_mature']}"
            if rc["n_sold_mature"] else "0/0"
        )
        nl = a.get("no_of_listings") or rc["n_listings"]
        results.append({
            "agent_master_id": a.get("agent_master_id"),
            "agent":           name[:40],
            "area":            rc["area"] or "",
            "band":            _size_band(nl),
            "status":          verdict,
            "notes":           "; ".join(notes),
            "stored_sold":     row_n["sold"]  if row_n else None,
            "re_sold":         rc["n_sold"],
            "stored_diff":     row_n["diff"]  if row_n else None,
            "re_diff":         rc["median_diff"],
            "coverage":        coverage,
            "n_excluded":      rc["n_excluded_outliers"],
            "n_recovered":     rc["n_price_recovered"],
        })

    return results, tally


def _skipped(a: dict, reason: str) -> dict:
    return {
        "agent_master_id": a.get("agent_master_id"),
        "agent":           (a.get("agent_name") or "")[:40],
        "area": "", "band": "", "status": "SKIPPED", "notes": reason,
        "stored_sold": None, "re_sold": None,
        "stored_diff": None, "re_diff": None,
        "coverage": None, "n_excluded": 0, "n_recovered": 0,
    }


# --------------------------------------------------------------------------- #
# HTML report
# --------------------------------------------------------------------------- #
def build_html(
    a_lines: list[str],
    b_results: list[dict],
    tally: dict[str, int],
    n: int,
    seed: int,
    new_table: str,
    today: str,
) -> str:
    summary_cells = "".join(
        f'<td style="color:{RAG_COLORS.get(k,"#555")};font-weight:bold;padding:4px 12px">'
        f'{k}: {tally[k]}</td>'
        for k in ("GREEN", "AMBER", "RED", "SKIPPED")
    )

    rows_html = ""
    for r in b_results:
        bg    = ROW_BG.get(r["status"], "#fff")
        color = RAG_COLORS.get(r["status"], "#000")
        ss    = str(r["stored_sold"]) if r["stored_sold"] is not None else "—"
        sd    = str(r["stored_diff"]) if r["stored_diff"] is not None else "—"
        rd    = str(r["re_diff"])     if r["re_diff"]     is not None else "—"
        cov   = r.get("coverage") or ""
        excl  = f" excl={r['n_excluded']}" if r.get("n_excluded") else ""
        recov = f" recov={r['n_recovered']}" if r.get("n_recovered") else ""
        notes = r["notes"] + excl + recov
        rows_html += (
            f'<tr style="background:{bg}">'
            f'<td>{r["agent"]}</td>'
            f'<td style="text-align:center">{r["area"]}</td>'
            f'<td style="text-align:center">{r["band"]}</td>'
            f'<td style="color:{color};font-weight:bold;text-align:center">{r["status"]}</td>'
            f'<td style="text-align:center">{ss} / {r["re_sold"]}</td>'
            f'<td style="text-align:center">{sd} / {rd}</td>'
            f'<td style="text-align:center">{cov}</td>'
            f'<td style="font-size:0.85em">{notes}</td>'
            f'</tr>\n'
        )

    a_text = "\n".join(a_lines).replace("&", "&amp;").replace("<", "&lt;")

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>Agent Performance Validation {today}</title>
<style>
  body   {{ font-family:Arial,sans-serif; font-size:14px; margin:20px; color:#333 }}
  h2,h3  {{ color:#37474f }}
  table  {{ border-collapse:collapse; width:100% }}
  th,td  {{ border:1px solid #ccc; padding:6px 10px; vertical-align:top }}
  th     {{ background:#37474f; color:#fff; text-align:left }}
  pre    {{ background:#f5f5f5; padding:12px; font-size:12px; overflow-x:auto }}
  .sum   {{ border:none; width:auto; margin-bottom:16px }}
  .sum td {{ border:none }}
</style>
</head><body>
<h2>PDI Agent Performance Validation — {today}</h2>
<p><b>New table:</b> {new_table} &nbsp;|&nbsp;
   <b>Sample:</b> {n} agents (seed={seed}) &nbsp;|&nbsp;
   <b>Total:</b> {sum(tally.values())}</p>
<table class="sum"><tr>{summary_cells}</tr></table>

<h3>Section B — Per-agent RAG (NEW table vs independent recompute)</h3>
<p style="font-size:0.85em">
Sold S/R = stored vs recompute &nbsp;|&nbsp;
Diff% S/R = stored vs recompute (recompute = MEDIAN) &nbsp;|&nbsp;
Coverage = usable PPD price-points / mature sold (24mo)
</p>
<table>
<tr>
  <th>Agent</th><th>Area</th><th>Band</th><th>RAG</th>
  <th>Sold S/R</th><th>Diff% S/R</th><th>Coverage</th><th>Notes</th>
</tr>
{rows_html}
</table>

<h3>Section A — Table health &amp; cross-table divergence</h3>
<pre>{a_text}</pre>

<p style="font-size:0.8em;color:#999">
Two-window logic: activity counts on {COUNT_LOOKBACK_DAYS}d window;
realised price metric on {PRICE_LOOKBACK_MONTHS}mo window.
Baseline = listed_price (original asking); recovered from price when
listed_price is null/placeholder (≤£{PLACEHOLDER_PRICE:,}).
Outlier exclusion: MAD fence median ± {MAD_K} × 1.4826 × MAD.
</p>
</body></html>"""


# --------------------------------------------------------------------------- #
# Email
# --------------------------------------------------------------------------- #
def send_email(html_body: str, tally: dict[str, int], cfg: Any) -> None:
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    if not getattr(cfg, "EnableMailNotification", False):
        print("Email disabled (EnableMailNotification=False in config.py).")
        return

    smtp_host = getattr(cfg, "MAIL_HOST", "")
    smtp_port = int(getattr(cfg, "MAIL_PORT", 587))
    smtp_user = getattr(cfg, "MAIL_USER", "")
    smtp_pass = getattr(cfg, "MAIL_PASS", "")
    smtp_ssl  = getattr(cfg, "MAIL_SSL",  False)
    to_list   = getattr(cfg, "MailTO",    [])
    from_addr = getattr(cfg, "MAIL_FROM", smtp_user)

    if not smtp_host or not to_list:
        print("Email skipped: MAIL_HOST or MailTO not configured in config.py.")
        return

    to_addrs = to_list if isinstance(to_list, list) else [to_list]
    worst = "GREEN"
    for level in ("RED", "AMBER"):
        if tally.get(level, 0) > 0:
            worst = level
            break

    subject = f"[{worst}] PDI Agent Performance Validation — {date.today()}"
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = from_addr
    msg["To"]      = ", ".join(to_addrs)
    msg.attach(MIMEText(html_body, "html"))

    if smtp_ssl:
        with smtplib.SMTP_SSL(smtp_host, smtp_port) as s:
            if smtp_user and smtp_pass:
                s.login(smtp_user, smtp_pass)
            s.sendmail(from_addr, to_addrs, msg.as_string())
    else:
        with smtplib.SMTP(smtp_host, smtp_port) as s:
            s.ehlo()
            s.starttls()
            if smtp_user and smtp_pass:
                s.login(smtp_user, smtp_pass)
            s.sendmail(from_addr, to_addrs, msg.as_string())
    print(f"Email sent → {msg['To']}  |  subject: {subject}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Read-only PDI agent-performance RAG validation harness"
    )
    ap.add_argument("--sample",         type=int, default=40,
                    help="total agents to validate — stratified across size bands (default 40)")
    ap.add_argument("--seed",           type=int, default=None,
                    help="RAND() seed for the stratified sample. Default: derived from "
                         "today's date (YYYYMMDD), so scheduled runs rotate through "
                         "different agents/areas/sizes over time while any single day's "
                         "run stays reproducible. Pass a fixed seed to repeat a "
                         "historical sample (e.g. --seed 42).")
    ap.add_argument("--region",
                    help="restrict sample to agents whose address starts with this prefix, e.g. SW")
    ap.add_argument("--agent-id",       type=int,
                    help="validate a single agent_master_id (overrides --sample / --seed)")
    ap.add_argument("--table", "--new-table", dest="table",
                    default=TARGET_TABLE_DEFAULT,
                    help="table to validate — only needed for ad-hoc scrutiny of a "
                         f"specific table (default: the live {TARGET_TABLE_DEFAULT})")
    ap.add_argument("--baseline-table", default=BASELINE_TABLE_DEFAULT,
                    help="drift-comparison table (default: the _bkp table the atomic "
                         "swap maintains; drift is skipped if it doesn't exist)")
    ap.add_argument("--skip-section-a", action="store_true",
                    help="skip whole-table health summary (faster when re-running)")
    ap.add_argument("--email",          action="store_true",
                    help="send HTML report via config.py SMTP settings")
    args = ap.parse_args()

    if args.seed is None:
        # rotate the sample daily: different agents/areas/sizes surface over time
        args.seed = int(date.today().strftime("%Y%m%d"))
        print(f"seed not given — using date-derived seed {args.seed} "
              f"(re-run with --seed {args.seed} to reproduce this sample)")

    today = str(date.today())
    cx    = connect()
    cur   = cx.cursor(dictionary=True)
    tally = {"GREEN": 0, "AMBER": 0, "RED": 0, "SKIPPED": 0}

    try:
        a_lines: list[str] = []
        if not args.skip_section_a:
            a_lines = section_a(cur, args.table, args.baseline_table)
            for line in a_lines:
                print(line)

        use_master = table_accessible(cur, "PDI_PortalsData.pdi_agent_master")
        print(
            f"\nagent→listing mapping: {'EXACT (pdi_agent_master)' if use_master else 'APPROXIMATE (display name/addr)'}"
        )

        # build agent list
        if args.agent_id is not None:
            cur.execute(cap(
                f"SELECT agent_master_id, agent_name, agent_address, no_of_listings "
                f"FROM {args.table} WHERE agent_master_id = %s"
            ), (args.agent_id,))
            agents = cur.fetchall()
            if not agents:
                print(f"agent_master_id={args.agent_id} not found in {args.table}.")
                return 1
        else:
            agents = stratified_sample(
                cur, args.table, args.sample, args.seed, args.region
            )

        band_tally: Counter = Counter(
            _size_band(a.get("no_of_listings") or 0) for a in agents
        )
        print(
            f"\nSection B — {len(agents)} agents  "
            + "  ".join(f"{b}={band_tally[b]}" for b, *_ in SIZE_BANDS if band_tally[b])
        )
        print("-" * 80)

        b_results, tally = section_b(cur, args.table, agents, use_master)

        # console output
        hdr = (
            f"{'Agent':40} {'Area':4} {'Band':7} {'RAG':6} "
            f"{'Sold S/R':9} {'Diff% S/R':12} {'Coverage':9} Notes"
        )
        print(hdr)
        print("-" * len(hdr))
        for r in b_results:
            ss   = str(r["stored_sold"]) if r["stored_sold"] is not None else "—"
            sd   = str(r["stored_diff"]) if r["stored_diff"] is not None else "—"
            rd   = str(r["re_diff"])     if r["re_diff"]     is not None else "—"
            cov  = r.get("coverage") or ""
            excl = f" excl={r['n_excluded']}" if r.get("n_excluded") else ""
            print(
                f"{r['agent']:40} {r['area']:4} {r['band']:7} {r['status']:6} "
                f"{ss:>4}/{str(r['re_sold'] or '—'):>4}  "
                f"{sd:>6}/{rd:>6}  "
                f"{cov:>8}  "
                f"{r['notes']}{excl}"
            )

        print(
            f"\nTally:  GREEN={tally['GREEN']}  AMBER={tally['AMBER']}  "
            f"RED={tally['RED']}  SKIPPED={tally['SKIPPED']}"
        )

        _ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
        _tbl_slug = (args.table or "").split(".")[-1][:60]
        _fname    = f"report_{_ts}_{_tbl_slug}.html" if _tbl_slug else f"report_{_ts}.html"
        html_path = pathlib.Path(__file__).parent / _fname

        html = build_html(
            a_lines, b_results, tally,
            args.sample, args.seed, args.table, today,
        )
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"HTML report → {html_path}")

        if args.email:
            try:
                import config as cfg
                send_email(html, tally, cfg)
            except ImportError:
                print("WARNING: config.py not found; skipping email.")

    finally:
        cur.close()
        cx.close()

    print("\nDONE (no rows were written).")

    worst = "GREEN"
    for level in ("RED", "AMBER"):
        if tally.get(level, 0) > 0:
            worst = level
            break
    return {"GREEN": 0, "AMBER": 1, "RED": 2}[worst]


if __name__ == "__main__":
    raise SystemExit(main())
