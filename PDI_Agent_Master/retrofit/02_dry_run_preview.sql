-- ============================================================================
-- STEP 2: dry-run preview. Read-only -- writes nothing. Run this and read the
-- output BEFORE ever running 03_apply_dedupe.sql. Requires step 1's evidence
-- table to be at least partially populated (it only ranks rows that have
-- evidence computed; anything still pending shows up in the "not yet
-- evaluated" count at the bottom so you know how complete the picture is).
--
-- Logic: within each duplicate group (same ZL branch claimed by >1 RM address,
-- or same RM branch claimed by >1 ZL address), the row with the strongest
-- shared-listing evidence is the keeper; every other row in that group had
-- the false side of its pairing stripped (demoted to single-portal), not the
-- whole row deleted -- the branch itself may still be real, just not a match
-- for THIS counterpart.
--
-- Three outcomes, deliberately conservative:
--   SAFE_DEMOTE   -- loses in exactly one grouping, not the top pick there,
--                    and isn't itself the top pick being contradicted by the
--                    other grouping. These are what 03_apply_dedupe.sql acts on.
--   CONFLICT      -- loses in one grouping but wins in the other, or loses in
--                    both (would leave an empty row). Left alone -- needs a
--                    human to look at it, not an automated rule.
--   KEEP          -- not a loser anywhere it's flagged.
-- ============================================================================

WITH ranked AS (
    SELECT
        m.id,
        m.agent_name_rm, m.address_rm, m.agent_name_zl, m.address_zl,
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
),
classified AS (
    SELECT
        r.*,
        CASE
            WHEN (r.in_zl_group AND r.rn_zl > 1) AND (r.in_rm_group AND r.rn_rm > 1) THEN 'CONFLICT_loses_both'
            WHEN (r.in_zl_group AND r.rn_zl > 1) AND (r.in_rm_group AND r.rn_rm = 1) THEN 'CONFLICT_contradicts_rm_win'
            WHEN (r.in_rm_group AND r.rn_rm > 1) AND (r.in_zl_group AND r.rn_zl = 1) THEN 'CONFLICT_contradicts_zl_win'
            WHEN r.in_zl_group AND r.rn_zl > 1 THEN 'SAFE_DEMOTE_null_zl_side'
            WHEN r.in_rm_group AND r.rn_rm > 1 THEN 'SAFE_DEMOTE_null_rm_side'
            ELSE 'KEEP'
        END AS action
    FROM ranked r
)
SELECT action, COUNT(*) AS n_rows
FROM classified
GROUP BY action
ORDER BY n_rows DESC;

-- Sample of what SAFE_DEMOTE actually looks like (spot-check before trusting it):
-- WITH ranked AS ( ... same as above ... )
-- SELECT * FROM classified WHERE action LIKE 'SAFE_DEMOTE%' ORDER BY shared_property_count DESC LIMIT 20;

-- How much of the backlog step 1 hasn't reached yet:
SELECT
    (SELECT COUNT(*) FROM PDI_PortalsData.agent_master_dedupe_flagged) AS total_flagged,
    (SELECT COUNT(*) FROM PDI_PortalsData.agent_master_dedupe_evidence) AS evidence_computed,
    (SELECT COUNT(*) FROM PDI_PortalsData.agent_master_dedupe_flagged f
        LEFT JOIN PDI_PortalsData.agent_master_dedupe_evidence e ON e.id = f.id
        WHERE e.id IS NULL) AS still_pending;
