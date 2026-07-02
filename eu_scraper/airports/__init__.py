"""
Airport registry. Importing this module registers every cracked airport / group
operator into scraper.REGISTRY. Keys are lowercase group/airport ids used by the
/scrape endpoint (?airports=aena,zrh,vie or ?airports=all).
"""
from .. import scraper as S
# Europe
from . import aena, zurich, vienna, portugal, budapest, dublin, prague, mag
# North America
from . import usfids, panynj, fruition, sfo, bos, iad, phx, yvr, yyc
# Middle East + South Asia
from . import dubai, doha, bengaluru, kuwait, hyderabad
# East/SE Asia + Oceania
from . import changi, hongkong, sydney, auckland, narita
# Latin America + Africa
from . import acsa, brazil, aa2000, santiago, bogota


# ---- group wrappers (honour per-request subset/sweep via drv attributes) ----
def _reg_aena(drv):
    return aena.scrape(drv, airports=getattr(drv, "aena_subset", None),
                       sweep_all=getattr(drv, "aena_all", False))

def _reg_ana(drv):
    return portugal.scrape(drv, airports=getattr(drv, "ana_subset", None))

def _reg_mag(drv):
    return mag.scrape(drv, airports=getattr(drv, "mag_subset", None))

def _reg_usfids(drv):
    return usfids.scrape(drv, airports=getattr(drv, "usfids_subset", None))

def _reg_panynj(drv):
    return panynj.scrape(drv, airports=getattr(drv, "panynj_subset", None))

def _reg_fruition(drv):
    return fruition.scrape(drv, airports=getattr(drv, "fruition_subset", None))

def _reg_acsa(drv):
    return acsa.scrape(drv, sweep_all=getattr(drv, "acsa_all", False))

def _reg_aa2000(drv):
    return aa2000.scrape(drv, airports=getattr(drv, "aa2000_subset", None),
                         sweep_all=getattr(drv, "aa2000_all", False))


# ---- registrations ----
# Europe
S.register("aena", _reg_aena)       # ~25-49 Spanish airports (group)
S.register("ana", _reg_ana)         # ~11 Portuguese airports (group)
S.register("zrh", zurich.scrape)
S.register("vie", vienna.scrape)
S.register("bud", budapest.scrape)
S.register("dub", dublin.scrape)
S.register("prg", prague.scrape)
S.register("mag", _reg_mag)         # MAG group: MAN/STN/EMA
# North America
S.register("usfids", _reg_usfids)   # group: DFW/CLT/MCO/LAS (tail reg)
S.register("panynj", _reg_panynj)   # group: JFK/EWR/LGA
S.register("fruition", _reg_fruition)  # DEN (tail+type)
S.register("sfo", sfo.scrape)
S.register("bos", bos.scrape)
S.register("iad", iad.scrape)       # MWAA (tail+type; also DCA)
S.register("phx", phx.scrape)
S.register("yvr", yvr.scrape)
S.register("yyc", yyc.scrape)
# Middle East + South Asia
S.register("dxb", dubai.scrape)
S.register("doh", doha.scrape)
S.register("blr", bengaluru.scrape)  # tail reg
S.register("kwi", kuwait.scrape)
S.register("hyd", hyderabad.scrape)
# East/SE Asia + Oceania
S.register("sin", changi.scrape)     # Changi (aircraft type)
S.register("hkg", hongkong.scrape)
S.register("syd", sydney.scrape)
S.register("akl", auckland.scrape)
S.register("nrt", narita.scrape)
# Latin America + Africa
S.register("acsa", _reg_acsa)        # all South African airports (group)
S.register("gru", brazil.scrape)     # Sao Paulo/Guarulhos
S.register("aa2000", _reg_aa2000)    # ~18-56 Argentine airports (group)
S.register("scl", santiago.scrape)
S.register("bog", bogota.scrape)


# ---- IATA -> key map (caller convenience; group lists expanded) ----
IATA_TO_KEY = {
    "ZRH": "zrh", "VIE": "vie", "BUD": "bud", "DUB": "dub", "PRG": "prg",
    "SFO": "sfo", "BOS": "bos", "IAD": "iad", "PHX": "phx", "YVR": "yvr", "YYC": "yyc",
    "DXB": "dxb", "DOH": "doh", "BLR": "blr", "KWI": "kwi", "HYD": "hyd",
    "SIN": "sin", "HKG": "hkg", "SYD": "syd", "AKL": "akl", "NRT": "nrt",
    "GRU": "gru", "SCL": "scl", "BOG": "bog",
}

def _expand(module, key, attr="DEFAULT"):
    codes = getattr(module, attr, None)
    if codes:
        for _c in codes:
            IATA_TO_KEY[_c] = key

_expand(aena, "aena"); _expand(portugal, "ana"); _expand(mag, "mag")
_expand(usfids, "usfids"); _expand(panynj, "panynj"); _expand(fruition, "fruition")
_expand(aa2000, "aa2000")
# ACSA exposes IATAS rather than DEFAULT
_expand(acsa, "acsa", attr="IATAS")
