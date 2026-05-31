"""cas_table_parser — DETERMINISTISCHER Vor-Parser für LH CAS Dienstpläne.

Zweck (Teil 1 von "beides kombiniert"):
    Die CAS-PDF ist eine strukturierte Tabelle ("Alle zeiten in UTC"). Die HARTEN
    Fakten — Datum, Flugnummer, Routing-IATAs, Abflug-/Ankunftszeit — lassen sich
    ohne LLM exakt aus den Wort-Koordinaten lesen. Das liefert eine reproduzierbare
    Grundwahrheit, gegen die der Sonnet-Reader gekreuzt/korrigiert werden kann.

LAYOUT-ROBUSTHEIT (wichtig):
    Dieser Parser ist auf das LH-CAS-PUB-Layout ausgelegt. Er ist DEFENSIV gebaut:
      - layout_ok()-Selbstcheck erkennt, ob die PDF dem erwarteten Layout ähnelt.
        Wenn nicht → confidence='none', days=[] → der LLM-Reader übernimmt allein.
      - Pro Feld: was nicht SICHER erkennbar ist, bleibt None statt geraten.
    Ändert LH das Format grundlegend, liefert der Parser also nichts Falsches —
    er tritt einfach zurück und der (layout-tolerante) Sonnet-Reader trägt.

Kein Hardcoding von Daten/Beträgen — nur generische Tabellen-Mechanik.
"""
from __future__ import annotations

import io
import re
from collections import defaultdict
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

_WD = {'Mo', 'Di', 'Mi', 'Do', 'Fr', 'Sa', 'So'}
_MONTHS = {
    'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4, 'MAY': 5, 'JUN': 6,
    'JUL': 7, 'AUG': 8, 'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12,
}
_MONTH_ABBR = set(_MONTHS.keys())

_FLIGHT_RE = re.compile(r'\bLH(\d{2,4})\b', re.I)
_IATA_RE = re.compile(r'\b([A-Z]{3})\b')
# Ab-An-Zeitpaar als EIN Token: "11:55-20:50" (arr kann fehlen: "22:50-")
_TIMEPAIR_RE = re.compile(r'\b([0-2]\d:[0-5]\d)-([0-2]\d:[0-5]\d)?')
# Zeitpaar als GANZER Token (fullmatch): "11:55-20:50" ODER dep-only "13:05-".
# Kein trailing \b — sonst scheitert "13:05-" am Zeilenende.
_TIMEPAIR_TOK = re.compile(r'([0-2]\d:[0-5]\d)-([0-2]\d:[0-5]\d)?$')
_DAY_RE = re.compile(r'^(Mo|Di|Mi|Do|Fr|Sa|So)\s+(\d{1,2})\b')
_BRIEF_RE = re.compile(r'Briefingzeit.*?(\d{2})/(\d{2})/(\d{2})\s+(\d{2}:\d{2})')
# Header: "... FRA MAR 2025 25 FEB 2025 ..." → Plan-Monat = erstes Monat+Jahr-Paar
_HEADER_MONTH_RE = re.compile(r'\b(' + '|'.join(_MONTHS) + r')\s+(20\d{2})\b')

# Tokens, die NIE ein Reise-Airport sind (Header/Spalten-Artefakte)
_STOP_IATA = {
    'EUR', 'EURO', 'LAW', 'FDZ', 'MTV', 'FZM', 'MAX', 'UTC', 'GMT', 'LIN', 'YA',
    'FBA', 'PUB', 'NTF', 'OFF', 'FRS', 'AMV', 'AMD', 'LMN', 'SBY', 'RES',
} | _MONTH_ABBR


def _as_opener(pdf_bytes_or_path):
    import pdfplumber
    if isinstance(pdf_bytes_or_path, (bytes, bytearray)):
        return pdfplumber.open(io.BytesIO(pdf_bytes_or_path))
    return pdfplumber.open(pdf_bytes_or_path)


def _lines_from_pdf(pdf_bytes_or_path) -> List[Tuple[int, str]]:
    """(seiten_idx, zeilen_text) rekonstruiert aus Wort-Koordinaten."""
    rows: List[Tuple[int, str]] = []
    with _as_opener(pdf_bytes_or_path) as pdf:
        for pi, page in enumerate(pdf.pages):
            by_y: Dict[int, List[Tuple[float, str]]] = defaultdict(list)
            for w in page.extract_words():
                by_y[round(w['top'])].append((w['x0'], w['text']))
            for y in sorted(by_y):
                cells = sorted(by_y[y], key=lambda c: c[0])
                rows.append((pi, ' '.join(t for _, t in cells)))
    return rows


