-- Scope-audit for D7 (see ../AGENT_MASTER_REVIEW_BRIEF.md): finds every
-- matched ZL branch attached to more than one distinct RM address, and every
-- matched RM branch attached to more than one distinct ZL address. This is
-- the check that should feed the validate-then-swap gate once D7 is fixed
-- (target: both counts near zero after the fix).
--
-- Measured 2026-07-13 on production: 3,262 / 18,833 distinct matched ZL
-- branches (17.3%) and 967 / 21,639 distinct matched RM branches (4.5%) hit
-- this. 22,367 of 23,282 matched rows (96%) come from the SAME_AGENT_NAME
-- strategy, which is the one missing the address check.

-- ZL branches matched to >1 distinct RM address
SELECT
    agent_name_zl, address_zl, COUNT(DISTINCT address_rm) AS n_rm_addr, COUNT(*) AS n_rows
FROM PDI_PortalsData.pdi_agent_master
WHERE agent_name_zl IS NOT NULL AND agent_name_rm IS NOT NULL AND address_rm IS NOT NULL
GROUP BY agent_name_zl, address_zl
HAVING n_rm_addr > 1
ORDER BY n_rm_addr DESC;

-- RM branches matched to >1 distinct ZL address
SELECT
    agent_name_rm, address_rm, COUNT(DISTINCT address_zl) AS n_zl_addr, COUNT(*) AS n_rows
FROM PDI_PortalsData.pdi_agent_master
WHERE agent_name_zl IS NOT NULL AND agent_name_rm IS NOT NULL AND address_zl IS NOT NULL
GROUP BY agent_name_rm, address_rm
HAVING n_zl_addr > 1
ORDER BY n_zl_addr DESC;

-- Summary counts + which matching_algo is driving the affected rows
SELECT COUNT(*) AS n_zl_branches_with_multi_rm FROM (
  SELECT agent_name_zl, address_zl
  FROM PDI_PortalsData.pdi_agent_master
  WHERE agent_name_zl IS NOT NULL AND agent_name_rm IS NOT NULL AND address_rm IS NOT NULL
  GROUP BY agent_name_zl, address_zl
  HAVING COUNT(DISTINCT address_rm) > 1
) t;

SELECT COUNT(*) AS n_rm_branches_with_multi_zl FROM (
  SELECT agent_name_rm, address_rm
  FROM PDI_PortalsData.pdi_agent_master
  WHERE agent_name_zl IS NOT NULL AND agent_name_rm IS NOT NULL AND address_zl IS NOT NULL
  GROUP BY agent_name_rm, address_rm
  HAVING COUNT(DISTINCT address_zl) > 1
) t;

SELECT matching_algo, COUNT(*) FROM PDI_PortalsData.pdi_agent_master m
WHERE (agent_name_zl, address_zl) IN (
  SELECT agent_name_zl, address_zl
  FROM PDI_PortalsData.pdi_agent_master
  WHERE agent_name_zl IS NOT NULL AND agent_name_rm IS NOT NULL AND address_rm IS NOT NULL
  GROUP BY agent_name_zl, address_zl
  HAVING COUNT(DISTINCT address_rm) > 1
)
GROUP BY matching_algo;
