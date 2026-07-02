"""
ACSA — Airports Company South Africa. THE Africa group win: ONE board cracks ALL
nine ACSA airports:
    JNB (OR Tambo), CPT (Cape Town), DUR (King Shaka), BFN (Bram Fischer/
    Bloemfontein), PLZ (Chief Dawid Stuurman/Port Elizabeth), ELS (King Phalo/
    East London), GRJ (George), KIM (Kimberley), UTN (Upington).

Source (LIVE-verified 2026-07-03):
    https://www.airports.co.za/utilities/live-flight-info
    A SharePoint/ASP.NET page. There is no clean JSON endpoint — the board is
    rendered server-side after a form postback. We drive it headlessly: set the
    hidden search fields (hdnFromAirport → departures FROM a city, hdnToAirport →
    arrivals TO a city), click the hidden ASP.NET submit, and parse the returned
    `flight-card` markup. ONE page load, then repeated postbacks (viewstate carries)
    — ~9 airports × 2 directions from a single browser page.

Each flight-card carries: flight-number ("UR713"), flight-airline ("UGANDA
AIRLINES"), a status badge (Scheduled / Estimated / Delayed / Cancelled), and
detail rows: "Schedule Time" (HH:MM local), "Departing From" (origin city),
"Destination" (dest city), "Flight Status", "Updated Time" (HH:MM estimated, or
TBA), "Check-in Counters", "Gate".

No tail registration or aircraft type is published on this board. Times are SAST
(Africa/Johannesburg, UTC+2, no DST) and carry no date — the board is the current
day only, so we stamp today's local date (past-day history accrues by repeated
polling, same model as the native German boards).
"""
from __future__ import annotations

import re
from datetime import datetime

from .. import scraper as S
from .. import airports_ref as REF

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

URL = "https://www.airports.co.za/utilities/live-flight-info"
TZ = "Africa/Johannesburg"
# ASP.NET control-id prefix for the flight-search web part (stable, verified).
_PFX = "ctl00_ctl49_g_3d254c0d_a345_4dc8_b11b_9b55f87a5892_ctl00_"

# The nine ACSA airports. Key = the board's `fromAirport`/`toAirport` city value;
# value = its IATA. DEFAULT sweeps the big three; sweep_all does all nine.
_CITY_IATA = {
    "Johannesburg": "JNB", "Cape Town": "CPT", "Durban": "DUR",
    "Bloemfontein": "BFN", "Port Elizabeth": "PLZ", "East London": "ELS",
    "George": "GRJ", "Kimberley": "KIM", "Upington": "UTN",
}
DEFAULT = ["Johannesburg", "Cape Town", "Durban"]
IATAS = list(_CITY_IATA.values())

# Ambiguous multi-airport cities / naming variants the dataset can't resolve on its
# own (keyed lowercase on the board's city string). REF.resolve handles the rest.
_OTHER = {
    "atlanta": "ATL", "beijing": "PEK", "cairo": "CAI", "lilongwe": "LLW",
    "manzini - king mswati iii": "SHO", "maseru": "MSU", "mauritius": "MRU",
    "nairobi": "NBO", "nelspruit - kruger": "MQP", "sao paulo": "GRU",
    "vilanculos": "VNX", "windhoek": "WDH", "guangzhou": "CAN",
}


def _resolve_other(city: str) -> str:
    if not city:
        return ""
    if city in _CITY_IATA:
        return _CITY_IATA[city]
    key = city.strip().lower()
    if key in _OTHER:
        return _OTHER[key]
    return REF.resolve(city) or ""

_CARD_RE = re.compile(r'<div class="flight-card">')
_FN_RE = re.compile(r'flight-number">\s*([^<]+)')
_AL_RE = re.compile(r'flight-airline">\s*([^<]+)')
_BADGE_RE = re.compile(r'status-badge status-(\w+)">\s*([^<]+?)\s*<')
_PAIR_RE = re.compile(
    r'detail-label">\s*([^<]+?)\s*</div>\s*<div class="detail-value">\s*([^<]*?)\s*</div>')
_TIME_RE = re.compile(r'^\d{1,2}:\d{2}$')
_COUNT_RE = re.compile(r'([\d,]+) flights found')


def _local_date() -> str:
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo(TZ)).strftime("%Y-%m-%d")
        except Exception:
            pass
    return datetime.utcnow().strftime("%Y-%m-%d")


