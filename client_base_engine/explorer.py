"""Static PDI-branded HTML explorer for one run (events.csv + summary.json), no server and no network calls.

Same look as AgencyGroups_Winkworth_KFH/engine/build_agency_explorer.py (navy/cyan, KPI tiles, league table,
opportunity monitor, adjustable fee rates). --anon hides competitor names, listing links, and brand/branch names, and
--name sets how the organisation is described on the page.
"""
from __future__ import annotations

import csv
import html
import json
from pathlib import Path

from .params import EVENTS, OPPORTUNITY_EVENTS

LOGO = Path(__file__).parent / "assets" / "pdi_logo_datauri.txt"

LABELS = {
    "with_you_now": "With you now",
    "relisted_with_competitor_sale": "Relisted for sale with a competitor",
    "relisted_with_competitor_let": "Relisted to let with a competitor",
    "withdrawn": "Withdrawn",
    "price_cut": "Price cut",
    "sold": "Sold",
    "relet_by_anyone": "Let by someone else",
    "renewal_due": "Competitor renewal due",
}
COLOURS = {
    "with_you_now": "#5B6B7C", "relisted_with_competitor_sale": "#002B5C", "relisted_with_competitor_let": "#00ADEF",
    "withdrawn": "#8A4FBF", "price_cut": "#B5452B", "sold": "#2E7D4F", "relet_by_anyone": "#7A8BA0",
    "renewal_due": "#C98A14",
}

CSS = """
:root{--navy:#002B5C;--cyan:#00ADEF;--tint:#E5F7FD;--ink:#0F1B2D;--paper:#F4F7FA;--card:#fff;--line:#DDE5EE;--muted:#5B6B7C}
*{box-sizing:border-box}body{margin:0;font:15px/1.55 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:var(--ink);background:var(--paper)}
header.top{background:var(--navy);color:#fff}.topbar{display:flex;align-items:center;justify-content:space-between;padding:16px 40px;gap:20px}
.topbar img{height:34px;background:#fff;border-radius:6px;padding:5px 9px}.topbar .who{font-size:12px;color:#9fd8f2;text-transform:uppercase;letter-spacing:.6px;font-weight:600}
.hbar{background:var(--cyan);height:4px}.htitle{padding:18px 40px 22px}.htitle h1{margin:0 0 4px;font-size:24px}.htitle .sub{color:#bcd3e8;font-size:14px}
.pill{display:inline-block;background:#ffffff1a;border:1px solid #ffffff38;color:#e8f6fd;padding:3px 10px;border-radius:20px;font-size:12px;margin:10px 6px 0 0}
nav{position:sticky;top:0;z-index:30;background:#fff;border-bottom:1px solid var(--line);padding:8px 40px;display:flex;gap:6px;flex-wrap:wrap;align-items:center;box-shadow:0 1px 4px #0001}
nav a{font-size:13px;text-decoration:none;color:var(--muted);padding:6px 10px;border-radius:6px}nav a:hover{background:var(--paper);color:var(--ink)}
nav .rv{margin-left:auto;display:flex;gap:8px;align-items:center;font-size:12px;color:var(--muted)}
nav .rv input{width:60px;padding:5px 7px;border:1px solid var(--line);border-radius:6px;font-size:13px}
.wrap{max-width:1280px;margin:0 auto;padding:24px 40px 80px}
section{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:24px 26px;margin:20px 0}
section h2{margin:0 0 4px;font-size:20px;color:var(--navy)}.lead{color:var(--muted);margin:0 0 16px;font-size:14px}
.kpis{display:grid;grid-template-columns:repeat(5,1fr);gap:12px}.kpi{border:1px solid var(--line);border-radius:10px;padding:15px}
.kpi .n{font-size:24px;font-weight:700;color:var(--navy)}.kpi .l{font-size:11.5px;color:var(--muted);margin-top:6px}.kpi .s{font-size:11px;color:var(--muted)}
.kpi.big{background:var(--navy);border-color:var(--navy)}.kpi.big .n{color:#fff;font-size:27px}.kpi.big .l,.kpi.big .s{color:#bcd3e8}
.ucards{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}.ucard{border:1px solid var(--line);border-radius:10px;padding:14px;border-top:4px solid var(--c);cursor:pointer}
.ucard .h{font-weight:700;font-size:13.5px}.ucard .n{font-size:22px;font-weight:700;color:var(--navy);margin-top:4px}.ucard p{font-size:12px;color:var(--muted);margin:6px 0 0}
.pickbar{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px}.pickbar select,.pickbar input{padding:9px 11px;border:1px solid var(--line);border-radius:8px;font-size:14px;background:#fff}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:8px 9px;border-bottom:1px solid var(--line);vertical-align:top}
th{position:sticky;top:0;background:#F8FAFC;cursor:pointer;font-size:12px;color:#3d4b5a;white-space:nowrap}tr.row:hover{background:var(--tint)}
.badge{display:inline-block;padding:3px 9px;border-radius:6px;font-size:11px;font-weight:600;color:#fff;white-space:nowrap}
.tag{display:inline-block;background:var(--tint);color:var(--navy);border-radius:5px;padding:1px 6px;font-size:11px}.fee{font-weight:700;color:var(--navy)}
.scrollx{overflow:auto;max-height:620px;border:1px solid var(--line);border-radius:8px}
.note{background:var(--tint);border:1px solid #bfe6f7;border-radius:8px;padding:11px 13px;font-size:13px;color:#0b4a6b;margin-top:14px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:22px}.grid2 ul{font-size:13px;color:#3d4b5a}
.foot{color:var(--muted);font-size:12px;text-align:center;margin-top:28px}.count{color:var(--muted);font-size:13px}
@media(max-width:900px){.kpis{grid-template-columns:repeat(2,1fr)}.ucards,.grid2{grid-template-columns:1fr 1fr}.wrap,.topbar,.htitle,nav{padding-left:16px;padding-right:16px}}
"""

