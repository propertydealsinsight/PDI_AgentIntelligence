# Client-base engine: output contract (schema 1.0)

PROP-1622. This is the contract the Portfolio Dashboard module will read. The rules below are the code in
`events.py` / `engine.py` / `rollup.py`; change both together and bump `schema_version` in `params.py` when a column or
rule changes meaning.

## 1. Who "the branch" is

* An organisation file (`orgs/<name>.json`) lists group -> brands -> branches. Each branch is one or more
  `pdi_agent_master` (agent_name, agent_address) pairs. Sales and lettings departments the portals list separately
  are two pairs in one branch.
* Each pair selects agent master rows exactly as the live agent report does (`fetchAgentData.py`, `T_agent_list`):
  `(agent_name_rm = name OR agent_name_zl = name) AND (address_rm = address OR address_zl = address)`.
  The branch's listings are those whose `(agent_name, agent_address)` equals one of those rows' Rightmove pair
  (`property_details`) or Zoopla pair (`property_details_zoopla`).
* Agent-name prefixes are never used to build a book. `name_prefixes` (per brand) only labels a later listing as
  `sister_brand` (a branch of the same brand that is not in the file).
* `propose-branches` lists candidate agent master rows for a brand (with listing counts per pair, and portal pairs that
  have no agent master row) for a person to confirm.

## 2. The book

Every property the branch has listed on either portal, any date, with `full_property_address` set and a postcode
(the agent report's filters). Optional `book_outcodes` limits it to properties in those outcodes.

Property key, in this order (`match_basis`):

| match_basis | rule |
|---|---|
| `uprn_on_listing` | the listing carries a UPRN, or another of the branch's listings with the same address key does (exactly one UPRN) |
| `uprn_from_address_master` | the address key matches exactly one `pdi_address_master` row in that postcode |
| `address_key` | no UPRN: key = first three words + postcode |

Address key = the agent report's normalisation: `', '` -> `' '`, drop `' and parking space'`, upper-case, drop
`FLAT `, `APARTMENT `, `UNIT `, first three space-separated words, joined to the postcode written in the address
(else the postcode column), in canonical form (`SE3 9LA`).

The branch's **last listing** = its listing on the property with the latest `first_published_date` (either portal;
Rightmove wins a tie).

## 3. Events

Later listings are any agent's listings, on either portal, that (a) are in the book's postcodes or carry one of the
book's UPRNs, (b) were first published within `lookback_months` (24) and (c) match a book property: by UPRN first;
else the address key resolves through the address master to the property's UPRN; else the same address key (but not
when both sides carry different UPRNs). The branch's own listings are never "later listings".

Only later listings first published **after** the branch's last listing count. One agent's adverts of one property
whose first-published dates chain within 92 days are one **episode**: represented by its live copy, else its Rightmove
copy, else the latest-updated copy; a sale episode is dated from its first advert, a let episode from its last.

Definitions used below (`model.py`):

* **live** = status `for_sale`, `to_rent` or `sale_under_offer` and `update_time` within `live_days` (180).
* **agreed** = sale: display `Sold STC` / `Sold subject to` / `Under offer`, or status `sold` / `sale_under_offer`;
  let: display `Let agreed` / `Let`, or status `rented`.
