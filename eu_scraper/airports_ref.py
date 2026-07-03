"""
airports_ref — origin/destination resolver for the EU headless scraper.

WHY THIS EXISTS
    Some airport boards give the "other" airport only as a CITY / AIRPORT NAME
    ("London Heathrow", "Frankfurt", "Paris, Orly") — no IATA code. The warehouse
    keys routes by IATA, so an arrival with only a name can't be tied to where the
    flight came FROM (breaks route-completion + inbound "dein Flieger kommt aus X").

    This module resolves a name (or an ICAO code) to an IATA code. It is backed by
    the repo's real airports dataset (`airports_compact.json`, ~7k airports with
    iata/icao/name/city) so we prefer a genuine dataset lookup over guesswork, plus:
      1. an explicit-token extractor (many names embed the code: "London LHR",
         "Reykjavik KEF", "Tenerife TFS", "Las Palmas (LPA)") — the most reliable,
      2. a compact curated map for common European hubs + multi-airport-city
         disambiguation (London/Paris/Milan/… where a bare city is ambiguous, and
         island/hub names the dataset mislabels),
      3. a normalized dataset fallback (unique airport-name / unique city match).

    FAIL-SAFE: if a name can't be resolved with confidence, resolve() returns None —
    the caller then leaves dest_iata empty rather than writing a wrong code.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata

# Dataset candidates: repo-root copy (running inside the main backend repo /
# main-backend Docker image, which COPYs the whole repo) AND the package-local
# copy (the eu_scraper Cloud-Run image builds with context=eu_scraper/ only —
# `--source ./eu_scraper` — so the repo-root file NEVER ships there; without
# the local copy the resolver silently degraded to curated-map-only in prod).
_DATA_CANDIDATES = (
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "airports_compact.json"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "airports_compact.json"),
)
_DATA_PATH = next((p for p in _DATA_CANDIDATES if os.path.exists(p)),
                  _DATA_CANDIDATES[0])

# Filler tokens dropped before matching an airport/city name.
_FILLER = {
    "airport", "international", "intl", "apt", "regional", "airfield", "field",
    "de", "the", "air", "base",
}

# ─────────────────────────────────────────────────────────────────────────────
#  Curated map — common EU origins + multi-airport-city disambiguation.
#  Keys are normalized (see _norm). These win over the raw dataset city index so
#  a bare "London" → LHR (not LGW/LTN/…), "Paris" → CDG, "Milan" → MXP, and island
#  names the dataset labels oddly (Madeira→FNC, Tenerife→TFS) resolve correctly.
# ─────────────────────────────────────────────────────────────────────────────
_CURATED = {
    # multi-airport cities — bare city defaults to the primary hub
    "london": "LHR", "london heathrow": "LHR", "london gatwick": "LGW",
    "london luton": "LTN", "london stansted": "STN", "london southend": "SEN",
    "london city": "LCY",
    "paris": "CDG", "paris ch gaulle": "CDG", "paris charles gaulle": "CDG",
    "paris orly": "ORY",
    "milan": "MXP", "milan malpensa": "MXP", "milan bergamo": "BGY",
    "milan linate": "LIN", "bergamo": "BGY",
    "rome": "FCO", "rome fiumicino": "FCO", "rome ciampino": "CIA",
    "moscow": "SVO", "istanbul": "IST", "istanbul sabiha": "SAW",
    "stockholm": "ARN", "stockholm arlanda": "ARN",
    "oslo": "OSL", "berlin": "BER",
    "brussels": "BRU", "brussels charleroi": "CRL", "charleroi": "CRL",
    "belfast": "BFS",
    # comma-suffixed specific airports seen on ANA/BUD boards
    "helsinki vantaa": "HEL", "vantaa": "HEL",
    "warsaw modlin": "WMI", "modlin": "WMI",
    "lyon st exupery": "LYS", "lyon saint exupery": "LYS",
    "venice marco polo": "VCE", "marco polo": "VCE",
    "doha hamad": "DOH", "hamad": "DOH",
    "seoul incheon": "ICN", "incheon": "ICN",
    "sao paulo guarulhos": "GRU", "guarulhos": "GRU",
    "rio de janeiro": "GIG",
    "cologne bonn": "CGN", "koln bonn": "CGN",
    # European hubs / common crew origins (unambiguous but pinned for speed/safety)
    "amsterdam": "AMS", "frankfurt": "FRA", "munich": "MUC", "vienna": "VIE",
    "zurich": "ZRH", "geneva": "GVA", "basel": "BSL", "mulhouse": "MLH",
    "madrid": "MAD", "barcelona": "BCN", "valencia": "VLC", "sevilla": "SVQ",
    "seville": "SVQ", "alicante": "ALC", "ibiza": "IBZ", "menorca": "MAH",
    "palma de mallorca": "PMI", "palma mallorca": "PMI", "palma": "PMI",
    "lisbon": "LIS", "lisboa": "LIS", "porto": "OPO", "faro": "FAO",
    "dublin": "DUB", "cork": "ORK", "shannon": "SNN",
    "edinburgh": "EDI", "manchester": "MAN", "bristol": "BRS", "leeds": "LBA",
    "liverpool": "LPL", "newcastle": "NCL", "east midlands": "EMA",
    "bournemouth": "BOH", "exeter": "EXT",
    "nice": "NCE", "marseille": "MRS", "bordeaux": "BOD", "nantes": "NTE",
    "la rochelle": "LRH", "toulouse": "TLS", "lyon": "LYS",
    "naples": "NAP", "palermo": "PMO", "florence": "FLR", "cagliari": "CAG",
    "venice": "VCE", "bologna": "BLQ", "turin": "TRN",
    "athens": "ATH", "rhodes": "RHO", "izmir": "ADB", "ankara": "ESB",
    "sofia": "SOF", "belgrade": "BEG", "katowice": "KTW", "chisinau": "KIV",
    "luxembourg": "LUX", "nuremberg": "NUE", "hamburg": "HAM",
    "dusseldorf": "DUS", "stuttgart": "STR", "hannover": "HAJ",
    "eindhoven": "EIN", "helsinki": "HEL", "reykjavik": "KEF",
    "larnaca": "LCA", "tel aviv": "TLV", "dubai": "DXB", "doha": "DOH",
    # Morocco / MENA / Africa crew routes
    "marrakech": "RAK", "marrakesh": "RAK", "tanger": "TNG", "tangier": "TNG",
    "oujda": "OUD", "monastir": "MIR", "hurghada": "HRG", "luanda": "LAD",
    # ─── German exonyms + German-board spellings (NRN/FDH/FKB boards label the
    #     other side in GERMAN: "Belgrad", "Marrakesch", "Palma d.M.", …) ───
    "belgrad": "BEG", "mailand": "MXP", "rom": "FCO", "wien": "VIE",
    "kopenhagen": "CPH", "warschau": "WAW", "lissabon": "LIS", "athen": "ATH",
    "moskau": "SVO", "brussel": "BRU", "venedig": "VCE", "neapel": "NAP",
    "nizza": "NCE", "genf": "GVA", "kattowitz": "KTW", "krakau": "KRK",
    "danzig": "GDN", "breslau": "WRO", "bukarest": "OTP", "prag": "PRG",
    "teneriffa": "TFS", "rhodos": "RHO", "korfu": "CFU", "marrakesch": "RAK",
    "palma d m": "PMI", "palma dm": "PMI",
    # cities the compact dataset misses / labels ambiguously (seen live on
    # NRN/FKB boards 2026-07-03)
    "girona": "GRO", "barcelona girona": "GRO", "castellon": "CDT",
    "fes": "FEZ", "fez": "FEZ", "kos": "KGS", "nador": "NDR",
    "pristina": "PRN", "tirana": "TIA", "trapani": "TPS", "zagreb": "ZAG",
    # islands / Portugal domestic that the dataset labels oddly or duplicates
    "madeira": "FNC", "funchal": "FNC", "porto santo": "PXO",
    "tenerife": "TFS", "tenerife sur": "TFS", "tenerife norte": "TFN",
    "ponta delgada": "PDL", "terceira": "TER", "horta": "HOR",
    "santa maria": "SMA", "flores": "FLW", "pico": "PIX", "corvo": "CVU",
    "graciosa": "GRW", "sao jorge": "SJZ",
    "asturias": "OVD",
    # Cape Verde
    "sal": "SID", "praia": "RAI", "sao vicente": "VXE", "boa vista": "BVC",
    # North America
    "toronto": "YYZ", "halifax": "YHZ",
    # ─── Latin America (multi-airport-city disambiguation + names the dataset
    #     mislabels, e.g. "Vitória"→VIT/Spain, "Buenos Aires"→ambiguous) ───
    "buenos aires": "EZE", "ezeiza": "EZE", "aeroparque": "AEP",
    "buenos aires ezeiza": "EZE", "buenos aires aeroparque": "AEP",
    "sao paulo": "GRU", "sao paulo guarulhos": "GRU", "guarulhos": "GRU",
    "sao paulo congonhas": "CGH", "congonhas": "CGH", "campinas": "VCP",
    "viracopos": "VCP", "rio de janeiro": "GIG", "galeao": "GIG",
    "santos dumont": "SDU", "rio de janeiro galeao": "GIG",
    "brasilia": "BSB", "belo horizonte": "CNF", "confins": "CNF",
    "salvador": "SSA", "recife": "REC", "fortaleza": "FOR",
    "porto alegre": "POA", "curitiba": "CWB", "florianopolis": "FLN",
    "manaus": "MAO", "belem": "BEL", "natal": "NAT", "goiania": "GYN",
    "cuiaba": "CGB", "vitoria": "VIX", "maceio": "MCZ", "joao pessoa": "JPA",
    "aracaju": "AJU", "teresina": "THE", "sao luis": "SLZ",
    "campo grande": "CGR", "foz do iguacu": "IGU", "navegantes": "NVT",
    "londrina": "LDB", "uberlandia": "UDI", "porto velho": "PVH",
    "rio branco": "RBR", "boa vista": "BVB", "macapa": "MCP", "palmas": "PMW",
    "ribeirao preto": "RAO", "porto seguro": "BPS", "ilheus": "IOS",
    "juazeiro do norte": "JDO", "montes claros": "MOC", "chapeco": "XAP",
    "joinville": "JOI", "santarem": "STM", "imperatriz": "IMP",
    "lima": "LIM", "santiago": "SCL", "santiago de chile": "SCL",
    "bogota": "BOG", "quito": "UIO", "guayaquil": "GYE", "montevideo": "MVD",
    "panama": "PTY", "panama city": "PTY", "ciudad de panama": "PTY",
    "tocumen": "PTY", "mexico city": "MEX", "ciudad de mexico": "MEX",
    "mexico": "MEX", "cancun": "CUN", "asuncion": "ASU", "la paz": "LPB",
    "santa cruz": "VVI", "cordoba": "COR", "mendoza": "MDZ", "rosario": "ROS",
    "bariloche": "BRC", "ushuaia": "USH", "salta": "SLA", "iguazu": "IGR",
    "cartagena": "CTG", "medellin": "MDE", "cali": "CLO", "san jose": "SJO",
    "san salvador": "SAL", "guatemala": "GUA", "san pedro sula": "SAP",
    "punta cana": "PUJ", "santo domingo": "SDQ", "havana": "HAV",
    "caracas": "CCS", "guadalajara": "GDL", "monterrey": "MTY",
    "tijuana": "TIJ", "los cabos": "SJD", "puerto vallarta": "PVR",
    # Chile domestic (SCL board) + regional
    "la serena": "LSC", "puerto montt": "PMC", "temuco": "ZCO",
    "valdivia": "ZAL", "osorno": "ZOS", "punta arenas": "PUQ",
    "balmaceda": "BBA", "copiapo": "CPO", "castro": "WCA", "arica": "ARI",
    "concepcion": "CCP", "antofagasta": "ANF", "calama": "CJC",
    "iquique": "IQQ", "puerto natales": "PNT", "el salvador chile": "ESR",
    # Peru / Ecuador / Colombia regional (LatAm boards)
    "cusco": "CUZ", "cuzco": "CUZ", "arequipa": "AQP", "iquitos": "IQT",
    "piura": "PIU", "tarapoto": "TPP", "trujillo": "TRU", "chiclayo": "CIX",
    "pucallpa": "PCL", "cajamarca": "CJA", "juliaca": "JUL",
    "barranquilla": "BAQ", "bucaramanga": "BGA", "pereira": "PEI",
    "santa marta": "SMR", "cucuta": "CUC", "monteria": "MTR",
    "armenia": "AXM", "neiva": "NVA", "pasto": "PSO", "leticia": "LET",
    "manta": "MEC", "cuenca": "CUE", "galapagos": "GPS", "baltra": "GPS",
    "coca": "OCC", "loja": "LOH",
    # ─── Africa (hubs + multi-airport / dataset-ambiguous) ───
    "johannesburg": "JNB", "cape town": "CPT", "durban": "DUR",
    "nairobi": "NBO", "cairo": "CAI", "addis ababa": "ADD", "lagos": "LOS",
    "abuja": "ABV", "accra": "ACC", "casablanca": "CMN", "tunis": "TUN",
    "algiers": "ALG", "alger": "ALG", "dar es salaam": "DAR", "dakar": "DSS",
    "abidjan": "ABJ", "kinshasa": "FIH", "windhoek": "WDH", "maputo": "MPM",
    "lusaka": "LUN", "harare": "HRE", "gaborone": "GBE", "kigali": "KGL",
    "entebbe": "EBB", "khartoum": "KRT", "kampala": "EBB", "lilongwe": "LLW",
    "maseru": "MSU", "mauritius": "MRU", "antananarivo": "TNR",
    "port louis": "MRU", "libreville": "LBV", "douala": "DLA",
    "yaounde": "NSI", "brazzaville": "BZV", "bamako": "BKO",
    "ouagadougou": "OUA", "conakry": "CKY", "cotonou": "COO", "lome": "LFW",
    "nouakchott": "NKC", "niamey": "NIM", "ndjamena": "NDJ", "bangui": "BGF",
    "kano": "KAN", "port harcourt": "PHC", "enugu": "ENU", "luanda": "LAD",
    "sao paulo brazil": "GRU",
}


# ─────────────────────────────────────────────────────────────────────────────
#  Dataset indexes (lazy, cached)
# ─────────────────────────────────────────────────────────────────────────────
_IATA: set | None = None
_ICAO2IATA: dict | None = None
_NAME2IATA: dict | None = None       # normalized airport name → iata (unique only)
_CITY2IATA: dict | None = None       # normalized city → iata (unique only)
_CUR: dict | None = None             # curated map with _norm'd keys (built lazily)


def _norm(s: str) -> str:
    """Lowercase, strip accents/punctuation, drop filler words, collapse spaces."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    toks = [t for t in s.split() if t and t not in _FILLER]
    return " ".join(toks)