BODY = """
<header class="top"><div class="topbar">__LOGO__<span class="who">Client-base intelligence</span></div><div class="hbar"></div>
<div class="htitle"><h1>What happened to __ORG__'s client base</h1>
<div class="sub">Every property __ORG__ has marketed, on Rightmove and Zoopla, matched to what has happened since &middot; PDI read-only production data, run __DATE__</div>
<div><span class="pill" id="pBook"></span><span class="pill" id="pBr"></span><span class="pill">Observable evidence only</span><span class="pill">PDI holds no client contacts</span></div></div></header>
<nav><a href="#overview">Overview</a><a href="#events">Events</a><a href="#league">Branches</a><a href="#monitor">Monitor</a><a href="#method">Method</a>
<div class="rv"><span>Sales %</span><input type="number" id="pS" step="0.1" min="0"><span>Lettings %</span><input type="number" id="pL" step="0.5" min="0"></div></nav>
<div class="wrap">
<section id="overview"><h2>Win-back fee pool</h2><p class="lead">Properties __ORG__ marketed before, with a fresh event now. Fee rates are adjustable above.</p>
<div class="kpis">
<div class="kpi"><div class="n" id="kBook">-</div><div class="l">Properties in the book</div><div class="s" id="kBookS"></div></div>
<div class="kpi"><div class="n" id="kNow">-</div><div class="l">With you now</div><div class="s" id="kNowF"></div></div>
<div class="kpi"><div class="n" id="kOpp">-</div><div class="l">Win-back opportunities</div><div class="s">relisted elsewhere, withdrawn, renewal due</div></div>
<div class="kpi"><div class="n" id="kSold">-</div><div class="l">Sold since your last listing</div><div class="s" id="kSoldS"></div></div>
<div class="kpi big"><div class="n" id="kFee">-</div><div class="l">Illustrative win-back fee pool</div><div class="s">Before CRM match, eligibility and conversion</div></div>
</div></section>
<section id="events"><h2>Events</h2><p class="lead">One state per property, plus price cuts on live listings. Click a card to filter the monitor.</p><div class="ucards" id="cards"></div></section>
<section id="league"><h2>Branches <span class="count" id="lgC"></span></h2><p class="lead">Click a branch to filter the monitor. Brand and group totals count a property once.</p>
<div class="scrollx"><table id="lg"><thead><tr><th data-k="brand">Brand</th><th data-k="branch">Branch</th><th data-k="book">Book</th>__EVTH__<th data-k="fee">Win-back fee</th></tr></thead><tbody></tbody></table></div></section>
<section id="monitor"><h2>Opportunity monitor <span class="count" id="mnC"></span></h2><p class="lead">Who to call, when and why. Sort by any column.</p>
<div class="pickbar"><select id="fE"><option value="">All events</option></select><select id="fB"><option value="">All branches</option></select>
<select id="fC"><option value="">Any match</option><option value="own_listing">Own listing</option><option value="uprn">UPRN</option><option value="address">Address</option></select>
<input id="fQ" placeholder="search address, postcode or agent" style="flex:1;min-width:200px"></div>
<div class="scrollx"><table id="mn"><thead><tr><th data-k="branch">Branch</th><th data-k="event">Event</th><th data-k="address">Property</th><th data-k="branch_last_date">Your last listing</th><th data-k="event_date">Event date</th><th data-k="detail">What happened</th><th data-k="current_agent">Now with</th><th data-k="confidence">Match</th><th data-k="_fee">Fee</th></tr></thead><tbody></tbody></table></div>
<div class="note">Fee = Sales % of the sale value, or Lettings % of a year's rent, using the observed price, else this property's own history, else the branch's median in that outcode. Price-cut rows carry no fee; the property's state row already does.</div></section>
<section id="method"><h2>Method and caveats</h2><div class="grid2">
<div><b>How a property gets its event</b><ul>
<li>The book: every property the branch has listed on either portal, any date, keyed by UPRN, else first three address words + postcode.</li>
<li>With you now: the branch's own listing is live (updated in the last __LIVE__ days).</li>
<li>Relisted with a competitor: another agent listed it after the branch's last listing and it is live and not yet agreed.</li>
<li>Otherwise the latest of: sold (Land Registry, else portal sold record, else sold STC), let by someone else (renewal due when a competitor let it __RMIN__ to __RMAX__ months ago), or withdrawn (sale ended without an offer in the last __WD__ months).</li>
<li>Price cut: the live listing has been reduced.</li></ul></div>
<div><b>Caveats</b><ul id="limits"></ul></div></div>
<div class="note">Governance: PDI returns signals on addresses. __ORG__ matches them to its own CRM and keeps all customer and contact data.</div></section>
<div class="foot">Property Deals Insight &middot; read-only production analysis, __DATE__ &middot; fee rates illustrative and adjustable</div></div>
"""

