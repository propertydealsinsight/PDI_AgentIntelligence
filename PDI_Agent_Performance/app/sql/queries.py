"""SQL queries for agent performance processing.

METRIC DEFINITION (see docs/agent_performance_gaps_and_fixes.md)
---------------------------------------------------------------
ONE TABLE, TWO CLOCKS:

  * ACTIVITY counts (no_of_listings / live / sold / withdrawn / turnaround) are
    measured on a RECENT window (last COUNT_LOOKBACK_DAYS) -- "what the agent is
    doing now".

  * REALISED sold-vs-asking (avg_difference_in_percentage) is measured on a
    MATURE window (last PRICE_LOOKBACK_MONTHS) because Land-Registry (PPD) data
    lags: Sold STC -> conveyancing (months) -> completion -> PPD update (~1 month).
    A 365-day window is almost entirely un-registered; coverage rises from ~0% at
    0-3 months to ~60% at 18-24 months. So the price metric deliberately reaches
    back further.

    avg_difference_in_percentage = MEDIAN over sold listings that HAVE a registered
    PPD sale, of:
        asking     = listed_price (ORIGINAL asking; recovered from `price` only
                     when listed_price is missing/a placeholder -- the £1 trick)
        sold_price = COALESCE(pdi_ppd_last_transaction_soldPrice, last_transaction_soldPrice)
        variation% = (sold_price - asking) / asking * 100
    counted only when txn_date >= first_published_date (this listing's sale, not
    an unrelated historical one). Statistical anomalies are excluded from the
    median via a robust MAD fence (median ± MAD_K * 1.4826 * MAD, applied only
    when there are >= MIN_POINTS_FOR_OUTLIER_FENCE valid points and MAD > 0) --
    the listing is still counted in no_of_sold_with_soldprice, only the value is
    dropped from the median. This mirrors validation/validate_agent_performance.py's
    recompute() so the two can be cross-checked. Coverage is published:
        no_of_sold_mature         -- sold listings in the mature window (denominator)
        no_of_sold_with_soldprice -- of those, how many had a usable PPD price (pre-exclusion)
        no_of_price_recovered     -- of those, how many had asking recovered from a placeholder
        no_of_excluded_outliers   -- of those, how many were dropped from the median by the MAD fence
"""

# tunables -----------------------------------------------------------------
COUNT_LOOKBACK_DAYS = 365        # recent window for activity counts
PRICE_LOOKBACK_MONTHS = 24       # mature window for the realised-price metric
PLACEHOLDER_PRICE = 1000         # a sale asking <= this is treated as hidden/placeholder
MAD_K = 3.5                      # robust outlier fence multiplier: median +/- MAD_K * 1.4826 * MAD
MIN_POINTS_FOR_OUTLIER_FENCE = 5 # below this many valid points, skip MAD fencing (too few to be robust)

_SOLD_SET = (
    "'Sold STC', 'Under offer', 'Reserved', 'Sold STCM', "
    "'Sold subject to', 'Under offer', 'Sold subject to contract'"
)

# dedup partition keys (shared by both dedup lineages) ----------------------
_ADDR_KEY = (
    "SUBSTRING_INDEX(REPLACE(REPLACE(REPLACE(UPPER(REPLACE(REPLACE("
    "t.full_property_address, ', ', ' '), ' and parking space', '')), 'FLAT ', ''),"
    " 'APARTMENT ', ''), 'UNIT ', ''), ' ', 3)"
)
_PART_UPRN = (
    f"IF(t.listing_status='new-homes','sale',t.listing_status), "
    f"COALESCE(t.uprn, LOWER(CONCAT({_ADDR_KEY}, ' ', t.postcode)), "
    f"LOWER(CONCAT_WS('|', t.postcode, t.property_type, t.num_bedrooms, t.price))), "
    f"IF(t.listed_days < 4, 0, t.listed_days)"
)
_PART_ATTR = (
    "IF(t.listing_status='new-homes','sale',t.listing_status), "
    "LOWER(CONCAT_WS('|', t.postcode, t.property_type, t.num_bedrooms, t.price)), "
    "IF(t.listed_days < 4, 0, t.listed_days)"
)
_ORDER = "t.marked_under_offer_sold DESC, t.first_published_date DESC"


def _dedup(source: str, prefix: str) -> str:
    """Two-pass window-function dedup over `source`, exposing `{prefix}_final`."""
    return f"""
{prefix}_1 AS (
    SELECT t.*, ROW_NUMBER() OVER (PARTITION BY {_PART_UPRN} ORDER BY {_ORDER}) AS rn1
    FROM {source} t
),
{prefix}_2 AS (
    SELECT t.*, ROW_NUMBER() OVER (PARTITION BY {_PART_ATTR} ORDER BY {_ORDER}) AS rn2
    FROM {prefix}_1 t WHERE t.rn1 = 1
),
{prefix}_final AS (SELECT * FROM {prefix}_2 WHERE rn2 = 1)"""


