"""
STR — Stuttgart Airport (stuttgart-airport.com). Akamai-bot-walled: plain
requests gets a hard 403 "Access Denied" (MIME-Version 1.0 block page) on the
WHOLE domain — one of the two long-open German gaps (with BRE).

CRACK (verified 2026-07-03): the board page
    https://www.stuttgart-airport.com/de/reisende-besucher/fliegen/ankunft-abflug
is fully SERVER-RENDERED (no JSON needed): ~400 <a class="flights-table__item">
rows covering BOTH directions x {today, tomorrow} x {previous, current}.
Headless Chromium passed the wall only after spoofing real-Chrome sec-ch-ua
client hints (see scraper.py context headers) — headless ships no/"Headless"
brands and Akamai 403s exactly on that; with the brands the same context loads
the page with all rows.

Per row (all in attributes/cells):
    data-filter        "EW 2648 EW2648 Eurowings Faro Faro FAO"
                        → flight number tokens + LAST TOKEN = IATA of the other
                          airport (arrivals: ORIGIN — no resolver guessing!)
    data-flights-day    today | tomorrow            (local date)
    data-flights-table-traffic  departure | arrival
    .cell-estimate      span.is--plan H:MM  (+ span.is--early/.is--late = est)
    .cell-from          other city (UPPERCASE)
    .cell-aircraft      type code (32A/32N/223/...)
    .cell-terminal      T1/T3, .cell-checkin, .cell-gate
    .cell-status        tag text (gestartet/unterwegs/gelandet/annulliert/...)

We keep only day=today (the warehouse writer only persists already-passed rows
anyway; tomorrow-rows would all be sched-only noise).
"""
from __future__ import annotations

import re
from datetime import datetime

from .. import scraper as S
from .. import airports_ref as REF

URL = "https://www.stuttgart-airport.com/de/reisende-besucher/fliegen/ankunft-abflug"
TZ = "Europe/Berlin"

_CANCEL_HINT = ("annull", "gestrichen", "cancel")


def _today_local() -> str:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(TZ)).strftime("%Y-%m-%d")
    except Exception:
        return datetime.utcnow().strftime("%Y-%m-%d")


def _cell_text(item, cls: str) -> str:
    node = item.query_selector(".flights-table__cell-" + cls)
    if node is None:
        return ""
    txt = node.inner_text() or ""
    # drop the mobile category label ("Terminal:", "Gate:", "Flugzeugtyp:")
    txt = re.sub(r"^[^:]{0,20}:\s*", "", txt.strip())
    return re.sub(r"\s+", " ", txt).strip()


def _row(item, day: str) -> tuple[str, dict] | None:
    traffic = (item.get_attribute("data-flights-table-traffic") or "").strip()
    if traffic not in ("departure", "arrival"):
        return None
    filt = (item.get_attribute("data-filter") or "").strip()
    toks = filt.split()
    if len(toks) < 3:
        return None
    r = S.empty_row()
    # "EW 2648 EW2648 Eurowings Faro Faro FAO" — first two tokens = flight,
    # third = airline name, LAST = IATA of the other airport.
    r["airline"] = toks[0].upper()
    r["flight"] = S.norm_flight(toks[0], toks[1])
    r["airline_name"] = toks[2]
    last = toks[-1].upper()
    city = _cell_text(item, "from").title()
    r["dest_name"] = city
    if len(last) == 3 and last.isalpha() and REF.is_iata(last):
        r["dest_iata"] = last
    else:
        r["dest_iata"] = REF.resolve(city) or ""
    est_cell = item.query_selector(".flights-table__cell-estimate")
    if est_cell is None:
        return None
    plan = est_cell.query_selector(".is--plan")
    ptxt = (plan.inner_text() or "").strip() if plan else ""
    m = re.search(r"(\d{1,2}:\d{2})", ptxt)
    if not m:
        return None
    r["sched"] = day + "T" + m.group(1).zfill(5) + ":00"
    est = est_cell.query_selector(".is--early, .is--late")
    if est is not None:
        em = re.search(r"(\d{1,2}:\d{2})", (est.inner_text() or "").strip())
        if em and em.group(1).zfill(5) != m.group(1).zfill(5):
            r["esti"] = day + "T" + em.group(1).zfill(5) + ":00"
    r["aircraft"] = _cell_text(item, "aircraft").upper()
    r["terminal"] = _cell_text(item, "terminal")
    r["gate"] = _cell_text(item, "gate")
    st = _cell_text(item, "status")
    r["status"] = st
    low = st.lower()
    r["cancelled"] = any(h in low for h in _CANCEL_HINT)
    r["_tz"] = TZ
    return traffic, S.finalize(r)


def scrape(drv) -> dict:
    page = drv.render(URL, wait_selector=".flights-table__item", wait_ms=12000)
    out = {"departure": [], "arrival": []}
    title = ""
    try:
        title = page.title() or ""
    except Exception:
        pass
    if "Access Denied" in title:
        return {"STR": {"departure": [], "arrival": [],
                        "error": "akamai_denied"}}
    day = _today_local()
    seen = set()
    for item in page.query_selector_all(
            '.flights-table__item[data-flights-day="today"]'):
        try:
            got = _row(item, day)
        except Exception:
            continue
        if not got:
            continue
        traffic, row = got
        k = (traffic, row["flight"], row["sched"])
        if k in seen:
            continue
        seen.add(k)
        out[traffic].append(row)
    return {"STR": out}
