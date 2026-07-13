-- Same shared-property-count validation as validate_shared_property_count.sql,
-- scoped to a postcode sector instead of a single agent_master.id, for
-- spot-checking a whole area's matches at once. See D7 in
-- ../AGENT_MASTER_REVIEW_BRIEF.md.
--
-- Author: Moiz Travadi (2026-07-13 review comment). Run read-only.

set @postcode := /* :postcode_like */ 'HA1 1S%';

WITH T_RM_LISTINGS AS (
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
     FROM PDI_PortalsData.property_details p
     WHERE postcode LIKE @postcode
     AND agent_name IS NOT NULL
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
     FROM PDI_PortalsData.property_details_zoopla p
     WHERE postcode LIKE @postcode
     AND agent_name IS NOT NULL
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
         WHERE postcode LIKE @postcode
         AND agent_name IS NOT NULL
     ) rm_sig
         ON rm_sig.agent_name = m.agent_name_rm
        AND rm_sig.agent_address = m.address_rm
     INNER JOIN (
         SELECT
             agent_name,
             agent_address,
             CONCAT(postcode, '|', price, '|', num_bedrooms, '|', listing_status) AS property_signature
         FROM T_ZL_LISTINGS
         WHERE postcode LIKE @postcode
         AND agent_name IS NOT NULL
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
