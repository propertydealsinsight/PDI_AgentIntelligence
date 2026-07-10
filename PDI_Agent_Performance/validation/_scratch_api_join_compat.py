"""Read-only: measure ranking-API join compatibility for prod vs new table.
API joins performance table on name=COALESCE(zl,rm), addr=COALESCE(addr_zl,addr_rm)
from pdi_agent_master. New table stores COALESCE(agent_master_name, zl, rm). Scratch."""
import pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
import config
import mysql.connector

cx = mysql.connector.connect(
    host=config.HOST, user=config.USER, password=config.PASSWORD,
    database=config.DATABASE, charset="utf8mb4", autocommit=True,
)
cur = cx.cursor(dictionary=True)
NEW = "PDI_PortalsData.pdi_agent_performance_07072026_09072026081154"
PROD = "PDI_PortalsData.pdi_agent_performance"

cur.execute("""
    SELECT /*+ MAX_EXECUTION_TIME(60000) */
        COUNT(*) total,
        SUM(agent_master_name IS NOT NULL) has_master_name,
        SUM(agent_master_name IS NOT NULL
            AND agent_master_name <> COALESCE(agent_name_zl, agent_name_rm)) master_name_differs
    FROM PDI_PortalsData.pdi_agent_master
""")
print("pdi_agent_master:", cur.fetchone())

for label, t in (("PROD", PROD), ("NEW", NEW)):
    cur.execute(f"""
        SELECT /*+ MAX_EXECUTION_TIME(120000) */
            COUNT(*) master_rows,
            SUM(t.id IS NOT NULL) api_join_matches
        FROM PDI_PortalsData.pdi_agent_master pam
        LEFT JOIN {t} t
          ON t.agent_name = COALESCE(pam.agent_name_zl, pam.agent_name_rm)
         AND t.agent_address = COALESCE(pam.address_zl, pam.address_rm)
        WHERE COALESCE(pam.agent_name_zl, pam.agent_name_rm) IS NOT NULL
    """)
    print(f"{label}: API-key join matches vs pdi_agent_master:", cur.fetchone())

cur.close(); cx.close()
