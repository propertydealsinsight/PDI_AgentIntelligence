-- ============================================================================
-- STEP 1 of the D7 quick-win retrofit (brief AGENT_MASTER_REVIEW_BRIEF.md, D7
-- item 4 / D3): materialise shared-listing evidence for every row that sits
-- in a one-branch-to-many duplicate group, so we can rank each group and
-- demote the losers. Read-heavy only -- no writes to pdi_agent_master itself.
--
-- Why a procedure and not one query: a single query computing this for all
-- ~8,354 flagged rows at once was tried and timed out against the production
-- read-only execution-time cap (see AGENT_MASTER_REVIEW_BRIEF.md D7 methodology
-- note). Per-branch queries are cheap (~1-3s each, proven on production for
-- branches with 1,400+ listings); this procedure runs them one at a time in a
-- resumable loop instead of one giant join.
--
-- Requires a WRITE-capable account (not the pdi_prospect_readonly account
-- used for read-only analysis in this review -- see [[pdi-portals-readonly-db]]).
-- Expect the full ~8,354-row backlog to take hours; it is safe to stop and
-- re-run -- it only computes evidence for ids not already in
-- agent_master_dedupe_evidence, so a second CALL picks up where the last one
-- left off. Recommended: validate on a slice first (see the LIMIT note in the
-- cursor below) before committing to the full backlog.
-- ============================================================================

-- Flagged rows: every row that shares its ZL branch with >1 distinct RM
-- address, or shares its RM branch with >1 distinct ZL address.
DROP TABLE IF EXISTS PDI_PortalsData.agent_master_dedupe_flagged;
CREATE TABLE PDI_PortalsData.agent_master_dedupe_flagged (
    id INT PRIMARY KEY,
    in_zl_group TINYINT(1) NOT NULL DEFAULT 0,
    in_rm_group TINYINT(1) NOT NULL DEFAULT 0
);

INSERT INTO PDI_PortalsData.agent_master_dedupe_flagged (id, in_zl_group)
SELECT m.id, 1
FROM PDI_PortalsData.pdi_agent_master m
WHERE (m.agent_name_zl, m.address_zl) IN (
    SELECT agent_name_zl, address_zl FROM PDI_PortalsData.pdi_agent_master
    WHERE agent_name_zl IS NOT NULL AND agent_name_rm IS NOT NULL AND address_rm IS NOT NULL
    GROUP BY agent_name_zl, address_zl HAVING COUNT(DISTINCT address_rm) > 1
)
ON DUPLICATE KEY UPDATE in_zl_group = 1;

INSERT INTO PDI_PortalsData.agent_master_dedupe_flagged (id, in_rm_group)
SELECT m.id, 1
FROM PDI_PortalsData.pdi_agent_master m
WHERE (m.agent_name_rm, m.address_rm) IN (
    SELECT agent_name_rm, address_rm FROM PDI_PortalsData.pdi_agent_master
    WHERE agent_name_zl IS NOT NULL AND agent_name_rm IS NOT NULL AND address_zl IS NOT NULL
    GROUP BY agent_name_rm, address_rm HAVING COUNT(DISTINCT address_zl) > 1
)
ON DUPLICATE KEY UPDATE in_rm_group = 1;

-- Evidence results land here. Resumable: re-running the procedure only
-- computes ids not already present.
CREATE TABLE IF NOT EXISTS PDI_PortalsData.agent_master_dedupe_evidence (
    id INT PRIMARY KEY,
    shared_property_count INT NOT NULL,
    computed_dt DATETIME DEFAULT CURRENT_TIMESTAMP
);