def _listing_select(portal: str, table: str, name_col: str, addr_col: str, residential_pred: str) -> str:
    """One branch of T_UnionTables (RM or ZL)."""
    return f"""
    SELECT
        '{portal}' AS portal,
        p.listing_id, p.property_identifier, agent_name, agent_address, p.agent_logo,
        first_published_date, listing_update_date, displayable_address, full_property_address,
        uprn, postcode, num_bedrooms,
        CASE
            WHEN LOWER(p.price_frequency) = 'weekly'    THEN ROUND(p.price * 52 / 12)
            WHEN LOWER(p.price_frequency) = 'monthly'   THEN p.price
            WHEN LOWER(p.price_frequency) = 'pppm'      THEN p.price
            WHEN LOWER(p.price_frequency) = 'quarterly' THEN ROUND(p.price / 3)
            WHEN LOWER(p.price_frequency) = 'yearly'    THEN ROUND(p.price / 12)
            ELSE p.price
        END AS price,
        CASE
            WHEN LOWER(p.price_frequency) = 'weekly'    THEN ROUND(p.listed_price * 52 / 12)
            WHEN LOWER(p.price_frequency) = 'monthly'   THEN p.listed_price
            WHEN LOWER(p.price_frequency) = 'pppm'      THEN p.listed_price
            WHEN LOWER(p.price_frequency) = 'quarterly' THEN ROUND(p.listed_price / 3)
            WHEN LOWER(p.price_frequency) = 'yearly'    THEN ROUND(p.listed_price / 12)
            ELSE p.listed_price
        END AS listed_price,
        -- Land Registry actual sold price + its transaction date (source of truth)
        COALESCE(p.pdi_ppd_last_transaction_soldPrice, p.last_transaction_soldPrice) AS sold_price,
        COALESCE(p.pdi_ppd_last_transaction_date, p.last_transaction_date)           AS txn_date,
        outcode, listing_status, display_status, status, listing_update_reason,
        CASE
            WHEN p.property_type IN ('Apartment') THEN 'Flat'
            WHEN p.property_type IN ('End of Terrace') THEN 'end_terrace'
            ELSE REPLACE(REPLACE(p.property_type, '-', '_'), ' ', '_')
        END AS property_type,
        details_url,
        (DATEDIFF(NOW(), first_published_date)) AS listed_days,
        -- RECENT flag: within the activity window
        IF(first_published_date >= DATE_SUB(CURDATE(), INTERVAL {COUNT_LOOKBACK_DAYS} DAY), 1, 0) AS is_recent,
        IF(p.display_status IN ({_SOLD_SET}), 1, 0) AS marked_under_offer_sold,
        IF(IFNULL(p.display_status, '') NOT IN ({_SOLD_SET}) AND p.status IN ('for_sale', 'to_rent'), 1, 0) AS active_listing,
        IF(IFNULL(p.display_status, '') NOT IN ({_SOLD_SET}) AND p.status IN ('removed', 'archived', 'EXPIRED'), 1, 0) AS withdrawn_listing
    FROM PDI_PortalsData.{table} p
    JOIN PDI_PortalsData.pdi_agent_master pam ON (p.agent_name = pam.{name_col} AND p.agent_address = pam.{addr_col})
    JOIN PDI_PortalsData.property_type_mapping p_mapping ON (p.property_type = p_mapping.property_type)
    WHERE pam.id = %(agent_id)s
      AND p_mapping.property_category IN ('flats', 'houses')
      AND p.listing_status IN ('sale', 'new-homes')
      AND p.first_published_date >= DATE_SUB(CURDATE(), INTERVAL {PRICE_LOOKBACK_MONTHS} MONTH)
      AND {residential_pred}"""


FETCH_AGENTS_QUERY = """
SELECT
    p.id,
    COALESCE(p.agent_master_name, p.agent_name_zl, p.agent_name_rm) AS agent_name,
    COALESCE(p.address_zl, p.address_rm) AS agent_address,
    p.agent_logo
FROM PDI_PortalsData.pdi_agent_master p
{where_clause}
ORDER BY p.id
"""

