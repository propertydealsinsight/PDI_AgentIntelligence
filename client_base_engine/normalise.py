"""Address, postcode, price and date normalisation shared by the book, the event matcher and the tests.

The address key follows the live agent report exactly (PortfolioDashboard/python/fetchAgentData.py): first three
space-separated words of the upper-cased address after ', ' -> ' ', ' and parking space' removed and the words
FLAT / APARTMENT / UNIT dropped, joined to the postcode (taken from the address text first, else the postcode column).
Postcodes are put in one canonical form ("SE3 9LA") on both sides, which the SQL version does not do.
"""
from __future__ import annotations

import datetime as dt
import re

POSTCODE_RE = re.compile(r"[A-Z]{1,2}[0-9][A-Z0-9]?\s*[0-9][A-Z]{2}")


def canon_postcode(pc: str | None) -> str:
    s = re.sub(r"\s+", "", (pc or "").upper())
    if len(s) < 5:
        return s
    return f"{s[:-3]} {s[-3:]}"


def address_postcode(address: str | None, postcode_col: str | None) -> str:
    """Postcode written in the address (handles agents listing from a different postcode), else the column."""
    m = POSTCODE_RE.search((address or "").upper())
    return canon_postcode(m.group(0) if m else postcode_col)


def outcode_of(postcode: str | None) -> str:
    pc = canon_postcode(postcode)
    return pc.split(" ")[0] if pc else ""


def first_three_words(address: str | None) -> str:
    s = (address or "").replace(", ", " ").replace(" and parking space", "").upper()
    s = s.replace("FLAT ", "").replace("APARTMENT ", "").replace("UNIT ", "")
    return " ".join(s.split(" ")[:3])


def address_key(address: str | None, postcode: str | None) -> str:
    """Normalised address key: first three words + canonical postcode."""
    return f"{first_three_words(address)}|{canon_postcode(postcode)}"


def to_dt(x) -> dt.datetime | None:
    if isinstance(x, dt.datetime):
        return x
    if isinstance(x, dt.date):
        return dt.datetime(x.year, x.month, x.day)
    if isinstance(x, str) and x:
        try:
            return dt.datetime.fromisoformat(x[:19])
        except ValueError:
            return None
    return None


def months_between(a: dt.datetime | None, b: dt.datetime | None) -> float | None:
    """(b - a) in 30.4-day months, as the source engines measure it."""
    if not a or not b:
        return None
    return (b - a).days / 30.4


def monthly_rent(price, price_per_month, frequency) -> float | None:
    """Monthly rent from a rental listing, as the agent report does (weekly x52/12, quarterly /3, yearly /12).
    Out-of-range values (under 200 or over 60,000 a month) are treated as unknown, as the rent review does."""
    m = None
    if price_per_month:
        m = float(price_per_month)
    elif price:
        f = (frequency or "").lower()
        p = float(price)
        if "week" in f or f == "pw":
            m = round(p * 52 / 12)
        elif "quarter" in f:
            m = round(p / 3)
        elif "year" in f or "annum" in f or f == "pa":
            m = round(p / 12)
        else:
            m = p
    if m is None or m < 200 or m > 60000:
        return None
    return m