JS = r"""
const D=__BLOB__,R=D.rows,$=id=>document.getElementById(id),EV=D.events,LB=D.labels,COL=D.colours,OPP=D.opp;
$('pS').value=D.params.sale_fee_pct;$('pL').value=D.params.let_fee_pct;
const gp=()=>({s:+$('pS').value||0,l:+$('pL').value||0});
const fee=(o,p)=>o.fee_basis==='sale'?(o.value_used||0)*p.s/100:o.fee_basis==='let'?(o.value_used||0)*12*p.l/100:0;
const money=v=>{v=Math.round(v);return v>=1e6?'£'+(v/1e6).toFixed(2)+'M':v>=1e3?'£'+(v/1e3).toFixed(0)+'k':'£'+v.toLocaleString()};
const esc=s=>(''+(s??'')).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const bk=o=>o.brand+' / '+o.branch;
const once=rows=>{const s=new Set();return rows.filter(o=>{const k=o.property_key+'|'+o.event;if(s.has(k))return false;s.add(k);return true})};
EV.forEach(e=>{const o=document.createElement('option');o.value=e;o.textContent=LB[e];$('fE').appendChild(o)});
D.branches.forEach(b=>{const o=document.createElement('option');o.value=b.brand+' / '+b.branch;o.textContent=b.brand+' / '+b.branch;$('fB').appendChild(o)});
D.limits.forEach(t=>{const li=document.createElement('li');li.textContent=t;$('limits').appendChild(li)});
$('pBook').textContent=D.book.toLocaleString()+' properties';$('pBr').textContent=D.branches.length+' branch'+(D.branches.length>1?'es':'');
function recalc(){const p=gp(),G=once(R),sum=a=>a.reduce((s,o)=>s+fee(o,p),0),by=e=>G.filter(o=>o.event===e);
 $('kBook').textContent=D.book.toLocaleString();$('kBookS').textContent=D.uprnShare+'% keyed by UPRN';
 $('kNow').textContent=by('with_you_now').length.toLocaleString();$('kNowF').textContent=money(sum(by('with_you_now')))+' pipeline';
 const opp=G.filter(o=>OPP.includes(o.event));$('kOpp').textContent=opp.length.toLocaleString();$('kFee').textContent=money(sum(opp));
 const sold=by('sold');$('kSold').textContent=sold.length.toLocaleString();$('kSoldS').textContent=sold.filter(o=>!['branch','group'].includes(o.current_agent_relation)).length+' marketed by others';
 const c=$('cards');c.innerHTML='';EV.forEach(e=>{const a=by(e),d=document.createElement('div');d.className='ucard';d.style.setProperty('--c',COL[e]);
  d.innerHTML=`<div class="h">${LB[e]}</div><div class="n">${a.length.toLocaleString()}</div><p>${e==='price_cut'?'overlay, no fee':money(sum(a))}</p>`;
  d.onclick=()=>{$('fE').value=e;mon();$('monitor').scrollIntoView({behavior:'smooth'})};c.appendChild(d)});league();mon()}
let ls={k:'fee',d:-1},ms={k:'_fee',d:-1};
function league(){const p=gp();const r=D.branches.map(b=>{const rows=R.filter(o=>o.brand===b.brand&&o.branch===b.branch),x={brand:b.brand,branch:b.branch,book:b.book};
 EV.forEach(e=>x[e]=rows.filter(o=>o.event===e).length);x.fee=rows.filter(o=>OPP.includes(o.event)).reduce((s,o)=>s+fee(o,p),0);return x});
 r.sort((a,b)=>typeof a[ls.k]==='string'?ls.d*a[ls.k].localeCompare(b[ls.k]):ls.d*(a[ls.k]-b[ls.k]));
 const tb=document.querySelector('#lg tbody');tb.innerHTML='';r.forEach(x=>{const tr=document.createElement('tr');tr.className='row';
 tr.innerHTML=`<td>${esc(x.brand)}</td><td><b>${esc(x.branch)}</b></td><td>${x.book.toLocaleString()}</td>`+EV.map(e=>`<td>${x[e]}</td>`).join('')+`<td class="fee">${money(x.fee)}</td>`;
 tr.onclick=()=>{$('fB').value=x.brand+' / '+x.branch;mon();$('monitor').scrollIntoView({behavior:'smooth'})};tb.appendChild(tr)});$('lgC').textContent='('+r.length+')'}
function mon(){const p=gp(),e=$('fE').value,b=$('fB').value,c=$('fC').value,q=$('fQ').value.toLowerCase().trim();
 let r=R.filter(o=>(!e||o.event===e)&&(!b||bk(o)===b)&&(!c||o.confidence===c)&&(!q||(o.address+' '+o.postcode+' '+o.current_agent).toLowerCase().includes(q))).map(o=>Object.assign({_fee:fee(o,p)},o));
 r.sort((x,y)=>typeof x[ms.k]==='number'?ms.d*(x[ms.k]-y[ms.k]):ms.d*(''+x[ms.k]).localeCompare(''+y[ms.k],undefined,{numeric:true}));
 const tb=document.querySelector('#mn tbody');tb.innerHTML='';r.slice(0,1000).forEach(o=>{const tr=document.createElement('tr');tr.className='row';
 const link=o.url?` <a href="${esc(o.url)}" target="_blank" rel="noopener" style="font-size:11px">&#8599;</a>`:'';
 tr.innerHTML=`<td>${esc(o.branch)}</td><td><span class="badge" style="background:${COL[o.event]}">${esc(LB[o.event])}</span></td><td>${esc(o.address)}${link}</td><td><span class="tag">${esc(o.branch_last_type)}</span> ${esc(o.branch_last_date)}</td><td>${esc(o.event_date)}</td><td style="max-width:300px">${esc(o.detail)}</td><td>${esc(o.current_agent)}</td><td><span class="tag">${esc(o.confidence)}</span></td><td class="fee">${money(o._fee)}</td>`;tb.appendChild(tr)});
 $('mnC').textContent='('+r.length.toLocaleString()+(r.length>1000?', top 1,000 shown':'')+')'}
['fE','fB','fC','fQ'].forEach(id=>$(id).addEventListener('input',mon));['pS','pL'].forEach(id=>$(id).addEventListener('input',recalc));
document.querySelectorAll('#lg th').forEach(th=>th.onclick=()=>{ls.d=ls.k===th.dataset.k?-ls.d:-1;ls.k=th.dataset.k;league()});
document.querySelectorAll('#mn th').forEach(th=>th.onclick=()=>{ms.d=ms.k===th.dataset.k?-ms.d:-1;ms.k=th.dataset.k;mon()});
recalc();
"""


