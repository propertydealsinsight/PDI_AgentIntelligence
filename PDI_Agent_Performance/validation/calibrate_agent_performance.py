"""
Calibration harness — measures how far agent-performance metrics are from TRUTH.

The RAG harness (validate_agent_performance.py) proves the pipeline faithfully
implements the decided definitions (pre-swap gate + anomaly checks). It CANNOT
prove the definitions match reality, because it recomputes from the same raw
tables with the same rules. This script closes that gap with four experiments
(design doc TODO T3, gotcha G7):

  C1  PPD RECALL      For sample outcodes: of ALL Land Registry transactions
                      (PropDealsIns.pdi_pricepaiddata = assumed ground truth),
                      what share does our pipeline attribute to any agent with a
                      usable price point?  -> "% of the real market we explain".
  C2  FALL-THROUGH    Sold-STC listings published 18-30 months ago with NO PPD
                      registration after going live = fell through OR failed to
                      match. Upper bound on how much no_of_sold_listings
                      overstates completions.
  C3  ASKING AUDIT    For sold listings with a valid price point: capture lag
                      (first_published_date vs our first record) and
                      history-confirmed pre-baseline price cuts. LOWER bound on
                      the diff% flattery (pre-first-scrape cuts are invisible).
  C4  RIGHTMOVE PANEL Writes a CSV panel of agents with our sold/live counts and
                      blank columns to fill from Rightmove's public agent pages
                      (external benchmark; fill manually or via scraper infra).

Strictly READ-ONLY. Credentials from config.py, overridable via PDI_RO_* env
vars (same convention as validate_agent_performance.py).

USAGE
-----
    python validation/calibrate_agent_performance.py                       # all checks
    python validation/calibrate_agent_performance.py --checks c1,c2
    python validation/calibrate_agent_performance.py --outcodes B1,SW11,LS1
    python validation/calibrate_agent_performance.py --checks c4 --panel-size 20
"""

from __future__ import annotations

import argparse
import csv
import pathlib
import statistics
import sys
from datetime import date, datetime
from decimal import Decimal

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from validate_agent_performance import (  # noqa: E402
    COUNT_LOOKBACK_DAYS,
    PLACEHOLDER_PRICE,
    PRICE_LOOKBACK_MONTHS,
    SOLD_STATUSES,
    cap,
    connect,
)

# Diverse default outcodes: big city, London, suburban, northern, coastal, Scots
DEFAULT_OUTCODES = ["B1", "SW11", "LS1", "M4", "NE1", "BS3", "CF10", "BN1"]

# C2 window: old enough that PPD lag can no longer explain absence
FALLTHROUGH_MIN_MONTHS = 18
FALLTHROUGH_MAX_MONTHS = 30

PPD_TABLE = "PropDealsIns.pdi_pricepaiddata"
PPD_RESIDENTIAL = "prop_type IN ('D','S','T','F')"   # exclude 'O' (other)
PPD_STANDARD = "ppd_ctg_type = 'A'"                  # standard sales only

LIVE_TABLE = "PDI_PortalsData.pdi_agent_performance"

PORTALS = (
    ("Rightmove", "property_details", "property_details_history",
     "p.residential='YES' AND p.commercial='NO'"),
    ("Zoopla", "property_details_zoopla", "property_details_zoopla_history",
     "p.category='residential'"),
)


def fetch_outcode_sold(cur, outcode: str, min_months: int = 0,
                       max_months: int = PRICE_LOOKBACK_MONTHS) -> list[dict]:
    """Sold-status listings for an outcode published inside [max_months, min_months] ago."""
    rows: list[dict] = []
    for portal, table, _hist, resfilter in PORTALS:
        cur.execute(cap(f"""
            SELECT '{portal}' AS portal, p.listing_id, p.uprn, p.postcode,
                   p.num_bedrooms, p.property_type, p.price, p.listed_price,
                   COALESCE(p.pdi_ppd_last_transaction_soldPrice,
                            p.last_transaction_soldPrice)  AS sold_price,
                   COALESCE(p.pdi_ppd_last_transaction_date,
                            p.last_transaction_date)       AS txn_date,
                   p.first_published_date, p.display_status
            FROM PDI_PortalsData.{table} p
            WHERE p.outcode = %(oc)s
              AND p.listing_status IN ('sale', 'new-homes')
              AND p.first_published_date
                    BETWEEN DATE_SUB(CURDATE(), INTERVAL %(maxm)s MONTH)
                        AND DATE_SUB(CURDATE(), INTERVAL %(minm)s MONTH)
              AND {resfilter}
        """), {"oc": outcode, "maxm": max_months, "minm": min_months})
        rows.extend(cur.fetchall())
    return [r for r in rows if r["display_status"] in SOLD_STATUSES]


