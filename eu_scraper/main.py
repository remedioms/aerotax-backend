"""
main — HTTP entrypoint for the AeroX EU headless-scraper Cloud Run service.

Routes (all secret-guarded via X-Scrape-Secret == env SCRAPE_SECRET, if set):
  GET  /                      → health (lists registry keys)
  POST /scrape?airports=aena,zrh,vie     → scrape those groups/airports + write
  POST /scrape?airports=all              → scrape every registered group
       &aena=MAD,BCN,PMI                 → restrict the AENA sweep to a subset
       &aena=all                         → sweep ALL ~49 Spanish airports
       &write=0                          → dry run (scrape, don't write)

Fail-safe: a single airport/group failure never breaks the batch or crashes the
service — errors are captured per-airport and returned in the JSON summary.
"""
from __future__ import annotations

import os
import time

from flask import Flask, request, jsonify

from . import scraper
from . import supabase_writer as writer
from . import airports as _airports_pkg  # noqa: F401  (populates the registry)

app = Flask(__name__)

SECRET = os.environ.get("SCRAPE_SECRET") or ""


def _authed(req) -> bool:
    if not SECRET:
        return True
    return req.headers.get("X-Scrape-Secret", "") == SECRET


@app.get("/")
def health():
    scraper._load_registry()
    return jsonify({
        "service": "aerotax-eu-scraper",
        "ok": True,
        "registry": sorted(scraper.REGISTRY.keys()),
        "aena_default_count": len(_airports_pkg.aena.DEFAULT),
        "supabase": writer.available(),
    })


@app.post("/scrape")
def scrape():
    if not _authed(request):
        return jsonify({"error": "unauthorized"}), 401

    raw = (request.args.get("airports") or "all").strip().lower()
    do_write = request.args.get("write", "1") != "0"
    aena_arg = (request.args.get("aena") or "").strip()

    scraper._load_registry()
    if raw in ("all", "*"):
        targets = list(scraper.REGISTRY.keys())
    else:
        wanted = [t.strip() for t in raw.split(",") if t.strip()]
        targets = []
        for t in wanted:
            key = t if t in scraper.REGISTRY else _airports_pkg.IATA_TO_KEY.get(t.upper())
            if key and key not in targets:
                targets.append(key)

    t0 = time.time()
    driver_attrs = {}
    if aena_arg.lower() == "all":
        driver_attrs["aena_all"] = True
    elif aena_arg:
        driver_attrs["aena_subset"] = [c.strip().upper()
                                       for c in aena_arg.split(",") if c.strip()]
    scraped = scraper.scrape_targets(
        targets or None,
        headless=os.environ.get("HEADLESS", "1") != "0",
        driver_attrs=driver_attrs,
    )

    summary = {
        "targets": targets,
        "scrape_seconds": round(time.time() - t0, 1),
        "airports": {},
    }
    total_rows = 0
    for iata, dirs in scraped.items():
        if iata.startswith("_") or not isinstance(dirs, dict) or "departure" not in dirs:
            summary.setdefault("meta", {})[iata] = dirs
            continue
        n = len(dirs.get("departure") or []) + len(dirs.get("arrival") or [])
        total_rows += n
        summary["airports"][iata] = {
            "departures": len(dirs.get("departure") or []),
            "arrivals": len(dirs.get("arrival") or []),
            "error": dirs.get("error"),
        }
    summary["total_rows_scraped"] = total_rows

    if do_write:
        tw = time.time()
        summary["write"] = writer.write_all(scraped)
        summary["write_seconds"] = round(time.time() - tw, 1)
    else:
        summary["write"] = "skipped (write=0)"

    return jsonify(summary)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
