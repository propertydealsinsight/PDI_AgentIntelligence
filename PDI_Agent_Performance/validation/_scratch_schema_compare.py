"""Read-only: compare prod vs new table schemas + freshness. Deleted after use."""
import pathlib, sys
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
import config
import mysql.connector

cx = mysql.connector.connect(
    host=config.HOST, user=config.USER, password=config.PASSWORD,
    database=config.DATABASE, charset="utf8mb4", autocommit=True,
)
cur = cx.cursor()

TABLES = [
    "pdi_agent_performance",
    "pdi_agent_performance_07072026_09072026081154",
]
cols = {}
for t in TABLES:
    cur.execute(f"SHOW COLUMNS FROM PDI_PortalsData.{t}")
    cols[t] = [(r[0], r[1], r[2], r[4], r[5]) for r in cur.fetchall()]
    print(f"\n=== {t} ===")
    for c in cols[t]:
        print(f"  {c[0]:35} {c[1]:20} null={c[2]} default={c[3]} extra={c[4]}")

old = {c[0] for c in cols[TABLES[0]]}
new = {c[0] for c in cols[TABLES[1]]}
print("\nOnly in PROD :", sorted(old - new))
print("Only in NEW  :", sorted(new - old))

for t in TABLES:
    names = {c[0] for c in cols[t]}
    sel = ["COUNT(*)"]
    for dc in ("update_dt", "create_dt"):
        if dc in names:
            sel.append(f"MAX({dc})")
            sel.append(f"SUM({dc} >= DATE_SUB(NOW(), INTERVAL 15 DAY))")
    cur.execute(f"SELECT /*+ MAX_EXECUTION_TIME(60000) */ {', '.join(sel)} FROM PDI_PortalsData.{t}")
    print(f"\n{t} freshness: {cur.fetchone()}  (cols: {sel})")

cur.close(); cx.close()
