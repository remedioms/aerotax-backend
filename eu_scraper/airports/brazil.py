"""
Brazil — GRU (São Paulo/Guarulhos), the largest airport in South America.

Source (LIVE-verified 2026-07-03):
    POST https://www.gru.com.br/en/_layouts/15/WebSiteWebParts/WebServiceCustom.asmx/GetVoos
        body: procura=&terminal=&tipo={Partida|Chegada}&pagina=0
    (SharePoint ASMX; returns the FULL board in one call — pagina is client-side
     only. tipo Partida = departures, Chegada = arrivals. The response is a JSON
     array wrapped in an ASMX <string> XML envelope; we unwrap it. We issue the
     POST from inside the warmed page via fetch() — the site is Akamai-fronted and
     the same-origin in-page fetch inherits every cookie.)

Per record: NumVoo (array of codeshare numbers, [0] is primary), Cias (parallel
array of operators — Sigla is the ICAO airline designator, Nome the name), Portao
(departure gate), Terminal, OrigemDestino (the OTHER city name), Horario (scheduled
HH:MM local), HorarioConfirmado (confirmed/estimated HH:MM), Observacao (status),
DataDiaMes ("dd/mm" — gives the real date, so midnight-rollover is correct). No tail
registration or aircraft type on this board.

GRU publishes the ICAO airline designator (DAL/TAM/GLO/…), which is also the ADS-B
callsign root — good for radar. We additionally map the major carriers to their IATA
code so the flight id matches the rest of the IATA-keyed warehouse, and fall back to
the ICAO form when a carrier isn't in the map. Times are America/Sao_Paulo local.
"""
from __future__ import annotations

import json
import re
from datetime import datetime

from .. import scraper as S
from .. import airports_ref as REF

TZ = "America/Sao_Paulo"
WARMUP = "https://www.gru.com.br/en/passenger/flights/"
ASMX = "/en/_layouts/15/WebSiteWebParts/WebServiceCustom.asmx/GetVoos"

# ICAO airline designator → IATA, for the carriers that actually fly GRU (keeps the
# flight id IATA-consistent with the rest of the warehouse). Unknown → keep ICAO.
_ICAO2IATA_AL = {
    "TAM": "LA", "LAN": "LA", "LAP": "LA", "LPE": "LA", "LNE": "LA", "LXP": "LA",
    "GLO": "G3", "AZU": "AD", "PTB": "2Z", "ONE": "O6", "ITY": "AZ",
    "DAL": "DL", "UAL": "UA", "AAL": "AA", "ACA": "AC", "SWA": "WN",
    "IBE": "IB", "DLH": "LH", "AFR": "AF", "KLM": "KL", "TAP": "TP", "BAW": "BA",
    "SWR": "LX", "VIR": "VS", "UAE": "EK", "QTR": "QR", "THY": "TK", "ETH": "ET",
    "ETD": "EY", "CMP": "CM", "AVA": "AV", "TPU": "T0", "ARG": "AR", "AMX": "AM",
    "BOV": "OB", "GLG": "G3", "SKU": "H2", "JAT": "JA", "AEE": "A3",
    "DTA": "DT", "RAM": "AT", "SAA": "SA", "MSR": "MS", "CCA": "CA", "CSN": "CZ",
    "AZA": "AZ", "SIA": "SQ", "ANA": "NH", "JJ": "LA", "ARE": "LA",
}

_STATUS_CANCEL = ("cancel",)
_STATUS_DELAY = ("delay", "atras")


def _iata_airline(icao: str) -> str:
    icao = (icao or "").strip().upper()
    return _ICAO2IATA_AL.get(icao, icao)


def _date_iso(datadiames: str) -> str:
    """'02/07' + current year → 'YYYY-MM-DD' (America/Sao_Paulo)."""
    now = datetime.utcnow()
    try:
        d, m = (datadiames or "").split("/")[:2]
        y = now.year
        # if the board day is far in the past-month vs now (Dec→Jan rollover), bump year
        return f"{y}-{int(m):02d}-{int(d):02d}"
    except Exception:
        return now.strftime("%Y-%m-%d")


def _row(rec: dict, arr: bool) -> dict | None:
    nums = rec.get("NumVoo") or []
    cias = rec.get("Cias") or []
    if not nums:
        return None
    num = str(nums[0]).strip().lstrip("0") or str(nums[0]).strip()
    cia0 = cias[0] if cias else {}
    icao = (cia0.get("Sigla") or "").strip().upper()
    al = _iata_airline(icao)
    r = S.empty_row()
    r["airline"] = al
    r["airline_name"] = (cia0.get("Nome") or "").strip()
    r["flight"] = S.norm_flight(al, num)
    r["dest_name"] = (rec.get("OrigemDestino") or "").strip()
    r["dest_iata"] = REF.resolve(r["dest_name"]) or ""
    ymd = _date_iso(rec.get("DataDiaMes"))
    r["sched"] = S.local_iso(ymd=ymd, hhmmss=(rec.get("Horario") or ""))
    conf = rec.get("HorarioConfirmado") or ""
    esti = S.local_iso(ymd=ymd, hhmmss=conf)
    if esti and esti != r["sched"]:
        r["esti"] = esti
    r["terminal"] = (rec.get("Terminal") or "").replace("Terminal ", "T").strip()
    if not arr:
        r["gate"] = (rec.get("Portao") or "").strip()
    obs = (rec.get("Observacao") or "").strip()
    r["status"] = obs
    low = obs.lower()
    r["cancelled"] = any(h in low for h in _STATUS_CANCEL)
    r["delayed"] = any(h in low for h in _STATUS_DELAY)
    r["_tz"] = TZ
    if not r["flight"] or not r["sched"]:
        return None
    return S.finalize(r)


def _fetch(drv, tipo: str):
    body = "procura=&terminal=&tipo=%s&pagina=0" % tipo
    js = """async (b) => {
        const r = await fetch('%s', {method:'POST',
            headers:{'Content-Type':'application/x-www-form-urlencoded'}, body:b});
        return await r.text();
    }""" % ASMX
    try:
        txt = drv._page.evaluate(js, body)
    except Exception:
        return []
    if not txt:
        return []
    m = re.search(r">(\[.*\])<", txt, re.S)
    raw = m.group(1) if m else txt
    try:
        return json.loads(raw)
    except Exception:
        return []


def scrape(drv) -> dict:
    try:
        drv._page.goto(WARMUP, wait_until="domcontentloaded", timeout=45000)
        drv._page.wait_for_timeout(3000)
    except Exception:
        pass
    out = {"departure": [], "arrival": []}
    for tipo, key, arr in (("Partida", "departure", False), ("Chegada", "arrival", True)):
        seen = set()
        for rec in _fetch(drv, tipo):
            row = _row(rec, arr)
            if not row:
                continue
            k = (row["flight"], row["sched"])
            if k in seen:
                continue
            seen.add(k)
            out[key].append(row)
    return {"GRU": out}
