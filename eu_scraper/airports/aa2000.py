"""
AA2000 — Aeropuertos Argentina. THE Latin-America group win: ONE public JSON API
covers ALL 56 Argentine airports it operates, incl. EZE (Ezeiza) and AEP
(Aeroparque), plus COR/MDZ/BRC/IGR/SLA/USH/FTE/TUC/ROS/NQN/…

Endpoint (discovered from the Next.js bundle, LIVE-verified 2026-07-03):
    GET https://webaa-api-h4d5amdfcze7hthn.a02.azurefd.net/web-prod/v1/api-aa/all-flights
        ?c=2000&idarpt=<IATA>&movtp={D|A}&f=DD-MM-YYYY
    movtp D = departures, A = arrivals. `f` is a date → we pull yesterday+today.
    A companion /all-airports lists every airport id (country ARS = the operated
    Argentine set). Azure-Front-Door hosted; we fetch through the warmed context.

Per record (FULL fields incl. tail + gate): nro ("AR 1651"), idaerolinea (IATA),
aerolinea (name), destorig (other city), IATAdestorig (other IATA!), stda
(scheduled "dd/mm HH:MM" local), etda (estimated), atda (actual), gate (departures),
belt (arrivals), termsec/sector (terminal), matricula (tail reg!), acftype (type,
often null), estin (English status). Times are America/Argentina/Buenos_Aires.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .. import scraper as S

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

API = "https://webaa-api-h4d5amdfcze7hthn.a02.azurefd.net/web-prod/v1/api-aa"
WARMUP = "https://www.aeropuertosargentina.com/es/vuelos"
REFERER = "https://www.aeropuertosargentina.com/"
TZ = "America/Argentina/Buenos_Aires"

# Big-traffic Argentine airports (default light cycle); sweep_all does all 56.
DEFAULT = ["EZE", "AEP", "COR", "MDZ", "BRC", "IGR", "SLA", "USH", "FTE",
           "TUC", "ROS", "NQN", "MDQ", "REL", "BHI", "CRD", "JUJ", "CPC"]


def _now_local():
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo(TZ))
        except Exception:
            pass
    return datetime.utcnow()


def _parse_dt(s: str, now) -> str | None:
    """'01/07 23:15' (local dd/mm HH:MM, no year) → 'YYYY-MM-DDTHH:MM:00'.
    Year is inferred from `now`, with Dec↔Jan rollover handling."""
    if not s or not str(s).strip():
        return None
    try:
        datepart, timepart = str(s).strip().split()
        d, m = datepart.split("/")
        hh, mm = timepart.split(":")
        d, m, hh, mm = int(d), int(m), int(hh), int(mm)
        y = now.year
        if now.month == 12 and m == 1:
            y += 1
        elif now.month == 1 and m == 12:
            y -= 1
        return f"{y:04d}-{m:02d}-{d:02d}T{hh:02d}:{mm:02d}:00"
    except Exception:
        return None


def _row(rec: dict, arr: bool, now) -> dict | None:
    nro = (rec.get("nro") or "").strip()
    al = (rec.get("idaerolinea") or "").strip().upper()
    if not nro:
        return None
    r = S.empty_row()
    r["airline"] = al
    r["airline_name"] = (rec.get("aerolinea") or "").strip().title()
    r["flight"] = S.norm_flight(nro, "")
    r["dest_iata"] = (rec.get("IATAdestorig") or "").strip().upper()
    r["dest_name"] = (rec.get("destorig") or "").strip()
    r["sched"] = _parse_dt(rec.get("stda"), now)
    esti = _parse_dt(rec.get("atda"), now) or _parse_dt(rec.get("etda"), now)
    if esti and esti != r["sched"]:
        r["esti"] = esti
    if arr:
        r["gate"] = (rec.get("belt") or "").strip()
    else:
        r["gate"] = (rec.get("gate") or "").strip()
    r["terminal"] = (rec.get("termsec") or rec.get("sector") or rec.get("term") or "").strip()
    r["reg"] = (rec.get("matricula") or "").strip().upper()
    r["aircraft"] = (rec.get("acftype") or "").strip().upper()
    st = (rec.get("estin") or "").strip()
    r["status"] = st
    low = st.lower()
    r["cancelled"] = "cancel" in low
    r["delayed"] = "delay" in low
    r["_tz"] = TZ
    if not r["flight"] or not r["sched"]:
        return None
    return S.finalize(r)


def _airport(drv, iata: str, dates: list[str], now) -> dict:
    out = {"departure": [], "arrival": []}
    for mv, key, arr in (("D", "departure", False), ("A", "arrival", True)):
        seen = set()
        for f in dates:
            url = f"{API}/all-flights?c=2000&idarpt={iata}&movtp={mv}&f={f}"
            data = drv.get_json(url, referer=REFERER)
            if not isinstance(data, list):
                continue
            for rec in data:
                row = _row(rec, arr, now)
                if not row:
                    continue
                k = (row["flight"], row["sched"])
                if k in seen:
                    continue
                seen.add(k)
                out[key].append(row)
    return out


def _all_ars(drv) -> list[str] | None:
    data = drv.get_json(f"{API}/all-airports", referer=REFERER)
    if not isinstance(data, list):
        return None
    return [a["id"] for a in data if a.get("country") == "ARS" and a.get("id")]


def scrape(drv, airports=None, sweep_all=False) -> dict:
    drv.warm(WARMUP, wait_ms=3500)
    now = _now_local()
    dates = [(now - timedelta(days=1)).strftime("%d-%m-%Y"), now.strftime("%d-%m-%Y")]
    if sweep_all:
        airports = _all_ars(drv) or DEFAULT
    codes = airports or DEFAULT
    res = {}
    for iata in codes:
        try:
            res[iata] = _airport(drv, iata, dates, now)
        except Exception as e:
            res[iata] = {"departure": [], "arrival": [], "error": str(e)}
    return res
