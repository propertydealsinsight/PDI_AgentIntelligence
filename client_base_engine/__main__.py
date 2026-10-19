"""Command line:

  python -m client_base_engine run --org client_base_engine/orgs/<name>.json --out client_base_engine/out/<name>/
  python -m client_base_engine explorer --run client_base_engine/out/<name>/ [--anon] [--name "a London agency group"]
  python -m client_base_engine propose-branches --brand "Winkworth" [--outcodes SE3 SE10] [--address-like Blackheath]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from pathlib import Path

from . import explorer, propose
from .db import ReadOnlyDB
from .engine import run_org
from .org import load_org
from .output import write_run


def _logging(out: Path | None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if out:
        out.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(out / "run.log", mode="w", encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers, force=True)


def cmd_run(a) -> int:
    out = Path(a.out)
    _logging(out)
    org = load_org(a.org)
    started = dt.datetime.now()
    db = ReadOnlyDB(sleep_s=a.sleep)
    try:
        result = run_org(org, db, only_branch=a.branch)
    finally:
        db.close()
    summary = write_run(out, org, result, db.timings, started)
    t = summary["totals"]
    logging.info("done: %d book properties, events %s", t["book_properties"],
                 {k: v["count"] for k, v in t["events"].items()})
    logging.info("opportunity fee £%s, pipeline £%s; %d queries in %ss (slowest %ss) -> %s",
                 f"{t['opportunity_fee']:,}", f"{t['pipeline_fee']:,}", summary["run"]["queries"],
                 summary["run"]["seconds"], summary["run"]["slowest_query_seconds"], out)
    return 0


def cmd_explorer(a) -> int:
    path = explorer.build(a.run, a.out, anon=a.anon, name=a.name)
    print("written", path)
    return 0


def cmd_propose(a) -> int:
    _logging(None)
    db = ReadOnlyDB(sleep_s=a.sleep)
    try:
        res = propose.propose(db, a.brand, [o.upper() for o in a.outcodes or []], a.address_like)
    finally:
        db.close()
    propose.print_result(res)
    if a.save:
        propose.save(res, a.save)
        print("saved", a.save)
    if a.write_draft:
        d = propose.draft_org(res, org=Path(a.write_draft).stem, group=a.group or a.brand[0], brand=a.brand[0])
        Path(a.write_draft).write_text(json.dumps(d, indent=2), encoding="utf-8")
        print("draft org written", a.write_draft, "- confirm every branch before running")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="client_base_engine", description="PDI client-base engine (read-only)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run an organisation file")
    r.add_argument("--org", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--branch", help="run one branch only (others are still resolved, for group/competitor labels)")
    r.add_argument("--sleep", type=float, default=0.3, help="pause between queries, seconds")
    r.set_defaults(fn=cmd_run)
    e = sub.add_parser("explorer", help="build the static HTML explorer from a run folder")
    e.add_argument("--run", required=True)
    e.add_argument("--out")
    e.add_argument("--anon", action="store_true", help="hide competitor names, links, brand and branch names")
    e.add_argument("--name", help='how the organisation is named on the page, e.g. "a London agency group"')
    e.set_defaults(fn=cmd_explorer)
    p = sub.add_parser("propose-branches", help="list candidate agent master rows for a brand")
    p.add_argument("--brand", action="append", required=True, help="agent name prefix (repeatable)")
    p.add_argument("--outcodes", nargs="*")
    p.add_argument("--address-like", help="only rows whose address contains this text")
    p.add_argument("--group")
    p.add_argument("--save", help="save the full listing as JSON")
    p.add_argument("--write-draft", help="write a draft org JSON to this path")
    p.add_argument("--sleep", type=float, default=0.3)
    p.set_defaults(fn=cmd_propose)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
