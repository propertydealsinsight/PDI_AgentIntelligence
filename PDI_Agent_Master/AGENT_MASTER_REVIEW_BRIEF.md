# Agent Master (`p_generate_agent_master`) — Review Brief

**Purpose of this document:** starting point for the review of the agent-master
procedure and its process. Everything below was established during the Agent
Performance rebuild (July 2026, see
`../PDI_Agent_Performance/docs/agent_performance_design_and_decisions.md`) —
the numbers are measured from production, not guessed. Status: review
**in progress** — developer (Moiz Travadi) surfaced the first concrete defect
(D7, 2026-07-13) with a real example and diagnostic queries; root cause and
scope confirmed against production, see D7.

---

## 1. What the procedure does today (baseline understanding)

`PDI_PortalsData.p_generate_agent_master` rebuilds `pdi_agent_master` from
scratch on each run (event `event_generate_agent_master`, **ENABLED**, runs on
a schedule — e.g. ran Sat 2026-07-12 15:00):

1. Creates a fresh timestamped table `LIKE pdi_agent_master`; carries forward
   rows with `matching_algo IN ('SAME_AGENT_NAME',
   'NORMALISED_AGENT_NAME_REMOVED_SPACE', 'MANUAL')` plus `MANUAL` rows from
   `pdi_agent_master_probable_match`.
2. **Loop 1** (per outcode from `PropDealsIns.Postcode_sectors`): matches
   unmatched Rightmove branches to Zoopla branches via normalized-name
   similarity (`are_strings_similar`) + shared property signatures
   (`postcode|price|beds|listing_status`). Strategies: `SAME_AGENT_NAME`, then
   `NORMALISED_AGENT_NAME_REMOVED_SPACE`.
3. **Loop 2**: same-signature + similar full-property-address pairs go to
   `pdi_agent_master_probable_match` as `SAME_FULL_ADDRESS` (human review pool).
4. **Loop 3**: remaining unmatched RM-only / ZL-only branches inserted as
   single-portal rows.
5. If the temp table has rows: `RENAME` old master → `pdi_agent_master_bkp`,
   temp → `pdi_agent_master`.

`agent_master_name` = `COALESCE(rm.agent_name, zl.agent_name)` — i.e. **the
Rightmove spelling whenever both portals matched**, otherwise whichever portal
name exists.

## 2. Known defects & design issues (each verified, in priority order)

### D1. `agent_master_id` is NOT stable across rebuilds  ⟵ biggest structural issue
The rebuild inserts WITHOUT ids into a fresh table and rename-swaps, so every
regeneration reassigns all ids. Anything that stores `agent_master_id` (the
agent-performance table does) is only valid against the master snapshot it was
built from. Today this is survivable because the performance job re-runs weekly
(Sun 12:00) after the master event — **that ordering is load-bearing and
undocumented**. Review options: stable ids (match rows to previous snapshot and
carry ids forward), or a durable business key.

### D2. Loop-1 copy-paste bug (confirmed in source)
In loop 1, `temp_rm_property_signatures` builds its "already matched" exclusion
by joining **RM listings against the master's ZL columns**:
`ON p.agent_name = ptmp.agent_name_zl AND p.agent_address = ptmp.address_zl
WHERE ptmp.agent_name_rm IS NULL` — wrong columns (loop 2 does it correctly
with `agent_name_rm`/`address_rm`). Effect: already-matched RM branches are not
excluded from loop-1 signature matching — wasted work and edge-case duplicate
matches.

### D3. Duplicate master rows for the same branch (measured)
- 63,516 master rows → only **59,068 distinct** `(COALESCE(name_zl, name_rm),
  COALESCE(addr_zl, addr_rm))` keys → ~4.4k duplicates overall.
- Among agents active enough to appear in agent performance: 25,785 → 23,159
  distinct (~2.6k duplicates). The performance pipeline resolves these
  last-writer-wins via its unique key; the master itself keeps the duplicates.
- Typical cause: the same Zoopla branch matched to two different RM branches
  (or carried forward + re-matched). Review: unique constraint on the master?
  merge rule?