def layout_ok(rows: List[Tuple[int, str]]) -> bool:
    """Selbstcheck: sieht die PDF nach LH-CAS-PUB-Layout aus?
    Erwartet: ein Tag-mit-Wochentag-Muster UND eine Briefingzeit/UTC-Markierung.
    """
    has_day = any(_DAY_RE.match(line) for _, line in rows)
    has_brief = any('Briefingzeit' in line for _, line in rows)
    has_utc = any('UTC' in line or 'Alle zeiten' in line for _, line in rows)
    has_cas = any('Crew Assignment System' in line or 'Einsatzplan' in line for _, line in rows)
    # mind. Tag-Muster + (Briefing ODER UTC ODER CAS-Header)
    return has_day and (has_brief or has_utc or has_cas)


def _detect_year_month(rows: List[Tuple[int, str]]) -> Tuple[Optional[int], Optional[int]]:
    """Plan-Monat+Jahr aus dem Header ('... FRA MAR 2025 ...')."""
    for _, line in rows[:6]:
        m = _HEADER_MONTH_RE.search(line)
        if m:
            return int(m.group(2)), _MONTHS[m.group(1)]
    # Fallback: erste Briefingzeit TT/MM/JJ
    for _, line in rows:
        m = _BRIEF_RE.search(line)
        if m:
            return 2000 + int(m.group(3)), int(m.group(2))
    return None, None


def _detect_homebase(rows: List[Tuple[int, str]]) -> Optional[str]:
    for _, line in rows:
        m = re.search(r'Homebase\s+([A-Z]{3})\b', line)
        if m:
            return m.group(1)
        m = re.search(r'\bLT-([A-Z]{3})\b', line)
        if m:
            return m.group(1)
    return None


def parse_cas_pdf(pdf_bytes_or_path) -> Dict[str, Any]:
    """Haupteinstieg. Siehe Modul-Docstring. Defensiv: bei fremdem Layout
    confidence='none' + days=[]."""
    rows = _lines_from_pdf(pdf_bytes_or_path)

    if not layout_ok(rows):
        return {
            'year': None, 'homebase': None, 'days': [],
            'confidence': 'none',
            'warnings': ['Layout nicht als LH-CAS-PUB erkannt — deterministischer '
                         'Parser tritt zurück, LLM-Reader übernimmt.'],
        }

    year, start_month = _detect_year_month(rows)
    homebase = (_detect_homebase(rows) or 'FRA')
    warnings: List[str] = []
    if not year or not start_month:
        warnings.append('Jahr/Monat nicht erkennbar — Datums-Zuordnung unsicher')

    days: List[Dict[str, Any]] = []
    # Tage werden dem PLAN-Monat (start_month) zugeordnet. Ein Dienstplan listet
    # i. d. R. nur seinen Monat, kann aber am Rand 1-2 Vormonats-Tage (hohe dom
    # am Anfang, z. B. "Di 31") und/oder Folgemonats-Tage (niedrige dom am Ende)
    # zeigen. Wir tracken die Position relativ zum ersten ECHTEN Plan-Monatstag.
    prev_dom: Optional[int] = None
    seen_plan_start = False  # True ab dem ersten Tag, der zum Plan-Monat gehört
    cur_year, cur_month = year, start_month

    # Header-Zeilen (erste 6) NIE als Tag/Flug behandeln
    body = [(pi, ln) for idx, (pi, ln) in enumerate(rows) if idx >= 6]

    for pi, line in body:
        md = _DAY_RE.match(line)
        if not md:
            # Flug-Folgezeile (kein Wochentag) → zum letzten Tag mergen
            if days and (_FLIGHT_RE.search(line) or _TIMEPAIR_RE.search(line)):
                _merge_flight_line(days[-1], line, homebase)
            continue

        dom = int(md.group(2))
        eff_year, eff_month = cur_year, cur_month

        if cur_month:
            if not seen_plan_start:
                # Vormonats-Ausläufer am Anfang: hohe dom (>~20) VOR dem ersten
                # niedrigen Tag → gehört zum Vormonat.
                if prev_dom is None and dom >= 20:
                    eff_month = cur_month - 1
                    if eff_month < 1:
                        eff_month = 12
                        eff_year = (cur_year or 0) - 1
                else:
                    seen_plan_start = True
            else:
                # Innerhalb des Plans: Rückfall der dom (z. B. 31 → 1) = Folgemonat
                if prev_dom is not None and dom < prev_dom - 5:
                    cur_month += 1
                    if cur_month > 12:
                        cur_month = 1
                        cur_year = (cur_year or 0) + 1
                    eff_year, eff_month = cur_year, cur_month

        prev_dom = dom

        datum = None
        if eff_year and eff_month:
            try:
                datum = date(eff_year, eff_month, dom).isoformat()
            except ValueError:
                datum = None

        day = {
            'datum': datum, 'weekday': md.group(1), 'day_of_month': dom,
            'raw': line[:160], 'flight_numbers': [], 'routing': [],
            'dep_time': None, 'arr_time': None, 'is_flight': False,
        }
        _merge_flight_line(day, line, homebase)
        days.append(day)

    # Nachbearbeitung: Ankunft von Nachtflügen aus der FOLGEZEILE ziehen.
    # LH-Layout schreibt bei Flügen über Mitternacht die Ankunft (IATA + Zeit) in
    # die Zeile des Folgetags, z. B. Flugtag "IAD 22:50-" → Folgetag "... FRA 07:35".
    _backfill_overnight_arrivals(days)

    has_flight = any(d['is_flight'] for d in days)
    confidence = 'high' if (year and start_month and homebase and has_flight) else 'partial'
    return {'year': year, 'homebase': homebase, 'days': days,
            'confidence': confidence, 'warnings': warnings}


