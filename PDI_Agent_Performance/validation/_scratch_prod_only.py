"""Read-only: how stale are prod rows missing from the new table? Scratch."""
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

cur.execute(f"""
    SELECT /*+ MAX_EXECUTION_TIME(120000) */
        COUNT(*) prod_only,
        SUM(COALESCE(p.update_dt, p.create_dt) >= DATE_SUB(NOW(), INTERVAL 15 DAY)) fresh_15d,
        SUM(p.no_of_sold_listings > 5) sold_gt5,
        SUM(p.no_of_live_listings + p.no_of_sold_listings > 0) any_activity
    FROM {PROD} p
    LEFT JOIN {NEW} n
      ON p.agent_name = n.agent_name AND p.agent_address = n.agent_address
    WHERE n.id IS NULL
""")
print("PROD-only rows (no name+addr match in NEW):", cur.fetchone())

cur.execute(f"""
    SELECT /*+ MAX_EXECUTION_TIME(120000) */ COUNT(*) new_only
    FROM {NEW} n
    LEFT JOIN {PROD} p
      ON p.agent_name = n.agent_name AND p.agent_address = n.agent_address
    WHERE p.id IS NULL
""")
print("NEW-only rows (not in PROD):", cur.fetchone())

# how many prod-only rows would the ranking API's sold>5 filter surface today?
cur.execute(f"""
    SELECT /*+ MAX_EXECUTION_TIME(120000) */
        SUM(p.no_of_sold_listings > 5
            AND COALESCE(p.update_dt, p.create_dt) >= DATE_SUB(NOW(), INTERVAL 15 DAY)) fresh_and_ranked
    FROM {PROD} p
    LEFT JOIN {NEW} n
      ON p.agent_name = n.agent_name AND p.agent_address = n.agent_address
    WHERE n.id IS NULL
""")
print("PROD-only, fresh AND sold>5 (would-be ranking candidates lost on swap):", cur.fetchone())
cur.close(); cx.close()
