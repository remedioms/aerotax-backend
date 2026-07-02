"""
SCL — Santiago de Chile (Nuevo Pudahuel / Aeropuerto AMB). One open JSON endpoint
returns the WHOLE board (both directions) in a single call:

    GET https://www.aeropuertosantiagodechile.cl/api/vuelos   (LIVE-verified 2026-07-03)
    → {"status":"ok","message":[ {record}, ... ]}

Per record: npg_iata_airline ("LA 102" → IATA airline + number), numero_vuelo,
S_L ("Departure"/"Arrival"), fecha ("dd/mm/yyyy") + hora ("HH:MM") local, Puerta
(departure gate), Cinta (arrival belt), destino / origen (city names), observacion
(Spanish status: EMBARQUE / ULTIMA LLAMADA / CANCELADO / ATRASADO / ATERRIZADO / …).
No estimated time, tail reg or aircraft type on this board. Times America/Santiago.
"""
from __future__ import annotations

from .. import scraper as S
from .. import airports_ref as REF

API = "https://www.aeropuertosantiagodechile.cl/api/vuelos"
WARMUP = "https://www.aeropuertosantiagodechile.cl/es/pasajeros/informacion-de-vuelos"
TZ = "America/Santiago"
_MAX_PAGES = 12

_CANCEL = ("cancel", "anulad")
_DELAY = ("atras", "demor", "retras")


def _row(rec: dict) -> tuple | None:
    arr = (rec.get("S_L") or "").strip().lower().startswith("arr")
    iata_al = (rec.get("npg_iata_airline") or "").strip()
    parts = iata_al.split()
    al = (parts[0] if parts else "").upper()
    num = parts[1] if len(parts) > 1 else (rec.get("numero_vuelo") or "")
    if not (al and num):
        return None
    r = S.empty_row()
    r["airline"] = al
    r["flight"] = S.norm_flight(al, num)
    other = (rec.get("origen") if arr else rec.get("destino")) or ""
    r["dest_name"] = other.strip()
    r["dest_iata"] = REF.resolve(other) or ""
    r["sched"] = S.local_iso(date_ddmmyyyy=rec.get("fecha"), hhmmss=(rec.get("hora") or "") + ":00")
    if arr:
        r["gate"] = (rec.get("Cinta") or "").strip()
    else:
        r["gate"] = (rec.get("Puerta") or "").strip()
    if (r["gate"] or "").lower() in ("null", "none"):
        r["gate"] = ""
    st = (rec.get("observacion") or "").strip()
    r["status"] = st.title() if st else ""
    low = st.lower()
    r["cancelled"] = any(h in low for h in _CANCEL)
    r["delayed"] = any(h in low for h in _DELAY)
    r["_tz"] = TZ
    if not r["flight"] or not r["sched"]:
        return None
    return (arr, S.finalize(r))


_JS = """async (body) => {
    const r = await fetch('/api/vuelos', {method:'POST',
        headers:{'Content-Type':'application/json'}, body: body});
    return await r.text();
}"""


def _post(drv, sl: str, ni: str, ho: str, page: int):
    """CloudFront rejects a bare GET; the board is a JSON POST run same-origin from
    the warmed page. Filters: salida_llegada × nac_inter × horario, paginated."""
    import json as _j
    body = _j.dumps({
        "action": "get_vuelos_nuevo_home", "coincidencia": "0",
        "salida_llegada": sl, "nac_inter": ni, "horario": ho,
        "rango": "2", "idioma": "es", "page": page,
    })
    try:
        txt = drv._page.evaluate(_JS, body)
        data = _j.loads(txt) if txt else None
        if isinstance(data, str):
            data = _j.loads(data)
        return data.get("message") if isinstance(data, dict) else None
    except Exception:
        return None


def scrape(drv) -> dict:
    try:
        drv._page.goto(WARMUP, wait_until="domcontentloaded", timeout=45000)
        drv._page.wait_for_timeout(2500)
    except Exception:
        pass
    out = {"departure": [], "arrival": []}
    seen = set()
    for sl in ("Departure", "Arrival"):
        for ni in ("Domestic", "International"):
            for ho in ("am", "pm"):
                for page in range(1, _MAX_PAGES + 1):
                    msgs = _post(drv, sl, ni, ho, page)
                    if not isinstance(msgs, list) or not msgs:
                        break
                    added = 0
                    for rec in msgs:
                        parsed = _row(rec)
                        if not parsed:
                            continue
                        arr, row = parsed
                        k = (arr, row["flight"], row["sched"])
                        if k in seen:
                            continue
                        seen.add(k)
                        out["arrival" if arr else "departure"].append(row)
                        added += 1
                    if added == 0:
                        break
    return {"SCL": out}