- **Root cause confirmed 2026-07-13 → see D7**: the matching strategies that
  produce 96% of matches accept a pair on name-similarity + ANY shared
  property signature, with no address check and no minimum evidence threshold
  — this is what's manufacturing the duplicates, not a downstream artifact.
  Fix D7 first; D3's duplicate count should fall sharply as a result.

### D4. Two competing "canonical name" conventions in the estate
- `agent_master_name` = RM-first (this procedure).
- The customer-facing ranking API, the PHP autocomplete, and the agent
  performance table all use **ZL-first** `COALESCE(agent_name_zl,
  agent_name_rm)` — measured: `agent_master_name` differs from the ZL-first key
  for **16,230 / 63,516 rows (25.5%)**.
- There is also **no master address column at all** — every consumer coalesces
  `address_zl` first. So the current convention is "name RM-first, address
  ZL-first", which nobody would design on purpose.
- The label is also unstable: a ZL-only row's label flips to the RM spelling
  when it later matches.
- Decision needed: ONE canonical display name (+ address), stored once, used
  everywhere. Note `agent_master_name` IS used as display label by
  MarketIntelligenceFeed and the dedup/merge procedures — changing its
  semantics needs their sign-off.

### D5. Operational couplings (do not break these during the redesign)
- **Agent Performance** (`../PDI_Agent_Performance/`): reads master rows via
  `FETCH_AGENTS_QUERY` (ZL-first names — deliberate, matches the API join; see
  its design doc D4), pulls listings by the exact rm/zl pairs, stores
  `agent_master_id`. Weekly cron Sunday 12:00 — must stay AFTER any master
  rebuild in the week.
- **MarketIntelligenceFeed**: `agent_master_name` in listing feeds
  (`COALESCE(l.agent_master_name, l.agent_name)`) and in
  `p_find_duplicate_listings` / `p_find_duplicate_exact_agent_listings`.
- **RM/ZL merge procedures** (`pdi_p_merge_zoopla_address_to_rightmove_v4`,
  `pdi_p_merge_rightmove_address_to_zoopla_v2`): use `agent_master_name` in
  similarity checks.
- **Ranking API** (`PropertyDataDBAPIs/routers/Agent/agentPerformance.py`):
  joins `pdi_agent_master` to raw listings by exact rm/zl pairs and to the
  performance table by ZL-first name+address. Customer-facing (agentchecker).
- The rebuild's rename-swap drops/replaces `pdi_agent_master_bkp` — same
  rolling-backup pattern as agent performance.

### D7. Root cause of the 1-branch-to-many mismatches (confirmed 2026-07-13) ⟵ answers D6's open question, likely THE main driver of D3

Developer (Moiz) flagged the concrete failure with a real example: Zoopla
branch `Foxtons - Hemel Hempstead` (75 Waterhouse Street) matches **three**
different Rightmove "Foxtons" rows, two of which are clearly wrong branches —
`Block B, Wilmington Close, Watford` and the unparsed `Foxtons, WD25`
(`pdi_agent_master` ids 278–280). Root cause verified against production
(read-only, capped queries — see [[pdi-portals-readonly-db]]):

- **Strategies 1 & 2 (`SAME_AGENT_NAME` / `NORMALISED_*`) require only
  name-similarity + "at least one shared property signature"
  (`postcode|price|beds|listing_status`) — no minimum count, and no address
  correspondence check between `address_rm` and `address_zl` at all.** For a
  national chain like Foxtons, thousands of listings share the same coarse
  signature purely by chance, so "≥1 shared signature" is not discriminating
  evidence — it's noise.
- Measured with the developer's own `shared_property_count` query: the
  *correct* pair (id 278, same address both sides) has **271** shared
  signatures; the two *wrong* pairs have **3** (id 279, out of 1,404 RM
  listings at that branch) and **1** (id 280, out of 66) — i.e. the wrong
  matches are surviving on essentially coincidental overlap, several orders of
  magnitude below the genuine match.
- **Scope, not an edge case:** of 18,833 distinct matched ZL branches, **3,262
  (17.3%) map to more than one distinct RM address**; of 21,639 distinct
  matched RM branches, **967 (4.5%) map to more than one distinct ZL address**.
  Of all 23,282 matched rows, **22,367 (96%) come from `SAME_AGENT_NAME`**
  alone — i.e. this is the dominant live strategy, and it's the one with the
  gap. **7,075 of those `SAME_AGENT_NAME` rows sit inside a 1-to-many ZL
  duplicate group.**