def _mk_sched(ymd: str, hhmm: str) -> str | None:
    if not hhmm or not _TIME_RE.match(hhmm):
        return None
    h, m = hhmm.split(":")
    return f"{ymd}T{int(h):02d}:{int(m):02d}:00"


def _card_row(block: str, arr: bool, ymd: str) -> dict | None:
    fn = _FN_RE.search(block)
    if not fn:
        return None
    flight = fn.group(1).strip().upper()
    if not flight:
        return None
    r = S.empty_row()
    al = _AL_RE.search(block)
    r["airline_name"] = (al.group(1).strip().title() if al else "")
    # IATA airline designators are 2 chars (incl. alnum like 4Z / W3).
    r["airline"] = flight[:2].upper()
    r["flight"] = S.norm_flight(flight, "")

    pairs = {k.strip(): v.strip() for k, v in _PAIR_RE.findall(block)}
    r["sched"] = _mk_sched(ymd, pairs.get("Schedule Time", ""))
    upd = pairs.get("Updated Time", "")
    esti = _mk_sched(ymd, upd)
    if esti and esti != r["sched"]:
        r["esti"] = esti

    # For a departure the OTHER airport is the Destination; for an arrival it's the
    # origin ("Departing From"). Resolve the city name → IATA (fail-safe → "").
    other_city = pairs.get("Departing From", "") if arr else pairs.get("Destination", "")
    r["dest_name"] = other_city
    r["dest_iata"] = _resolve_other(other_city)

    gate = pairs.get("Gate", "")
    r["gate"] = "" if gate.upper() in ("TBA", "", "-") else gate

    badge = _BADGE_RE.search(block)
    status_txt = (badge.group(2).strip() if badge else pairs.get("Flight Status", ""))
    r["status"] = status_txt
    low = status_txt.lower()
    r["cancelled"] = "cancel" in low
    r["delayed"] = "delay" in low or (badge is not None and badge.group(1) == "delayed" and not r["cancelled"])
    r["_tz"] = TZ
    if not r["flight"] or not r["sched"]:
        return None
    return S.finalize(r)


def _search(drv, from_city: str, to_city: str) -> str | None:
    """Run one server-side postback; return the results HTML (or None)."""
    page = drv._page
    js = (
        "()=>{const s=(i,v)=>{const e=document.getElementById(i);if(e)e.value=v;};"
        f"s('{_PFX}hdnSearchMode','advanced');"
        f"s('{_PFX}hdnArrivalDeparture','both');"
        f"s('{_PFX}hdnFromAirport','{from_city}');"
        f"s('{_PFX}hdnToAirport','{to_city}');"
        f"s('{_PFX}hdnFlightType','');}}"
    )
    try:
        page.evaluate(js)
        with page.expect_navigation(timeout=25000, wait_until="load"):
            page.evaluate(
                f"()=>{{var b=document.getElementById('{_PFX}btnSearchHidden');if(b)b.click();}}")
        page.wait_for_timeout(2000)
        return page.content()
    except Exception:
        try:
            return page.content()
        except Exception:
            return None


def _parse(html: str, arr: bool, ymd: str) -> list[dict]:
    if not html:
        return []
    rows, seen = [], set()
    parts = _CARD_RE.split(html)[1:]
    for block in parts:
        row = _card_row(block, arr, ymd)
        if not row:
            continue
        k = (row["flight"], row["sched"])
        if k in seen:
            continue
        seen.add(k)
        rows.append(row)
    return rows


def scrape(drv, airports=None, sweep_all=False) -> dict:
    cities = list(_CITY_IATA.keys()) if sweep_all else (airports or DEFAULT)
    ymd = _local_date()
    # One page load; every city/direction reuses it via postback.
    try:
        drv._page.goto(URL, timeout=45000, wait_until="domcontentloaded")
        drv._page.wait_for_timeout(3000)
    except Exception:
        pass
    res = {}
    for city in cities:
        iata = _CITY_IATA.get(city)
        if not iata:
            continue
        out = {"departure": [], "arrival": []}
        try:
            dep_html = _search(drv, city, "")
            out["departure"] = _parse(dep_html, False, ymd)
            arr_html = _search(drv, "", city)
            out["arrival"] = _parse(arr_html, True, ymd)
        except Exception as e:
            out["error"] = str(e)
        res[iata] = out
    return res
