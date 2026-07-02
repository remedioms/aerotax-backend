"""
HKG — Hong Kong International Airport. Clean public REST feed the site's own board
calls directly (not bot-walled, but fetched via the warmed context for uniformity):

    GET https://www.hongkongairport.com/flightinfo-rest/rest/flights
        ?span=1&date=YYYY-MM-DD&lang=en&cargo=false&arrival={true|false}
    (LIVE-verified 2026-07-03. `span` returns the day's flights; call once per date.
     `arrival` toggles direction, `cargo=false` = passenger flights only.)

Response: a list of day-buckets [{date, arrival, cargo, list:[...]}]. Each list item:
  time (LOCAL "HH:MM"), flight:[{no:"CX 251", airline:"CPA"(ICAO)}] (list = codeshares,
  first is operating), status (free text "Dep 02:31 (03/07/2026)" / "Est at 08:30
  (03/07/2026)" / "At gate" / "Cancelled" / "Landed"), destination:[IATA,…] (already
  IATA codes; a list when the flight routes via a stop), terminal ("T1"), aisle, gate.
No tail registration / aircraft type in this feed. destination[-1] is the final
destination (departures); destination[0] is the true origin (arrivals).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from .. import scraper as S

API = "https://www.hongkongairport.com/flightinfo-rest/rest/flights"
WARMUP = "https://www.hongkongairport.com/en/flights/departures/passenger.page"
REFERER = "https://www.hongkongairport.com/"
TZ = "Asia/Hong_Kong"

# a status like "Dep 02:31 (03/07/2026)" or "Est at 08:30 (03/07/2026)" or "Landed 06:12"
_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})(?:\s*\((\d{2})/(\d{2})/(\d{4})\))?")
_CANCEL = ("cancel",)
_DELAY = ("delay",)


def _esti_from_status(status: str, flight_date: str) -> str | None:
    """Pull the estimated/actual clock out of the free-text status. Uses the
    embedded (dd/mm/yyyy) when present, else the flight's own scheduled date."""
    if not status:
        return None
    m = _TIME_RE.search(status)
    if not m:
        return None
    hh, mm, dd, mo, yy = m.groups()
    if dd:
        y = f"{yy}-{mo}-{dd}"
    else:
        y = flight_date
    if not y:
        return None
    return f"{y}T{int(hh):02d}:{mm}:00"


def _row(item: dict, day_date: str, arr: bool) -> dict | None:
    flights = item.get("flight") or []
    if not flights:
        return None
    op = flights[0] or {}
    no = (op.get("no") or "").replace(" ", "").upper()
    if not no:
        return None
    m = re.match(r"^([A-Z0-9]{2})(\d.*)$", no)
    al = m.group(1) if m else no[:2]
    r = S.empty_row()
    r["airline"] = al
    r["airline_name"] = (op.get("airline") or "").strip()  # ICAO-ish code, best we have
    r["flight"] = no
    # Departures carry `destination`, arrivals carry `origin` — both are IATA lists
    # (a via-stop makes them multi-element). Final dest = last; true origin = first.
    places = [d for d in ((item.get("origin") if arr else item.get("destination")) or []) if d]
    if places:
        r["dest_iata"] = (places[0] if arr else places[-1]).strip().upper()
    tm = (item.get("time") or "").strip()
    r["sched"] = f"{day_date}T{tm[:5]}:00" if (day_date and len(tm) >= 4) else None
    st = (item.get("status") or "").strip()
    esti = _esti_from_status(st, day_date)
    if esti and esti != r["sched"]:
        r["esti"] = esti
    r["terminal"] = (item.get("terminal") or "").strip()
    # Arrivals: physical stand + baggage hall/belt; Departures: boarding gate.
    if arr:
        r["gate"] = (item.get("stand") or "").strip()
        r["hall"] = (item.get("hall") or "").strip()
    else:
        r["gate"] = (item.get("gate") or "").strip()
    r["status"] = st
    low = st.lower()
    r["cancelled"] = any(h in low for h in _CANCEL)
    r["delayed"] = any(h in low for h in _DELAY)
    r["_tz"] = TZ
    if not r["flight"] or not r["sched"]:
        return None
    return S.finalize(r)


def _fetch(drv, date: str, arr: bool, key: str, out: dict, seen: set):
    url = f"{API}?span=1&date={date}&lang=en&cargo=false&arrival={'true' if arr else 'false'}"
    data = drv.get_json(url, referer=REFERER)
    if not isinstance(data, list):
        return
    for bucket in data:
        day_date = (bucket.get("date") or date).strip()
        for item in bucket.get("list") or []:
            row = _row(item, day_date, arr)
            if not row:
                continue
            k = (row["flight"], row["sched"])
            if k in seen:
                continue
            seen.add(k)
            out[key].append(row)


def scrape(drv) -> dict:
    drv.warm(WARMUP, wait_ms=2000)
    now = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8)))
    days = [(now - timedelta(days=1)).strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d")]
    out = {"departure": [], "arrival": []}
    for arr, key in ((False, "departure"), (True, "arrival")):
        seen = set()
        for d in days:
            _fetch(drv, d, arr, key, out, seen)
    return {"HKG": out}
