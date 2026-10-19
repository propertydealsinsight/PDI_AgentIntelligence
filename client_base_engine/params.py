"""Thresholds and fee rates. Every value here is written down in CONTRACT.md; change both together.

An organisation file can override any of these under "params"; the run records the values it used in summary.json.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields

SCHEMA_VERSION = "1.0"

EVENTS = (
    "with_you_now",
    "relisted_with_competitor_sale",
    "relisted_with_competitor_let",
    "withdrawn",
    "price_cut",
    "sold",
    "relet_by_anyone",
    "renewal_due",
)

# Events whose est_fee counts toward the win-back opportunity pool in the roll-up.
OPPORTUNITY_EVENTS = ("relisted_with_competitor_sale", "relisted_with_competitor_let", "withdrawn", "renewal_due")


@dataclass(frozen=True)
class Params:
    live_days: int = 180              # a for_sale/to_rent listing counts as live if updated within this many days
    lookback_months: int = 24         # later listings and sold/let/withdrawn events are looked for this far back
    withdrawn_months: int = 6         # a sale listing that ended without selling within this window = withdrawn
    renewal_min_months: float = 8.5   # competitor let first published this long ago ...
    renewal_max_months: float = 11.5  # ... up to this long ago = renewal_due
    price_cut_min_pct: float = 1.5    # price at least this % below the listed (original) price = price cut
    sale_fee_pct: float = 1.5         # est_fee on sale value
    let_fee_pct: float = 12.0         # est_fee on annual rent (agency_lens: max(let 10%, management 12%))
    cycle_gap_months: float = 18.0    # gap that marks a new ownership cycle (old warm_resale rule); reported, not a filter

    @classmethod
    def from_dict(cls, d: dict | None) -> "Params":
        d = d or {}
        known = {f.name for f in fields(cls)}
        unknown = set(d) - known
        if unknown:
            raise ValueError(f"unknown params: {sorted(unknown)}")
        return cls(**{k: type(getattr(cls(), k))(v) for k, v in d.items()})

    def to_dict(self) -> dict:
        return asdict(self)
