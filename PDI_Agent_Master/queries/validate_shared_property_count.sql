-- Shared listing-signature count for a single pdi_agent_master.id.
-- Use to check the strength of evidence behind any RM<->ZL branch match:
-- a genuine match has shared_property_count in the hundreds; a coincidental
-- one (wrong branch, same brand) is typically single digits. See D7 in
-- ../AGENT_MASTER_REVIEW_BRIEF.md — the Foxtons example measured 271 (correct)
-- vs 3 and 1 (wrong) for the same brand name.
--
-- Author: Moiz Travadi (2026-07-13 review comment). Run read-only.

WITH T_agent_list AS (
    SELECT *
    FROM PDI_PortalsData.pdi_agent_master am
    WHERE am.id = /* :agent_master_id */ 12723
),
T_distinct_rm AS (SELECT DISTINCT t.agent_name_rm, t.address_rm FROM T_agent_list t),
T_distinct_zl AS (SELECT DISTINCT t.agent_name_zl, t.address_zl FROM T_agent_list t),
T_RM_LISTINGS AS (
    SELECT
         agent_name,
         agent_address,
         postcode,
         if(listing_status = 'rent',
            CASE
                WHEN LOWER(p.price_frequency) = 'weekly'    THEN ROUND(p.price * 52 / 12)
                WHEN LOWER(p.price_frequency) = 'monthly'   THEN p.price
                WHEN LOWER(p.price_frequency) = 'pppm'      THEN p.price
                WHEN LOWER(p.price_frequency) = 'quarterly' THEN ROUND(p.price / 3)
                WHEN LOWER(p.price_frequency) = 'yearly'    THEN ROUND(p.price / 12)
                ELSE p.price
            END,
            price
         ) as price,
         num_bedrooms,
         IF(listing_status = 'new-homes', 'sale', listing_status) as listing_status
     FROM T_distinct_rm t
     JOIN PDI_PortalsData.property_details p
     ON t.agent_name_rm = p.agent_name AND t.address_rm = p.agent_address
),
T_ZL_LISTINGS AS (
    SELECT
         agent_name,
         agent_address,
         postcode,
         if(listing_status = 'rent',
            CASE
                WHEN LOWER(p.price_frequency) = 'weekly'    THEN ROUND(p.price * 52 / 12)
                WHEN LOWER(p.price_frequency) = 'monthly'   THEN p.price
                WHEN LOWER(p.price_frequency) = 'pppm'      THEN p.price
                WHEN LOWER(p.price_frequency) = 'quarterly' THEN ROUND(p.price / 3)
                WHEN LOWER(p.price_frequency) = 'yearly'    THEN ROUND(p.price / 12)
                ELSE p.price
            END,
            price
         ) as price,
         num_bedrooms,
         IF(listing_status = 'new-homes', 'sale', listing_status) as listing_status
     FROM T_distinct_zl t
     JOIN PDI_PortalsData.property_details_zoopla p
     ON t.agent_name_zl = p.agent_name AND t.address_zl = p.agent_address
),
T_shared_property_count AS (
    SELECT
         m.id,
         COUNT(DISTINCT rm_sig.property_signature) AS shared_property_count
     FROM PDI_PortalsData.pdi_agent_master m
     INNER JOIN (
         SELECT
             agent_name,
             agent_address,
             CONCAT(postcode, '|', price, '|', num_bedrooms, '|', listing_status) AS property_signature
         FROM T_RM_LISTINGS
     ) rm_sig
         ON rm_sig.agent_name = m.agent_name_rm
        AND rm_sig.agent_address = m.address_rm
     INNER JOIN (
         SELECT
             agent_name,
             agent_address,
             CONCAT(postcode, '|', price, '|', num_bedrooms, '|', listing_status) AS property_signature
         FROM T_ZL_LISTINGS
     ) zl_sig
         ON zl_sig.agent_name = m.agent_name_zl
        AND zl_sig.agent_address = m.address_zl
        AND zl_sig.property_signature = rm_sig.property_signature
     WHERE m.agent_name_rm IS NOT NULL
       AND m.agent_name_zl IS NOT NULL
     GROUP BY m.id
)
SELECT p.*, t.shared_property_count FROM
T_shared_property_count t JOIN PDI_PortalsData.pdi_agent_master p ON t.id = p.id
ORDER BY t.shared_property_count DESC;
