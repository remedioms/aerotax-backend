#!/usr/bin/env python3
"""
Pre-Bake der Destinations-Fotos nach Cloudflare R2.

WARUM (Pre-Release-Audit, Skalierungs-Blocker): Heute holt das Backend
Destinations-Fotos pro Request live von Pexels und cached sie nur PRO Instanz
im RAM. Bei ~5k Usern x ~625 Instanzen sprengt das das Pexels-Quota -> leere
(graue) Karten. R2 hat 10 GB gratis + NULL Egress (du nutzt es schon fuer
Avatare). Dieses Script holt EINMAL je Destination ein gutes Foto und legt es
nach R2 -> das Backend kann dann die stabile R2-URL ausliefern statt pro Request
Pexels zu fragen. Quota-Problem geloest, schneller, billiger.

SICHER & EIGENSTAENDIG: Aendert NICHT app.py und legt KEINEN neuen Endpoint an
(das waere laut CLAUDE.md eine 'vorher fragen'-Aenderung). Es befuellt nur R2.
Das Ausliefern der R2-URL im Backend ist der dokumentierte naechste Schritt
(siehe README am Ende).

NUTZUNG:
    export PEXELS_API_KEY=...          # schon gesetzt (pexels_blueprint nutzt ihn)
    export R2_ENDPOINT=...             # schon gesetzt (Avatare)
    export R2_ACCESS_KEY_ID=...
    export R2_SECRET_ACCESS_KEY=...
    export R2_PHOTOS_BUCKET=aerox-photos   # NEU: eigener Bucket fuer Destinationen
    export R2_PHOTOS_PUBLIC_BASE=https://photos.aerosteuer.de   # dessen public base
    python3 tools/prebake_destination_photos.py                 # baked die Default-Liste
    python3 tools/prebake_destination_photos.py --destinations my_list.json --force

--destinations: optional JSON [{"iata":"SFO","city":"San Francisco","query":"San Francisco skyline"}, ...]
--force: auch ueberschreiben, wenn das Objekt in R2 schon existiert (sonst idempotent/skip).

Quota-schonend: Pexels Free = 200 req/h, 20k/Monat -> das Script drosselt auf
~1 req/2s und ist idempotent (ueberspringt schon gebakte). Mehrfach laufbar.
"""
import os
import sys
import json
import time
import argparse
import urllib.request
import urllib.parse

PEXELS_KEY = os.environ.get("PEXELS_API_KEY", "")
R2_ENDPOINT = os.environ.get("R2_ENDPOINT", "")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_PHOTOS_BUCKET = os.environ.get("R2_PHOTOS_BUCKET", "aerox-photos")
R2_PUBLIC_BASE = os.environ.get("R2_PHOTOS_PUBLIC_BASE", "").rstrip("/")

# Starter-Liste der haeufigsten Crew-Destinationen. Erweiterbar via --destinations.
# (Bewusst klein gehalten — bei ~1 MB/Foto sind selbst 500 Staedte < 0.5 GB.)
DEFAULT_DESTINATIONS = [
    {"iata": "SFO", "city": "San Francisco", "query": "San Francisco skyline golden gate"},
    {"iata": "JFK", "city": "New York", "query": "New York City skyline manhattan"},
    {"iata": "MIA", "city": "Miami", "query": "Miami beach skyline"},
    {"iata": "BCN", "city": "Barcelona", "query": "Barcelona city sagrada familia"},
    {"iata": "BIO", "city": "Bilbao", "query": "Bilbao guggenheim city"},
    {"iata": "MUC", "city": "Muenchen", "query": "Munich marienplatz city"},
    {"iata": "FRA", "city": "Frankfurt", "query": "Frankfurt skyline main"},
    {"iata": "BOS", "city": "Boston", "query": "Boston skyline harbor"},
    {"iata": "NAP", "city": "Neapel", "query": "Naples italy bay vesuvius"},
    {"iata": "IST", "city": "Istanbul", "query": "Istanbul bosphorus mosque"},
    {"iata": "AGP", "city": "Malaga", "query": "Malaga spain coast city"},
    {"iata": "AMS", "city": "Amsterdam", "query": "Amsterdam canals city"},
    {"iata": "NCE", "city": "Nizza", "query": "Nice france riviera coast"},
    {"iata": "STR", "city": "Stuttgart", "query": "Stuttgart germany city"},
    {"iata": "ARN", "city": "Stockholm", "query": "Stockholm sweden old town"},
    {"iata": "MLA", "city": "Malta", "query": "Valletta malta harbor"},
    {"iata": "GYD", "city": "Baku", "query": "Baku azerbaijan flame towers"},
    {"iata": "WAW", "city": "Warschau", "query": "Warsaw poland old town"},
    {"iata": "PRG", "city": "Prag", "query": "Prague czech old town castle"},
    {"iata": "BUD", "city": "Budapest", "query": "Budapest hungary parliament danube"},
    {"iata": "BER", "city": "Berlin", "query": "Berlin germany brandenburg gate"},
    {"iata": "HAM", "city": "Hamburg", "query": "Hamburg germany harbor elbphilharmonie"},
    {"iata": "SKG", "city": "Thessaloniki", "query": "Thessaloniki greece waterfront"},
]


