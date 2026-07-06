#!/usr/bin/env python3
"""AeroX FR24-Grauzonen-Harvester — verteilte Positions-Ernte.

Läuft als winziger Daemon auf mehreren Maschinen mit VERSCHIEDENEN IPs (z.B.
Oracle-Always-Free-VMs). Jede Instanz pollt ihre zugewiesene(n) FR24-Korridor-
Kachel(n) und upsertet die normalisierten Rows nach Supabase `fr24_live`. Das
AeroX-Backend liest diese Tabelle warm (kein eigener FR24-Kontakt) → FR24-Last
über viele IPs verteilt, kein Single-IP-Block, alle Zonen parallel.

ENV (Pflicht):
  SUPABASE_URL   z.B. https://xxxx.supabase.co
  SUPABASE_KEY   Service-Role-Key (Schreibrecht auf fr24_live)
ENV (optional):
  TILES          Kommaliste von Kachel-Indizes (0-7) für DIESE Instanz,
                 z.B. "0,1" — Default "all" (alle, für Einzel-Node-Test).
                 Verteilung: VM1 TILES=0,1  VM2 TILES=2,3  VM3 TILES=4,5  VM4 TILES=6,7
  POLL_SECONDS   Sekunden zwischen zwei Kachel-Fetches DIESER Instanz (Default 20;
                 pro Kachel also alle len(TILES)*POLL_SECONDS s → höflich zu FR24).
  MAXAGE         FR24-maxage-Param (Default 14400).

Deps: requests. Deploy siehe README.md.
"""
import json
import os
import random
import sys
import time

import requests

FR24_FEED_URL = "https://data-cloud.flightradar24.com/zones/fcgi/feed.js"
# UA-Pool: pro Fetch rotiert (jede IP sieht nicht immer denselben UA → weniger
# Fingerprint-Signal). Alle aktuelle Desktop-Browser.
FR24_UAS = [
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
     "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (X11; Linux x86_64; rv:126.0) Gecko/20100101 Firefox/126.0"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
]

# Kacheln (lat_n,lat_s,lon_w,lon_e). 0-7 = COVERAGE-LÖCHER (kritisch: dort liefern
# die freien ADS-B-Netze nichts → FR24 ist die einzige Positionsquelle). 8-14 =
# EUROPA-ENRICHMENT: FR24 liefert dort Route+Tail für jeden Flieger gratis →
# füttert das Warehouse (Routen-Cache) und spart AeroDataBox-Routen-Spend.
# Europa ist dicht → Kacheln clippen ggf. bei 1500/Call (fürs Enrichment egal:
# 1500 Routen/Tails geschenkt; für POSITIONEN in EU nutzen wir eh die freien Netze).
# Round-Robin über ALLE → gleiche höfliche Rate, nur längerer Zyklus.
# (Backend-Selbst-Harvest-Fallback nutzt NUR 0-7, damit die kritischen Löcher
#  auch ohne NAS schnell frisch bleiben.)
FR24_TILES = [
    (55, 20, 55, 110),    # 0 Zentralasien/West-China        ── Löcher ──
    (72, 45, 55, 140),    # 1 Trans-Sibirien
    (55, 20, 110, 145),   # 2 Ost-China/Korea/Japan-Anflug
    (45, 8, 30, 65),      # 3 Naher Osten / Kaspisch
    (35, -10, 60, 100),   # 4 Indien / Indischer Ozean
    (72, 35, -60, -10),   # 5 Nordatlantik
    (60, 15, 140, 180),   # 6 Nordpazifik-West
    (40, -40, -25, 55),   # 7 Afrika / Südatlantik
    (60, 48, -11, 3),     # 8  UK/Irland/Benelux-NW           ── Europa-Enrichment ──
    (52, 42, -3, 10),     # 9  Frankreich/Benelux/W-DE/Schweiz
    (56, 45, 9, 20),      # 10 Mittel-EU (DE-Ost/PL/CZ/AT)
    (72, 55, 4, 32),      # 11 Skandinavien/Baltikum
    (45, 35, -10, 5),     # 12 Iberien
    (47, 35, 6, 30),      # 13 Italien/Adria/Balkan/Griechenland
    (52, 44, 20, 40),     # 14 Ost-EU/Ukraine/Türkei-Nord
]


