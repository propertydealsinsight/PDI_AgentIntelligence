-- ============================================================================
-- Agent Performance — corrected-metric schema changes
-- Date: 2026-07-08
-- Applies to the NEW (Python) output table pdi_agent_performance_<ddmmyyyy>.
-- Run ONCE against the table the Python pipeline writes to, BEFORE running the
-- pipeline with the updated app/sql/queries.py.
--
-- Why:
--   1) avg_difference_in_percentage was decimal(4,2) — hard range ±99.99, so any
--      real value outside that was silently CLAMPED (8 agents were pinned at
--      -99.99). Widen it.
--   2) The corrected metric publishes COVERAGE so a bare average can be audited.
--      NOTE the "two clocks": counts are on the recent window, the price metric on
--      a mature window, so the coverage denominator is no_of_sold_mature (NOT
--      no_of_sold_listings):
--        no_of_sold_mature         -> sold listings in the mature price window
--                                     (last 24 months) -- the coverage denominator
--        no_of_sold_with_soldprice -> of those, how many had a usable Land-Registry
--                                     sold price (the median is based on THESE)
--        no_of_price_recovered     -> of those, how many had their ORIGINAL asking
--                                     recovered from a placeholder/£1 price
-- ============================================================================

-- NOTE: replace the table name with the actual dated table if different.
SET @tbl := 'pdi_agent_performance_07072026';

-- 1) widen the (mis-typed) percentage column so real values are never clamped
ALTER TABLE `PDI_PortalsData`.`pdi_agent_performance_07072026`
    MODIFY COLUMN `avg_difference_in_percentage` DECIMAL(6,2) NULL;

-- 2) add coverage / provability columns
ALTER TABLE `PDI_PortalsData`.`pdi_agent_performance_07072026`
    ADD COLUMN `no_of_sold_mature`         INT NULL AFTER `no_of_withdrawn_listing`,
    ADD COLUMN `no_of_sold_with_soldprice` INT NULL AFTER `no_of_sold_mature`,
    ADD COLUMN `no_of_price_recovered`     INT NULL AFTER `no_of_sold_with_soldprice`;

-- ----------------------------------------------------------------------------
-- Optional but recommended for the live prod table too (populated by the
-- procedure) so both sources of truth carry the same columns and can be
-- validated side-by-side. Uncomment when ready:
--
-- ALTER TABLE `PDI_PortalsData`.`pdi_agent_performance`
--     MODIFY COLUMN `avg_difference_in_percentage` DECIMAL(6,2) NULL,
--     ADD COLUMN `no_of_listings`            INT NULL,
--     ADD COLUMN `no_of_withdrawn_listing`   INT NULL,
--     ADD COLUMN `no_of_sold_mature`         INT NULL,
--     ADD COLUMN `no_of_sold_with_soldprice` INT NULL,
--     ADD COLUMN `no_of_price_recovered`     INT NULL;
-- ----------------------------------------------------------------------------