def dedup(rows: list[dict]) -> list[dict]:
    """Same dedup key as the pipeline: uprn, else postcode|type|beds."""
    best: dict = {}
    for r in rows:
        key = r["uprn"] or f"{r['postcode']}|{r['property_type']}|{r['num_bedrooms']}"
        rank = r["first_published_date"] or date(1900, 1, 1)
        if key not in best or rank > best[key][0]:
            best[key] = (rank, r)
    return [v[1] for v in best.values()]


def has_valid_pricepoint(r: dict) -> bool:
    lp, p = r.get("listed_price"), r.get("price")
    asking = lp if (lp and lp > PLACEHOLDER_PRICE) else (p if (p and p > PLACEHOLDER_PRICE) else None)
    return bool(
        asking and r.get("sold_price") and r["sold_price"] > 0
        and r.get("txn_date") and r.get("first_published_date")
        and r["txn_date"] >= r["first_published_date"]
    )


# --------------------------------------------------------------------------- #
# C1 — PPD recall
# --------------------------------------------------------------------------- #
def c1_ppd_recall(cur, outcodes: list[str]) -> list[str]:
    out = ["", "C1 — PPD RECALL (share of the real market we attribute to an agent)",
           "=" * 74,
           f"{'outcode':8} {'ppd_universe':>12} {'attrib_sold':>11} {'w/pricepoint':>12} "
           f"{'recall_sold':>11} {'recall_price':>12}"]
    tot_u = tot_s = tot_p = 0
    for oc in outcodes:
        cur.execute(cap(f"""
            SELECT COUNT(*) c FROM {PPD_TABLE}
            WHERE postcode LIKE %(pc)s
              AND date_of_trn >= DATE_SUB(CURDATE(), INTERVAL {PRICE_LOOKBACK_MONTHS} MONTH)
              AND {PPD_RESIDENTIAL} AND {PPD_STANDARD}
        """), {"pc": f"{oc} %"})
        universe = cur.fetchone()["c"]

        sold = dedup(fetch_outcode_sold(cur, oc))
        n_sold = len(sold)
        n_price = sum(1 for r in sold if has_valid_pricepoint(r))

        tot_u += universe; tot_s += n_sold; tot_p += n_price
        rs = f"{n_sold / universe * 100:5.1f}%" if universe else "  n/a"
        rp = f"{n_price / universe * 100:5.1f}%" if universe else "  n/a"
        out.append(f"{oc:8} {universe:>12,} {n_sold:>11,} {n_price:>12,} {rs:>11} {rp:>12}")

    if tot_u:
        out.append("-" * 74)
        out.append(f"{'TOTAL':8} {tot_u:>12,} {tot_s:>11,} {tot_p:>12,} "
                   f"{tot_s / tot_u * 100:>10.1f}% {tot_p / tot_u * 100:>11.1f}%")
    out += ["",
            "  recall_sold  = deduped Sold-STC listings / all PPD residential sales (24mo)",
            "  recall_price = of those, with a usable Land-Registry price point (feeds diff%)",
            "  Caveats: PPD includes never-portal-listed sales (private/offline auction);",
            "  our window is by publish date, PPD's by transaction date. Treat as directional."]
    return out


# --------------------------------------------------------------------------- #
# C2 — fall-through bound
# --------------------------------------------------------------------------- #
def c2_fallthrough(cur, outcodes: list[str]) -> list[str]:
    out = ["", "C2 — FALL-THROUGH BOUND (Sold-STC 18-30mo ago, never registered)",
           "=" * 74,
           f"{'outcode':8} {'stc_18_30mo':>11} {'registered':>10} {'unregistered':>12} {'bound':>7}"]
    tot_s = tot_r = 0
    for oc in outcodes:
        sold = dedup(fetch_outcode_sold(
            cur, oc, min_months=FALLTHROUGH_MIN_MONTHS, max_months=FALLTHROUGH_MAX_MONTHS))
        registered = sum(
            1 for r in sold
            if r.get("sold_price") and r.get("txn_date") and r.get("first_published_date")
            and r["txn_date"] >= r["first_published_date"]
        )
        n = len(sold)
        tot_s += n; tot_r += registered
        pct = f"{(n - registered) / n * 100:5.1f}%" if n else "  n/a"
        out.append(f"{oc:8} {n:>11,} {registered:>10,} {n - registered:>12,} {pct:>7}")
    if tot_s:
        out.append("-" * 74)
        out.append(f"{'TOTAL':8} {tot_s:>11,} {tot_r:>10,} {tot_s - tot_r:>12,} "
                   f"{(tot_s - tot_r) / tot_s * 100:>6.1f}%")
    out += ["",
            "  'unregistered' = fell through OR PPD address-match failure — an UPPER bound",
            "  on how much no_of_sold_listings overstates completed sales."]
    return out


