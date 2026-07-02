"""
AENA (Spain) — THE big group win. ONE JSON endpoint pattern covers ALL ~49
Spanish airports (MAD/BCN/PMI/AGP/ALC/VLC/SVQ/BIO/IBZ/LPA/TFN/TFS/ACE/FUE/…).

Endpoint (discovered from the infovuelos React bundle, LIVE-verified 2026-07-02):
    GET https://www.aena.es/sites/Satellite
        ?pagename=AENA_ConsultarVuelos&airport=<IATA>&flightType={S|L}&dosDias=si
    flightType S = Salidas (departures), L = Llegadas (arrivals).
    dosDias=si → today + the next 2 calendar days (forward rolling window; past-day
    history accrues by repeated polling, same model as the native German boards).

Each record carries: numVuelo, iataCompania, nombreCompania, fecha+horaProgramada
(LOCAL), fechaEstimada+horaEstimada (LOCAL), iataOtro (other airport), ciudadIataOtro
(city), estado (status code), terminal, tipoAeronave (type code), puertaPrimera
(departure gate) / cintaPrimera (arrival belt).

The endpoint works with plain curl today but is Akamai-fronted, so we fetch it
THROUGH the warmed browser context (inherits the bot cookies) to stay robust on
Cloud Run.
"""
from __future__ import annotations

from .. import scraper as S

BASE = "https://www.aena.es/sites/Satellite?pagename=AENA_ConsultarVuelos"
WARMUP = "https://www.aena.es/en/flight-info.html"

# Canary Islands run on Atlantic/Canary (UTC+0/+1), the mainland+Balearics on
# Europe/Madrid. Everything else defaults to Europe/Madrid.
_CANARY = {"LPA", "TFN", "TFS", "ACE", "FUE", "SPC", "GMZ", "VDE", "JCU"}

# The big-traffic Spanish airports (25). The module can take `all=True` to sweep
# every AENA airport, but the default keeps a cycle light.
DEFAULT = [
    "MAD", "BCN", "PMI", "AGP", "ALC", "VLC", "SVQ", "BIO", "IBZ", "LPA",
    "TFN", "TFS", "ACE", "FUE", "SCQ", "VGO", "GRX", "XRY", "RMU", "MAH",
    "OVD", "SDR", "LEI", "REU", "GRO",
]

# estado (AENA status code) → readable English. Unknown codes pass through raw.
_STATUS = {
    "SCH": "Scheduled", "INI": "Check-in open", "BOR": "Boarding",
    "EMB": "Boarding", "ULL": "Last call", "CER": "Gate closed",
    "RET": "Delayed", "HOR": "Estimated", "FLY": "Airborne",
    "LND": "Landed", "FNL": "Final approach", "IBK": "Baggage",
    "OPE": "Landed", "DIV": "Diverted", "CAN": "Cancelled",
    "ANU": "Cancelled", "DEP": "Departed", "SAL": "Departed",
}
_CANCEL = {"CAN", "ANU", "CNL", "CANCELADO"}


def _row(rec: dict, arr: bool) -> dict | None:
    r = S.empty_row()
    num = (rec.get("numVuelo") or "").strip()
    al = (rec.get("iataCompania") or rec.get("compania") or "").strip().upper()
    if not (num and al):
        return None
    r["airline"] = al
    r["airline_name"] = (rec.get("nombreCompania") or "").strip()
    r["flight"] = S.norm_flight(al, num)
    r["dest_iata"] = (rec.get("iataOtro") or "").strip().upper()
    city = (rec.get("ciudadIataOtro") or "").strip().title()
    r["dest_name"] = city
    r["sched"] = S.local_iso(date_ddmmyyyy=rec.get("fecha"),
                             hhmmss=rec.get("horaProgramada"))
    esti = S.local_iso(date_ddmmyyyy=(rec.get("fechaEstimada") or rec.get("fecha")),
                       hhmmss=rec.get("horaEstimada"))
    if esti and esti != r["sched"]:
        r["esti"] = esti
    term = (rec.get("terminal") or "").strip()
    r["terminal"] = "" if term.lower() in ("null", "none") else term
    r["aircraft"] = (rec.get("tipoAeronave") or "").strip().upper()
    if arr:
        r["gate"] = (rec.get("cintaPrimera") or "").strip()   # baggage belt
    else:
        r["gate"] = (rec.get("puertaPrimera") or "").strip()  # departure gate
    if (r["gate"] or "").lower() in ("null", "none"):
        r["gate"] = ""
    est = (rec.get("estado") or "").strip().upper()
    r["status"] = _STATUS.get(est, est.title() if est else "")
    r["cancelled"] = est in _CANCEL
    r["delayed"] = (est == "RET")
    if not r["flight"] or not r["sched"]:
        return None
    return S.finalize(r)


def _airport(drv, iata: str) -> dict:
    tz = "Atlantic/Canary" if iata in _CANARY else "Europe/Madrid"
    out = {"departure": [], "arrival": []}
    for ft, key, arr in (("S", "departure", False), ("L", "arrival", True)):
        url = f"{BASE}&airport={iata}&flightType={ft}&dosDias=si"
        data = drv.get_json(url, referer=WARMUP)
        if not isinstance(data, list):
            continue
        seen = set()
        for rec in data:
            row = _row(rec, arr)
            if not row:
                continue
            k = (row["flight"], row["sched"])
            if k in seen:
                continue
            seen.add(k)
            # AENA times are already airport-local; tz only documents the airport.
            row["_tz"] = tz
            out[key].append(row)
    return out


def scrape(drv, airports=None, sweep_all=False) -> dict:
    drv.warm(WARMUP)
    if sweep_all:
        airports = _all_airports(drv) or DEFAULT
    codes = airports or DEFAULT
    res = {}
    for iata in codes:
        try:
            res[iata] = _airport(drv, iata)
        except Exception as e:
            res[iata] = {"departure": [], "arrival": [], "error": str(e)}
    return res


def _all_airports(drv):
    """Pull AENA's own airport registry (every code with a live board)."""
    txt = drv.get_text(
        "https://www.aena.es/sites/Satellite?pagename=AENA_InfoAeropuertosScript&d=NonTouch",
        referer=WARMUP)
    if not txt:
        return None
    try:
        import json
        arr = json.loads(txt.split("infoAeropuertoJSON=", 1)[1].strip().rstrip(";"))
        return [a["iata"] for a in arr if a.get("carto") == "true"]
    except Exception:
        return None
