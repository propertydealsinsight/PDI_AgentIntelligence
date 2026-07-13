# Agent Master (`p_generate_agent_master`) — Review Brief

**Purpose of this document:** starting point for the review of the agent-master
procedure and its process. Everything below was established during the Agent
Performance rebuild (July 2026, see
`../PDI_Agent_Performance/docs/agent_performance_design_and_decisions.md`) —
the numbers are measured from production, not guessed. Status: review
**in progress** — developer (Moiz Travadi) surfaced the first concrete defect
(D7, 2026-07-13) with a real example and diagnostic queries; root cause and
scope confirmed against production, see D7.

**Rollout status (2026-07-13):**
- **Phase 1 — running in production.** User confirmed 2026-07-13 13:31:
  a run has produced `PDI_PortalsData.pdi_agent_master_13072026113200`
  (23,775 rows). **Open question, asked back to the user:** is this the
  corrected main procedure with the live rename-swap enabled (in which case
  `pdi_agent_master` itself is/will be this data), or the renamed test copy
  (`.../procedure/p_generate_agent_master copy.sql`, swap deliberately
  commented out, seeds only `MANUAL`) — and has the run fully finished all
  three loops? Phase 2 depends on the answer (see below), don't guess it.
- **Phase 2 — scripts ready in `PDI_Agent_Master/retrofit/`, sequencing:
  AFTER Phase 1 completes and its output is what's live in
  `pdi_agent_master` — not in parallel.** Reason: step 1's "flagged
  duplicate groups" list and step 3's writes are both computed *from*
  `pdi_agent_master` — running them while Phase 1 is still inserting rows
  (or before Phase 1's corrected data has been swapped in) means Phase 2
  would spend its multi-hour evidence run deduplicating data that's about to
  be replaced, and risks reading a half-written table mid-swap. Nothing
  written yet.
- **Phase 3 — needs design + validation before any code change:** D7's
  evidence threshold + address-correspondence rule itself (items 1–2 of the
  plan below). Not started.
- **Phase 4 (id scheme) — explicitly deferred by the user (2026-07-13):**
  keep the current auto-increment `id` column exactly as it works today for
  now. When implemented, the new `AGT-######` code is added as an
  **additional column alongside the existing id**, not a replacement — it
  only becomes the primary key later, once matching has stabilised (low
  churn, Phases 2–3 bedded in). Schema is written
  (`PDI_Agent_Master/id_scheme/01_registry_schema.sql`) and safe to sit
  unused, but do not wire it into the procedure yet.

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

**Direction agreed with the user (2026-07-13, echoing what they'd already told
Moiz):** `id` should become a permanent master key — assigned once, never
reassigned on rebuild — plus an active/inactive status column so a
decommissioned agent (no longer trading) gets flagged rather than silently
dropped or having its id reissued. Whether this replaces the current `id`
column or sits alongside it is still open, pending impact.

**Impact check (2026-07-13):** grepped every repo this account can reach
(`PDI_AgentIntelligence`, `PropertyDataDBAPIs`, `PDI4Institutions`,
`PDI_InsightJavaRepo2025`, `PDICommercialProspects`, `PDI_ZooplaAdapter`) for
the literal string `agent_master_id`. It appears in exactly one place outside
`PDI_PortalsData` itself: `PDI_Agent_Performance`, where it's written into the
performance table purely as a traceability column (`agent.id` copied in at
upsert time, `app/sql/queries.py` `UPSERT_PERFORMANCE_QUERY_TEMPLATE`). It is
**not** used as a join key anywhere — `FETCH_AGENTS_QUERY` selects `p.id` only
to iterate the current snapshot within a single run; every real lookup (the
performance table's own unique key, and the ranking API's joins in
`PropertyDataDBAPIs/routers/Agent/agentPerformance.py`) goes through
`(agent_name, agent_address)`, ZL-first. Nothing in MarketIntelligenceFeed or
the RM/ZL merge procedures references the column at all. Consequence: the
stored `agent_master_id` in the performance table *already* changes every
week under the current unstable scheme, so nothing today treats it as
durable — making it durable is a **low-impact, additive change** (stabilise
the existing `id` column + add the active/inactive flag), not a downstream
migration. Caveat: this is a repo-wide grep, not exhaustive — worth a final
check closer to implementation for anything outside these repos (ad-hoc
scripts, saved reports).

**Format decided with the user (2026-07-13): a prefixed permanent sequence**,
e.g. `AGT-000123` — not a bare re-assignable int, not a content hash, not a
UUID. Reasoning: human-readable and sortable for anyone debugging in
Workbench, and (unlike a hash) doesn't silently mint a "new" identity if an
agent's name/address text is re-normalised slightly later.

**The real problem is the mechanism, not the format.** `pdi_agent_master`'s
identity is a *cross-portal match*, and match quality is expected to keep
improving (that's what D7 is) — an RM-only branch today may correctly gain a
ZL counterpart next month; a branch wrongly paired today (D7) may get
correctly re-paired later. A permanent id can't be pinned to "the match"
itself, because the match is allowed to change. It has to be pinned to each
**portal-side branch** (an RM listing-office identity and a ZL listing-office
identity, each independently stable on its own), with the master row's id
following whichever side already had one — and a policy for what happens when
two separately-coded single-portal branches turn out to be the same real
branch once matched (a **merge**). This is a standard MDM ("golden record")
problem, not unique to this schema.

**Explicitly deferred by the user (2026-07-13): do not implement yet.** Keep
the current auto-increment `id` exactly as it works today. When this does
get built, the `AGT-######` code is added as an **additional column
alongside the existing id, not a replacement** — it only becomes the primary
identifier later, once matching (Phases 2–3) has bedded in and churn is low.
Minting permanent ids while the matching logic underneath is still being
corrected would lock in identities for entities that are about to be
re-merged/re-split anyway. The plan below is written down so it isn't
re-derived from scratch whenever this is picked back up, not as a to-do for
right now.

**Implementation plan (Phase 4, paused — see above):**
1. **Schema only, safe to ship today** (`PDI_Agent_Master/id_scheme/01_registry_schema.sql`,
   written 2026-07-13, not yet run): a permanent registry
   (`agent_master_identity_registry`, keyed on the RM side and the ZL side of
   each branch independently, `UNIQUE` on each), a single-row sequence counter
   table, and an alias table (`agent_master_code_aliases`) recording
   retired→canonical code mappings when a merge happens. Creating these tables
   changes no existing behaviour — nothing reads from them yet.
2. Rewrite the rebuild's seeding step to **resolve-or-mint** against the
   registry instead of inserting without ids: for every branch (RM side, ZL
   side) the matching loops produce, look up the registry by that side's
   `(agent_name, agent_address)`; reuse its code if found, mint the next
   `AGT-######` if not.
3. **Merge handling**: when a matching run pairs two branches that already
   each carry their own code (one from an old RM-only registry entry, one
   from an old ZL-only entry), keep the *older* code as canonical on the
   master row and write the newer one into `agent_master_code_aliases` — so
   anything that cached the retired code before the merge (a saved report, a
   support ticket) can still resolve it.
4. Active/inactive: set `is_active = 0` on a registry row once neither side
   has had a listing in N months (proposed N=6 — needs a decision, not a
   guess; check the distribution the way every other threshold in this brief
   was checked before picking N).
5. **Real downstream change this time (small, but real — the earlier "no
   impact" finding was about join *logic*, not the column *type*):** the id's
   physical type changes from `int` to `varchar(20)`. Agent Performance's
   `agent_master_id` column needs the matching `ALTER TABLE` — mechanical, one
   migration, but coordinate the timing so it isn't silently truncating/
   mismatching mid-cutover.
6. Validate before cutover the same way the D7 retrofit is being validated:
   dry-run the resolve-or-mint logic against a snapshot, diff against the
   current table's row-for-row identity, confirm every currently-matched
   branch resolves to a sensible code before flipping the live procedure over.

**Separately, on the RM-only/ZL-only single-portal split (63,617-row
composition, §1 of the companion HTML report):** this is *not* itself a
defect. Measured independently of the master table: Rightmove carries 49,364
distinct `(agent_name, agent_address)` pairs in `property_details` vs.
Zoopla's 30,490 in `property_details_zoopla` — Rightmove genuinely has ~62%
more branch coverage, so RM-only rows outnumbering ZL-only rows is expected
portal-coverage skew, not a matching failure. The genuine gap the
active/inactive idea points at is real but distinct from D7: the master has
no concept of "this agent stopped trading," for single-portal and matched
rows alike.

### D2. Loop-1 copy-paste bug (confirmed in source) — fixed
In loop 1, `temp_rm_property_signatures` builds its "already matched" exclusion
by joining **RM listings against the master's ZL columns**:
`ON p.agent_name = ptmp.agent_name_zl AND p.agent_address = ptmp.address_zl
WHERE ptmp.agent_name_rm IS NULL` — wrong columns (loop 2 does it correctly
with `agent_name_rm`/`address_rm`). Effect: already-matched RM branches are not
excluded from loop-1 signature matching — wasted work and edge-case duplicate
matches. **Fixed in commit `1f04fb1`.**

**Also fixed (2026-07-13, user's own catch): unqualified temp tables.** Every
internal `CREATE TEMPORARY TABLE` / `DROP TABLE` / `ALTER TABLE` for
`temp_rm_agents_summary`, `temp_zl_agents_summary`,
`temp_rm_property_signatures`, `temp_zl_property_signatures`, and
`temp_agent_matches` was unqualified, so they were created under whatever
schema happened to be the caller's default at `CALL` time, not necessarily
`PDI_PortalsData` — user reported this exact symptom ("keeps getting created
in wrong schema"). All now qualified with `PDI_PortalsData.`, matching the
established convention in [[feedback_always_qualify_procedure_schema]]. Also
added `DROP PROCEDURE IF EXISTS PDI_PortalsData.p_generate_agent_master;` and
schema-qualified the `CREATE PROCEDURE` line itself, so the file can be
redeployed cleanly (MySQL has no `CREATE OR REPLACE PROCEDURE`). Commit
`4ad64d0`. **None of this is deployed to production yet** — three commits
now sit in the repo (`b750563` baseline, `1f04fb1` D2 fix, `4ad64d0` schema
qualification) waiting on a `DROP PROCEDURE` + `CREATE PROCEDURE` run against
`PDI_PortalsData` before the next scheduled rebuild picks them up.

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
   - **Now confirmed viable with real data (2026-07-13):** Rightmove's own
     `agent_address` field used to be far less detailed (per the user, who
     watched this happen) — confirmed on the Foxtons example: the vague `Foxtons,
     WD25` label's last listing is dated 2025-03-05, and the two properly detailed
     addresses (`75 Waterhouse Street, Hemel Hempstead, HP1 1ED` and `Block B,
     Wilmington Close, Watford, WD18 0FQ`) both start 2025-03-10 — a clean format
     cutover, not a gradual drift. Checked 4 more generic-looking RM addresses
     from the duplicate-group list (`Barnard Marcus, Streatham/Tooting`, `Acorn,
     London Bridge`, `Hose Rhodes Dickson, Newport`): 3 of 4 haven't listed
     anything since 2023–2024 — stale/dead branch labels, not just vague ones (one
     counter-example, bare `Morden`, is still live despite the generic format, so
     recency and address-detail are correlated but not the same signal — combine
     both, don't substitute one for the other).
   - **User's explicit steer:** use RM branch name+address matching as an
     **additional weighted lens layered on top of the property-signature
     evidence, never as a sole source of truth** — the fuzzy-matching
     infrastructure for this already exists (`PDI_PortalsData.are_strings_similar()`
     is already used for name similarity in Strategies 1–2 and for
     `full_property_address` in Strategy 3); extending it to compare
     `agent_address_rm` vs `agent_address_zl` directly is a natural fit, not new
     infrastructure.
3. When a branch has multiple candidate matches on the other portal, **keep
   only the best one** (highest shared-property-count / address-similarity),
   not all candidates that clear the threshold independently — today's loop
   lets every qualifying pair through.
4. **Retrofit the existing ~4k affected rows**: run the shared-property-count
   query across the 3,262+967 flagged groups, keep the strongest pair per
   branch, demote/delete the rest (or route to
   `pdi_agent_master_probable_match` for human review rather than silently
   dropping). **Scripts written 2026-07-13** in `PDI_Agent_Master/retrofit/`:
   `01_compute_dedupe_evidence.sql` (resumable, batched evidence
   materialisation — a single all-rows query timed out, so this computes
   shared-listing counts one branch at a time), `02_dry_run_preview.sql`
   (read-only — classifies every flagged row as safe-to-demote, a genuine
   conflict needing human review, or keep-as-is), `03_apply_dedupe.sql`
   (backs up `pdi_agent_master` first, then only acts on the safe-to-demote
   rows inside a transaction — conflicting rows are deliberately left alone).
   **Not yet run against production** — needs a write-capable account (not the
   readonly one used for this whole review) and the full evidence backlog is
   a multi-hour batch job, so run it off-peak; see the scripts' own comments.
5. Commit the developer's two diagnostic queries (shared-property-count per
   `agent_master_id`; postcode-scoped validation) into this repo as reusable
   validation tools — they're the right building blocks for both the one-off
   cleanup and an ongoing pre-swap gate (see review output #4 below), not just
   one-off review aids. **Done** — see `PDI_Agent_Master/queries/`.

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
- ~~The procedure source lives only in the database — no repo copy.~~ **Done
  2026-07-13**: `procedure/p_generate_agent_master.sql` committed as baseline
  (commit `b750563`), D2 fixed in a separate commit (`1f04fb1`) — not yet
  deployed to production.

## 3. Suggested review outputs

1. ~~Repo baseline: current procedure source~~ **done** (see D6 above);
   `pdi_agent_master` DDL with indexes still to commit.
2. Decisions on: stable ids (D1 — **direction agreed, impact checked low-risk,
   implementation still open**), canonical name+address (D4), duplicate policy
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
