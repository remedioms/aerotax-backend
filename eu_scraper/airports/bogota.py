"""
BOG — Bogotá El Dorado (OPAIN). Clean public JSON API the Nuxt site calls directly:

    GET https://api.eldorado.aero/api/v2/flights/{departures|arrivals}
    (LIVE-verified 2026-07-03) → {"flights":[ {record}, ... ]}

Per record (FULL fields): airline{code(IATA), name}, number, flighttype (D/I),
status{code, en, es}, city{code(IATA of the OTHER airport!), en, es}, gate,
terminal, claim (arrival baggage belt), scheduleDate / estimatedDate / actualDate
("YYYY-MM-DD HH:MM:SS" local). No tail reg or aircraft type. Times America/Bogota.
"""
from __future__ import annotations

from .. import scraper as S

API = "https://api.eldorado.aero/api/v2/flights"
WARMUP = "https://eldorado.aero/vuelos/salidas"
REFERER = "https://eldorado.aero/"
TZ = "America/Bogota"


def _dt(s: str) -> str | None:
    """'2026-07-02 15:15:00' (local) → '2026-07-02T15:15:00'."""
    if not s or not str(s).strip():
        return None
    return str(s).strip().replace(" ", "T")[:19]


def _row(rec: dict, arr: bool) -> dict | None:
    al = (rec.get("airline") or {})
    code = (al.get("code") or "").strip().upper()
    num = (rec.get("number") or "").strip()
    if not (code and num):
        return None
    r = S.empty_row()
    r["airline"] = code
    r["airline_name"] = (al.get("name") or "").strip().title()
    r["flight"] = S.norm_flight(code, num)
    city = rec.get("city") or {}
    r["dest_iata"] = (city.get("code") or "").strip().upper()
    r["dest_name"] = (city.get("en") or city.get("es") or "").strip()
    r["sched"] = _dt(rec.get("scheduleDate"))
    esti = _dt(rec.get("actualDate")) or _dt(rec.get("estimatedDate"))
    if esti and esti != r["sched"]:
        r["esti"] = esti
    r["terminal"] = (rec.get("terminal") or "").strip()
    r["gate"] = ((rec.get("claim") if arr else rec.get("gate")) or "").strip()
    st = rec.get("status") or {}
    code_st = (st.get("code") or "").strip().upper()
    r["status"] = (st.get("en") or code_st or "").strip()
    r["cancelled"] = code_st in ("CAN", "CNL") or "cancel" in r["status"].lower()
    r["delayed"] = code_st == "DLY" or "delay" in r["status"].lower()
    r["_tz"] = TZ
    if not r["flight"] or not r["sched"]:
        return None
    return S.finalize(r)


def scrape(drv) -> dict:
    drv.warm(WARMUP, wait_ms=2500)
    out = {"departure": [], "arrival": []}
    for mv, key, arr in (("departures", "departure", False), ("arrivals", "arrival", True)):
        data = drv.get_json(f"{API}/{mv}", referer=REFERER)
        flights = data.get("flights") if isinstance(data, dict) else None
        if not isinstance(flights, list):
            continue
        seen = set()
        for rec in flights:
            row = _row(rec, arr)
            if not row:
                continue
            k = (row["flight"], row["sched"])
            if k in seen:
                continue
            seen.add(k)
            out[key].append(row)
    return {"BOG": out}
