"""SQL queries for agent performance processing."""

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

AGENT_STATS_QUERY = """
WITH T_UnionTables AS (
    SELECT
        'Rightmove' AS portal,
        p.listing_id,
        p.property_identifier,
        agent_name,
        agent_address,
        p.agent_logo,
        first_published_date,
        listing_update_date,
        displayable_address,
        full_property_address,
        uprn,
        postcode,
        num_bedrooms,
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
        outcode,
        listing_status,
        display_status,
        status,
        listing_update_reason,
        CASE
            WHEN p.property_type IN ('Apartment') THEN 'Flat'
            WHEN p.property_type IN ('End of Terrace') THEN 'end_terrace'
            ELSE REPLACE(REPLACE(p.property_type, '-', '_'), ' ', '_')
        END AS property_type,
        details_url,
        (DATEDIFF(NOW(), first_published_date)) AS listed_days,
        IF(
            p.display_status IN (
                'Sold STC', 'Under offer', 'Reserved', 'Sold STCM',
                'Sold subject to', 'Under offer', 'Sold subject to contract'
            ),
            1, 0
        ) AS marked_under_offer_sold,
        IF(
            IFNULL(p.display_status, '') NOT IN (
                'Sold STC', 'Under offer', 'Reserved', 'Sold STCM',
                'Sold subject to', 'Under offer', 'Sold subject to contract'
            )
            AND p.status IN ('for_sale', 'to_rent'),
            1, 0
        ) AS active_listing,
        IF(
            IFNULL(p.display_status, '') NOT IN (
                'Sold STC', 'Under offer', 'Reserved', 'Sold STCM',
                'Sold subject to', 'Under offer', 'Sold subject to contract'
            )
            AND p.status IN ('removed', 'archived', 'EXPIRED'),
            1, 0
        ) AS withdrawn_listing
    FROM PDI_PortalsData.property_details p
    JOIN PDI_PortalsData.pdi_agent_master pam ON (
        p.agent_name = pam.agent_name_rm
        AND p.agent_address = pam.address_rm
    )
    JOIN PDI_PortalsData.property_type_mapping p_mapping ON (
        p.property_type = p_mapping.property_type
    )
    WHERE pam.id = %(agent_id)s
      AND p_mapping.property_category IN ('flats', 'houses')
      AND p.listing_status IN ('sale', 'new-homes')
      AND p.first_published_date >= DATE_SUB(CURDATE(), INTERVAL 365 DAY)
      AND p.residential = 'YES'

    UNION ALL

    SELECT
        'Zoopla' AS portal,
        p.listing_id,
        p.property_identifier,
        agent_name,
        agent_address,
        p.agent_logo,
        first_published_date,
        listing_update_date,
        displayable_address,
        full_property_address,
        uprn,
        postcode,
        num_bedrooms,
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
        outcode,
        listing_status,
        display_status,
        status,
        listing_update_reason,
        CASE
            WHEN p.property_type IN ('Apartment') THEN 'Flat'
            WHEN p.property_type IN ('End of Terrace') THEN 'end_terrace'
            ELSE REPLACE(REPLACE(p.property_type, '-', '_'), ' ', '_')
        END AS property_type,
        details_url,
        (DATEDIFF(NOW(), first_published_date)) AS listed_days,
        IF(
            p.display_status IN (
                'Sold STC', 'Under offer', 'Reserved', 'Sold STCM',
                'Sold subject to', 'Under offer', 'Sold subject to contract'
            ),
            1, 0
        ) AS marked_under_offer_sold,
        IF(
            IFNULL(p.display_status, '') NOT IN (
                'Sold STC', 'Under offer', 'Reserved', 'Sold STCM',
                'Sold subject to', 'Under offer', 'Sold subject to contract'
            )
            AND p.status IN ('for_sale', 'to_rent'),
            1, 0
        ) AS active_listing,
        IF(
            IFNULL(p.display_status, '') NOT IN (
                'Sold STC', 'Under offer', 'Reserved', 'Sold STCM',
                'Sold subject to', 'Under offer', 'Sold subject to contract'
            )
            AND p.status IN ('removed', 'archived', 'EXPIRED'),
            1, 0
        ) AS withdrawn_listing
    FROM PDI_PortalsData.property_details_zoopla p
    JOIN PDI_PortalsData.pdi_agent_master pam ON (
        p.agent_name = pam.agent_name_zl
        AND p.agent_address = pam.address_zl
    )
    JOIN PDI_PortalsData.property_type_mapping p_mapping ON (
        p.property_type = p_mapping.property_type
    )
    WHERE pam.id = %(agent_id)s
      AND p_mapping.property_category IN ('flats', 'houses')
      AND p.listing_status IN ('sale', 'new-homes')
      AND p.first_published_date >= DATE_SUB(CURDATE(), INTERVAL 365 DAY)
      AND p.category = 'residential'
),
T_dedup AS (
    SELECT
        t.*,
        ROW_NUMBER() OVER (
            PARTITION BY
                IF(t.listing_status = 'new-homes', 'sale', t.listing_status),
                COALESCE(
                    t.uprn,
                    LOWER(CONCAT(
                        SUBSTRING_INDEX(
                            REPLACE(
                                REPLACE(
                                    REPLACE(
                                        UPPER(
                                            REPLACE(
                                                REPLACE(t.full_property_address, ', ', ' '),
                                                ' and parking space', ''
                                            )
                                        ),
                                        'FLAT ', ''
                                    ),
                                    'APARTMENT ', ''
                                ),
                                'UNIT ', ''
                            ),
                            ' ',
                            3
                        ),
                        ' ',
                        t.postcode
                    )),
                    LOWER(CONCAT_WS('|', t.postcode, t.property_type, t.num_bedrooms, t.price))
                ),
                IF(t.listed_days < 4, 0, t.listed_days)
            ORDER BY t.marked_under_offer_sold DESC, t.first_published_date DESC
        ) AS row_num
    FROM T_UnionTables t
),
T_dedup_one_more_time_with_listing_parameter AS (
    SELECT
        t.*,
        ROW_NUMBER() OVER (
            PARTITION BY
                IF(t.listing_status = 'new-homes', 'sale', t.listing_status),
                LOWER(CONCAT_WS('|', t.postcode, t.property_type, t.num_bedrooms, t.price)),
                IF(t.listed_days < 4, 0, t.listed_days)
            ORDER BY t.marked_under_offer_sold DESC, t.first_published_date DESC
        ) AS row_num_with_listing_parameter
    FROM T_dedup t
    WHERE t.row_num = 1
),
T_final_data AS (
    SELECT *
    FROM T_dedup_one_more_time_with_listing_parameter t
    WHERE t.row_num_with_listing_parameter = 1
),
T_RAW_STATS AS (
    SELECT
        t.*,
        ph.new_value,
        ph.changed_at,
        DATEDIFF(
            ph.changed_at,
            IF(t.first_published_date > t.listing_update_date, t.listing_update_date, t.first_published_date)
        ) AS days_diff,
        ROUND(((ROUND(t.price - COALESCE(t.listed_price, t.price)) / COALESCE(t.listed_price, t.price)) * 100), 2) AS avg_difference_in_percentage,
        ROW_NUMBER() OVER (PARTITION BY t.listing_id ORDER BY ph.changed_at DESC) AS row_num_status
    FROM T_final_data t
    JOIN PDI_PortalsData.property_details_history ph ON (
        ph.column_name = 'display_status'
        AND ph.listing_id = t.listing_id
    )
    WHERE t.portal = 'Rightmove'
      AND t.marked_under_offer_sold = 1

    UNION ALL

    SELECT
        t.*,
        ph.new_value,
        ph.changed_at,
        DATEDIFF(
            ph.changed_at,
            IF(t.first_published_date > t.listing_update_date, t.listing_update_date, t.first_published_date)
        ) AS days_diff,
        ROUND(((ROUND(t.price - COALESCE(t.listed_price, t.price)) / COALESCE(t.listed_price, t.price)) * 100), 2) AS avg_difference_in_percentage,
        ROW_NUMBER() OVER (PARTITION BY t.listing_id ORDER BY ph.changed_at DESC) AS row_num_status
    FROM T_final_data t
    JOIN PDI_PortalsData.property_details_zoopla_history ph ON (
        ph.column_name = 'display_status'
        AND ph.listing_id = t.listing_id
    )
    WHERE t.portal = 'Zoopla'
      AND t.marked_under_offer_sold = 1
),
T_STATS AS (
    SELECT
        (SELECT COUNT(*) FROM T_final_data) AS no_of_listings,
        (SELECT COUNT(*) FROM T_final_data WHERE active_listing = 1) AS no_of_live_listings,
        (SELECT COUNT(*) FROM T_final_data WHERE marked_under_offer_sold = 1) AS no_of_sold_listings,
        (SELECT COUNT(*) FROM T_final_data WHERE withdrawn_listing = 1) AS no_of_withdrawn_listing,
        (
            SELECT ROUND(AVG(days_diff))
            FROM T_RAW_STATS
            WHERE row_num_status = 1
              AND days_diff > 1
        ) AS turnaround_days,
        (
            SELECT ROUND(AVG(avg_difference_in_percentage), 2)
            FROM T_RAW_STATS
            WHERE row_num_status = 1
              AND days_diff > 1
        ) AS avg_difference_in_percentage,
        (
            SELECT agent_logo
            FROM T_final_data
            ORDER BY first_published_date DESC
            LIMIT 1
        ) AS agent_logo
)
SELECT *
FROM T_STATS
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
    avg_difference_in_percentage = VALUES(avg_difference_in_percentage),
    turnaround_days = VALUES(turnaround_days),
    update_dt = NOW()
"""


def build_upsert_query(performance_table: str) -> str:
    return UPSERT_PERFORMANCE_QUERY_TEMPLATE.format(
        performance_table=performance_table
    )
