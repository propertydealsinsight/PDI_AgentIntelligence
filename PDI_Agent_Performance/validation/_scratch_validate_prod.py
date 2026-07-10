"""Read-only: RAG-score PROD pdi_agent_performance stored values against the
independent recompute, using the SAME seed-42 stratified sample of 100 agents
as the new-table validation run, for a like-for-like comparison. Scratch file."""
import pathlib, sys
from collections import Counter

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from validate_agent_performance import (
    connect, stratified_sample, fetch_agent_raw, recompute, rag,
    stored_stats, table_accessible, _size_band, cap, PROD_TABLE,
)

NEW_TABLE = "PDI_PortalsData.pdi_agent_performance_07072026_09072026081154"
SAMPLE, SEED = 100, 42

cx = connect()
cur = cx.cursor(dictionary=True)
use_master = table_accessible(cur, "PDI_PortalsData.pdi_agent_master")
print(f"mapping: {'EXACT' if use_master else 'APPROX'} | scoring stored values from: {PROD_TABLE}")

agents = stratified_sample(cur, NEW_TABLE, SAMPLE, SEED, None)
print(f"sample: {len(agents)} agents (same seed-42 sample as the new-table run)\n")

tally = Counter()
rows_out = []
for a in agents:
    name, addr = a["agent_name"], a["agent_address"]
    raw, exact, timed_out = fetch_agent_raw(cur, a, use_master)
    if timed_out or not raw:
        tally["SKIPPED"] += 1
        rows_out.append((a["agent_master_id"], name[:38], "", "SKIPPED",
                         "timeout" if timed_out else "no raw listings", None, None, None, None))
        continue
    rc = recompute(raw)
    stored = stored_stats(cur, PROD_TABLE, name, addr)
    verdict, notes = rag(stored, rc)
    tally[verdict] += 1
    rows_out.append((
        a["agent_master_id"], name[:38], _size_band(a.get("no_of_listings") or 0),
        verdict, "; ".join(notes),
        stored["sold"] if stored else None, rc["n_sold"],
        stored["diff"] if stored else None, rc["median_diff"],
    ))

hdr = f"{'id':>6} {'agent':38} {'band':7} {'RAG':7} {'soldS/R':>9} {'diffS/R':>15}  notes"
print(hdr); print("-" * len(hdr))
for r in rows_out:
    mid, nm, band, v, notes, ss, rs, sd, rd = r
    print(f"{str(mid):>6} {nm:38} {band:7} {v:7} "
          f"{str(ss) if ss is not None else '—':>4}/{str(rs) if rs is not None else '—':<4} "
          f"{str(sd) if sd is not None else '—':>7}/{str(rd) if rd is not None else '—':<7} {notes}")

print(f"\nPROD tally: GREEN={tally['GREEN']} AMBER={tally['AMBER']} "
      f"RED={tally['RED']} SKIPPED={tally['SKIPPED']}")
print("(new-table run on the identical sample was: GREEN=75 AMBER=19 RED=6)")

# how many sampled agents are missing from prod entirely
missing = sum(1 for r in rows_out if r[3] == "AMBER" and "not in table" in r[4])
print(f"sampled agents absent from prod table: {missing}")

# prod-only agent count (rows in prod with no name+addr match in new table)
cur.execute(cap(f"""
    SELECT COUNT(*) c FROM {PROD_TABLE} p
    LEFT JOIN {NEW_TABLE} n
      ON p.agent_name=n.agent_name AND p.agent_address=n.agent_address
    WHERE n.id IS NULL
"""))
print(f"prod rows with NO match in new table: {cur.fetchone()['c']:,} (of 40,217)")
cur.close(); cx.close()
print("\nDONE (read-only, no writes).")
