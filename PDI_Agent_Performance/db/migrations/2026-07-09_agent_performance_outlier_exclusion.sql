-- ============================================================================
-- Agent Performance — MAD outlier-exclusion column
-- Date: 2026-07-09
-- Target: pdi_agent_performance_07072026_08072026150455 -- the actual current
-- pipeline OUTPUT table (produced by the 4-hour prod run, being validated now).
--
-- NOTE: pdi_agent_performance_07072026 (no timestamp suffix) is NOT this table --
-- it's the old/defective schema template that config.MAIN_PERFORMANCE_TABLE
-- points at purely so CREATE TABLE ... LIKE has a source to copy. It will be
-- discarded once validation is done. Migrate it too (block below) so the NEXT
-- pipeline run's freshly-created staging table inherits the column automatically
-- -- otherwise every future run needs this migration repeated by hand.
--
-- Why:
--   app/sql/queries.py previously computed the sold-vs-asking median/mean over
--   EVERY valid price point, with no statistical outlier exclusion -- despite
--   docs/agent_performance_gaps_and_fixes.md section 4 documenting a robust
--   MAD fence (median +/- 3.5 * 1.4826 * MAD) as the decided design, and
--   validation/validate_agent_performance.py already implementing it
--   independently. A handful of extreme Land-Registry sale prices were flowing
--   straight into the headline % for low-volume agents (e.g. clamped values up
--   to the DECIMAL(6,2) ceiling of 9999.99). This column makes that exclusion
--   auditable: the listing stays counted in no_of_sold_with_soldprice, only the
--   value is dropped from the average, and now that drop is visible per agent.
--
-- Adding the column alone does NOT retroactively fix rows already written by
-- the old (unfenced) query -- those still hold the old numbers until the
-- pipeline is re-run with the fixed app/sql/queries.py.
-- ============================================================================

-- 1) the actual output table being validated right now
ALTER TABLE `PDI_PortalsData`.`pdi_agent_performance_07072026_08072026150455`
    ADD COLUMN `no_of_excluded_outliers` INT NULL AFTER `no_of_price_recovered`;

-- 2) the schema template every future run's CREATE TABLE ... LIKE copies from --
--    migrate this too or the next run's fresh staging table won't have the column.
ALTER TABLE `PDI_PortalsData`.`pdi_agent_performance_07072026`
    ADD COLUMN `no_of_excluded_outliers` INT NULL AFTER `no_of_price_recovered`;

-- ----------------------------------------------------------------------------
-- Optional: apply to the live prod table too if/when it's brought onto the
-- corrected schema (see the note in 2026-07-08_agent_performance_corrected_metric.sql).
--
-- ALTER TABLE `PDI_PortalsData`.`pdi_agent_performance`
--     ADD COLUMN `no_of_excluded_outliers` INT NULL AFTER `no_of_price_recovered`;
-- ----------------------------------------------------------------------------
