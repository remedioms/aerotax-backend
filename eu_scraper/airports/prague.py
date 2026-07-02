"""
PRG — Václav Havel Airport Prague. The flight-information board is a hash-route Vue
app that fetches a clean JSON API on a dedicated host:

    GET https://api.prg.aero/en/{departures|arrivals}-shorttime
        ?offset=0&limit=200&from=DD-MM-YYYY_HH-mm
    (LIVE-verified 2026-07-02.  `from` = window start, `offset` paginates forward.)

Per record: time (scheduled UTC, '...+0000'), time-new (revised/actual UTC),
flyNumber ("LY2524"), destination ("Tel Aviv (TLV)") + destination-id ("TLV", IATA!),
state ("Departed 00:14"/"Arrived 23:01"/"Cancelled"), terminal ("T1"), gates
(departures gate), company (name) + company-id (ICAO), codeshare[].
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from .. import scraper as S

API = "https://api.prg.aero/en"
WARMUP = "https://www.prg.aero/en/flight-information/"
REFERER = "https://www.prg.aero/"
TZ = "Europe/Prague"
_MAX_PAGES = 4
_LIMIT = 200


def _utc(s: str):
    if not s:
        return None
    s = str(s).strip()
    # '2026-07-01T20:15:00+0000' → normalise the tz offset for fromisoformat
    m = re.match(r"^(.*[+-]\d{2})(\d{2})$", s)
    if m:
        s = f"{m.group(1)}:{m.group(2)}"
    return S.utc_to_local_iso(s, TZ)


def _row(rec: dict, arr: bool) -> dict | None:
    fn = (rec.get("flyNumber") or "").strip()
    if not fn:
        return None
    r = S.empty_row()
    # Prague flyNumbers are IATA-style 2-char prefixes ("LY2524","W43280","6E8011").
    r["airline"] = fn[:2].upper()
    r["airline_name"] = (rec.get("company") or "").strip()
    r["flight"] = S.norm_flight(fn, "")
    r["dest_iata"] = (rec.get("destination-id") or "").strip().upper()
    dest = (rec.get("destination") or "").strip()
    r["dest_name"] = re.sub(r"\s*\([A-Z]{3}\)\s*$", "", dest)  # drop trailing (IATA)
    r["sched"] = _utc(rec.get("time"))
    esti = _utc(rec.get("time-new"))
    # `time-new` is occasionally stale (wrong DAY, correct clock) → only trust it
    # inside a sane window around the schedule, else drop it (status still carries it).
    if esti and r["sched"]:
        d = S.delay_min(r["sched"], esti)
        early = S.delay_min(esti, r["sched"])
        if esti != r["sched"] and d <= 18 * 60 and early <= 3 * 60:
            r["esti"] = esti
    r["terminal"] = (rec.get("terminal") or rec.get("hall") or "").strip()
    r["gate"] = (rec.get("gates") or "").strip()
    st = (rec.get("state") or "").strip()
    r["status"] = st
    low = st.lower()
    r["cancelled"] = "cancel" in low or str(rec.get("state_id")) == "9"
    r["delayed"] = "delay" in low
    r["_tz"] = TZ
    if not r["flight"] or not r["sched"]:
        return None
    return S.finalize(r)


def scrape(drv) -> dict:
    drv.warm(WARMUP, wait_ms=2000)
    now = datetime.now(timezone.utc)
    frm = (now - timedelta(days=1)).strftime("%d-%m-%Y_%H-%M")
    out = {"departure": [], "arrival": []}
    for mv, key, arr in (("departures", "departure", False), ("arrivals", "arrival", True)):
        seen = set()
        for page in range(_MAX_PAGES):
            url = f"{API}/{mv}-shorttime?offset={page*_LIMIT}&limit={_LIMIT}&from={frm}"
            data = drv.get_json(url, referer=REFERER)
            rows = data if isinstance(data, list) else (data or {}).get("flights") if isinstance(data, dict) else None
            if not isinstance(rows, list) or not rows:
                break
            added = 0
            for rec in rows:
                row = _row(rec, arr)
                if not row:
                    continue
                k = (row["flight"], row["sched"])
                if k in seen:
                    continue
                seen.add(k)
                out[key].append(row)
                added += 1
            if len(rows) < _LIMIT:
                break
    return {"PRG": out}