def _load():
    global _IATA, _ICAO2IATA, _NAME2IATA, _CITY2IATA, _CUR
    if _IATA is not None:
        return
    # Normalize curated keys through the SAME _norm the lookups use, so filler
    # words (e.g. "de" in "Rio de Janeiro") can never cause a silent key miss.
    _CUR = {_norm(k): v for k, v in _CURATED.items()}
    _IATA, _ICAO2IATA = set(), {}
    name_hits: dict = {}
    city_hits: dict = {}
    try:
        with open(_DATA_PATH) as f:
            data = json.load(f)
        fields = data.get("fields") or []
        rows = data.get("rows") or []
        i_iata = fields.index("iata")
        i_icao = fields.index("icao")
        i_name = fields.index("name")
        i_city = fields.index("city")
        for r in rows:
            iata = (r[i_iata] or "").strip().upper()
            if len(iata) != 3 or not iata.isalpha():
                continue
            _IATA.add(iata)
            icao = (r[i_icao] or "").strip().upper()
            if len(icao) == 4 and icao.isalpha():
                _ICAO2IATA.setdefault(icao, iata)
            nn = _norm(r[i_name])
            if nn:
                name_hits.setdefault(nn, set()).add(iata)
            cn = _norm(r[i_city])
            if cn:
                city_hits.setdefault(cn, set()).add(iata)
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError, IndexError):
        pass
    # Keep only UNAMBIGUOUS name/city → iata mappings (a city with 2+ airports is
    # left to the curated map / explicit token, never guessed).
    _NAME2IATA = {k: next(iter(v)) for k, v in name_hits.items() if len(v) == 1}
    _CITY2IATA = {k: next(iter(v)) for k, v in city_hits.items() if len(v) == 1}


