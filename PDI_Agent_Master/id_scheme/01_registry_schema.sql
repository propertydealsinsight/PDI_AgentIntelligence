-- ============================================================================
-- D1 stable-id scheme, step 1: the permanent identity registry.
-- Format decided with the user 2026-07-13: prefixed permanent sequence,
-- e.g. AGT-000123 (not a bare re-assignable int, not a hash, not a UUID).
--
-- THE REAL PROBLEM THIS SOLVES: the format is the easy part. The hard part is
-- that pdi_agent_master's "identity" is a cross-portal MATCH, and matching
-- quality keeps improving (that's what D7 is) -- so a branch that's RM-only
-- today may correctly match a ZL branch next month, and a branch matched to
-- the wrong ZL counterpart today (D7) may get correctly re-paired later. A
-- permanent id can't be pinned to "the match" because the match itself is
-- allowed to change. It has to be pinned to each PORTAL-SIDE branch instead
-- (a physical RM listing-office identity, and a physical ZL listing-office
-- identity, each independently stable), with the master row's id following
-- whichever side already had one, and an alias table recording what happens
-- when two previously-separate registry entries turn out to be the same
-- real branch (a "merge").
--
-- This file is schema only -- safe to create today, changes no existing
-- behaviour (nothing reads from these tables yet). Wiring this into
-- p_generate_agent_master (resolve-or-mint on every rebuild, instead of
-- insert-without-id + rename-swap) is a separate, bigger implementation task
-- -- see the brief's D1 section for the phased sequencing.
-- ============================================================================

-- One row per stable real-world branch identity, on EITHER portal side.
-- rm_key / zl_key are the normalised (agent_name, agent_address) pair that
-- earns this code -- a branch that's only ever appeared on one portal has
-- only that side populated; once it's matched to the other portal, the
-- other side's key gets filled in on the SAME row (no new code minted).
CREATE TABLE IF NOT EXISTS PDI_PortalsData.agent_master_identity_registry (
    agent_master_code   VARCHAR(20) NOT NULL PRIMARY KEY,       -- e.g. 'AGT-000123'
    rm_agent_name       VARCHAR(100) NULL,
    rm_agent_address    VARCHAR(255) NULL,
    zl_agent_name       VARCHAR(100) NULL,
    zl_agent_address    VARCHAR(255) NULL,
    is_active           TINYINT(1) NOT NULL DEFAULT 1,          -- D1's active/inactive flag
    last_listing_dt     DATETIME NULL,                          -- most recent listing seen on either portal; drives is_active
    first_seen_dt       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    update_dt           DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_rm (rm_agent_name, rm_agent_address),
    UNIQUE KEY uq_zl (zl_agent_name, zl_agent_address)
);

-- The sequence itself. A single-row counter table (not AUTO_INCREMENT on the
-- registry's own PK, because the PK is the formatted string, not the raw
-- number) -- read-modify-write this under a row lock when minting a new code.
CREATE TABLE IF NOT EXISTS PDI_PortalsData.agent_master_code_sequence (
    id TINYINT NOT NULL PRIMARY KEY DEFAULT 1,
    next_value INT NOT NULL DEFAULT 1
);
INSERT IGNORE INTO PDI_PortalsData.agent_master_code_sequence (id, next_value) VALUES (1, 1);

-- Records what happens when two previously-separate registry entries (each
-- already carrying their own code because they'd only ever been seen
-- single-portal) turn out to be the same real branch once matched -- e.g. an
-- RM-only branch minted AGT-000045 last year, a ZL-only branch for the same
-- real office minted AGT-000230 separately, and this month's matching run
-- (correctly) pairs them. Policy: the OLDER code survives as canonical; the
-- newer one is retired here so anything that cached it (Agent Performance,
-- a saved report, a support ticket) can still resolve it.
CREATE TABLE IF NOT EXISTS PDI_PortalsData.agent_master_code_aliases (
    retired_code    VARCHAR(20) NOT NULL PRIMARY KEY,
    canonical_code  VARCHAR(20) NOT NULL,
    merged_dt       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    merge_reason    VARCHAR(255) NULL,
    KEY idx_canonical (canonical_code)
);