DROP PROCEDURE IF EXISTS PDI_PortalsData.p_compute_agent_master_dedupe_evidence;
DELIMITER $$
CREATE DEFINER=`admin`@`%` PROCEDURE `PDI_PortalsData`.`p_compute_agent_master_dedupe_evidence`()
BEGIN
    DECLARE done INT DEFAULT FALSE;
    DECLARE v_id INT;
    DECLARE v_count INT DEFAULT 0;

    -- To validate on a slice first instead of the whole backlog, add
    -- "LIMIT 200" to this cursor's SELECT and re-run -- each call then
    -- picks up the next 200 un-computed ids.
    DECLARE cur CURSOR FOR
        SELECT f.id
        FROM PDI_PortalsData.agent_master_dedupe_flagged f
        LEFT JOIN PDI_PortalsData.agent_master_dedupe_evidence e ON e.id = f.id
        WHERE e.id IS NULL;
    DECLARE CONTINUE HANDLER FOR NOT FOUND SET done = TRUE;
    -- A single id's query hitting the execution-time cap must not kill the
    -- whole batch -- skip it (it stays un-computed and gets retried on the
    -- next CALL) and keep going.
    DECLARE CONTINUE HANDLER FOR SQLEXCEPTION SET v_count = -1;

    OPEN cur;
    read_loop: LOOP
        FETCH cur INTO v_id;
        IF done THEN
            LEAVE read_loop;
        END IF;

        SET v_count = 0;

        WITH T_agent AS (
            SELECT * FROM PDI_PortalsData.pdi_agent_master WHERE id = v_id
        ),
        T_rm_listings AS (
            SELECT
                p.postcode,
                if(p.listing_status = 'rent',
                    CASE WHEN LOWER(p.price_frequency) = 'weekly'    THEN ROUND(p.price * 52 / 12)
                         WHEN LOWER(p.price_frequency) = 'monthly'   THEN p.price
                         WHEN LOWER(p.price_frequency) = 'pppm'      THEN p.price
                         WHEN LOWER(p.price_frequency) = 'quarterly' THEN ROUND(p.price / 3)
                         WHEN LOWER(p.price_frequency) = 'yearly'    THEN ROUND(p.price / 12)
                         ELSE p.price END, p.price) AS price,
                p.num_bedrooms,
                IF(p.listing_status = 'new-homes', 'sale', p.listing_status) AS listing_status
            FROM T_agent t
            JOIN PDI_PortalsData.property_details p
                ON t.agent_name_rm = p.agent_name AND t.address_rm = p.agent_address
        ),
        T_zl_listings AS (
            SELECT
                p.postcode,
                if(p.listing_status = 'rent',
                    CASE WHEN LOWER(p.price_frequency) = 'weekly'    THEN ROUND(p.price * 52 / 12)
                         WHEN LOWER(p.price_frequency) = 'monthly'   THEN p.price
                         WHEN LOWER(p.price_frequency) = 'pppm'      THEN p.price
                         WHEN LOWER(p.price_frequency) = 'quarterly' THEN ROUND(p.price / 3)
                         WHEN LOWER(p.price_frequency) = 'yearly'    THEN ROUND(p.price / 12)
                         ELSE p.price END, p.price) AS price,
                p.num_bedrooms,
                IF(p.listing_status = 'new-homes', 'sale', p.listing_status) AS listing_status
            FROM T_agent t
            JOIN PDI_PortalsData.property_details_zoopla p
                ON t.agent_name_zl = p.agent_name AND t.address_zl = p.agent_address
        ),
        rm_sig AS (SELECT CONCAT(postcode, '|', price, '|', num_bedrooms, '|', listing_status) AS sig FROM T_rm_listings),
        zl_sig AS (SELECT CONCAT(postcode, '|', price, '|', num_bedrooms, '|', listing_status) AS sig FROM T_zl_listings)
        SELECT /*+ MAX_EXECUTION_TIME(15000) */ COUNT(DISTINCT rm_sig.sig) INTO v_count
        FROM rm_sig INNER JOIN zl_sig ON zl_sig.sig = rm_sig.sig;

        IF v_count >= 0 THEN
            INSERT INTO PDI_PortalsData.agent_master_dedupe_evidence (id, shared_property_count)
            VALUES (v_id, v_count)
            ON DUPLICATE KEY UPDATE shared_property_count = VALUES(shared_property_count), computed_dt = CURRENT_TIMESTAMP;
        END IF;
    END LOOP;
    CLOSE cur;
END$$
DELIMITER ;

-- Run it (repeatable/resumable):
-- CALL PDI_PortalsData.p_compute_agent_master_dedupe_evidence();
-- Progress check:
-- SELECT (SELECT COUNT(*) FROM PDI_PortalsData.agent_master_dedupe_evidence) AS done,
--        (SELECT COUNT(*) FROM PDI_PortalsData.agent_master_dedupe_flagged) AS total;
