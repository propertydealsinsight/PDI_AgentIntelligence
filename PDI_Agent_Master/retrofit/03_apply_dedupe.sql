-- ============================================================================
-- STEP 3: the actual write. Only run this after reviewing 02_dry_run_preview.sql's
-- output and being comfortable with the SAFE_DEMOTE counts. Only acts on rows
-- classified SAFE_DEMOTE -- CONFLICT rows are deliberately left untouched for
-- manual review (see 02's comments for why).
--
-- What it does to a SAFE_DEMOTE row: strips the losing side of the pairing
-- (sets that side's name/address to NULL and matching_algo to NULL), it does
-- NOT delete the row outright -- the branch on the surviving side may still be
-- real, it just isn't a match for this specific counterpart. A later, separate
-- cleanup pass removes any single-portal duplicates this creates (a demoted
-- row landing on exactly the same (name,address) as an existing single-portal
-- row for that branch).
--
-- Runs inside a transaction with a full backup taken first. If anything looks
-- wrong after, restore from the backup table named below rather than trying
-- to hand-fix rows.
-- ============================================================================

-- 1. Full backup, timestamped, before touching anything.
SET @backup_name = CONCAT('PDI_PortalsData.pdi_agent_master_dedupe_bkp_', DATE_FORMAT(NOW(), '%Y%m%d%H%i%s'));
SET @sql = CONCAT('CREATE TABLE ', @backup_name, ' AS SELECT * FROM PDI_PortalsData.pdi_agent_master;');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
SELECT @backup_name AS backup_table_created;

-- 2. Materialise the classification from 02 into a real table so the UPDATEs
--    below are driven by a fixed snapshot, not re-evaluated mid-transaction.
DROP TABLE IF EXISTS PDI_PortalsData.agent_master_dedupe_actions;
CREATE TABLE PDI_PortalsData.agent_master_dedupe_actions AS
WITH ranked AS (
    SELECT
        m.id,
        f.in_zl_group, f.in_rm_group,
        e.shared_property_count,
        CASE WHEN f.in_zl_group THEN
            RANK() OVER (PARTITION BY m.agent_name_zl, m.address_zl ORDER BY e.shared_property_count DESC, m.id ASC)
        END AS rn_zl,
        CASE WHEN f.in_rm_group THEN
            RANK() OVER (PARTITION BY m.agent_name_rm, m.address_rm ORDER BY e.shared_property_count DESC, m.id ASC)
        END AS rn_rm
    FROM PDI_PortalsData.agent_master_dedupe_flagged f
    JOIN PDI_PortalsData.agent_master_dedupe_evidence e ON e.id = f.id
    JOIN PDI_PortalsData.pdi_agent_master m ON m.id = f.id
)
SELECT
    r.id,
    CASE
        WHEN (r.in_zl_group AND r.rn_zl > 1) AND (r.in_rm_group AND r.rn_rm > 1) THEN 'CONFLICT_loses_both'
        WHEN (r.in_zl_group AND r.rn_zl > 1) AND (r.in_rm_group AND r.rn_rm = 1) THEN 'CONFLICT_contradicts_rm_win'
        WHEN (r.in_rm_group AND r.rn_rm > 1) AND (r.in_zl_group AND r.rn_zl = 1) THEN 'CONFLICT_contradicts_zl_win'
        WHEN r.in_zl_group AND r.rn_zl > 1 THEN 'SAFE_DEMOTE_null_zl_side'
        WHEN r.in_rm_group AND r.rn_rm > 1 THEN 'SAFE_DEMOTE_null_rm_side'
        ELSE 'KEEP'
    END AS action
FROM ranked r;

SELECT action, COUNT(*) FROM PDI_PortalsData.agent_master_dedupe_actions GROUP BY action;

-- 3. The actual writes, transactional.
START TRANSACTION;

UPDATE PDI_PortalsData.pdi_agent_master m
JOIN PDI_PortalsData.agent_master_dedupe_actions a ON a.id = m.id
SET m.agent_name_zl = NULL, m.address_zl = NULL, m.matching_algo = NULL
WHERE a.action = 'SAFE_DEMOTE_null_zl_side';

UPDATE PDI_PortalsData.pdi_agent_master m
JOIN PDI_PortalsData.agent_master_dedupe_actions a ON a.id = m.id
SET m.agent_name_rm = NULL, m.address_rm = NULL, m.matching_algo = NULL
WHERE a.action = 'SAFE_DEMOTE_null_rm_side';

-- 4. Cleanup: a demoted row can now be an exact duplicate of an existing
--    single-portal row for the same branch (e.g. this RM branch already had
--    its own correct RM-only row before this demotion created a second one).
--    Keep the lowest id, drop the rest, for single-portal rows only.
DELETE t1 FROM PDI_PortalsData.pdi_agent_master t1
JOIN PDI_PortalsData.pdi_agent_master t2
    ON t1.agent_name_rm = t2.agent_name_rm
   AND t1.address_rm = t2.address_rm
   AND t1.agent_name_zl IS NULL AND t2.agent_name_zl IS NULL
   AND t1.id > t2.id
WHERE t1.agent_name_rm IS NOT NULL;

DELETE t1 FROM PDI_PortalsData.pdi_agent_master t1
JOIN PDI_PortalsData.pdi_agent_master t2
    ON t1.agent_name_zl = t2.agent_name_zl
   AND t1.address_zl = t2.address_zl
   AND t1.agent_name_rm IS NULL AND t2.agent_name_rm IS NULL
   AND t1.id > t2.id
WHERE t1.agent_name_zl IS NOT NULL;

-- 5. Check the numbers before committing. Compare against the backup table's
--    row count and the action-type counts from step 2 above -- the delta
--    should roughly equal the number of duplicate-cleanup deletes in step 4.
SELECT
    (SELECT COUNT(*) FROM PDI_PortalsData.pdi_agent_master) AS live_row_count_now,
    @backup_name AS backup_table,
    (SELECT COUNT(*) FROM PDI_PortalsData.agent_master_dedupe_actions WHERE action LIKE 'SAFE_DEMOTE%') AS rows_demoted;

-- If the numbers look right: COMMIT;
-- If anything looks wrong:    ROLLBACK;  -- then investigate against the backup table
