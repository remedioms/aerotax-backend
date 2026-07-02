"""
PANYNJ — Port Authority of NY & NJ airport boards: JFK, EWR, LGA (LIVE-verified
2026-07-02). All three share one Apollo GraphQL endpoint per site:

    POST https://www.<site>/api/graphql
    body  lz-string(compressToEncodedURIComponent) of the Apollo JSON payload
          {operationName, variables, query}   (the site's link compresses the body;
          the server rejects a plain-JSON body, so we vendor a tiny pure-python
          lz-string compressor below — no runtime dependency added.)

    query GetDepartingFlights / GetArrivingFlights(airport, dateTime range "from/to",
          limit, after)  → data[] + paging.next (cursor). We page a full 2-day window.

Per record: dateScheduled/timeScheduled (LOCAL), dateRevised/timeRevised (estimate),
destinationAirportCode / originAirportCode (IATA!), airlineCode, airlineName,
flightNumber, terminal, gate, status ("Departed"/"Arrived"/"Cancelled"/"Delayed").
No tail/aircraft type in this feed. Times are America/New_York local already.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from .. import scraper as S
from .. import airports_ref as REF

TZ = "America/New_York"

# key → (site-origin, airport IATA)
AIRPORTS = {
    "JFK": ("https://www.jfkairport.com", "JFK"),
    "EWR": ("https://www.newarkairport.com", "EWR"),
    "LGA": ("https://www.laguardiaairport.com", "LGA"),
}
DEFAULT = list(AIRPORTS.keys())

# ── vendored lz-string compressToEncodedURIComponent (pure python, verified byte-
#    identical to the `lzstring` pypi port against the live site) ──────────────
_keyStrUriSafe = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+-$"

def _compress(uncompressed, bitsPerChar, getCharFromInt):
    if (uncompressed is None):
        return ""

    context_dictionary = {}
    context_dictionaryToCreate= {}
    context_c = ""
    context_wc = ""
    context_w = ""
    context_enlargeIn = 2 # Compensate for the first entry which should not count
    context_dictSize = 3
    context_numBits = 2
    context_data = []
    context_data_val = 0
    context_data_position = 0

    for ii in range(len(uncompressed)):
        context_c = uncompressed[ii]
        if context_c not in context_dictionary:
            context_dictionary[context_c] = context_dictSize
            context_dictSize += 1
            context_dictionaryToCreate[context_c] = True

        context_wc = context_w + context_c
        if context_wc in context_dictionary:
            context_w = context_wc
        else:
            if context_w in context_dictionaryToCreate:
                if ord(context_w[0]) < 256:
                    for i in range(context_numBits):
                        context_data_val = (context_data_val << 1)
                        if context_data_position == bitsPerChar-1:
                            context_data_position = 0
                            context_data.append(getCharFromInt(context_data_val))
                            context_data_val = 0
                        else:
                            context_data_position += 1
                    value = ord(context_w[0])
                    for i in range(8):
                        context_data_val = (context_data_val << 1) | (value & 1)
                        if context_data_position == bitsPerChar - 1:
                            context_data_position = 0
                            context_data.append(getCharFromInt(context_data_val))
                            context_data_val = 0
                        else:
                            context_data_position += 1
                        value = value >> 1

                else:
                    value = 1
                    for i in range(context_numBits):
                        context_data_val = (context_data_val << 1) | value
                        if context_data_position == bitsPerChar - 1:
                            context_data_position = 0
                            context_data.append(getCharFromInt(context_data_val))
                            context_data_val = 0
                        else:
                            context_data_position += 1
                        value = 0
                    value = ord(context_w[0])
                    for i in range(16):
                        context_data_val = (context_data_val << 1) | (value & 1)
                        if context_data_position == bitsPerChar - 1:
                            context_data_position = 0
                            context_data.append(getCharFromInt(context_data_val))
                            context_data_val = 0
                        else:
                            context_data_position += 1
                        value = value >> 1
                context_enlargeIn -= 1
                if context_enlargeIn == 0:
                    context_enlargeIn = math.pow(2, context_numBits)
                    context_numBits += 1
                del context_dictionaryToCreate[context_w]
            else:
                value = context_dictionary[context_w]
                for i in range(context_numBits):
                    context_data_val = (context_data_val << 1) | (value & 1)
                    if context_data_position == bitsPerChar - 1:
                        context_data_position = 0
                        context_data.append(getCharFromInt(context_data_val))
                        context_data_val = 0
                    else:
                        context_data_position += 1
                    value = value >> 1

            context_enlargeIn -= 1
            if context_enlargeIn == 0:
                context_enlargeIn = math.pow(2, context_numBits)
                context_numBits += 1
            
            # Add wc to the dictionary.
            context_dictionary[context_wc] = context_dictSize
            context_dictSize += 1
            context_w = str(context_c)

    # Output the code for w.
    if context_w != "":
        if context_w in context_dictionaryToCreate:
            if ord(context_w[0]) < 256:
                for i in range(context_numBits):
                    context_data_val = (context_data_val << 1)
                    if context_data_position == bitsPerChar-1:
                        context_data_position = 0
                        context_data.append(getCharFromInt(context_data_val))
                        context_data_val = 0
                    else:
                        context_data_position += 1
                value = ord(context_w[0])
                for i in range(8):
                    context_data_val = (context_data_val << 1) | (value & 1)
                    if context_data_position == bitsPerChar - 1:
                        context_data_position = 0
                        context_data.append(getCharFromInt(context_data_val))
                        context_data_val = 0
                    else:
                        context_data_position += 1
                    value = value >> 1
            else:
                value = 1
                for i in range(context_numBits):
                    context_data_val = (context_data_val << 1) | value
                    if context_data_position == bitsPerChar - 1:
                        context_data_position = 0
                        context_data.append(getCharFromInt(context_data_val))
                        context_data_val = 0
                    else:
                        context_data_position += 1
                    value = 0
                value = ord(context_w[0])
                for i in range(16):
                    context_data_val = (context_data_val << 1) | (value & 1)
                    if context_data_position == bitsPerChar - 1:
                        context_data_position = 0
                        context_data.append(getCharFromInt(context_data_val))
                        context_data_val = 0
                    else:
                        context_data_position += 1
                    value = value >> 1
            context_enlargeIn -= 1
            if context_enlargeIn == 0:
                context_enlargeIn = math.pow(2, context_numBits)
                context_numBits += 1
            del context_dictionaryToCreate[context_w]
        else:
            value = context_dictionary[context_w]
            for i in range(context_numBits):
                context_data_val = (context_data_val << 1) | (value & 1)
                if context_data_position == bitsPerChar - 1:
                    context_data_position = 0
                    context_data.append(getCharFromInt(context_data_val))
                    context_data_val = 0
                else:
                    context_data_position += 1
                value = value >> 1

    context_enlargeIn -= 1
    if context_enlargeIn == 0:
        context_enlargeIn = math.pow(2, context_numBits)
        context_numBits += 1

    # Mark the end of the stream
    value = 2
    for i in range(context_numBits):
        context_data_val = (context_data_val << 1) | (value & 1)
        if context_data_position == bitsPerChar - 1:
            context_data_position = 0
            context_data.append(getCharFromInt(context_data_val))
            context_data_val = 0
        else:
            context_data_position += 1
        value = value >> 1

    # Flush the last char
    while True:
        context_data_val = (context_data_val << 1)
        if context_data_position == bitsPerChar - 1:
            context_data.append(getCharFromInt(context_data_val))
            break
        else:
           context_data_position += 1

    return "".join(context_data)



def _lz_uri(s: str) -> str:
    return _compress(s, 6, lambda a: _keyStrUriSafe[a])


_DEP_Q = ("query GetDepartingFlights($departureAirport: String!, $departureDateTime: String!, "
          "$destinationAirport: String, $carrierCode: String, $limit: Int, $after: String) { "
          "getDepartingFlights(departureAirport: $departureAirport, departureDateTime: $departureDateTime, "
          "destinationAirport: $destinationAirport, carrierCode: $carrierCode, limit: $limit, after: $after) { "
          "data { dateScheduled timeScheduled dateRevised timeRevised destinationName destinationAirportCode "
          "airlineCode airlineName flightNumber terminal gate status __typename } "
          "paging { next __typename } __typename } }")

_ARR_Q = ("query GetArrivingFlights($arrivalAirport: String!, $arrivalDateTime: String!, "
          "$originAirport: String, $carrierCode: String, $limit: Int, $after: String) { "
          "getArrivingFlights(arrivalAirport: $arrivalAirport, arrivalDateTime: $arrivalDateTime, "
          "originAirport: $originAirport, carrierCode: $carrierCode, limit: $limit, after: $after) { "
          "data { dateScheduled timeScheduled dateRevised timeRevised originName originAirportCode "
          "airlineCode airlineName flightNumber terminal gate status __typename } "
          "paging { next __typename } __typename } }")


def _to_iso(date_str, time_str):
    """'2026-07-02' + '06:03 PM' -> naive-local '2026-07-02T18:03:00'."""
    if not date_str or not time_str:
        return None
    for fmt in ("%Y-%m-%d %I:%M %p", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(f"{date_str} {time_str}".strip(), fmt).strftime("%Y-%m-%dT%H:%M:00")
        except Exception:
            continue
    return None


def _row(rec: dict, arr: bool) -> dict | None:
    al = (rec.get("airlineCode") or "").strip().upper()
    num = str(rec.get("flightNumber") or "").strip()
    if not (al and num):
        return None
    r = S.empty_row()
    r["airline"] = al
    r["airline_name"] = (rec.get("airlineName") or "").strip()
    r["flight"] = S.norm_flight(al, num)
    code = ((rec.get("originAirportCode") if arr else rec.get("destinationAirportCode")) or "").strip().upper()
    name = ((rec.get("originName") if arr else rec.get("destinationName")) or "").strip()
    r["dest_name"] = name
    r["dest_iata"] = REF.resolve(name, code=code) or code
    r["sched"] = _to_iso(rec.get("dateScheduled"), rec.get("timeScheduled"))
    esti = _to_iso(rec.get("dateRevised") or rec.get("dateScheduled"), rec.get("timeRevised"))
    if esti and esti != r["sched"]:
        r["esti"] = esti
    r["terminal"] = (rec.get("terminal") or "").strip()
    r["gate"] = (rec.get("gate") or "").strip()
    st = (rec.get("status") or "").strip()
    r["status"] = st
    low = st.lower()
    r["cancelled"] = "cancel" in low
    r["delayed"] = "delay" in low
    r["_tz"] = TZ
    if not r["flight"] or not r["sched"]:
        return None
    return S.finalize(r)


def _page(drv, origin, query, op, field, airport_var, dt_var, iata, rng):
    out = []
    after = ""
    for _ in range(30):  # 30 * 200 caps a very busy 2-day board
        variables = {airport_var: iata, dt_var: rng, "carrierCode": "", "limit": 200, "after": after}
        if op == "GetDepartingFlights":
            variables["destinationAirport"] = ""
        else:
            variables["originAirport"] = ""
        body = _lz_uri(json.dumps({"operationName": op, "variables": variables, "query": query}))
        try:
            resp = drv._ctx.request.post(
                f"{origin}/api/graphql", data=body, timeout=25000,
                headers={"content-type": "text/plain;charset=UTF-8",
                         "Origin": origin, "Referer": f"{origin}/flights"})
            if not resp.ok:
                break
            node = (resp.json().get("data") or {}).get(field) or {}
        except Exception:
            break
        rows = node.get("data") or []
        out.extend(rows)
        nxt = (node.get("paging") or {}).get("next")
        if not nxt or not rows:
            break
        after = nxt
    return out


def scrape(drv, airports=None) -> dict:
    now = datetime.now()
    lo = (now - timedelta(days=1)).strftime("%Y-%m-%dT00:00")
    hi = (now + timedelta(days=1)).strftime("%Y-%m-%dT23:59")
    rng = f"{lo}/{hi}"
    res = {}
    for code in (airports or DEFAULT):
        code = code.upper()
        if code not in AIRPORTS:
            continue
        origin, iata = AIRPORTS[code]
        drv.warm(f"{origin}/flights", wait_ms=1500)
        out = {"departure": [], "arrival": []}
        for query, op, field, avar, dvar, arr, key in (
                (_DEP_Q, "GetDepartingFlights", "getDepartingFlights", "departureAirport", "departureDateTime", False, "departure"),
                (_ARR_Q, "GetArrivingFlights", "getArrivingFlights", "arrivalAirport", "arrivalDateTime", True, "arrival")):
            seen = set()
            for rec in _page(drv, origin, query, op, field, avar, dvar, iata, rng):
                row = _row(rec, arr)
                if not row:
                    continue
                k = (row["flight"], row["sched"], row["dest_iata"])
                if k in seen:
                    continue
                seen.add(k)
                out[key].append(row)
        res[code] = out
    return res