* **last change** = `listing_update_date` (the portal's own), else `first_published_date`. `update_time` is not used
  for end dates: back-office jobs touch it long after a listing has ended (2022 listings show ~760 days later).
* **relation** of a listing = `branch` (this branch's pairs) / `group` (another branch in the file) / `sister_brand`
  (name prefix match) / `competitor` / `unknown` (sold with no identifiable seller).

Each property gets at most **one state event**, chosen in this order, plus a `price_cut` overlay:

| event | exact rule | replaces (old engine label) |
|---|---|---|
| `with_you_now` | the branch has a live listing on it; or (no branch listing live) a later listing by a `group` or `sister_brand` branch is live and not agreed (see `current_agent_relation`). A competitor also live is noted in `detail` | Foxtons `multi_agency` (now a note, not a loss); downstream "retained" |
| `relisted_with_competitor_sale` | branch not live; the latest later **sale** episode by a `competitor` is live and not agreed. `gap_months` = months from the branch's last listing; `detail` says new cycle (gap >= 18) or same cycle | agency_lens `warm_resale` (= this with gap >= 18); Foxtons `switched` (sales); downstream re-sale with a competitor; rent review `forSale` |
| `relisted_with_competitor_let` | as above for a **let** episode | agency_lens `warm_relet` (gap >= 18); Foxtons `switched` (lettings) |
| `sold` | nothing live; most recent of the outcome events below; sale after the branch's last listing and within the lookback: Land Registry (address master `lastSoldDate` by UPRN, or `pdi_ppd_last_transaction_date` on any listing row), else the portal's sold record (`last_transaction_date`), else a sale listing (the branch's last or later) that is agreed, dated by its last change. `current_agent` = the agreed listing, or the latest sale listing first published within 18 months before the sale | Foxtons downstream "sold book" (`land_registry_completion` / `portal_sold_record` / `sold_stc`) |
| `renewal_due` | nothing live; the most recent outcome is a later let episode by a `competitor` (agreed, or ended = presumed let) first published 8.5 to 11.5 months ago | agency_lens `renewal_radar` (warm only; cold renewals are not in a client base) |
| `relet_by_anyone` | as `renewal_due` but outside the window, or let by a `group` / `sister_brand` branch | rent review `reLet` |
| `withdrawn` | nothing live; the most recent outcome is the latest sale listing (the branch's last, or a later one) that is not live, not agreed, has no sale recorded on or after its first publication, and whose last change is within 6 months | eXp `withdrawn_chance` (now limited to the book) |
| `price_cut` (overlay) | the live listing behind `with_you_now` or `relisted_*` is reduced: price at least 1.5% under `listed_price`, or `listing_update_reason = 'price_reduced'`. Carries no fee | eXp `motivated_vendor` (reduction part) |

"Most recent outcome" = whichever of sold / let / withdrawn has the latest date (sale date, let episode date,
last change). Not carried: Foxtons `relet_window` (the branch's own lets ~12 months on belong to the rent review
panel, `observed_rent_review.inc.php`); eXp `luxury_priority` (filter `value_used >= 750000` instead); eXp staleness
(days on market = `event_date` to today).

Thresholds (`params.py`, overridable per organisation under `"params"`, recorded in `summary.json`):
`live_days` 180, `lookback_months` 24, `withdrawn_months` 6, `renewal_min_months` 8.5, `renewal_max_months` 11.5,
`price_cut_min_pct` 1.5, `sale_fee_pct` 1.5, `let_fee_pct` 12.0, `cycle_gap_months` 18 (reported, not a filter).
Fixed in code: episode 92 days, seller window 548 days.

### Fees (`est_fee`)

agency_lens `revenue()` maths: sale events = `sale_fee_pct`% of the sale value; let events = `let_fee_pct`% of 12
months' rent (agency_lens took max(10% let, 12% management) = 12%). Value used (`value_basis`): the event listing's
price (`observed`), else the property's last price in the branch's own listings (`property_history`), else the
branch's median asking price/rent in that outcode (`branch_outcode_median`), else none (fee 0). `sold` uses the sale
price. `price_cut` rows carry 0.

## 4. Output files (`out/<org>/`)

### `events.csv` / `events.jsonl` (same columns, one row per event)

| column | type | meaning |
|---|---|---|
| org, group, brand, branch | text | from the organisation file |
| property_key | text | `U:<uprn>` or `A:<address key>` |
| uprn | text | property UPRN if known |
| address, postcode, outcode | text | from the branch's last listing (postcode canonical) |
| match_basis | enum | section 2 |
| branch_last_type | `sale`/`let` | branch's last listing |
| branch_last_date | date | its first-published date |
| branch_last_price | number | asking price, or monthly rent |
| branch_last_status | text | `status/display_status` |
| branch_last_portal | `rightmove`/`zoopla` | |
| event | enum | section 3 |
| event_date | date | live listing: first published (episode); sold: sale date; let: episode date; withdrawn: last change; price_cut: listing_update_date |
| detail | text | plain-English explanation |
| current_agent | text | agent on the event listing (empty when unknown) |
| current_agent_relation | enum | `branch`, `group`, `sister_brand`, `competitor`, `unknown` |
| current_price | number | sale price or monthly rent on the event (sold: sale price) |
| price_type | `sale`/`monthly_rent` | |
| portal, url | text | event listing's portal and details URL |
| event_address, event_uprn | text | the event listing's own address text and UPRN (to check address-key matches) |
| gap_months | number | months from the branch's last listing to the event listing (relisted / let events) |
| est_fee | number | section 3 |
| fee_basis | `sale`/`let`/`none` | |
| value_used | number | value the fee was computed on (so a UI can recompute with its own rates) |
| value_basis | enum | `observed`, `property_history`, `branch_outcode_median`, `none` |
| confidence | enum | `own_listing` (event on the branch's own listing), `uprn` (event listing carries the property's UPRN, or Land Registry by UPRN), `address` (matched on the address key or through the address master) |

### `book.csv`
One row per (branch, property): the key, address, match_basis, number of the branch's listings, its last listing,
and the property's state event (empty = nothing since). This is the watch list.

### `summary.json`
`schema_version`, `org`, `group`, `run_date`, `params`, `book_outcodes`,
`totals` (group), `brands[] {brand, totals, branches[] {branch, totals, run}}`, `run` (seconds, queries, slowest
query, query errors, skipped chunks, per-brand book diagnostics), `data_limits[]`.
`totals` = `{book_properties, properties_with_event, events{<event>: {count, est_fee}}, opportunity_fee,
pipeline_fee, sold_by_you_fee, sold_by_others_fee, confidence{own_listing, uprn, address}}`.
`run` per branch = agent master rows used, Rightmove/Zoopla pairs, listings per portal, book size by match basis,
own-listing UPRNs, competitor listings in the last 12 months, timings.

`query_log.csv` (every query: label, seconds, rows) and `run.log` are written alongside.

## 5. Roll-up rules

* Branch totals count every row of the branch.
* Brand and group totals count each `(property_key, event)` once, so a property in two branches' books is not double
  counted; branch totals can add up to more than their brand's.
* `opportunity_fee` = relisted_with_competitor_sale + relisted_with_competitor_let + withdrawn + renewal_due.
  `pipeline_fee` = with_you_now. `sold_by_you_fee` / `sold_by_others_fee` = sold split by relation (branch or group
  vs the rest). `relet_by_anyone` and `price_cut` are in no fee pool.

## 6. Known data limits

* Portal listings only; asking prices and rents, not achieved ones. Off-portal instructions are invisible.
* UPRN is filled on about 17% of `property_details` overall (it was 70% on Winkworth Blackheath's own listings, and
  95% of that book was UPRN-keyed after the address master). The rest match on the three-word address key, which can
  merge two flats or miss a reformatted address.
* A branch pair missing from `pdi_agent_master` is invisible; known agent master mis-pairings (review D7) can attach
  a wrong Zoopla branch (seen on KFH Kingston and DJ Alexander Dundas Street rows).
* Let dates are first-published dates; every ended rental is presumed let (some agents never show "let agreed").
* Land Registry lags completions by weeks to months, so recent sales show as `portal_agreed` or `withdrawn` first.
* Later listings are read only within the lookback and only in the book's postcodes or by its UPRNs.
* "Withdrawn" can be a sale that has not yet reached Land Registry; "live" trusts `update_time` within 180 days.

## 7. When it moves into the app

`sql/PROPOSED_client_base_tables.sql` (for review, not run) holds one table per output: runs, branches, book,
events. The agent report's "what happened since" panel reads `events` for one branch; the On-Market Activity rebuild
reads `with_you_now` / `relisted_*` / `price_cut`; the watch list is `book` plus the latest event.