def is_iata(code: str) -> bool:
    _load()
    return bool(code) and code.strip().upper() in _IATA


def icao_to_iata(code: str) -> str | None:
    """4-letter ICAO → IATA via the dataset. None if unknown."""
    _load()
    if not code:
        return None
    return _ICAO2IATA.get(code.strip().upper())


_TOKEN_RE = re.compile(r"\(([A-Za-z]{3})\)|(?:^|[\s/,])([A-Z]{3})(?=$|[\s/,])")


def _explicit_token(raw: str) -> str | None:
    """Pull an embedded IATA code from a name: 'London LHR', 'Reykjavik KEF',
    'Tenerife TFS', 'Las Palmas (LPA)' → the code, if it's a real IATA."""
    if not raw:
        return None
    for m in _TOKEN_RE.finditer(raw):
        code = (m.group(1) or m.group(2) or "").upper()
        if is_iata(code):
            return code
    return None


def resolve(name: str, code: str | None = None) -> str | None:
    """Resolve an origin/destination to an IATA code. Fail-safe → None.

    `code` is an optional code the feed already supplied (may be IATA or ICAO);
    it is trusted first. `name` is the city/airport name string.
    Order: given-code → embedded-token → curated map → dataset name → dataset city.
    """
    _load()
    # 1. a code the feed handed us (IATA as-is, or ICAO → IATA)
    if code:
        c = code.strip().upper()
        if is_iata(c):
            return c
        got = icao_to_iata(c)
        if got:
            return got
    if not name:
        return None
    # 2. explicit IATA token embedded in the name (most reliable)
    tok = _explicit_token(name)
    if tok:
        return tok
    # 3. curated map (handles multi-airport-city disambiguation + odd island names)
    nn = _norm(name)
    if not nn:
        return None
    if nn in _CUR:
        return _CUR[nn]
    # 3b. drop a trailing/leading segment for comma forms not caught above
    #     ("brussels charleroi" already covered; try first token for "x, y")
    parts = [p for p in re.split(r"[,/]", name) if p.strip()]
    if len(parts) > 1:
        for seg in (parts[-1], parts[0]):
            sn = _norm(seg)
            if sn in _CUR:
                return _CUR[sn]
    # 4. dataset: exact unique airport-name match
    if nn in _NAME2IATA:
        return _NAME2IATA[nn]
    # 5. dataset: exact unique city match (bare-city first token too)
    if nn in _CITY2IATA:
        return _CITY2IATA[nn]
    if len(parts) > 1:
        for seg in (parts[0], parts[-1]):
            sn = _norm(seg)
            if sn in _CUR:
                return _CUR[sn]
            if sn in _CITY2IATA:
                return _CITY2IATA[sn]
            if sn in _NAME2IATA:
                return _NAME2IATA[sn]
    return None