def _n(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _row_to_opensky(v):
    """FR24-feed.js-Zeile → OpenSky-State-Row. IDENTISCH zu _fr24_row_to_opensky
    im Backend, damit die gespeicherte `row` 0 Transformation braucht."""
    try:
        if not isinstance(v, list) or len(v) < 17:
            return None
        lat, lon = v[1], v[2]
        if lat in (None, 0) and lon in (None, 0):
            return None
        alt_ft = _n(v[4]); gs_kt = _n(v[5]); vs_fpm = _n(v[15])
        ts = _n(v[10]) or time.time()
        cs = (str(v[16]).strip() or None) if v[16] else None
        reg = (str(v[9]).strip().upper() or None) if v[9] else None
        return [
            (str(v[0]).strip().lower() or None), cs, reg, ts, ts,
            _n(lon), _n(lat),
            (alt_ft * 0.3048) if alt_ft is not None else None,
            bool(v[14]),
            (gs_kt * 0.514444) if gs_kt is not None else None,
            _n(v[3]),
            (vs_fpm * 0.00508) if vs_fpm is not None else None,
            None,
            (alt_ft * 0.3048) if alt_ft is not None else None,
            (str(v[6]).strip() or None) if v[6] else None,
            False, 0,
        ]
    except Exception:
        return None


class _Blocked(Exception):
    """FR24 hat gedrosselt/geblockt (429/403 ODER 200 mit leerem ac trotz Luft-
    verkehr in der Kachel) → Aufrufer macht Exponential-Backoff."""


def fetch_tile(session, tile):
    n, s, w, e = tile
    url = (f"{FR24_FEED_URL}?bounds={n},{s},{w},{e}"
           "&faa=1&mlat=1&flarm=1&adsb=1&gnd=0&air=1&vehicles=0"
           f"&estimated=1&maxage={os.environ.get('MAXAGE', '14400')}&gliders=0&stats=0")
    r = session.get(url, headers={
        "User-Agent": random.choice(FR24_UAS), "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.flightradar24.com/",
    }, timeout=15)
    if r.status_code in (403, 429):
        raise _Blocked(f"http {r.status_code}")
    r.raise_for_status()
    obj = r.json()
    # 200 mit full_count>0 aber KEINE ac-Zeilen = Soft-Block (IP gedrosselt).
    if obj.get("full_count") and not any(isinstance(v, list) for v in obj.values()):
        raise _Blocked("empty_ac (soft-block)")
    out = []
    for k, v in obj.items():
        if not isinstance(v, list):
            continue
        row = _row_to_opensky(v)
        if row is not None and row[0] is not None:
            # Route (IATA) aus den FR24-ROHfeldern [11]/[12] — die normalisierte
            # OpenSky-Row trägt sie NICHT; separat fürs Warehouse-Enrichment.
            origin = (str(v[11]).strip().upper() or None) if len(v) > 11 and v[11] else None
            dest = (str(v[12]).strip().upper() or None) if len(v) > 12 and v[12] else None
            flight = (str(v[13]).strip().upper() or None) if len(v) > 13 and v[13] else None
            out.append({"row": row, "origin": origin, "dest": dest, "flight": flight})
    return out


def upsert(session, sb_url, sb_key, items, tile_idx):
    if not items:
        return 0
    now_iso = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    payload = [{
        "hex": it["row"][0], "callsign": it["row"][1],
        "lat": it["row"][6], "lon": it["row"][5],
        "origin": it["origin"], "dest": it["dest"], "flight": it["flight"],
        "row": it["row"], "tile": str(tile_idx), "updated_at": now_iso,
    } for it in items]
    ep = sb_url.rstrip('/') + "/rest/v1/fr24_live?on_conflict=hex"
    r = session.post(ep, headers={
        "apikey": sb_key, "Authorization": f"Bearer {sb_key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }, data=json.dumps(payload), timeout=20)
    r.raise_for_status()
    return len(payload)


def main():
    sb_url = os.environ.get("SUPABASE_URL", "").strip()
    sb_key = os.environ.get("SUPABASE_KEY", "").strip()
    if not sb_url or not sb_key:
        print("FATAL: SUPABASE_URL und SUPABASE_KEY erforderlich", file=sys.stderr)
        sys.exit(2)
    tiles_env = os.environ.get("TILES", "all").strip().lower()
    if tiles_env in ("all", "*", ""):
        my_tiles = list(range(len(FR24_TILES)))
    else:
        my_tiles = [int(x) for x in tiles_env.replace(";", ",").split(",")
                    if x.strip().isdigit() and 0 <= int(x) < len(FR24_TILES)]
    if not my_tiles:
        print("FATAL: keine gültigen TILES", file=sys.stderr)
        sys.exit(2)
    poll = float(os.environ.get("POLL_SECONDS", "20"))
    # Optionaler Proxy (später ohne Code-Änderung reinsteckbar): HTTPS_PROXY /
    # HARVESTER_PROXY. requests liest HTTPS_PROXY automatisch; HARVESTER_PROXY
    # setzt ihn explizit auf die Session.
    session = requests.Session()
    prox = os.environ.get("HARVESTER_PROXY", "").strip()
    if prox:
        session.proxies.update({"http": prox, "https": prox})
    print(f"[fr24-harvester] tiles={my_tiles} poll={poll}s proxy={'yes' if prox else 'no'} "
          f"-> {sb_url}", flush=True)

    # QUIET (Default AN auf der NAS): unterdrückt die Erfolgs-Logzeile pro Fetch,
    # damit die Docker-json-Logs NICHT alle 20 s auf die HDDs schreiben und die
    # Platten in den Ruhezustand gehen können (Synology-HDD-Hibernation). Fehler/
    # Blocks werden weiter geloggt; alle HEARTBEAT_MIN Minuten eine Sammelzeile.
    quiet = os.environ.get("QUIET", "0").strip() not in ("0", "", "false", "no")
    heartbeat_min = float(os.environ.get("HEARTBEAT_MIN", "30"))
    i = 0
    backoff = 0.0          # wächst bei Blocks, sinkt bei Erfolg
    win_rows = 0           # aufsummierte Rows seit letztem Heartbeat
    last_hb = time.time()
    while True:
        idx = my_tiles[i % len(my_tiles)]
        i += 1
        try:
            rows = fetch_tile(session, FR24_TILES[idx])
            n = upsert(session, sb_url, sb_key, rows, idx)
            win_rows += n
            if not quiet:
                print(f"[fr24-harvester] tile{idx} rows={len(rows)} upserted={n}", flush=True)
            elif time.time() - last_hb >= heartbeat_min * 60:
                print(f"[fr24-harvester] heartbeat: {win_rows} rows upserted in "
                      f"letzten {heartbeat_min:.0f}min", flush=True)
                win_rows = 0
                last_hb = time.time()
            backoff = max(0.0, backoff - 30.0)      # erholt sich schrittweise
        except _Blocked as e:
            backoff = min(900.0, (backoff * 2) or 60.0)   # 60s→2m→4m…→15m Deckel
            print(f"[fr24-harvester] tile{idx} BLOCKED {e} -> backoff {backoff:.0f}s "
                  f"(IP evtl. gedrosselt; ggf. Region/Proxy wechseln)",
                  file=sys.stderr, flush=True)
            time.sleep(backoff)
        except Exception as e:
            print(f"[fr24-harvester] tile{idx} ERROR {type(e).__name__}: "
                  f"{str(e)[:100]}", file=sys.stderr, flush=True)
            time.sleep(30)
        # Jitter ±30% auf das Poll-Intervall → kein maschinell-regelmäßiges Muster.
        time.sleep(max(5.0, poll * random.uniform(0.7, 1.3)))


if __name__ == "__main__":
    main()
