"""
Airport registry. Importing this module registers every cracked airport / group
operator into scraper.REGISTRY. Keys are lowercase group/airport ids used by the
/scrape endpoint (?airports=aena,zrh,vie or ?airports=all).
"""
from .. import scraper as S
from . import aena, zurich, vienna


def _reg_aena(drv):
    # honour a per-request airport subset via drv attribute (set by main.py)
    subset = getattr(drv, "aena_subset", None)
    sweep = getattr(drv, "aena_all", False)
    return aena.scrape(drv, airports=subset, sweep_all=sweep)


S.register("aena", _reg_aena)      # ~25-49 Spanish airports (group)
S.register("zrh", zurich.scrape)   # Zurich (full fields incl. tail reg)
S.register("vie", vienna.scrape)   # Vienna (departures)

# Convenience: expand a caller-facing IATA (e.g. "MAD") to the group key.
IATA_TO_KEY = {"ZRH": "zrh", "VIE": "vie"}
for _c in aena.DEFAULT:
    IATA_TO_KEY[_c] = "aena"