- This is very likely the dominant contributor to the ~4.4k duplicate rows in
  D3 — a matching bug that *produces* duplicates, not just a downstream
  symptom. Should be fixed together with D3, not separately.

**Suggested fix (plan of action):**
1. Require a **minimum shared-property-count threshold** before accepting a
   `SAME_AGENT_NAME`/`NORMALISED_*` match (not just "≥1"). Pick the cutoff from
   the real distribution — pull `shared_property_count` for a sample of the
   3,262 flagged ZL groups and look at where genuine vs. coincidental pairs
   separate (271 vs. 3/1 in the Foxtons case suggests the gap is wide and a
   modest threshold, e.g. requiring shared count to be some multiple of the
   next-best candidate, would cleanly separate them) — do not guess a fixed
   number without checking the distribution first.
2. Add an **explicit address-correspondence signal** as a required (or
   heavily weighted) condition — e.g. matching postcode sector between
   `address_rm`/`address_zl`, or address-string similarity — so brand-name
   match alone can never carry a pair. This is literally what the developer
   flagged: "we are not matching agent branch address."
3. When a branch has multiple candidate matches on the other portal, **keep
   only the best one** (highest shared-property-count / address-similarity),
   not all candidates that clear the threshold independently — today's loop
   lets every qualifying pair through.
4. **Retrofit the existing ~4k affected rows**: run the shared-property-count
   query across the 3,262+967 flagged groups, keep the strongest pair per
   branch, demote/delete the rest (or route to
   `pdi_agent_master_probable_match` for human review rather than silently
   dropping).
5. Commit the developer's two diagnostic queries (shared-property-count per
   `agent_master_id`; postcode-scoped validation) into this repo as reusable
   validation tools — they're the right building blocks for both the one-off
   cleanup and an ongoing pre-swap gate (see review output #4 below), not just
   one-off review aids.

### D6. Smaller review items
- `INSERT IGNORE` semantics depend on whatever unique keys `pdi_agent_master`
  has — verify what they actually are (unknown as of this brief).
- ~~One RM branch can match multiple ZL branches (and vice versa) — is that
  intended? What dedups it?~~ **Answered 2026-07-13, see D7**: not intended,
  confirmed root cause, nothing dedups it today.
- `pdi_agent_master_probable_match` (`SAME_FULL_ADDRESS` pool): what is the
  human-review workflow? Rows with `NOT_SAME` are kept, everything else deleted
  each run — is the review loop actually happening?
- No logging/metrics: the procedure reports nothing (row counts per strategy,
  new matches, drops). A rebuild that half-fails is invisible (this exact
  scenario is why the performance job now has a pre-swap gate — consider the
  same validate-then-swap pattern here).
- The procedure source lives only in the database — no repo copy. First step of
  the review: `SHOW CREATE PROCEDURE PDI_PortalsData.p_generate_agent_master;`
  and commit the export into this folder as the baseline.

## 3. Suggested review outputs

1. Repo baseline: current procedure source + `pdi_agent_master` DDL (with
   indexes) committed here.
2. Decisions on: stable ids (D1), canonical name+address (D4), duplicate policy
   (D3), minimum-evidence threshold + address-correspondence rule (D7).
3. Fix list, in priority order: **D7's threshold + address check (root cause
   of most duplicates), D2 (quick win, one-line join fix), then D1/D3/D4**.
4. A validate-then-swap gate for the master rebuild (row counts vs previous,
   duplicate keys, share of matched vs single-portal rows, **share of
   ZL/RM branches with >1 match on the other portal — D7's check**) mirroring
   `PDI_Agent_Performance`'s pattern.
5. Documented scheduling contract: master rebuild → then performance refresh.
6. One-off retrofit pass over the ~4k rows currently affected by D7/D3 (see
   D7 step 4) once the threshold/address rule is agreed, so the fix also
   cleans up the existing backlog and not just future rebuilds.

---
*Measurements in this brief: July 2026, production `PDI_PortalsData` via
read-only queries. Cross-reference:
`../PDI_Agent_Performance/docs/agent_performance_design_and_decisions.md`
(gotchas G2, G6 and decision D4 are the same findings from the consumer side).*