# --------------------------------------------------------------------------- #
# C3 — asking-price audit (capture lag + history-confirmed cuts)
# --------------------------------------------------------------------------- #
def c3_asking_audit(cur, outcodes: list[str], sample: int) -> list[str]:
    pts = []
    for oc in outcodes:
        pts.extend(r for r in dedup(fetch_outcode_sold(cur, oc)) if has_valid_pricepoint(r))
    pts = pts[:sample]

    # No first-seen timestamp exists on the listings tables (only modified_dt =
    # last write), so the tracking-lag proxy is the earliest history entry per
    # listing — only available for listings that had at least one tracked change.
    hist_by_portal = {p: t for p, _tbl, t, _f in PORTALS}
    lags = []
    confirmed_cut = 0
    checked = 0
    flattery_pp = []
    for r in pts:
        baseline = r["listed_price"] if (r["listed_price"] and r["listed_price"] > PLACEHOLDER_PRICE) else r["price"]
        cur.execute(cap(f"""
            SELECT old_value, column_name, changed_at
            FROM PDI_PortalsData.{hist_by_portal[r['portal']]}
            WHERE listing_id = %(lid)s
        """), {"lid": r["listing_id"]})
        hist = cur.fetchall()
        checked += 1

        fp = r.get("first_published_date")
        first_change = min((h["changed_at"] for h in hist if h["changed_at"]), default=None)
        if fp and first_change:
            fp_d = fp.date() if isinstance(fp, datetime) else fp
            fc_d = first_change.date() if isinstance(first_change, datetime) else first_change
            lags.append(max((fc_d - fp_d).days, 0))

        olds = []
        for h in hist:
            if h["column_name"] in ("price", "listed_price"):
                try:
                    olds.append(Decimal(str(h["old_value"]).replace(",", "")))
                except Exception:
                    pass
        if olds and baseline:
            peak = max(olds)
            if peak > Decimal(baseline) * Decimal("1.02"):
                confirmed_cut += 1
                flattery_pp.append(float((peak - Decimal(baseline)) / peak * 100))

    out = ["", "C3 — ASKING-PRICE AUDIT (is 'original asking' really original?)",
           "=" * 74,
           f"  sample: {len(pts)} sold listings with valid price points from {len(outcodes)} outcodes"]
    if lags:
        out += [f"  tracking lag (first_published -> first history entry; {len(lags)} listings with history):",
                f"    median={statistics.median(lags):.0f}d  p90={sorted(lags)[int(len(lags) * .9)]}d  "
                f"share >30d = {sum(1 for l in lags if l > 30) / len(lags) * 100:.1f}%  "
                f"(long lag = window where a pre-tracking cut could hide)"]
    else:
        out.append("  tracking lag: no history entries found for the sample — skipped")
    if checked:
        out += [f"  history-confirmed baseline understatement: {confirmed_cut}/{checked} "
                f"({confirmed_cut / checked * 100:.1f}%) had an earlier asking >2% above our baseline"]
        if flattery_pp:
            out.append(f"    median understatement among those: {statistics.median(flattery_pp):.1f}pp")
    out += ["",
            "  LOWER bound: cuts made before we ever scraped the listing are invisible",
            "  to the history log; the capture-lag share sizes that blind spot."]
    return out


