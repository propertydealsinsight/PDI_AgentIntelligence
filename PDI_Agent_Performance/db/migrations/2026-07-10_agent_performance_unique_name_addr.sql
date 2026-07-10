-- ============================================================================
-- Agent Performance — API-compatible naming + unique (agent_name, agent_address)
-- Date: 2026-07-10
--
-- Context (cutover-by-rename plan):
--   The live prod table PDI_PortalsData.pdi_agent_performance has
--       UNIQUE KEY idx_pdi_agent_performance_agent_name_agent_address
--                  (agent_name, agent_address)
--   and the customer-facing API (PropertyDataDBAPIs routers/Agent/
--   agentPerformance.py — same query serves agentchecker.co.uk AND the PDI UI
--   via the Java proxy) joins the table on
--       COALESCE(agent_name_zl, agent_name_rm) + COALESCE(address_zl, address_rm)
--   and implicitly relies on at most ONE row per (name, address).
--
--   app/sql/queries.py FETCH_AGENTS_QUERY was changed on 2026-07-10 to store
--   exactly that COALESCE(zl, rm) key. Measured on 2026-07-10: the pipeline's
--   ~25.8k agents collapse to ~23.2k distinct (name, address) keys, so ~2.6k
--   master rows collide. WITHOUT a unique index the upsert would write them as
--   duplicate rows and the API JOIN would fan out (duplicate agents in customer
--   responses). WITH the index, ON DUPLICATE KEY UPDATE resolves collisions
--   last-writer-wins — the same semantics prod has today.
--
-- Target: pdi_agent_performance_07072026 — the SCHEMA TEMPLATE that
--   create_staging_table() copies via CREATE TABLE ... LIKE. Every future run's
--   staging table inherits this index automatically.
--
--   NOTE: the template still holds the OLD defective data (25,813 rows,
--   only 23,193 distinct name+addr pairs), so ADD UNIQUE would fail on it.
--   Its data is discard-only (kept purely for schema), so it is emptied first.
--   Do NOT run the TRUNCATE against any table whose data you still need.
-- ============================================================================

TRUNCATE TABLE `PDI_PortalsData`.`pdi_agent_performance_07072026`;

ALTER TABLE `PDI_PortalsData`.`pdi_agent_performance_07072026`
    ADD UNIQUE KEY `idx_pdi_agent_performance_agent_name_agent_address`
        (`agent_name`, `agent_address`);

-- ----------------------------------------------------------------------------
-- No change needed on the live pdi_agent_performance (it already has the index)
-- and none on pdi_agent_performance_07072026_09072026081154 (it was produced
-- with the old agent_master_name naming and will be superseded by a fresh run).
-- ----------------------------------------------------------------------------