AGENT_STATS_QUERY = f"""
WITH T_UnionTables AS (
    {_listing_select('Rightmove', 'property_details', 'agent_name_rm', 'address_rm', "p.residential = 'YES'")}
    UNION ALL
    {_listing_select('Zoopla', 'property_details_zoopla', 'agent_name_zl', 'address_zl', "p.category = 'residential'")}
),
-- ==== RECENT lineage: activity counts + turnaround (last {COUNT_LOOKBACK_DAYS} days) ====
{_dedup('(SELECT * FROM T_UnionTables WHERE is_recent = 1)', 'T_rec')},
-- ==== MATURE lineage: realised sold-vs-asking (last {PRICE_LOOKBACK_MONTHS} months) ====
{_dedup('T_UnionTables', 'T_all')},
T_sold AS (
    SELECT
        t.listing_id,
        CASE
            WHEN t.listed_price IS NOT NULL AND t.listed_price > {PLACEHOLDER_PRICE} THEN t.listed_price
            WHEN t.price        IS NOT NULL AND t.price        > {PLACEHOLDER_PRICE} THEN t.price
            ELSE NULL
        END AS asking,
        IF((t.listed_price IS NULL OR t.listed_price <= {PLACEHOLDER_PRICE})
           AND t.price > {PLACEHOLDER_PRICE}, 1, 0) AS asking_recovered,
        t.sold_price, t.txn_date, t.first_published_date
    FROM T_all_final t
    WHERE t.marked_under_offer_sold = 1
),
T_valid AS (
    SELECT listing_id, asking_recovered,
           ROUND(((sold_price - asking) / asking) * 100, 2) AS variation_pct
    FROM T_sold
    WHERE asking IS NOT NULL AND asking > 0
      AND sold_price IS NOT NULL AND sold_price > 0
      AND txn_date IS NOT NULL
      AND txn_date >= first_published_date          -- registered sale, after go-live
),
-- ==== robust MAD outlier fence around the RAW median (§4 of the gaps doc) ====
T_med_raw AS (
    SELECT variation_pct,
           ROW_NUMBER() OVER (ORDER BY variation_pct) AS rn,
           COUNT(*)     OVER ()                       AS cnt
    FROM T_valid
),
T_med_raw_val AS (
    SELECT AVG(variation_pct) AS med, MAX(cnt) AS n_valid
    FROM T_med_raw
    WHERE rn IN (FLOOR((cnt + 1) / 2), CEIL((cnt + 1) / 2))
),
T_mad AS (
    SELECT ABS(v.variation_pct - r.med)                                   AS abs_dev,
           ROW_NUMBER() OVER (ORDER BY ABS(v.variation_pct - r.med))      AS rn,
           COUNT(*)     OVER ()                                           AS cnt
    FROM T_valid v CROSS JOIN T_med_raw_val r
),
T_mad_val AS (
    SELECT AVG(abs_dev) AS mad
    FROM T_mad
    WHERE rn IN (FLOOR((cnt + 1) / 2), CEIL((cnt + 1) / 2))
),
T_fence AS (
    -- fence only applies with enough points and a non-degenerate MAD; otherwise keep everything
    SELECT
        r.n_valid,
        CASE WHEN r.n_valid >= {MIN_POINTS_FOR_OUTLIER_FENCE} AND m.mad > 0
             THEN r.med - {MAD_K} * 1.4826 * m.mad END AS lo,
        CASE WHEN r.n_valid >= {MIN_POINTS_FOR_OUTLIER_FENCE} AND m.mad > 0
             THEN r.med + {MAD_K} * 1.4826 * m.mad END AS hi
    FROM T_med_raw_val r CROSS JOIN T_mad_val m
),
T_kept AS (
    -- valid points surviving the fence; this is what the median/mean is computed over.
    -- Excluded points are still counted in no_of_sold_with_soldprice, just not here.
    SELECT v.listing_id, v.variation_pct
    FROM T_valid v CROSS JOIN T_fence f
    WHERE f.lo IS NULL OR v.variation_pct BETWEEN f.lo AND f.hi
),
T_ranked AS (
    SELECT variation_pct,
           ROW_NUMBER() OVER (ORDER BY variation_pct) AS rn,
           COUNT(*)     OVER ()                       AS cnt
    FROM T_kept
),
T_turnaround AS (
    SELECT days_diff FROM (
        SELECT
            DATEDIFF(ph.changed_at,
                IF(t.first_published_date > t.listing_update_date, t.listing_update_date, t.first_published_date)) AS days_diff,
            ROW_NUMBER() OVER (PARTITION BY t.listing_id ORDER BY ph.changed_at DESC) AS rns
        FROM T_rec_final t
        JOIN PDI_PortalsData.property_details_history ph
            ON ph.column_name = 'display_status' AND ph.listing_id = t.listing_id
        WHERE t.portal = 'Rightmove' AND t.marked_under_offer_sold = 1
        UNION ALL
        SELECT
            DATEDIFF(ph.changed_at,
                IF(t.first_published_date > t.listing_update_date, t.listing_update_date, t.first_published_date)) AS days_diff,
            ROW_NUMBER() OVER (PARTITION BY t.listing_id ORDER BY ph.changed_at DESC) AS rns
        FROM T_rec_final t
        JOIN PDI_PortalsData.property_details_zoopla_history ph
            ON ph.column_name = 'display_status' AND ph.listing_id = t.listing_id
        WHERE t.portal = 'Zoopla' AND t.marked_under_offer_sold = 1
    ) x
    WHERE x.rns = 1 AND x.days_diff > 1
)
SELECT
    -- RECENT activity (last {COUNT_LOOKBACK_DAYS} days)
    (SELECT COUNT(*) FROM T_rec_final)                                   AS no_of_listings,
    (SELECT COUNT(*) FROM T_rec_final WHERE active_listing = 1)          AS no_of_live_listings,
    (SELECT COUNT(*) FROM T_rec_final WHERE marked_under_offer_sold = 1) AS no_of_sold_listings,
    (SELECT COUNT(*) FROM T_rec_final WHERE withdrawn_listing = 1)       AS no_of_withdrawn_listing,
    -- MATURE realised-price coverage (last {PRICE_LOOKBACK_MONTHS} months)
    (SELECT COUNT(*) FROM T_all_final WHERE marked_under_offer_sold = 1) AS no_of_sold_mature,
    (SELECT COUNT(*) FROM T_valid)                                       AS no_of_sold_with_soldprice,
    (SELECT COALESCE(SUM(asking_recovered), 0) FROM T_valid)            AS no_of_price_recovered,
    -- points removed from the median by the MAD fence (still counted above, just excluded here)
    ((SELECT COUNT(*) FROM T_valid) - (SELECT COUNT(*) FROM T_kept))    AS no_of_excluded_outliers,
    -- ROBUST headline: MEDIAN of valid, non-outlier sold-vs-ORIGINAL-asking variations
    (
        SELECT ROUND(AVG(variation_pct), 2)
        FROM T_ranked
        WHERE rn IN (FLOOR((cnt + 1) / 2), CEIL((cnt + 1) / 2))
    )                                                                    AS avg_difference_in_percentage,
    (SELECT ROUND(AVG(days_diff)) FROM T_turnaround)                     AS turnaround_days,
    (SELECT agent_logo FROM T_rec_final ORDER BY first_published_date DESC LIMIT 1) AS agent_logo
"""