def _r2_client():
    import boto3
    return boto3.client(
        "s3", endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
    )


def _pexels_photo_url(query):
    """Top Landscape-Foto fuer den Query (oder None)."""
    url = ("https://api.pexels.com/v1/search?per_page=1&orientation=landscape&query="
           + urllib.parse.quote(query))
    req = urllib.request.Request(url, headers={"Authorization": PEXELS_KEY})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read().decode())
    photos = data.get("photos") or []
    if not photos:
        return None
    # 'large' = ~1280px, guter Kompromiss fuer eine Karte.
    return photos[0]["src"].get("large") or photos[0]["src"].get("original")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--destinations", help="JSON-Datei mit [{iata,city,query}]")
    ap.add_argument("--force", action="store_true", help="auch ueberschreiben")
    args = ap.parse_args()

    missing = [k for k, v in {
        "PEXELS_API_KEY": PEXELS_KEY, "R2_ENDPOINT": R2_ENDPOINT,
        "R2_ACCESS_KEY_ID": R2_ACCESS_KEY_ID, "R2_SECRET_ACCESS_KEY": R2_SECRET_ACCESS_KEY,
        "R2_PHOTOS_PUBLIC_BASE": R2_PUBLIC_BASE,
    }.items() if not v]
    if missing:
        print("FEHLT (env):", ", ".join(missing)); sys.exit(2)

    dests = DEFAULT_DESTINATIONS
    if args.destinations:
        dests = json.load(open(args.destinations, encoding="utf-8"))

    cli = _r2_client()
    baked = skipped = failed = 0
    for d in dests:
        iata = d["iata"].upper()
        key = f"destinations/{iata}.jpg"
        if not args.force:
            try:
                cli.head_object(Bucket=R2_PHOTOS_BUCKET, Key=key)
                print(f"skip {iata} (schon in R2)"); skipped += 1; continue
            except Exception:
                pass
        try:
            q = d.get("query") or d.get("city") or iata
            src = _pexels_photo_url(q)
            if not src:
                print(f"FAIL {iata}: kein Pexels-Treffer fuer '{q}'"); failed += 1; continue
            with urllib.request.urlopen(src, timeout=30) as r:
                img = r.read()
            cli.put_object(Bucket=R2_PHOTOS_BUCKET, Key=key, Body=img,
                           ContentType="image/jpeg",
                           CacheControl="public, max-age=31536000, immutable")
            print(f"OK   {iata} -> {R2_PUBLIC_BASE}/{key}  ({len(img)//1024} KB)")
            baked += 1
            time.sleep(2)   # Pexels-Quota schonen
        except Exception as ex:
            print(f"FAIL {iata}: {ex}"); failed += 1

    print(f"\nFertig: {baked} gebaked, {skipped} schon da, {failed} fehlgeschlagen.")
    print(f"R2-URL-Schema: {R2_PUBLIC_BASE}/destinations/<IATA>.jpg")
    print("\nNAECHSTER SCHRITT (Backend, separat & vorher abstimmen):")
    print("  Im Foto-Endpoint zuerst die R2-URL pruefen/zurueckgeben, Pexels nur")
    print("  noch als Fallback fuer nicht-gebakte Destinationen. Spart bei 5k Usern")
    print("  das Pexels-Quota komplett.")


if __name__ == "__main__":
    main()