def _num(v):
    try:
        return float(v) if v not in ("", None) else None
    except ValueError:
        return None


def build(run_dir: str | Path, out: str | Path | None = None, anon: bool = False, name: str | None = None) -> Path:
    run_dir = Path(run_dir)
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    with open(run_dir / "events.csv", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    keep = ("brand", "branch", "property_key", "address", "postcode", "branch_last_type", "branch_last_date", "event",
            "event_date", "detail", "current_agent", "current_agent_relation", "url", "fee_basis", "value_used",
            "confidence")
    branches = [{"brand": b["brand"], "branch": br["branch"], "book": br["totals"]["book_properties"]}
                for b in summary["brands"] for br in b["branches"]]
    brand_alias, branch_alias = {}, {}
    if anon:
        for i, b in enumerate(summary["brands"]):
            brand_alias[b["brand"]] = f"Brand {chr(65 + i)}"
        for i, b in enumerate(branches):
            branch_alias[(b["brand"], b["branch"])] = f"Branch {i + 1}"
    data_rows = []
    for r in rows:
        o = {k: r.get(k, "") for k in keep}
        o["value_used"] = _num(o["value_used"])
        if anon:
            rel = o["current_agent_relation"]
            o["current_agent"] = {"branch": "the branch", "group": "the group", "unknown": ""}.get(rel, "another agent")
            o["detail"] = f"{LABELS[o['event']]} ({o['current_agent'] or 'agent not known'}), {o['event_date']}"
            o["url"] = ""
            o["branch"] = branch_alias.get((o["brand"], o["branch"]), o["branch"])
            o["brand"] = brand_alias.get(o["brand"], o["brand"])
        data_rows.append(o)
    if anon:
        for b in branches:
            b["branch"] = branch_alias[(b["brand"], b["branch"])]
            b["brand"] = brand_alias[b["brand"]]
    tot = summary["totals"]
    book = tot["book_properties"]
    uprn_n = sum(br["run"]["book_by_match_basis"]["uprn_on_listing"] + br["run"]["book_by_match_basis"]["uprn_from_address_master"]
                 for b in summary["brands"] for br in b["branches"])
    branch_book = sum(br["run"]["book_properties"] for b in summary["brands"] for br in b["branches"]) or 1
    org_label = name or (summary["group"] if not anon else "the organisation")
    p = summary["params"]
    data = {"rows": data_rows, "events": list(EVENTS), "labels": LABELS, "colours": COLOURS, "opp": list(OPPORTUNITY_EVENTS),
            "params": p, "branches": branches, "book": book, "uprnShare": round(uprn_n / branch_book * 100),
            "limits": summary.get("data_limits", [])}
    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    logo = LOGO.read_text(encoding="utf-8").strip() if LOGO.exists() else ""
    logo_html = f'<img src="{logo}" alt="Property Deals Insight">' if logo else "<b>Property Deals Insight</b>"
    evth = "".join(f'<th data-k="{e}">{html.escape(LABELS[e])}</th>' for e in EVENTS)
    body = (BODY.replace("__LOGO__", logo_html).replace("__ORG__", html.escape(org_label))
            .replace("__DATE__", summary["run_date"]).replace("__EVTH__", evth)
            .replace("__LIVE__", str(p["live_days"])).replace("__RMIN__", str(p["renewal_min_months"]))
            .replace("__RMAX__", str(p["renewal_max_months"])).replace("__WD__", str(p["withdrawn_months"])))
    page = ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>Client Base Explorer</title><style>{CSS}</style></head><body>{body}'
            f'<script>{JS.replace("__BLOB__", blob)}</script></body></html>')
    out = Path(out) if out else run_dir / f"client_base_explorer{'_anon' if anon else ''}.html"
    out.write_text(page, encoding="utf-8")
    return out
