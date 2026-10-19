# Client-base engine (PROP-1622)

For any agency organisation: every property each branch has marketed (Rightmove and Zoopla, any date), what has
happened to it since the branch last listed it, and branch -> brand -> group totals by event and fee.
Generalises `agency_lens.py`, the Foxtons re-engagement and downstream engines, and the eXp signals, and identifies
branches the way the live agent report does (`pdi_agent_master` pairs, as in `fetchAgentData.py`).

Read-only against production: every query is a `SELECT /*+ MAX_EXECUTION_TIME(n) */` (n <= 55s) on an indexed
predicate, run one at a time with a short pause, logged to `query_log.csv`. The login comes only from the approved
runner `~/.claude/tools/rodb.py`; nothing here opens a config or credential file. Needs Python 3.13+ and `pymysql`.

Rules, columns and limits: [CONTRACT.md](CONTRACT.md). Proposed app tables: [sql/PROPOSED_client_base_tables.sql](sql/PROPOSED_client_base_tables.sql).

## Run it for a new organisation in 5 steps

Run from the repo root (`C:\PDI\GitHub\PDI_AgentIntelligence`).

1. **List candidate branches** for each brand (add `--outcodes` to keep it to an area, `--address-like` for one office):

   ```
   python -m client_base_engine propose-branches --brand "Winkworth" --outcodes SE3 SE10 --address-like Blackheath
   ```

   Each row is a `pdi_agent_master` row with its Rightmove and Zoopla pair and listing counts. Pairs with listings but
   no agent master row are listed separately: the live report and this engine cannot see them.

2. **Write the organisation file** `client_base_engine/orgs/<name>.json` (`--write-draft orgs/<name>.json` on step 1
   gives a draft with one branch per row). Group a branch's sales and lettings rows into one branch; tell franchises
   apart by address. See `orgs/winkworth_blackheath.json` (one branch) and `orgs/lomond_18oc.json` (3 brands, 31
   branches, `book_outcodes` to limit the book). Fee rates and thresholds can be overridden under `"params"`.

3. **Run it:**

   ```
   python -m client_base_engine run --org client_base_engine/orgs/<name>.json --out client_base_engine/out/<name>/
   ```

   Writes `events.csv`, `events.jsonl`, `book.csv`, `summary.json`, `query_log.csv`, `run.log`. `--branch "<name>"`
   runs one branch. `out/` is git-ignored: runs contain addresses, never commit them.

4. **Check it:** `summary.json` -> `brands[].branches[].run` shows the agent master rows and pairs each branch used,
   listings per portal, book size by match basis and the slowest query. Spot-check a few `events.csv` rows against
   their `url`, and the `address`-confidence rows against `event_address`.

5. **Build the explorer** (static PDI-branded page, no server):

   ```
   python -m client_base_engine explorer --run client_base_engine/out/<name>/
   python -m client_base_engine explorer --run client_base_engine/out/<name>/ --anon --name "a London agency group"
   ```

   `--anon` hides every agent name, listing link, brand and branch name (property addresses stay).

Tests (no database): `python -m unittest` from the repo root.

## Validation runs, 18 Oct 2026

**Winkworth Blackheath** (agent master 4117; one branch): 3,688 listings (2,079 Rightmove, 1,609 Zoopla) ->
1,616 properties (1,381 UPRN on listing, 154 UPRN from the address master, 81 address key). 36 queries, 13-24s over five runs,
slowest single query 1.4s. Events: with_you_now 151, relisted for sale with a competitor 44, relisted to let 8, withdrawn 47, sold 388
(Land Registry or portal; £2.7m of fee value sold by the branch, £1.4m by others), let by someone else 100, renewal due 3,
price cuts 82. Win-back fee pool £1.05m. Reconciles with the live report's figures: 1,183 distinct UPRNs on the
branch's Rightmove listings (exact match), and 222 Rightmove + 194 Zoopla non-Winkworth listings on those UPRNs in the
last 12 months (416 against the ~412 quoted). The live report's 1,529 matched properties counts only properties that
join to the address master (by code reading of `fetchAgentData.py`); this book also keeps the rest. The two counts
were not reconciled property by property.

**Lomond, same 18 outcodes as 4 Oct** (`orgs/lomond_18oc.json`; 31 agent master branches: KFH 15, Linley & Simpson
12, DJ Alexander 4, every agent master row with listings there): 13,886 properties; 395 queries, 176-225s over four runs, slowest
single query 4.1s. The reconciliation script and its output are in `out/lomond_18oc/` (`compare_lomond.py`,
`compare_4oct.txt`); the findings are summarised in the PROP-1622 hand-over.