UPSERT_PERFORMANCE_QUERY_TEMPLATE = """
INSERT INTO {performance_table} (
    agent_master_id,
    agent_name,
    agent_address,
    agent_logo,
    no_of_listings,
    no_of_live_listings,
    no_of_sold_listings,
    no_of_withdrawn_listing,
    no_of_sold_mature,
    no_of_sold_with_soldprice,
    no_of_price_recovered,
    no_of_excluded_outliers,
    avg_difference_in_percentage,
    turnaround_days,
    update_dt
) VALUES (
    %(agent_master_id)s,
    %(agent_name)s,
    %(agent_address)s,
    %(agent_logo)s,
    %(no_of_listings)s,
    %(no_of_live_listings)s,
    %(no_of_sold_listings)s,
    %(no_of_withdrawn_listing)s,
    %(no_of_sold_mature)s,
    %(no_of_sold_with_soldprice)s,
    %(no_of_price_recovered)s,
    %(no_of_excluded_outliers)s,
    %(avg_difference_in_percentage)s,
    %(turnaround_days)s,
    NOW()
)
ON DUPLICATE KEY UPDATE
    agent_name = VALUES(agent_name),
    agent_address = VALUES(agent_address),
    agent_logo = VALUES(agent_logo),
    no_of_listings = VALUES(no_of_listings),
    no_of_live_listings = VALUES(no_of_live_listings),
    no_of_sold_listings = VALUES(no_of_sold_listings),
    no_of_withdrawn_listing = VALUES(no_of_withdrawn_listing),
    no_of_sold_mature = VALUES(no_of_sold_mature),
    no_of_sold_with_soldprice = VALUES(no_of_sold_with_soldprice),
    no_of_price_recovered = VALUES(no_of_price_recovered),
    no_of_excluded_outliers = VALUES(no_of_excluded_outliers),
    avg_difference_in_percentage = VALUES(avg_difference_in_percentage),
    turnaround_days = VALUES(turnaround_days),
    update_dt = NOW()
"""


def build_upsert_query(performance_table: str) -> str:
    return UPSERT_PERFORMANCE_QUERY_TEMPLATE.format(
        performance_table=performance_table
    )
