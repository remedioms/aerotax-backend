"""
Airport registry. Importing this module registers every cracked airport / group
operator into scraper.REGISTRY. Keys are lowercase group/airport ids used by the
/scrape endpoint (?airports=aena,zrh,vie or ?airports=all).
"""
from .. import scraper as S
from . import aena, zurich, vienna, portugal, budapest, dublin, prague, mag


def _reg_aena(drv):
    # honour a per-request airport subset via drv attribute (set by main.py)
    subset = getattr(drv, "aena_subset", None)
    sweep = getattr(drv, "aena_all", False)
    return aena.scrape(drv, airports=subset, sweep_all=sweep)


def _reg_ana(drv):
    subset = getattr(drv, "ana_subset", None)
    return portugal.scrape(drv, airports=subset)


def _reg_mag(drv):
    subset = getattr(drv, "mag_subset", None)
    return mag.scrape(drv, airports=subset)


S.register("aena", _reg_aena)      # ~25-49 Spanish airports (group)
S.register("ana", _reg_ana)        # ~11 Portuguese airports (group: LIS/OPO/FAO/…)
S.register("zrh", zurich.scrape)   # Zurich (full fields incl. tail reg)
S.register("vie", vienna.scrape)   # Vienna (departures)
S.register("bud", budapest.scrape)  # Budapest (dep+arr, incl. gate/reg)
S.register("dub", dublin.scrape)   # Dublin (dep+arr, full day, incl. gate)
S.register("prg", prague.scrape)   # Prague (dep+arr, IATA dests + gate)
S.register("mag", _reg_mag)        # MAG group: MAN/STN/EMA (dep+arr, full gate)

# Convenience: expand a caller-facing IATA (e.g. "MAD") to the group key.
IATA_TO_KEY = {"ZRH": "zrh", "VIE": "vie", "BUD": "bud", "DUB": "dub", "PRG": "prg"}
for _c in aena.DEFAULT:
    IATA_TO_KEY[_c] = "aena"
for _c in portugal.DEFAULT:
    IATA_TO_KEY[_c] = "ana"
for _c in mag.DEFAULT:
    IATA_TO_KEY[_c] = "mag"