# --------------------------------------------------------------------------- #
# C4 — Rightmove spot-check panel
# --------------------------------------------------------------------------- #
def c4_rightmove_panel(cur, panel_size: int, seed: int) -> list[str]:
    cur.execute(cap(f"""
        SELECT agent_master_id, agent_name, agent_address,
               no_of_listings, no_of_live_listings, no_of_sold_listings
        FROM {LIVE_TABLE}
        WHERE no_of_sold_listings >= 10
        ORDER BY RAND(%(seed)s) LIMIT %(n)s
    """), {"seed": seed, "n": panel_size})
    agents = cur.fetchall()

    reports_dir = pathlib.Path(__file__).parent / "reports"
    reports_dir.mkdir(exist_ok=True)
    path = reports_dir / f"rightmove_panel_{date.today():%Y%m%d}.csv"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["agent_master_id", "agent_name", "agent_address",
                    "our_no_of_listings_365d", "our_live", "our_sold_365d",
                    "rightmove_sold_displayed", "rightmove_live_displayed",
                    "checked_on", "notes"])
        for a in agents:
            w.writerow([a["agent_master_id"], a["agent_name"], a["agent_address"],
                        a["no_of_listings"], a["no_of_live_listings"],
                        a["no_of_sold_listings"], "", "", "", ""])

    return ["", "C4 — RIGHTMOVE SPOT-CHECK PANEL", "=" * 74,
            f"  {len(agents)} agents (sold>=10, seed={seed}) -> {path}",
            "  Fill rightmove_* columns from each agent's public Rightmove branch page",
            "  (find-an-agent search; 'Sold by this agent' / results counts), then compare.",
            "  Expect our_sold to run HIGHER than completions (fall-throughs, C2) but in",
            "  the same order of magnitude as Rightmove's own STC display."]


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="Read-only truth-calibration harness")
    ap.add_argument("--checks", default="c1,c2,c3,c4",
                    help="comma list of checks to run (default all)")
    ap.add_argument("--outcodes", default=",".join(DEFAULT_OUTCODES),
                    help=f"comma list of outcodes (default {','.join(DEFAULT_OUTCODES)})")
    ap.add_argument("--sample", type=int, default=300,
                    help="C3: max sold listings to audit (default 300)")
    ap.add_argument("--panel-size", type=int, default=20,
                    help="C4: agents in the Rightmove panel (default 20)")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    checks = {c.strip().lower() for c in args.checks.split(",")}
    outcodes = [o.strip().upper() for o in args.outcodes.split(",") if o.strip()]

    cx = connect()
    cur = cx.cursor(dictionary=True)
    lines: list[str] = [
        f"AGENT PERFORMANCE TRUTH CALIBRATION — {date.today()}",
        f"outcodes: {', '.join(outcodes)} | windows: counts {COUNT_LOOKBACK_DAYS}d, "
        f"price {PRICE_LOOKBACK_MONTHS}mo | PPD universe: {PPD_TABLE}",
        "",
        "READ THIS FIRST — what these metrics are (and are not):",
        "  * Agent performance = MARKET-OBSERVED BEHAVIOUR on Rightmove + Zoopla only:",
        "    how long an agent takes to sell, how many they don't sell, and how close",
        "    to the FIRST LISTING PRICE they agree a sale at.",
        "  * 'Sold' = sale AGREED on a portal (Sold STC), never a completion claim.",
        "  * We can never know WHICH agent completed a multi-agency sale, and we will",
        "    never 100% match the Land Registry (address matching + tracking limits).",
        "  * Land Registry totals are not our metric — PropDealsIns.pdi_pricepaiddata",
        "    is the source of truth for market volume, not this table.",
        "  * This script is an OPTIONAL deep-scrutiny lever (per-agent/area analysis,",
        "    periodic review) — it is NOT part of the core pipeline and does not",
        "    change any data.",
    ]
    try:
        if "c1" in checks:
            lines += c1_ppd_recall(cur, outcodes)
        if "c2" in checks:
            lines += c2_fallthrough(cur, outcodes)
        if "c3" in checks:
            lines += c3_asking_audit(cur, outcodes, args.sample)
        if "c4" in checks:
            lines += c4_rightmove_panel(cur, args.panel_size, args.seed)
    finally:
        cur.close()
        cx.close()

    report = "\n".join(lines)
    print(report)
    reports_dir = pathlib.Path(__file__).parent / "reports"
    reports_dir.mkdir(exist_ok=True)
    out_path = reports_dir / f"calibration_{datetime.now():%Y%m%d_%H%M%S}.txt"
    out_path.write_text(report, encoding="utf-8")
    print(f"\nreport -> {out_path}\nDONE (no rows were written).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