# Folgezeilen-Ankunft: erstes "<IATA> <HH:MM>"-Paar (Ziel + Ankunftszeit).
_ARR_IN_NEXT_RE = re.compile(r'\b([A-Z]{3})\s+([0-2]\d:[0-5]\d)\b')


def _backfill_overnight_arrivals(days: List[Dict[str, Any]]) -> None:
    """Setzt arr_time (+ Ziel-IATA) für Flugtage, die nur dep haben, aus dem
    Folgetag. Defensiv: nur wenn der Folgetag KEIN eigener Flug ist (echte
    Tour-Fortsetzung) und ein eindeutiges <IATA> <HH:MM> trägt."""
    for i, d in enumerate(days):
        if not d['is_flight'] or not d['dep_time'] or d['arr_time']:
            continue
        if i + 1 >= len(days):
            continue
        nxt = days[i + 1]
        if nxt['flight_numbers']:
            continue  # Folgetag ist eigener Flug → nicht antasten
        m = _ARR_IN_NEXT_RE.search(nxt.get('raw') or '')
        if not m:
            continue
        arr_iata, arr_time = m.group(1), m.group(2)
        if arr_iata in _STOP_IATA:
            continue
        d['arr_time'] = arr_time
        if arr_iata not in d['routing']:
            d['routing'].append(arr_iata)


def _merge_flight_line(day: Dict[str, Any], line: str, homebase: str) -> None:
    """Extrahiert Flugnummern, Routing (Airport vor/nach Zeitpaar) und Ab/An-Zeiten.

    Das LH-Muster pro Flug ist: '<FROM> HH:MM-HH:MM <TO>'. Wir verankern die
    Zeiten am Zeitpaar-Token und lesen die IATAs links/rechts davon — NICHT die
    vielen anderen Zeit-Spalten (Block/FDZ/MTV).
    """
    # 1) Flugnummern
    for m in _FLIGHT_RE.finditer(line):
        fn = 'LH' + m.group(1)
        if fn not in day['flight_numbers']:
            day['flight_numbers'].append(fn)
            day['is_flight'] = True

    tokens = line.split()

    # 2) Zeitpaar(e) finden und Airports drumherum lesen
    for i, tok in enumerate(tokens):
        mp = _TIMEPAIR_TOK.fullmatch(tok)
        if not mp:
            continue
        dep_t = mp.group(1)
        arr_t = mp.group(2)  # kann None sein ("22:50-")
        # Airport links vom Zeitpaar
        left = _iata_before(tokens, i)
        # Airport rechts vom Zeitpaar
        right = _iata_after(tokens, i)
        for ap in (left, right):
            if ap and ap not in day['routing']:
                day['routing'].append(ap)
        if day['dep_time'] is None and dep_t:
            day['dep_time'] = dep_t
        if arr_t:
            day['arr_time'] = arr_t

    # 3) Falls KEIN Zeitpaar (z.B. Layover-Tag 'X HND'): trotzdem Routing-IATA lesen
    if not day['routing']:
        for tok in tokens:
            if _is_clean_iata(tok, homebase):
                if tok not in day['routing']:
                    day['routing'].append(tok)


def _iata_before(tokens: List[str], i: int) -> Optional[str]:
    for j in range(i - 1, max(-1, i - 3), -1):
        if 0 <= j < len(tokens) and _is_clean_iata(tokens[j], None):
            return tokens[j]
    return None


def _iata_after(tokens: List[str], i: int) -> Optional[str]:
    for j in range(i + 1, min(len(tokens), i + 3)):
        if _is_clean_iata(tokens[j], None):
            return tokens[j]
    return None


def _is_clean_iata(tok: str, homebase: Optional[str]) -> bool:
    if not tok or len(tok) != 3 or not tok.isalpha() or not tok.isupper():
        return False
    if tok in _STOP_IATA:
        return False
    return True
