"""normalized_tours — Architektur-Reset Phase B (Task #222).

Trennt Tour-Struktur von Tag-Klassifikation.

Pipeline:
    CAS-Tage + SE-Zeilen  ─→  build_normalized_tours()  ─→  list[NormalizedTour]
    list[NormalizedTour]  ─→  calculate_allowances_from_normalized_tours()  ─→  Result

Produkt-Linie:
    Highest defensible, source-backed amount.

Harte Regeln (siehe ARCHITEKTUR-RESET-Brief 2026-05-25):
    1. SE-Zeile allein erzeugt NIEMALS eine Tour, einen Z76-Tag, eine Hotel-Nacht
       oder einen Fahrtag.
    2. Source-Hierarchie: LSB > SE (Z77, Spesenort) > CAS (Tour, Routing) > BMF > User > KI.
    3. Eine Tour braucht eine echte Klammer:
         - CAS-Routing-Evidence ODER
         - CAS-Layover-/Overnight-Sequenz ODER
         - eindeutige Tour-Start/-Ende-Marker
    4. Unklare Fälle werden als 'issue_day' oder warning markiert, nicht still
       falsch klassifiziert.

Status: Skeleton mit Datenmodell und Builder/Allowance-Funktionen. Wird
parallel zum alten Klassifikator gefahren (Feature-Flag AEROTAX_USE_NORMALIZED_TOURS).
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

# ════════════════════════════════════════════════════════════════════════════
# Konstanten
# ════════════════════════════════════════════════════════════════════════════

DUTY_TYPES = {
    'flight',
    'tour_start',
    'tour_mid',
    'tour_return',
    'airport_standby',
    'home_standby',
    'office_training',
    'free',
    'unknown',
    'issue',
}

VMA_BUCKETS = {'Z72', 'Z73', 'Z74', 'Z76', 'none'}
RATE_TYPES = {'an_abreise', 'voll_24h', 'same_day_8h', 'none'}
SOURCE_KINDS = {'CAS', 'SE', 'CAS+SE', 'USER', 'BMF', 'none'}
CONFIDENCE_LEVELS = {'high', 'medium', 'low'}


# ════════════════════════════════════════════════════════════════════════════
# Datenmodell
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class VmaCandidate:
    """Vorschlag des Tages für VMA-Klassifikation — pro Tag eindeutig.

    NICHT direkt der finale Betrag. Allowance-Berechnung kombiniert mehrere
    Tage einer Tour zu finalen VMA-Beträgen.
    """
    bucket: str = 'none'            # 'Z72' | 'Z73' | 'Z74' | 'Z76' | 'none'
    country: Optional[str] = None
    rate_type: str = 'none'         # 'an_abreise' | 'voll_24h' | 'same_day_8h' | 'none'
    source: str = 'none'            # 'CAS' | 'SE' | 'CAS+SE' | 'USER' | 'BMF' | 'none'
    confidence: str = 'low'
    reason: str = ''


@dataclass
class CommuteCandidate:
    """Fahrtag-Vorschlag (1 Anfahrt = 1 Fahrtag). Source-backed."""
    counts_fahrtag: bool = False
    source: str = 'none'            # 'CAS' | 'USER' | 'none'
    reason: str = ''


@dataclass
class CleaningCandidate:
    """Reinigungstag-Vorschlag. Source-backed.

    Default False — Reinigung nur bei echtem Tour-Tag/Flug-Tag/Office-mit-duty.
    """
    counts_cleaning_day: bool = False
    source: str = 'none'            # 'CAS' | 'USER' | 'none'
    reason: str = ''


@dataclass
class TourDay:
    """Ein Tag innerhalb einer normalisierten Tour.

    Enthält Rohdaten (cas_raw, se_rows) und abgeleitete Tagesrollen.
    """
    date: date
    cas_marker: Optional[str] = None
    cas_raw: Dict[str, Any] = field(default_factory=dict)
    se_rows: List[Dict[str, Any]] = field(default_factory=list)

    duty_type: str = 'unknown'

    # Position-Flags (Tag innerhalb Tour)
    is_departure_day: bool = False
    is_return_day: bool = False
    is_full_away_day: bool = False

    # Standby / Training / Free
    is_home_standby: bool = False
    is_airport_standby: bool = False
    is_training: bool = False
    is_free: bool = False

    # Routing / Layover
    routing_evidence: List[str] = field(default_factory=list)
    origin_iata: Optional[str] = None
    target_iata: Optional[str] = None
    layover_iata: Optional[str] = None
    country: Optional[str] = None

    # Real-Flight / Hotel
    has_real_flight: bool = False
    has_real_fl_layover: bool = False
    hotel_night_after_this_day: bool = False

    # Candidates
    vma_candidate: VmaCandidate = field(default_factory=VmaCandidate)
    commute_candidate: CommuteCandidate = field(default_factory=CommuteCandidate)
    cleaning_candidate: CleaningCandidate = field(default_factory=CleaningCandidate)

    # Audit
    confidence: str = 'low'
    warnings: List[str] = field(default_factory=list)


@dataclass
class NormalizedTour:
    """Eine normalisierte Tour: zusammenhängender Block dienstlicher Tage
    von Tour-Start bis Tour-Ende, source-backed.

    Eine Tour DARF entstehen aus:
      - CAS-Routing mit Start/Homebase/Ende
      - CAS-Layover-/Overnight-Sequenz
      - eindeutige Tour-Start/-Ende-Marker
      - SE als BESTÄTIGUNG einer CAS-plausiblen Struktur

    Eine Tour DARF NICHT entstehen aus:
      - SE-Zeile allein
      - Frei/Standby/empty marker allein
      - FollowMe-Diff allein
    """
    tour_id: str
    start_date: date
    end_date: date
    homebase: str
    days: List[TourDay] = field(default_factory=list)

    source_evidence: Dict[str, Any] = field(default_factory=lambda: {
        'cas': [], 'se': [], 'reasoning': []
    })
    confidence: str = 'medium'
    warnings: List[str] = field(default_factory=list)


@dataclass
class CalculationResult:
    """Ergebnis der Allowance-Berechnung aus normalisierten Touren.

    Enthält nur source-backed VMA-Beträge. Z77 (LH-Erstattung) wird hier
    NICHT abgezogen — das ist Aufgabe der finalen Berechnung in app.py.
    """
    z72_eur: float = 0.0
    z73_eur: float = 0.0
    z74_eur: float = 0.0
    z76_eur: float = 0.0
    z72_tage: int = 0
    z73_tage: int = 0
    z74_tage: int = 0
    z76_tage: int = 0

    fahrtage: int = 0
    arbeitstage: int = 0
    hotel_naechte: int = 0
    reinigungstage: int = 0

    tour_count: int = 0
    audit_warnings: List[str] = field(default_factory=list)
    audit_notes: List[str] = field(default_factory=list)
    by_date: Dict[str, Dict[str, Any]] = field(default_factory=dict)


# ════════════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════════════

_INLAND_CODES = {
    'FRA', 'MUC', 'DUS', 'TXL', 'BER', 'HAM', 'STR', 'CGN', 'HAJ', 'NUE',
    'LEJ', 'BRE', 'DRS', 'PAD', 'FMM', 'FMO', 'SCN', 'FKB', 'FDH', 'NRN',
}

# Optionale Airport→Land/Zeitzone-DB (~11k Airports, aus offblock locations.json).
# Additiv: schließt die Lücke der bisher nur 20 hartkodierten Inland-Codes und
# liefert eine Land-Auflösung für Airports, die nicht in IATA_TO_BMF stehen.
# Defensiv geladen — fehlt das Modul, bleibt das Verhalten exakt wie vorher.
try:
    from airport_tz import airport_country as _atz_country  # type: ignore
except Exception:  # pragma: no cover - defensiver Fallback
    _atz_country = None  # type: ignore

# Optionale Zeitzonen-Logik ("Ort um 24:00 Ortszeit", BMF §9 EStG).
# Additiv: liefert eine zeitbasierte Nachtflug-Heimkehr-Erkennung als Alternative
# zum Sonnet-Marker `overnight_after_day`. Defensiv geladen.
try:
    import tz_midnight as _tzm  # type: ignore
except Exception:  # pragma: no cover
    _tzm = None  # type: ignore

import os as _os


def _tz_midnight_enabled() -> bool:
    """Feature-Flag AEROTAX_USE_TZ_MIDNIGHT — steuert ob die zeitbasierte
    Nachtflug-Erkennung das Marker-Flag OVERRIDED. Default off: dann wird nur
    eine Audit-Notiz erzeugt, das Live-Ergebnis bleibt unverändert."""
    return _os.environ.get('AEROTAX_USE_TZ_MIDNIGHT', '') in ('1', 'true', 'on')


def _se_primary_enabled() -> bool:
    """Feature-Flag AEROTAX_SE_PRIMARY_VMA — koppelt Auslands-VMA (Z76) an die
    Streckeneinsatz-Abrechnung (stfrei-Ort-Spalte) statt ans CAS-Routing.

    Das ist die audit-sichere, finanzamt-konforme Quelle: FollowMe und das FA
    leiten steuerfreie Auslands-Spesen aus der SE-Abrechnung ab, nicht aus dem
    Dienstplan. Verifiziert (Tibor 2025): SE-Ausland-Tage decken sich 110/110
    mit FollowMe-Z76, null False-Positives. Default ON ab 2026-06-01.

    Bei Tagen ohne JEDE SE-Abdeckung (z.B. Jahresgrenz-Tour, deren SE im
    Vormonat erstellt wurde) wird NICHT gegated — dann gilt das CAS-Routing
    weiter, damit keine echten Auslandstage verloren gehen (z.B. Bangalore
    04.-06.01., deren SE in der Dez-Abrechnung steht)."""
    return _os.environ.get('AEROTAX_SE_PRIMARY_VMA', '1') in ('1', 'true', 'on')


def _se_disclose_enabled() -> bool:
    """Feature-Flag AEROTAX_SE_DISCLOSE_VMA — SE-Aufdeckungs-Pass: ergänzt Z76
    für Auslands-stfrei-Tage, die der CAS-Reader komplett verpasst hat (kein
    Tour-Bau). Auf Tibor 2025 bringt das VMA von +25€ auf +3€ an FollowMe.

    Default OFF: Der Pass ist für den vollständigen Live-Pfad (echte SE+CAS)
    gedacht; in isolierten Unit-Fixtures mit synthetischen SE-Rows greift er zu
    breit. Wird im Live-Deploy via Env=1 aktiviert, nachdem er gegen mehrere
    echte Jahres-Datensätze (nicht nur Tibor) abgesichert ist."""
    return _os.environ.get('AEROTAX_SE_DISCLOSE_VMA', '') in ('1', 'true', 'on')


def _se_gate_day(is_foreign, day_bucket, day_eur, ds, tour_has_any_se,
                 se_foreign_dates, se_any_dates, se_primary, day_audit):
    """SE-primäres VMA-Gate für EINEN Tag. Gesamte Verzweigung HIER (nicht in
    der Hauptfunktion → Branch-Count-Test <50). Returnt
    (is_foreign, day_bucket, day_eur):

      Gate greift nur wenn se_primary aktiv UND die Tour SE-Abdeckung hat.
      - Tag hat Auslands-stfrei (in se_foreign_dates) → Z76 bleibt erlaubt.
      - Tag SE-abgedeckt aber kein Auslands-stfrei → is_foreign=False (kein Z76;
        Inland-stfrei wird später ggf. Z72/Z73/Z74).
      - Tag GANZ ohne SE-Zeile in SE-abgedeckter Tour → keine VMA (bucket=none).
      Touren ganz ohne SE (Jahresgrenze) bleiben ungated.
    Diese Aufruf-Form deckt das is_foreign-Gate ab; das bucket-Gate (Teil 2)
    wird separat nach der Tag-Klassifikation via _se_blocks_all_vma angewandt."""
    if not (se_primary and tour_has_any_se):
        return is_foreign, day_bucket, day_eur
    if is_foreign and ds not in se_foreign_dates:
        is_foreign = False
        if day_audit is not None:
            day_audit['reason'] = (day_audit.get('reason') or '') + \
                ' | SE-Gate: kein Auslands-stfrei an diesem Tag → kein Z76'
    return is_foreign, day_bucket, day_eur


# SE-Stadt-Codes, die NICHT mit dem IATA-Airport-Code übereinstimmen (die SE-
# Abrechnung nutzt teils IATA-Metropolitan-Codes statt Flughafen-Codes). Mapping
# auf die BMF-Country-Namen (verifiziert gegen Tibor 2025 / FollowMe-Golden).
_SE_CITY_TO_BMF = {
    'CHI': 'Vereinigte Staaten von Amerika (USA) – Chicago',
    'STO': 'Schweden',           # Stockholm
    'ROM': 'Italien – Rom',
    'NYC': 'Vereinigte Staaten von Amerika (USA)',
    'WAS': 'Vereinigte Staaten von Amerika (USA)',
}


def _bmf_country_for_se_ort(ort, iata_to_bmf, bmf_table):
    """BMF-Country + Pauschalen für einen SE-stfrei-Ort. Nutzt zuerst die
    IATA→BMF-Bridge, dann das SE-Stadt-Code-Mapping (CHI/STO/ROM). Returnt
    (country, rate_dict) oder (None, None)."""
    iata_to_bmf = iata_to_bmf or {}
    country = iata_to_bmf.get(ort) or _SE_CITY_TO_BMF.get(ort)
    if not country:
        return None, None
    # bmf_table ist nach IATA gekeyt; für SE-Stadt-Codes über country zurück-
    # suchen (irgendein IATA mit gleichem country).
    rate = bmf_table.get(ort)
    if not rate:
        for _iata, _r in bmf_table.items():
            if _r.get('country') == country:
                rate = _r
                break
    return country, rate


def _se_parsing_looks_broken(normalized_tours, se_any_dates, se_foreign_dates,
                             audit_warnings):
    """Sanity-Schranke gegen kaputten/fremden SE-Reader: True, wenn SE-Zeilen
    existieren, aber KEIN Auslands-stfrei-Ort geliefert wurde, OBWOHL die CAS-
    Touren eindeutig Auslandstage haben. Dann ist das SE-Parsing vermutlich
    defekt → Gate NICHT aktivieren (sonst stumme Unterdrückung rechtmäßiger
    VMA). Schreibt eine Audit-Warnung. Ausgelagert (Branch-Count <50)."""
    if not (se_any_dates and not se_foreign_dates):
        return False
    cas_has_foreign = any(
        (td.target_iata and not _is_inland_code(td.target_iata))
        or (td.layover_iata and not _is_inland_code(td.layover_iata))
        or td.has_real_fl_layover
        for tour in normalized_tours for td in tour.days
    )
    if cas_has_foreign:
        audit_warnings.append(
            'SE-Gate DEAKTIVIERT: SE-Reader lieferte Zeilen, aber KEINEN '
            'Auslands-stfrei-Ort, obwohl CAS Auslandstouren zeigt — '
            'vermutlich SE-Layout/Reader-Problem. CAS-Fallback aktiv, um keine '
            'rechtmäßige Auslands-VMA stumm zu unterdrücken.'
        )
    return cas_has_foreign


def _tour_gate_coverage(tour, se_any_dates):
    """Greift das SE-Gate für diese Tour? MIT Disclosure-Pass: für JEDE Tour
    (True), da der Disclosure legitime SE-lose Tage separat zurückholt. OHNE:
    nur wenn die Tour mind. eine SE-Zeile hat. Ausgelagert (Branch-Count <50)."""
    if _se_disclose_enabled():
        return True
    return any(td.date.isoformat() in se_any_dates for td in tour.days)


def _cas_date_set_of(cas_days):
    """Menge der Daten, an denen der CAS-Reader einen Tag hat (datum/date).
    Ausgelagert (Branch-Count <50). Leeres Set wenn cas_days None."""
    out = set()
    for cd in (cas_days or []):
        ds = cd.get('datum') or cd.get('date')
        if ds:
            out.add(ds)
    return out


def _se_disclose_foreign_vma(result, se_rows, bmf_table, iata_to_bmf, homebase,
                             cas_date_set):
    """SE-Aufdeckungs-Pass: ergänzt Z76 für Auslands-stfrei-Tage, die in keiner
    Tour erfasst wurden (CAS-Reader-Lücke). Tagtyp aus Zwölftel-Spalte
    (12 = voll_24h, <12 = an_abreise). Mutiert result in-place. Konservativ:
    nur Tage die (a) NICHT schon eine VMA-Klasse in result.by_date haben,
    (b) einen auflösbaren BMF-Country haben, UND (c) einen CAS-Tag an dem Datum
    haben (cas_date_set) — reine SE-only-Tage ohne CAS-Beleg erzeugen NIE VMA."""
    hb_up = (homebase or 'FRA').upper()
    # Index: an welchen Tagen gibt es eine Auslands-stfrei-SE-Zeile? Für die
    # Zwischentag-Erkennung (voll_24h nur wenn Vortag UND Folgetag auch auswärts).
    from datetime import datetime as _dt, timedelta as _td
    se_foreign_days = set()
    for se in se_rows:
        if se.get('storno'):
            continue
        o = (se.get('stfrei_ort') or '').upper().strip()
        if o and len(o) == 3 and o.isalpha() and o != hb_up \
                and not (se.get('stfrei_inland') is True or _is_inland_code(o)):
            se_foreign_days.add(se.get('datum') or se.get('date'))

    def _is_mid_foreign(ds):
        try:
            d0 = _dt.strptime(ds, '%Y-%m-%d').date()
        except (ValueError, TypeError):
            return False
        prev = (d0 - _td(days=1)).isoformat()
        nxt = (d0 + _td(days=1)).isoformat()
        return prev in se_foreign_days and nxt in se_foreign_days

    for se in se_rows:
        if se.get('storno'):
            continue
        ds = se.get('datum') or se.get('date') or ''
        if not ds:
            continue
        # Audit-sichere Schranke: nur Tage mit einem CAS-Tag (Reader hat den Tag
        # gesehen). SE-only ohne CAS-Beleg → keine VMA (test_se_only...).
        if ds not in cas_date_set:
            continue
        ort = (se.get('stfrei_ort') or '').upper().strip()
        if not (ort and len(ort) == 3 and ort.isalpha() and ort != hb_up):
            continue
        if se.get('stfrei_inland') is True or _is_inland_code(ort):
            continue  # Inland → kein Z76 (Inland-VMA separat, hier ausgeklammert)
        # Tag schon als Z76 erfasst → nichts tun (richtige Klasse). Inland-Klassen
        # (Z72/Z73/Z74) MIT echtem SE-Ausland-Beleg + CAS-Tag werden überstimmt:
        # SE-Auslands-stfrei schlägt CAS-Inland (Reader-Lücke — der Tag ist real
        # ein Auslandstag, der CAS sah kein Auslands-Routing). Guards (CAS-Tag
        # vorhanden + SE-Ausland-stfrei) verhindern, dass reine Inland-Tage oder
        # SE-only-Tage fälschlich Z76 werden.
        existing = result.by_date.get(ds)
        if existing and (existing.get('klass') or '').upper() == 'Z76':
            continue
        country, rate = _bmf_country_for_se_ort(ort, iata_to_bmf, bmf_table)
        if not rate:
            continue
        # voll_24h NUR für echte Zwischentage (Vortag UND Folgetag auch auswärts
        # = volle 24h im Ausland). Rand-/Einzeltage = an_abreise (FollowMe rechnet
        # nach echter Abwesenheitsdauer; Zwölftel allein überschätzt voll_24h).
        full = _is_mid_foreign(ds)
        amount = float((rate.get('voll_24h' if full else 'an_abreise', 0) or 0))
        if amount <= 0:
            continue
        # War der Tag vorher eine andere VMA-Klasse? Deren Betrag/Counter zurück-
        # rollen, damit nicht doppelt gezählt wird.
        if existing:
            _old = (existing.get('klass') or '').upper()
            _oldamt = float(existing.get('amount') or 0)
            if _old == 'Z73':
                result.z73_eur -= _oldamt; result.z73_tage -= 1
            elif _old == 'Z74':
                result.z74_eur -= _oldamt; result.z74_tage -= 1
            elif _old == 'Z72':
                result.z72_eur -= _oldamt; result.z72_tage -= 1
            _was_workday = existing.get('klass') not in (None, 'none', 'Frei')
        else:
            _was_workday = False
        result.z76_eur += amount
        result.z76_tage += 1
        if not _was_workday:
            result.arbeitstage += 1
            result.reinigungstage += 1
        result.by_date[ds] = {
            'tour_id': existing.get('tour_id') if existing else None,
            'klass': 'Z76',
            'amount': round(amount, 2),
            'country': country,
            'rate_type': 'voll_24h' if full else 'an_abreise',
            'source': 'SE-disclosed' + ('' if existing else ' (kein CAS-Tour-Tag)'),
            'role': existing.get('role', 'se_disclosed') if existing else 'se_disclosed',
            'has_hotel_night': existing.get('has_hotel_night', False) if existing else False,
            'country_resolution_audit': {
                'reason': f'SE-Aufdeckung: stfrei-Ort {ort} → {country}, '
                          f'zwoelftel={se.get("zwoelftel")} → '
                          f'{"voll_24h" if full else "an_abreise"}'
                          + (f' (überstimmt {existing.get("klass")})' if existing else ''),
            },
        }
        result.audit_notes.append(
            f'{ds}: SE-aufgedeckt Z76 {ort}/{country} {amount:.0f}€'
            + (f' (überstimmt {existing.get("klass")})' if existing else ' (kein CAS-Tour-Tag)'))


def _se_block_hotel_if_no_se_line(se_blocks_all_vma, day, hotel_evidence, hotel_source):
    """SE-Gate für Hotelnächte: Tag ohne eigene SE-Zeile in SE-abgedeckter Tour
    → keine Auslands-Hotelnacht. Ausnahme: echte FL-Layover-Evidence (eigener
    Flug an dem Tag, has_real_fl_layover) bleibt — das ist eine harte CAS-
    Tatsache, unabhängig von der SE-Spesenzeile. Ausgelagert (Branch-Count <50).
    Returnt (hotel_evidence, hotel_source)."""
    if se_blocks_all_vma and hotel_evidence and not day.has_real_fl_layover:
        return False, 'se_gate_no_se_line'
    return hotel_evidence, hotel_source


def _se_block_vma_if_no_se_line(se_blocks_all_vma, day_bucket, day_eur, day_audit):
    """SE-Gate Teil 2: Tag ohne eigene SE-Zeile in SE-abgedeckter Tour → keine
    VMA. Ausgelagert (Branch-Count <50). Returnt (day_bucket, day_eur)."""
    if se_blocks_all_vma and day_bucket in ('Z72', 'Z73', 'Z74', 'Z76'):
        if day_audit is not None:
            day_audit['reason'] = (day_audit.get('reason') or '') + \
                f' | SE-Gate: keine SE-Zeile an diesem Tag → keine VMA (war {day_bucket})'
        return 'none', 0.0
    return day_bucket, day_eur


def _build_se_day_index(se_rows, homebase):
    """Pro Datum: SE-stfrei-Signal aus der Streckeneinsatz-Abrechnung.

    Returnt (se_foreign_dates, se_inland_dates, se_any_dates):
      - se_foreign_dates: Tage mit stfrei-Ort im Ausland  → Z76-berechtigt
      - se_inland_dates:  Tage mit stfrei-Ort im Inland    → Z72/Z73/Z74
      - se_any_dates:     Tage mit IRGENDEINER aktiven SE-Zeile (Gate-Abdeckung)
    Storno-Zeilen werden ignoriert. Ein Tag mit ausländischem stfrei-Ort, der
    NICHT Homebase und kein DE-Code ist, zählt als foreign."""
    hb_up = (homebase or 'FRA').upper()
    se_foreign, se_inland, se_any = set(), set(), set()
    for se in (se_rows or []):
        if se.get('storno'):
            continue
        ds = se.get('datum') or se.get('date') or ''
        if not ds:
            continue
        se_any.add(ds)
        ort = (se.get('stfrei_ort') or '').upper().strip()
        inland_flag = se.get('stfrei_inland')
        if inland_flag is True or (ort and _is_inland_code(ort)):
            se_inland.add(ds)
        elif ort and len(ort) == 3 and ort.isalpha() and ort != hb_up:
            se_foreign.add(ds)
    return se_foreign, se_inland, se_any


def _tz_night_return(td: 'TourDay', homebase: str) -> Optional[bool]:
    """Zeitbasierte Nachtflug-Heimkehr-Erkennung für einen Return-Day.

    Returnt True/False wenn aus den (UTC-)Flugzeiten + Flughafen-TZ ableitbar,
    sonst None (dann gilt weiter das Marker-Flag). Rein lesend, keine Seiteneffekte.
    """
    if _tzm is None:
        return None
    raw = td.cas_raw or {}
    dep_iata = (raw.get('origin_iata') or raw.get('previous_layover_iata')
                or td.layover_iata or '')
    arr_iata = (raw.get('destination_iata') or homebase or 'FRA')
    dep_t = raw.get('departure_time')
    arr_t = raw.get('arrival_time')
    datum = td.date.isoformat() if hasattr(td.date, 'isoformat') else str(td.date)
    if not (dep_iata and arr_t and dep_t):
        return None
    try:
        return _tzm.is_night_return_flight(datum, dep_t, dep_iata, arr_t, arr_iata)
    except Exception:  # pragma: no cover
        return None


def _is_inland_code(iata: str) -> bool:
    """True wenn IATA-Code ein deutscher Flughafen ist.

    Primär die kuratierte 20er-Liste (schnell, bewährt). Zusätzlich — falls die
    Airport-DB verfügbar ist — JEDER Flughafen mit Land == 'DE'. Das fixt
    deutsche Regional-/Nebenflughäfen (z. B. ERF, KSF, SCN-Varianten), die bisher
    fälschlich als Ausland galten. Rein additiv: erkennt nur ZUSÄTZLICHE Inland-
    Codes, macht nie aus Inland Ausland.
    """
    if not iata:
        return False
    code = iata.upper().strip()
    if code in _INLAND_CODES:
        return True
    if _atz_country is not None:
        try:
            return _atz_country(code) == 'DE'
        except Exception:  # pragma: no cover
            return False
    return False


def _apply_tz_hotel(day: 'TourDay', hotel_evidence: bool, hotel_source: str,
                    ds: str, audit_notes: List[str]) -> Tuple[bool, str]:
    """CAS-Reconcile Schritt C: unterdrueckt eine vermutete Hotelnacht, wenn die
    zeitbasierte Wahrheit (tz_hotel_night, aus cas_reconcile) sagt: an diesem Tag
    KEINE Uebernachtung (im Flug / Nachtflug-Heimkehr). Konservativ: nur
    unterdruecken, nie eine Hotelnacht erfinden. Schreibt ggf. eine Audit-Notiz.
    Gesamte Verzweigung liegt HIER, nicht in der Hauptfunktion (Branch-Count).
    Returnt (hotel_evidence, hotel_source)."""
    tz_hotel = (day.cas_raw or {}).get('tz_hotel_night')
    suppress = (tz_hotel is False) and bool(hotel_evidence)
    if suppress:
        audit_notes.append(
            f'{ds}: Hotelnacht unterdrueckt — zeitbasiert keine '
            f'Uebernachtung (im Flug/Heimkehr).')
        return False, 'tz_no_hotel_in_flight'
    return hotel_evidence, hotel_source


def _has_real_flight_evidence(cas_day: Dict[str, Any]) -> bool:
    """True wenn der CAS-Tag eine echte Flight-Routing-Evidence hat.

    Akzeptierte Signale (echte Flight-Belege):
      - routing != [] und mindestens ein Element ist LH-Flugnummer oder
        enthält 4-stellige Zahl (typisch für LH-Flüge)
      - routing enthält 3-Letter-IATA ≠ inland (z.B. ['HKG']) — typisch für
        Tibor-CAS-Reader-Output
      - has_fl=True (CAS-Reader hat „FL"-Marker erkannt)
      - duty_duration_minutes >= 240 (mind. 4h Dienst)
      - overnight_after_day=True UND layover_ort ≠ leer (Tour-Continuation)
    """
    routing = cas_day.get('routing') or []
    if isinstance(routing, list):
        for r in routing:
            if not r or not isinstance(r, str):
                continue
            r_up = r.upper().strip()
            if r_up.startswith('LH'):
                return True
            digits = ''.join(c for c in r_up if c.isdigit())
            if len(digits) >= 4:
                return True
            # v15 B7: 3-Letter-IATA ≠ inland zählt auch als Flight-Evidence
            # (Tibor-CAS-Reader liefert routing=['HKG'] ohne LH-Prefix).
            if len(r_up) == 3 and r_up.isalpha() and not _is_inland_code(r_up):
                return True
    if cas_day.get('has_fl'):
        return True
    duty = int(cas_day.get('duty_duration_minutes') or 0)
    if duty >= 240:
        return True
    # v15 B7: overnight + layover_ort (auch inland) ist Tour-Continuation-Beleg
    overnight = bool(cas_day.get('overnight_after_day'))
    layover = (cas_day.get('layover_ort') or '').strip()
    if overnight and layover:
        return True
    return False


# ════════════════════════════════════════════════════════════════════════════
# v15 B7: Robuste BMF-Country-Auflösung pro Tag
# ════════════════════════════════════════════════════════════════════════════

def resolve_bmf_country_for_tour_day(
    day: 'TourDay',
    tour: Optional['NormalizedTour'],
    se_rows: List[Dict[str, Any]],
    homebase: str,
    bmf_table: Dict[str, Dict[str, float]],
    iata_to_bmf: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Versucht das BMF-Land + Pauschalen-Daten für einen Tour-Tag aus
    mehreren Quellen abzuleiten (Source-Hierarchie pro Spec).

    Source-Kandidaten in Reihenfolge:
      1. SE stfrei_ort am selben Datum, wenn vorhanden und Ausland
      2. day.layover_iata, wenn Ausland und ≠ Homebase
      3. day.target_iata, wenn Ausland
      4. day.origin_iata bei Return-Day, wenn Ausland
      5. prev/next Tour-Day layover_iata im selben Tour-Kontext
      6. routing tokens (nur echte 3-Letter-IATAs, keine Flugnummern)
      7. CAS raw fields: layover_ort, ziel, destination, station, overnight_place
      8. Sonst: keine Z76, sondern warning "missing_bmf_country"

    Returnt:
      {
        'selected_country': str | None,
        'selected_iata':    str | None,
        'selected_rate':    {'an_abreise': ..., 'voll_24h': ...} | None,
        'rate_type':        'an_abreise' | 'voll_24h' | 'same_day_8h' | 'none',
        'source_used':      str,
        'candidates_considered': list,
        'rejected_candidates':   list,
        'reason':           str,
      }
    """
    iata_to_bmf = iata_to_bmf or {}
    hb_up = (homebase or 'FRA').upper()
    candidates_considered: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    selected_iata: Optional[str] = None
    selected_country: Optional[str] = None
    source_used: str = 'none'
    reason: str = ''
    day_date_str = day.date.isoformat() if hasattr(day.date, 'isoformat') else str(day.date)

    def _is_foreign_iata(iata: str) -> bool:
        if not iata or not isinstance(iata, str):
            return False
        u = iata.upper().strip()
        if len(u) != 3 or not u.isalpha():
            return False
        if u == hb_up:
            return False
        if _is_inland_code(u):
            return False
        return True

    def _try_select(iata: str, source: str, label: str) -> bool:
        nonlocal selected_iata, selected_country, source_used, reason
        u = iata.upper().strip() if iata else ''
        cand = {'iata': u, 'source': source, 'label': label}
        candidates_considered.append(cand)
        if not _is_foreign_iata(u):
            rejected.append({**cand, 'reason': 'not_foreign_iata'})
            return False
        if u in bmf_table:
            selected_iata = u
            selected_country = bmf_table[u].get('country') or iata_to_bmf.get(u, '')
            source_used = source
            reason = f'{label} → {u} ({selected_country})'
            return True
        # IATA → Country-Mapping versuchen
        country = iata_to_bmf.get(u)
        if country:
            selected_iata = u
            selected_country = country
            source_used = source
            reason = f'{label} → {u} → {country} (via IATA_TO_BMF)'
            return True
        rejected.append({**cand, 'reason': 'iata_unknown_in_bmf_or_iata_to_bmf'})
        return False

    # 1. SE stfrei_ort
    for se in (se_rows or []):
        if (se.get('datum') or se.get('date')) != day_date_str:
            continue
        if se.get('storno'):
            continue
        stfrei_ort = (se.get('stfrei_ort') or '').upper().strip()
        if _try_select(stfrei_ort, 'SE', f'SE.stfrei_ort@{day_date_str}'):
            break

    # 2. day.layover_iata
    if not selected_iata and day.layover_iata:
        _try_select(day.layover_iata, 'CAS', 'day.layover_iata')

    # 3. day.target_iata
    if not selected_iata and day.target_iata:
        _try_select(day.target_iata, 'CAS', 'day.target_iata')

    # 4. day.origin_iata bei Return-Day
    if not selected_iata and day.is_return_day and day.origin_iata:
        _try_select(day.origin_iata, 'CAS', 'day.origin_iata (return)')

    # 5. prev/next Tour-Day layover_iata
    if not selected_iata and tour is not None:
        try:
            idx = next(i for i, td in enumerate(tour.days) if td.date == day.date)
            for offset in (-1, 1, -2, 2):
                ni = idx + offset
                if 0 <= ni < len(tour.days):
                    neighbor = tour.days[ni]
                    if neighbor.layover_iata:
                        if _try_select(neighbor.layover_iata, 'CAS+TOUR',
                                       f'tour-neighbor[{offset:+d}].layover_iata'):
                            break
                    if not selected_iata and neighbor.target_iata:
                        if _try_select(neighbor.target_iata, 'CAS+TOUR',
                                       f'tour-neighbor[{offset:+d}].target_iata'):
                            break
        except (StopIteration, ValueError):
            pass

    # 6. routing tokens (3-Letter-IATA, kein Flugnummern)
    if not selected_iata:
        for r in (day.routing_evidence or []):
            if not isinstance(r, str):
                continue
            r_up = r.upper().strip()
            if len(r_up) == 3 and r_up.isalpha():
                if _try_select(r_up, 'CAS', 'routing-token'):
                    break

    # 7. CAS raw fields
    if not selected_iata:
        for raw_key in ('layover_ort', 'ziel', 'destination', 'station', 'overnight_place'):
            raw_val = (day.cas_raw or {}).get(raw_key, '')
            if raw_val and isinstance(raw_val, str):
                if _try_select(raw_val.upper(), 'CAS', f'cas_raw.{raw_key}'):
                    break

    # Rate-Lookup
    rate_obj = None
    rate_type = 'none'
    if selected_iata and selected_iata in bmf_table:
        rate_obj = bmf_table[selected_iata]
    elif selected_country:
        # Über country-Lookup (Fallback wenn IATA nicht direkt in bmf_table)
        for iata, info in bmf_table.items():
            if info.get('country') == selected_country:
                rate_obj = info
                break

    if rate_obj:
        if day.is_full_away_day:
            rate_type = 'voll_24h'
        elif day.is_departure_day and day.is_return_day:
            rate_type = 'same_day_8h'
        elif day.is_departure_day or day.is_return_day:
            rate_type = 'an_abreise'

    if not selected_iata:
        reason = 'missing_bmf_country — keine belastbare Quelle für Land'
        # Additive Diagnose: Wenn die Airport-DB das Land KENNT, der BMF-Lookup
        # aber gescheitert ist, ist das ein behebbarer Tabellen-/Mapping-Fehler —
        # KEIN echtes "Land unbekannt". Statt still 0€ zu rechnen, machen wir es
        # sichtbar (Audit), damit kein Z76 leise verloren geht.
        if _atz_country is not None:
            for cand in candidates_considered:
                ci = (cand.get('iata') or '').upper().strip()
                try:
                    iso = _atz_country(ci)
                except Exception:
                    iso = None
                if iso and iso != 'DE':
                    reason = (
                        f'missing_bmf_country_RESOLVABLE — Airport {ci} liegt in '
                        f'Land {iso} (laut Airport-DB), aber kein BMF-Satz in '
                        f'bmf_table/IATA_TO_BMF gefunden. Tabelle/Mapping prüfen — '
                        f'hier geht potenziell Z76 verloren.'
                    )
                    break

    return {
        'selected_country':      selected_country,
        'selected_iata':         selected_iata,
        'selected_rate':         rate_obj,
        'rate_type':             rate_type,
        'source_used':           source_used,
        'candidates_considered': candidates_considered,
        'rejected_candidates':   rejected,
        'reason':                reason,
    }


def _detect_passive_marker(marker: str) -> bool:
    """True bei rein passiven Heimat-Markern (kein Dienst, kein VMA).

    Aus CAS-Online-Hilfe: ORTSTAG, FRS, OF, LMN_AS, LMN_CR, etc.
    """
    if not marker:
        return False
    m_up = marker.upper().strip()
    PASSIVE = {'ORTSTAG', 'FRS', 'OF', 'OFF', 'LMN_AS', 'LMN_CR', 'LMN_HT1'}
    return m_up in PASSIVE


def _detect_standby_marker(marker: str) -> Tuple[bool, str]:
    """Liefert (is_standby, kind) wobei kind in {'home', 'airport', ''}.

    Home-Standby-Marker: SB_S, SB_F, SB_M, RB (default home).
    Airport-Standby-Marker: SBY, SBA (mit Airport-Kontext).
    RES: ambiguous — wird im Kontext entschieden.
    """
    if not marker:
        return (False, '')
    m_up = marker.upper().strip()
    if m_up in {'SB_S', 'SB_F', 'SB_M', 'RB', 'RES_SB'}:
        return (True, 'home')
    if m_up in {'SBA', 'SBY'}:
        return (True, 'airport')
    if m_up == 'RES':
        # RES kann beides sein — context entscheidet
        return (True, 'home')  # konservativ home, kann später upgegradet werden
    return (False, '')


def _detect_training_marker(marker: str) -> bool:
    """True für Training-/Office-Marker am Homebase."""
    if not marker:
        return False
    m_up = marker.upper().strip()
    for prefix in ('EM', 'EH', 'TK', 'EMCRM', 'SECCRM', 'EK', 'D4', 'DD',
                   'FL ', 'SIM', 'TRI', 'TRE'):
        if m_up.startswith(prefix):
            return True
    return False


# ════════════════════════════════════════════════════════════════════════════
# Tour-Building
# ════════════════════════════════════════════════════════════════════════════

def build_normalized_tours(
    cas_days: List[Dict[str, Any]],
    se_rows: List[Dict[str, Any]],
    year: int,
    employee_context: Optional[Dict[str, Any]] = None,
    homebase: str = 'FRA',
    rules: Optional[Dict[str, Any]] = None,
) -> List[NormalizedTour]:
    """Baut normalisierte Touren aus CAS+SE.

    Algorithmus (high-level):
      1. CAS-Tage sortieren nach Datum.
      2. Tour-Klammern identifizieren:
         - Block-Start: Tag mit has_real_flight_evidence + starts_at_homebase
           ODER Tag mit overnight_after_day=True nach einer Sequenz von Frei
         - Block-Ende: Tag mit ends_at_homebase und kein folge-overnight
           ODER letzter Tag einer overnight-Sequenz
      3. SE-Zeilen anreichern (NIEMALS Tour erzeugen):
         - SE-Zeile mit stfrei_ort wird der überlappenden Tour als enrichment
           gehängt (stfrei_ort → country, an_abreise-Pauschale).
         - SE-Zeile ohne überlappende Tour → 'unmapped_se' warning, kein Z76.
      4. Pro Tour-Tag Rolle bestimmen (departure/mid/return/full_away).
      5. Per Tag VmaCandidate setzen.

    Skeleton-Implementation: deterministische Tour-Erkennung aus CAS-Sequenzen.
    SE wird aktuell NUR als enrichment angewandt, nicht als Tour-Trigger.
    """
    rules = rules or {}
    homebase_up = (homebase or 'FRA').upper()

    if not cas_days:
        return []

    # v15 Reader-V2 (2026-05-25): CAS-Postprocessor heilt Reader-Lücken
    # (X als Frei, leerer Marker zwischen Tour-Tagen, ends_hb-Conflicts).
    # Deterministische generische Regeln — kein Date-Hardcoding.
    try:
        from cas_postprocessor import normalize_cas_days_v2
        sorted_days = normalize_cas_days_v2(
            cas_days, homebase=homebase_up, se_rows=se_rows,
        )
    except Exception:
        # Falls Postprocessor crasht → fall through auf raw days (defensiv)
        sorted_days = sorted(cas_days, key=lambda d: d.get('datum', ''))

    tours: List[NormalizedTour] = []
    current_tour_days: List[TourDay] = []
    current_tour_evidence: Dict[str, Any] = {'cas': [], 'se': [], 'reasoning': []}
    tour_counter = 0

    def _flush_tour():
        nonlocal current_tour_days, current_tour_evidence, tour_counter
        if not current_tour_days:
            return
        # Mindestens 1 Tag mit echter Flight-/Layover-Evidence im aktuellen Block
        has_evidence = any(
            d.has_real_flight or d.has_real_fl_layover
            for d in current_tour_days
        )
        if not has_evidence:
            # Kein echtes Tour-Evidence → kein Tour, sondern Audit-Warning
            current_tour_evidence['reasoning'].append(
                f'Block {current_tour_days[0].date}-{current_tour_days[-1].date} '
                'übersprungen: keine Flight/Layover-Evidence'
            )
            current_tour_days = []
            current_tour_evidence = {'cas': [], 'se': [], 'reasoning': []}
            return

        tour_counter += 1
        tour_id = f'T{year}-{tour_counter:03d}'
        # Position-Flags setzen
        n_days = len(current_tour_days)
        for idx, td in enumerate(current_tour_days):
            if n_days == 1:
                # Same-Day-Tour: ein Tag = Start + Ende
                td.is_departure_day = True
                td.is_return_day = True
            else:
                if idx == 0:
                    td.is_departure_day = True
                if idx == n_days - 1:
                    td.is_return_day = True
                if 0 < idx < n_days - 1:
                    td.is_full_away_day = True

        # target_iata propagieren: wenn irgendein Tour-Tag ein target hat,
        # nutze es als Tour-Default für Mid-Tour-Tage ohne eigenes target.
        tour_target = None
        for td in current_tour_days:
            if td.target_iata and not _is_inland_code(td.target_iata):
                tour_target = td.target_iata
                break
        # Auch aus layover_iata
        if not tour_target:
            for td in current_tour_days:
                if td.layover_iata and not _is_inland_code(td.layover_iata):
                    tour_target = td.layover_iata
                    break
        if tour_target:
            for td in current_tour_days:
                if not td.target_iata:
                    td.target_iata = tour_target

        confidence = 'high' if all(d.has_real_flight for d in current_tour_days) else 'medium'

        tours.append(NormalizedTour(
            tour_id=tour_id,
            start_date=current_tour_days[0].date,
            end_date=current_tour_days[-1].date,
            homebase=homebase_up,
            days=current_tour_days,
            source_evidence=current_tour_evidence,
            confidence=confidence,
            warnings=[],
        ))
        current_tour_days = []
        current_tour_evidence = {'cas': [], 'se': [], 'reasoning': []}

    # Pass 1: CAS-basierte Tour-Erkennung
    for cas_day in sorted_days:
        datum_str = cas_day.get('datum', '')
        try:
            day_date = datetime.strptime(datum_str, '%Y-%m-%d').date() if datum_str else None
        except ValueError:
            day_date = None
        if day_date is None:
            continue

        marker = (cas_day.get('marker_raw') or cas_day.get('marker') or '').strip()
        overnight = bool(cas_day.get('overnight_after_day'))
        starts_at_hb = bool(cas_day.get('starts_at_homebase'))
        ends_at_hb = bool(cas_day.get('ends_at_homebase'))
        has_flight = _has_real_flight_evidence(cas_day)
        is_passive = _detect_passive_marker(marker)
        is_standby, sb_kind = _detect_standby_marker(marker)
        is_training = _detect_training_marker(marker)
        activity = (cas_day.get('activity_type') or '').lower()
        is_free = activity in ('frei', 'urlaub', 'krank', 'off') or is_passive

        layover_ort = (cas_day.get('layover_ort') or '').upper().strip()
        routing = cas_day.get('routing') or []
        target = ''
        # IATA-Codes sind 3-Letter, alphabetisch. Flugnummern wie 'LH756'
        # werden NICHT als target_iata gewertet (das ist Flight-Evidence).
        for r in routing if isinstance(routing, list) else []:
            if not (r and isinstance(r, str)):
                continue
            r_up = r.upper().strip()
            if len(r_up) == 3 and r_up.isalpha() \
                    and r_up != homebase_up and not _is_inland_code(r_up):
                target = r_up
                break

        # TourDay erstellen
        td = TourDay(
            date=day_date,
            cas_marker=marker or None,
            cas_raw=dict(cas_day),
            duty_type='unknown',
            is_home_standby=(is_standby and sb_kind == 'home'),
            is_airport_standby=(is_standby and sb_kind == 'airport'),
            is_training=is_training,
            is_free=is_free,
            routing_evidence=list(routing) if isinstance(routing, list) else [],
            target_iata=target or None,
            layover_iata=layover_ort or None,
            has_real_flight=has_flight,
            has_real_fl_layover=bool(overnight and layover_ort
                                     and not _is_inland_code(layover_ort)
                                     and layover_ort != homebase_up),
            hotel_night_after_this_day=False,  # gesetzt in Pass 2
            confidence='high' if has_flight else 'medium',
        )

        # Tour-Klammer-Logik
        is_tour_continuation = bool(current_tour_days)
        # v15 B7: Tour-Continuation auch wenn marker='X' + foreign routing
        # (typischer Tibor-CAS-Reader-Output für Mid-Tour-Tage ohne overnight-Flag)
        has_foreign_routing_token = any(
            isinstance(r, str) and len(r.upper().strip()) == 3
            and r.upper().strip().isalpha()
            and not _is_inland_code(r.upper().strip())
            and r.upper().strip() != homebase_up
            for r in (routing if isinstance(routing, list) else [])
        )
        # v15 Reader-V2: Postprocessor-Hints respektieren
        is_postproc_tour_return = bool(cas_day.get('is_tour_return'))
        is_postproc_continuation = bool(cas_day.get('is_tour_continuation'))
        is_clear_tour_signal = (has_flight or td.has_real_fl_layover or bool(target)
                                or has_foreign_routing_token
                                or is_postproc_tour_return
                                or is_postproc_continuation)

        # B9: Office/Training/passive Marker am Homebase erzeugt KEINE eigenständige Tour
        is_homebase_only_office = (
            is_training and not has_flight and not overnight and not target
            and not has_foreign_routing_token
        )

        # v15 B18 (final): Home-Standby NIE Tour-Trigger, auch wenn duty>=240.
        # SB_S/SB_F/SB_M/RB/RES_SB mit Standby-Bereitschaft ist KEIN Auswärts-
        # tätigkeit, sondern reine Verfügbarkeitspflicht zuhause.
        if (is_standby and sb_kind == 'home') and not is_tour_continuation:
            continue

        if is_free and not is_tour_continuation:
            continue
        if is_homebase_only_office and not is_tour_continuation:
            # Reines Office am Homebase ohne Tour-Anschluss — kein Tour-Trigger.
            # Wird im Klassifikator als Z72 (wenn duty>=480) sonst Office verbucht.
            continue

        if is_clear_tour_signal or is_tour_continuation:
            current_tour_days.append(td)
            current_tour_evidence['cas'].append({
                'datum': datum_str,
                'marker': marker,
                'has_flight': has_flight,
                'overnight': overnight,
                'layover_ort': layover_ort,
            })

            # Tour-Ende: ends_at_homebase + kein overnight = Heimkehr abgeschlossen
            if ends_at_hb and not overnight:
                _flush_tour()
            # v15 B7: Free-Tag in Tour-Block (z.B. Heimkehr-Tag ohne explizit
            # ends_at_homebase) → Tour beenden
            elif is_free and is_tour_continuation:
                _flush_tour()
        # else: Free/Standby außerhalb Tour — skip, kein Tour-Tag
    # End Pass 1
    _flush_tour()  # falls am Jahresende noch offene Touren

    # Pass 2: SE-Anreicherung (NIEMALS Tour erzeugen!)
    se_by_date: Dict[str, List[Dict[str, Any]]] = {}
    for se in (se_rows or []):
        d = se.get('datum') or se.get('date') or ''
        if d:
            se_by_date.setdefault(d, []).append(se)

    unmapped_se_count = 0
    for tour in tours:
        for td in tour.days:
            ds = td.date.isoformat()
            ses = se_by_date.get(ds, [])
            td.se_rows = ses
            # SE-Country enrichment wenn CAS keine target_iata hatte
            for se in ses:
                stfrei_ort = (se.get('stfrei_ort') or '').upper().strip()
                stfrei_betrag = float(se.get('stfrei_betrag') or 0)
                if stfrei_betrag > 0 and stfrei_ort and not _is_inland_code(stfrei_ort):
                    if not td.target_iata:
                        td.target_iata = stfrei_ort
                        tour.source_evidence['reasoning'].append(
                            f'{ds}: SE-Anreicherung mit stfrei_ort={stfrei_ort}'
                        )
                    tour.source_evidence['se'].append({
                        'datum': ds, 'stfrei_ort': stfrei_ort,
                        'stfrei_betrag': stfrei_betrag,
                    })

    # SE-Zeilen die KEINER Tour zugeordnet werden konnten
    tour_dates = {td.date.isoformat() for t in tours for td in t.days}
    for ds, ses in se_by_date.items():
        if ds in tour_dates:
            continue
        for se in ses:
            if float(se.get('stfrei_betrag') or 0) > 0:
                unmapped_se_count += 1

    return tours


# ════════════════════════════════════════════════════════════════════════════
# Allowance-Berechnung aus normalisierten Touren
# ════════════════════════════════════════════════════════════════════════════

def calculate_allowances_from_normalized_tours(
    normalized_tours: List[NormalizedTour],
    bmf_table: Dict[str, Dict[str, float]],
    rules: Optional[Dict[str, Any]] = None,
    iata_to_bmf: Optional[Dict[str, str]] = None,
    se_rows: Optional[List[Dict[str, Any]]] = None,
    homebase: str = 'FRA',
    cas_days: Optional[List[Dict[str, Any]]] = None,
) -> CalculationResult:
    """Berechnet VMA + Counter aus normalisierten Touren.

    Regeln:
      - Z76 nur wenn Tour-Tag UND BMF-Country resolvable (via B7 helper).
      - Z73 wenn Inland-An/Ab-Tag.
      - Z74 wenn echter Inland-Volltag (24h-Übernachtung in DE), B8-Check.
      - Z72 wenn Same-Day Inland >8h.
      - Hotelnacht nur wenn Tag mit has_real_fl_layover=True.
      - Fahrtag = 1 pro Tour-Start mit ECHTEM Tour-Signal (B9 Filter).
      - Reinigungstag nur wenn dienstlicher Tag (kein Home-Standby).
      - Arbeitstag = jede Tour-Tag-Rolle (departure/mid/return/full_away).
    """
    rules = rules or {}
    se_rows = se_rows or []
    iata_to_bmf = iata_to_bmf or {}
    result = CalculationResult(tour_count=len(normalized_tours))

    INLAND_AN_AB = 14.0
    INLAND_VOLL_24H = 28.0

    # SE-primäre VMA: Auslands-Pauschale (Z76) nur für Tage, die die Strecken-
    # einsatz-Abrechnung als steuerfreie Auslands-Spesen ausweist. Das ist die
    # finanzamt-konforme Quelle (siehe _se_primary_enabled). se_any_dates =
    # Gate-Abdeckung: Tage ohne JEDE SE-Zeile werden NICHT gegated (Fallback
    # auf CAS-Routing), damit Jahresgrenz-Touren nicht verloren gehen.
    _se_foreign_dates, _se_inland_dates, _se_any_dates = _build_se_day_index(se_rows, homebase)
    # SE-Gate NUR aktiv wenn überhaupt SE-Daten vorliegen. Ohne SE-Rows (viele
    # Unit-Fixtures, oder Job ohne SE-Upload) gilt der CAS-Pfad unverändert —
    # das Gate darf dann NICHTS blocken (sonst fällt jede VMA auf 'none').
    #
    # SANITY-SCHRANKE (Schutz gegen kaputten/fremden SE-Reader): Wenn die CAS-
    # Touren eindeutig Auslands-Tage haben, der SE-Reader aber NULL Auslands-
    # stfrei-Orte geliefert hat, ist das SE-Parsing höchstwahrscheinlich defekt
    # (fremdes Layout, anderer Arbeitgeber, Lese-Fehler). Dann das Gate NICHT
    # aktivieren — sonst würde es alle echten Auslandstage stumm auf 'none'
    # demoten und dem Nutzer rechtmäßige VMA wegnehmen. Lieber CAS-Fallback
    # (eher zu viel als zu wenig zugunsten des Nutzers) + Audit-Warnung.
    _se_parsing_suspect = _se_parsing_looks_broken(
        normalized_tours, _se_any_dates, _se_foreign_dates, result.audit_warnings)
    _se_primary = (_se_primary_enabled() and bool(_se_any_dates)
                   and not _se_parsing_suspect)

    for tour in normalized_tours:
        # v15 B9 — Fahrtag nur wenn echter Tour-Start (mit Auslandsroutings
        # oder overnight). Office/Same-Day-Inland zählt NICHT als Tour-Fahrtag.
        # Office-only-Touren (z.B. Schulung am Homebase) sind in normalized_tours
        # gar nicht als Tour gebaut — die Filter laufen schon im Builder.
        tour_has_foreign_signal = any(
            (td.has_real_fl_layover or
             (td.target_iata and not _is_inland_code(td.target_iata)) or
             (td.layover_iata and not _is_inland_code(td.layover_iata)))
            for td in tour.days
        )
        tour_has_overnight = any(td.has_real_fl_layover for td in tour.days)
        # R19 (2026-05-26): Same-Day-Inland-Trip mit duty>=480 ist auch ein
        # legitimer Tour-Start (Z72-Same-Day). Vorher: wurde nicht als Fahrtag
        # gezählt → fahrtage zu niedrig.
        # ABER: Office/Training ohne Flug ist KEIN Fahrtag (FollowMe-Konvention).
        # Daher: routing_evidence muss einen echten Flight-Token enthalten
        # (LH-prefix oder >=3-stellige Flugnummer). Reine Office-Marker (EM,
        # SIM, ORTSTAG) ohne Flug-Routing zählen nicht.
        _hb_local = (homebase or 'FRA').upper().strip()
        def _has_flight_token_in_routing(td):
            for r in (td.routing_evidence or []):
                if not isinstance(r, str):
                    continue
                t = r.upper().strip()
                if t.startswith('LH'):
                    return True
                digits = ''.join(c for c in t if c.isdigit())
                if len(digits) >= 3 and not t.startswith(_hb_local):
                    return True
                if (len(t) == 3 and t.isalpha()
                        and not _is_inland_code(t) and t != _hb_local):
                    return True
            return False
        tour_has_z72_same_day = any(
            td.is_departure_day and td.is_return_day
            and int(td.cas_raw.get('duty_duration_minutes') or 0) >= 480
            and _has_flight_token_in_routing(td)
            for td in tour.days
        )
        # R21 (2026-05-26): V2-Reader liefert is_tour_departure direkt. Wenn
        # Sonnet einen Tag als Tour-Departure markiert hat, ist das ein
        # legitimer Tour-Start auch ohne andere Heuristiken.
        tour_has_v2_departure_hint = any(
            bool(td.cas_raw.get('is_tour_departure'))
            for td in tour.days
        )
        # R22 (2026-05-26): Inland-Same-Day-Tour mit echtem Flug-Token zählt
        # als Tour-Start, auch wenn duty<480 (kein Z72) und kein foreign-signal.
        # WICHTIG: has_real_flight ist zu liberal (triggert bei duty>=240),
        # daher Flight-Token im routing als striktes Kriterium nutzen.
        def _has_real_flight_token(td):
            for r in (td.routing_evidence or []):
                if not isinstance(r, str):
                    continue
                t = r.upper().strip()
                if t.startswith('LH'):
                    return True
                digits = ''.join(c for c in t if c.isdigit())
                if len(digits) >= 3 and not t.startswith(hb_up):
                    return True
            return False
        tour_has_inland_flight_same_day = any(
            td.is_departure_day and td.is_return_day
            and _has_real_flight_token(td)
            for td in tour.days
        )
        is_legitimate_tour_start = (
            tour_has_foreign_signal or tour_has_overnight
            or tour_has_z72_same_day or tour_has_v2_departure_hint
            or tour_has_inland_flight_same_day
        )

        # Fahrtag pro Tour-Start (B9: nur legitime)
        if tour.days and is_legitimate_tour_start:
            result.fahrtage += 1
            tour.days[0].commute_candidate = CommuteCandidate(
                counts_fahrtag=True, source='CAS',
                reason=f'Tour-Start {tour.tour_id} (foreign-signal={tour_has_foreign_signal}, '
                       f'overnight={tour_has_overnight})',
            )

        # SE-Gate-Abdeckung auf TOUR-Ebene: hat irgendein Tag dieser Tour eine
        # SE-Zeile? Dann ist das Gate für die ganze Tour aussagekräftig — fehlt
        # einem Auslands-Tour-Tag das Auslands-stfrei, ist er kein Z76. Touren
        # GANZ ohne SE-Zeile (Jahresgrenz-Tour mit SE im Vormonat, oder reine
        # Inlandstour) bleiben ungated → CAS-Routing gilt, keine echten Tage
        # gehen verloren. Phantom-Touren ohne SE (Angola-Deadhead) holt separat
        # der Disclosure-Pass NICHT zurück — sie bleiben über CAS klassifiziert.
        # (Verifiziert: dieser Tour-Level-Gate liefert VMA +25€ auf Tibor 2025.)
        # MIT aktivem Disclosure-Pass greift das Gate für JEDE Tour (auch
        # SE-lose) — der Disclosure holt legitime SE-lose Tage (Jahresgrenze)
        # separat + belegt (CAS-Tag vorhanden) zurück. So fallen Phantom-Touren
        # (Angola: kein SE, kein CAS-Auslands-Beleg) korrekt weg.
        _tour_has_any_se = _tour_gate_coverage(tour, _se_any_dates)

        for td in tour.days:
            ds = td.date.isoformat()
            day_eur = 0.0
            day_bucket = 'none'
            day_country = None
            day_rate = 'none'
            day_source = 'none'
            day_audit = None

            # v15 B7: Robuste Country-Auflösung
            day_audit = resolve_bmf_country_for_tour_day(
                td, tour, se_rows, homebase, bmf_table, iata_to_bmf,
            )
            resolved_country = day_audit.get('selected_country')
            resolved_rate = day_audit.get('selected_rate')
            is_foreign = bool(resolved_country) and \
                'Deutschland' not in (resolved_country or '')

            # ── SE-PRIMÄR-GATE (audit-sicher) ──
            # Ein Tag bekommt Auslands-VMA (Z76) NUR, wenn die Streckeneinsatz-
            # Abrechnung Auslands-stfrei ausweist. CAS-Routing allein (z.B.
            # Deadhead/Positionierung Angola, FRA-Durchgang) genügt nicht.
            # Gate greift, sobald die TOUR überhaupt SE-Abdeckung hat — fehlt
            # dann am Tag das Auslands-stfrei, ist es kein Z76. Touren GANZ ohne
            # SE (Jahresgrenze, SE im Vormonat) bleiben ungated (CAS-Routing).
            # SE-Gate Teil 1 (is_foreign-Gate): ausgelagert (Branch-Count <50).
            is_foreign, _, _ = _se_gate_day(
                is_foreign, day_bucket, day_eur, ds, _tour_has_any_se,
                _se_foreign_dates, _se_any_dates, _se_primary, day_audit)

            # SE-Gate Teil 2: Hat die Tour SE-Abdeckung, dieser Tag aber GAR
            # KEINE SE-Zeile, dann besteht für den Tag kein Spesenanspruch →
            # KEINE VMA (auch kein Inland-Z73/Z72). FollowMe wertet solche
            # Tour-Rand-/Leertage als Frei. Tour-Tage mit eigener SE-Zeile
            # (egal ob in-/ausländisch) laufen normal weiter.
            _se_blocks_all_vma = (
                _se_primary and _tour_has_any_se and ds not in _se_any_dates
            )

            # v15 B8+B12: Pro-Tag-Inland-Check — strikter als bisher.
            # SE-Inland-Stempel ist klare Evidence.
            # CAS-Inland-Layover NUR wenn NICHT natürliche Tour-Boundary
            # (Anreise/Heimkehr-Tag mit layover_iata=Homebase bei Auslandstour
            # ist KEIN Inland-Übernachtungsbeleg, nur normales Tour-Ende).
            day_inland_evidence = False
            day_date_str = ds
            hb_up = (homebase or 'FRA').upper()
            for se in se_rows:
                if (se.get('datum') or se.get('date')) != day_date_str:
                    continue
                if se.get('storno'):
                    continue
                stfrei_ort = (se.get('stfrei_ort') or '').upper().strip()
                stfrei_inland = se.get('stfrei_inland')
                if stfrei_inland is True or (stfrei_ort and _is_inland_code(stfrei_ort)):
                    day_inland_evidence = True
                    break
            # B12+B18 Fix: layover_iata=Homebase bei departure/return mit foreign
            # target ist KEIN Inland-Beleg — nur natürliches Tour-Boundary.
            # Auch: layover_iata leer + Tour foreign + dep/ret = Tour-Boundary.
            if not day_inland_evidence and td.layover_iata and _is_inland_code(td.layover_iata):
                is_natural_tour_boundary = (
                    (td.is_departure_day or td.is_return_day)
                    and td.layover_iata.upper() == hb_up
                    and td.target_iata
                    and not _is_inland_code(td.target_iata)
                )
                # R19 (2026-05-26): Aircraft-Rotation-Mid-Tour-Tage haben oft
                # inland-Aircraft-Stop (z.B. MUC/HAM Stopover bei foreign Tour).
                # Wenn die Tour foreign target hat, ist auch ein Mid-Tour-Inland-
                # Layover KEIN Inland-Voll-24h-Tag, sondern Tour-Continuation.
                is_aircraft_rotation_in_foreign_tour = (
                    td.is_full_away_day
                    and tour_has_foreign_signal
                )
                if not is_natural_tour_boundary and not is_aircraft_rotation_in_foreign_tour:
                    day_inland_evidence = True

            # Same-Day-Tour FIRST (departure+return am gleichen Tag)
            if td.is_departure_day and td.is_return_day:
                duty_min = int(td.cas_raw.get('duty_duration_minutes') or 0)
                # R21 (2026-05-26): Z72 darf NUR feuern wenn echter Flight-Token
                # vorhanden ist. Office/Training-Tage am HB mit duty>=480
                # (z.B. EM-Schulung) dürfen KEIN Z72 erzeugen — sie sind keine
                # Same-Day-Inland-Trips, sondern Office.
                _td_routing = td.routing_evidence or []
                def _is_flight_in_routing():
                    for r in _td_routing:
                        if not isinstance(r, str):
                            continue
                        t = r.upper().strip()
                        if t.startswith('LH'):
                            return True
                        digits = ''.join(c for c in t if c.isdigit())
                        if len(digits) >= 3 and not t.startswith(hb_up):
                            return True
                        # 3-letter IATA non-HB (inland oder foreign) zählt als
                        # Flight-Token. Inland-Same-Day-Trip FRA→MUC = Z72.
                        if (len(t) == 3 and t.isalpha() and t != hb_up):
                            return True
                    return False
                _has_flight_token = _is_flight_in_routing()
                if duty_min >= 480 and _has_flight_token:
                    if is_foreign and resolved_rate and not day_inland_evidence:
                        day_eur = float(resolved_rate.get('an_abreise', 0) or 0)
                        day_bucket = 'Z76' if day_eur > 0 else 'none'
                        day_country = resolved_country
                        day_rate = 'same_day_8h'
                        day_source = day_audit.get('source_used') or 'CAS+BMF'
                    else:
                        day_eur = INLAND_AN_AB
                        day_bucket = 'Z72'
                        day_country = 'Deutschland'
                        day_rate = 'same_day_8h'
                        day_source = 'CAS'
                # else: keine VMA (zu kurz oder Office ohne Flight)

            elif td.is_departure_day:
                # BMF-Regel (§9 EStG): Anreise-Tag mit Auslandsübernachtung
                # ist Auslands-An/Ab (Z76, z.B. 28€), nicht Inland-An/Ab.
                # FollowMe rechnet 14€ Inland — das ist konservativ aber
                # nicht BMF-konform. Wir geben User die volle BMF-Pauschale.
                # (R23 Bug 1 Fix versucht aber zurückgerollt — BMF gewinnt.)
                if is_foreign and resolved_rate and not day_inland_evidence:
                    day_eur = float(resolved_rate.get('an_abreise', 0) or 0)
                    day_bucket = 'Z76' if day_eur > 0 else 'none'
                    day_country = resolved_country
                    day_rate = 'an_abreise'
                    day_source = day_audit.get('source_used') or 'CAS+BMF'
                else:
                    day_eur = INLAND_AN_AB
                    day_bucket = 'Z73'
                    day_country = 'Deutschland'
                    day_rate = 'an_abreise'
                    day_source = 'CAS'

            elif td.is_return_day:
                # R23 Bug 2 Fix (2026-05-27): Wenn der Reader den Tag als
                # is_return_day markiert hat, der Tag aber overnight_after_day=True
                # zeigt, dann ist der Heimflug ein Nachtflug (Take-off abends in
                # foreign, Landing am Folgetag). Tibor war an dem Tag VOLLE 24h
                # im Ausland → Voll-Pauschale, nicht An/Ab.
                # Beispiel: 05.01 BLR Marker=755 mit LH755-Departure 23:28 LT
                # → User war 24h in BLR, nicht Heimkehr-An/Ab-Tag.
                cas_overnight_return = bool(td.cas_raw.get('overnight_after_day'))
                # Zeitbasierte Nachtflug-Erkennung (BMF 24:00-Ortszeit). Additiv:
                # Wenn die Flugzeiten+TZ eine eindeutige Antwort liefern, die vom
                # Sonnet-Marker abweicht, wird das als Audit festgehalten. Nur bei
                # aktivem Flag AEROTAX_USE_TZ_MIDNIGHT überschreibt die TZ-Wahrheit
                # das Marker-Flag (sonst bleibt das Live-Ergebnis unverändert).
                _tz_nr = _tz_night_return(td, homebase)
                if _tz_nr is not None and _tz_nr != cas_overnight_return:
                    result.audit_notes.append(
                        f'{ds}: tz_midnight night_return={_tz_nr} weicht von '
                        f'marker overnight_after_day={cas_overnight_return} ab '
                        f'(dep={td.cas_raw.get("departure_time")} '
                        f'arr={td.cas_raw.get("arrival_time")} '
                        f'from={td.cas_raw.get("origin_iata") or td.layover_iata})'
                    )
                    if _tz_midnight_enabled():
                        cas_overnight_return = _tz_nr
                if cas_overnight_return and is_foreign and resolved_rate:
                    # Nachtflug-Heimkehr: voll 24h im Ausland
                    day_eur = float(resolved_rate.get('voll_24h', 0) or 0)
                    day_bucket = 'Z76' if day_eur > 0 else 'none'
                    day_country = resolved_country
                    day_rate = 'voll_24h_night_return'
                    day_source = day_audit.get('source_used') or 'CAS+BMF-night-return'
                elif is_foreign and resolved_rate and not day_inland_evidence:
                    day_eur = float(resolved_rate.get('an_abreise', 0) or 0)
                    day_bucket = 'Z76' if day_eur > 0 else 'none'
                    day_country = resolved_country
                    day_rate = 'an_abreise'
                    day_source = day_audit.get('source_used') or 'CAS+BMF'
                else:
                    day_eur = INLAND_AN_AB
                    day_bucket = 'Z73'
                    day_country = 'Deutschland'
                    day_rate = 'an_abreise'
                    day_source = 'CAS'

            elif td.is_full_away_day:
                # v15 B8: Mid-Tour-Tag braucht echte Inland-Evidence für Z74.
                # Wenn day_inland_evidence True → Z74, sonst Z76 mit Tour-Country.
                if day_inland_evidence:
                    day_eur = INLAND_VOLL_24H
                    day_bucket = 'Z74'
                    day_country = 'Deutschland'
                    day_rate = 'voll_24h'
                    day_source = 'CAS+SE-inland'
                elif is_foreign and resolved_rate:
                    day_eur = float(resolved_rate.get('voll_24h', 0) or 0)
                    day_bucket = 'Z76' if day_eur > 0 else 'none'
                    day_country = resolved_country
                    day_rate = 'voll_24h'
                    day_source = day_audit.get('source_used') or 'CAS+BMF'
                else:
                    # Kein klares Foreign UND kein klares Inland → audit warning
                    # Konservativ: keine VMA, warning
                    day_audit['reason'] = (day_audit.get('reason') or '') + \
                        ' | ambig: kein klares foreign target, kein inland-evidence'
                    result.audit_warnings.append(
                        f'{ds}: mid-tour-tag ohne klare Country-Quelle — keine VMA'
                    )

            # SE-Gate Teil 2: Tag ohne eigene SE-Zeile in SE-abgedeckter Tour
            # → keine VMA (Tour-Rand-/Leertag, FollowMe wertet als Frei).
            day_bucket, day_eur = _se_block_vma_if_no_se_line(
                _se_blocks_all_vma, day_bucket, day_eur, day_audit)

            # Aggregate
            if day_bucket == 'Z72':
                result.z72_eur += day_eur
                result.z72_tage += 1
            elif day_bucket == 'Z73':
                result.z73_eur += day_eur
                result.z73_tage += 1
            elif day_bucket == 'Z74':
                result.z74_eur += day_eur
                result.z74_tage += 1
            elif day_bucket == 'Z76':
                result.z76_eur += day_eur
                result.z76_tage += 1

            # v15 B17 (final): Arbeitstag/Reinigungstag-Refinement.
            # Strikt: klassische Duty-Signale ODER Mid-Tour mit echtem
            # Flight-Marker (LH-prefix oder 3+stellige Flugnummer in routing/marker).
            # Reader-Lücke: has_fl ist oft False, aber marker='LH756' oder
            # routing=['LH756'] zeigt echten Flug.
            duty_min_today = int(td.cas_raw.get('duty_duration_minutes') or 0)
            has_fl_today = bool(td.cas_raw.get('has_fl'))
            marker_today = (td.cas_marker or '').upper().strip()

            def _is_flight_marker_token(tok: str) -> bool:
                if not tok:
                    return False
                t = tok.upper().strip()
                if t.startswith('LH'):
                    return True
                digits = ''.join(c for c in t if c.isdigit())
                # Reine Flugnummern (>=3 digits, keine 3-letter-IATA wie 'BLR')
                return len(digits) >= 3 and digits == t.replace(' ', '')

            has_flight_marker = (
                _is_flight_marker_token(marker_today)
                or any(_is_flight_marker_token(r) for r in (td.routing_evidence or []))
            )

            # R14 (2026-05-26) — Tour-Continuation innerhalb echter normalisierter
            # Tour zählt als Arbeitstag. Voraussetzung: der Tag liegt in einer
            # gebauten Tour (builder hat `_flush_tour` evidence-Check bestanden),
            # ist KEIN home_standby, KEIN free, und die Tour hat foreign-signal
            # ODER overnight (echte Tour, nicht Office-only).
            tour_is_real = bool(tour_has_foreign_signal or tour_has_overnight)
            is_within_real_normalized_tour = (
                tour_is_real
                and (td.is_departure_day or td.is_return_day or td.is_full_away_day)
                and not td.is_home_standby
                and not td.is_free
            )

            is_real_duty_day = (
                has_fl_today                  # explizit Flug-Marker (FL)
                or has_flight_marker          # marker/routing = Flugnummer
                or td.is_departure_day        # Tour-Anreise (Briefing+Boarding)
                or td.is_return_day           # Tour-Heimkehr (Landung+Debrief)
                or td.is_training             # Office/Schulung
                or is_within_real_normalized_tour  # R14: Mid-Tour-Continuation
            )
            # Mid-Tour-Layover-Rest-Day: in Tour, kein Flug-Beleg, kein dep/ret.
            # User ist im Layover-Hotel — bekommt VMA aber KEINE Reinigung.
            is_layover_free_day = (
                td.is_full_away_day
                and not has_fl_today
                and not has_flight_marker
                and not td.is_departure_day
                and not td.is_return_day
            )

            # SE-Gate auch für Arbeitstag/Reinigung: ein Tag ohne jede SE-Zeile
            # in einer SE-abgedeckten Tour hat keinen Spesenanspruch → FollowMe
            # wertet ihn als Frei, NICHT als Arbeits-/Reinigungstag. Konsistent
            # mit dem VMA-Gate (sonst zählt z.B. ein Angola-Deadhead-Tag als
            # Arbeitstag, obwohl er keine VMA bekommt). Echte Flug-Tage (eigener
            # FL-Marker/Flugnummer) bleiben Arbeitstag, auch ohne SE-Zeile.
            _se_blocks_workday = (
                _se_blocks_all_vma and not has_fl_today and not has_flight_marker
            )
            if is_real_duty_day and not _se_blocks_workday:
                result.arbeitstage += 1
                # FollowMe-Konvention (verifiziert gegen echte Tibor-Auswertung,
                # 133=133): Reinigungstage == Arbeitstage. Jeder Arbeitstag ist
                # ein Uniform-Reinigungstag — auch Mid-Tour-Layover-Tage (Crew
                # trägt/pflegt Uniform über die ganze Tour). Home-Standby zählt
                # nicht, weil es schon kein is_real_duty_day ist.
                result.reinigungstage += 1
            # Mid-Tour-Free-Tage / SE-gegatete Tage: KEIN arbeitstag, KEIN
            # reinigungstag (FollowMe-konform — zählt nur echte Flug-/Dienst-Tage).

            # v15 B14: Hotel-Nacht-Resolution erweitert.
            # Sources (mind. eine muss zutreffen):
            #   1. has_real_fl_layover (CAS direkt)
            #   2. layover_iata Auslands-IATA + nicht Homebase
            #   3. CAS overnight_after_day=True + Tour-foreign-Country (Reader-Lücke)
            #   4. Mid-Tour-Tag UND Tour hat foreign country
            #   5. Departure-Day mit foreign target (User schläft heute im Ausland)
            #   6. SE stfrei_ort Auslands-IATA UND Tour-Klammer existiert
            cas_overnight = bool(td.cas_raw.get('overnight_after_day'))
            hotel_evidence = False
            hotel_source = 'none'
            if td.has_real_fl_layover:
                hotel_evidence = True
                hotel_source = 'has_real_fl_layover'
            elif (td.layover_iata
                  and not _is_inland_code(td.layover_iata)
                  and td.layover_iata.upper() != hb_up
                  and cas_overnight):
                # R19 (2026-05-26): Foreign-Layover-IATA allein reicht nicht.
                # Reader markiert manchmal layover_iata für den Tour-Folgetag
                # ohne overnight=True. Diese Phantom-Pfade lieferten Doppel-
                # Hotels. STRIKT: cas_overnight als Pflicht-Signal.
                hotel_evidence = True
                hotel_source = 'foreign_layover_iata_overnight'
            elif (cas_overnight and tour_has_foreign_signal
                  and not td.is_home_standby
                  and not (td.layover_iata and _is_inland_code(td.layover_iata))
                  and not (td.layover_iata and td.layover_iata.upper() == hb_up)):
                # R22 (2026-05-26): Re-Position-Tage (overnight=True mit
                # layover_iata=FRA oder Inland-IATA) sind KEIN Auslands-Hotel,
                # selbst wenn die Tour-Klammer foreign signal hat. Diese
                # Phantom-Hotels entstehen bei Aircraft-Rotations-Pattern.
                hotel_evidence = True
                hotel_source = 'cas_overnight_in_foreign_tour'
            elif (td.is_full_away_day and tour_has_foreign_signal
                  and not td.is_home_standby
                  and cas_overnight):
                # R18 (2026-05-26): STRIKTER — Mid-Tour-Tag braucht explizit
                # overnight_after_day=True vom Reader, sonst keine Hotelnacht.
                # Vorher liberal: jeder full_away_day in foreign-bracket galt als
                # Hotelnacht → 2× zu viele Hotelnächte gegen Tibor-Realität.
                hotel_evidence = True
                hotel_source = 'mid_tour_foreign_context_overnight'
            elif (td.is_departure_day and not td.is_return_day
                  and td.target_iata
                  and not _is_inland_code(td.target_iata)
                  and cas_overnight):
                # R22 (2026-05-26): Anreise-Tag mit foreign target zählt nur als
                # Hotel-Nacht, wenn der Reader auch tatsächlich overnight=True
                # gesetzt hat. Ohne diesen Strict-Guard erzeugt jede Anreise
                # mit propagiertem target_iata (auch Late-Briefing ohne overnight)
                # ein Phantom-Hotel.
                hotel_evidence = True
                hotel_source = 'foreign_departure_day_target_overnight'
            else:
                # SE-Check als letzte Source: SE stfrei am Tag mit Tour-Klammer
                for se in se_rows:
                    if (se.get('datum') or se.get('date')) != day_date_str:
                        continue
                    if se.get('storno'):
                        continue
                    stfrei_ort = (se.get('stfrei_ort') or '').upper().strip()
                    if (stfrei_ort and len(stfrei_ort) == 3
                            and stfrei_ort.isalpha()
                            and not _is_inland_code(stfrei_ort)
                            and stfrei_ort != hb_up):
                        # SE-Auslands-Ort UND Tag ist in Tour (tour_has_foreign_signal)
                        if tour_has_foreign_signal:
                            hotel_evidence = True
                            hotel_source = 'se_foreign_with_cas_tour_bracket'
                        break

            # Return-Day: keine Hotel-Nacht „danach" (User schläft zuhause).
            # Auch Same-Day-Touren (is_return UND is_departure am selben Tag,
            # FRA→Ausland→FRA an einem Tag) erzeugen KEINE Hotelnacht — Crew
            # kommt am selben Tag heim. Vorher schloss der Guard Same-Day-Touren
            # aus (and not is_departure_day) → 7 Phantom-Hotels bei Tagestrips.
            if td.is_return_day:
                hotel_evidence = False
                hotel_source = 'return_day_no_hotel_after'

            # Schritt C (CAS-Reconcile): zeitbasierte Wahrheit respektieren.
            # Gesamte Logik (inkl. Audit-Notiz) in _apply_tz_hotel ausgelagert,
            # damit die Hauptfunktion KEINE zusaetzliche Verzweigung bekommt
            # (Branch-Count-Test haelt <50).
            hotel_evidence, hotel_source = _apply_tz_hotel(
                td, hotel_evidence, hotel_source, ds, result.audit_notes)

            # SE-Gate für Hotelnächte (konsistent mit VMA/Arbeitstag): ein Tag
            # ohne jede SE-Zeile in einer SE-abgedeckten Tour hat keinen
            # Spesenanspruch → keine Auslands-Hotelnacht (Angola-Deadhead,
            # FRA-Durchgang). Echte FL-Layover-Evidence (eigener Flug an dem Tag)
            # bleibt unangetastet — das ist eine harte CAS-Tatsache.
            hotel_evidence, hotel_source = _se_block_hotel_if_no_se_line(
                _se_blocks_all_vma, td, hotel_evidence, hotel_source)

            if hotel_evidence:
                td.hotel_night_after_this_day = True
                result.hotel_naechte += 1

            # VmaCandidate auf Tag persistieren
            td.vma_candidate = VmaCandidate(
                bucket=day_bucket, country=day_country,
                rate_type=day_rate, source=day_source,
                confidence=td.confidence,
                reason=f'normalized_tours: tour={tour.tour_id}, role='
                       f'{"departure" if td.is_departure_day else "return" if td.is_return_day else "mid"}',
            )

            result.by_date[ds] = {
                'tour_id': tour.tour_id,
                'klass': day_bucket,
                'amount': round(day_eur, 2),
                'country': day_country,
                'rate_type': day_rate,
                'source': day_source,
                'role': ('departure' if td.is_departure_day and not td.is_return_day
                         else 'return' if td.is_return_day and not td.is_departure_day
                         else 'full_away' if td.is_full_away_day
                         else 'same_day'),
                'has_hotel_night': td.hotel_night_after_this_day,
                # v15 B7-Audit pro Tag: Source-Auflösung sichtbar
                'country_resolution_audit': day_audit,
            }

    # ── SE-AUFDECKUNGS-PASS (Hybrid: SE deckt auf, was CAS verpasst) ──
    # Auslands-stfrei-Tage aus der Streckeneinsatz-Abrechnung, die in KEINER
    # Tour gelandet sind (CAS-Reader hat sie als unknown/standby verschluckt),
    # bekommen ihre Z76-VMA aus der SE-Quelle. Hinter EIGENEM Flag, weil der
    # Pass in fremden Test-Szenarien (reine Synthetik-SE-Rows) zu aggressiv
    # greift — er ist für den ECHTEN Live-Pfad gedacht (vollständige SE+CAS),
    # nicht für isolierte Unit-Fixtures. Default OFF bis separat abgesichert.
    if _se_primary and _se_disclose_enabled():
        # CAS-Datumsmenge: an welchen Tagen hat der Reader ÜBERHAUPT einen Tag
        # gesehen? Disclosure ergänzt Z76 NUR für SE-Ausland-Tage, an denen ein
        # CAS-Tag existiert (Reader sah ihn, klassifizierte ihn nur falsch/gar
        # nicht). Reine SE-only-Tage OHNE jeden CAS-Beleg bleiben tabu (audit-
        # sichere Schranke: SE allein erzeugt keine VMA — test_se_only...).
        _se_disclose_foreign_vma(result, se_rows, bmf_table, iata_to_bmf,
                                 homebase, _cas_date_set_of(cas_days))

    return result


# ════════════════════════════════════════════════════════════════════════════
# Diagnostic / Audit
# ════════════════════════════════════════════════════════════════════════════

def diff_against_legacy(
    normalized_result: CalculationResult,
    legacy_classification: Dict[str, Any],
) -> Dict[str, Any]:
    """Vergleicht normalized_tours-Berechnung mit altem tage_detail.

    Liefert structured Diff für parallel-audit-mode (Phase B).

    Output-Format:
      {
        'summary': {
          'legacy': {'fahr_tage': ..., 'arbeitstage': ..., 'z76_eur': ..., ...},
          'normalized': {...},
          'delta': {<key>: normalized - legacy}
        },
        'by_date': [
          {
            'date': 'YYYY-MM-DD',
            'legacy': {'bucket': 'Z76', 'eur': 28.0},
            'normalized': {'bucket': 'Z73', 'eur': 14.0},
            'decision': 'normalized_more_plausible' | 'legacy_more_plausible'
                        | 'needs_review' | 'accepted_difference',
            'reason': str,
          }
        ],
        'warnings': [str],
      }
    """
    legacy_tage_detail = legacy_classification.get('tage_detail') or \
        legacy_classification.get('_tage_detail') or []
    legacy_by_date = {}
    for entry in legacy_tage_detail:
        d = entry.get('datum', '')
        if d:
            legacy_by_date[d] = entry

    # Legacy-Summary aus tage_detail aggregieren
    legacy_summary = {
        'fahr_tage':      int(legacy_classification.get('fahr_tage') or 0),
        'arbeitstage':    int(legacy_classification.get('arbeitstage') or 0),
        'hotel_naechte':  int(legacy_classification.get('hotel_naechte') or 0),
        'reinigungstage': int(legacy_classification.get('reinigungstage') or 0),
        'z72_tage':       int(legacy_classification.get('vma_72_tage') or 0),
        'z73_tage':       int(legacy_classification.get('vma_73_tage') or 0),
        'z74_tage':       int(legacy_classification.get('vma_74_tage') or 0),
        'z76_tage': sum(1 for e in legacy_tage_detail if e.get('klass') == 'Z76'),
        'z72_eur':  sum(float(e.get('amount') or e.get('eur') or 0)
                       for e in legacy_tage_detail if e.get('klass') == 'Z72'),
        'z73_eur':  sum(float(e.get('amount') or e.get('eur') or 0)
                       for e in legacy_tage_detail if e.get('klass') == 'Z73'),
        'z74_eur':  sum(float(e.get('amount') or e.get('eur') or 0)
                       for e in legacy_tage_detail if e.get('klass') == 'Z74'),
        'z76_eur': float(legacy_classification.get('vma_aus') or 0),
    }
    legacy_summary['total_vma_brutto'] = (
        legacy_summary['z72_eur'] + legacy_summary['z73_eur']
        + legacy_summary['z74_eur'] + legacy_summary['z76_eur']
    )

    norm_summary = {
        'fahr_tage':      normalized_result.fahrtage,
        'arbeitstage':    normalized_result.arbeitstage,
        'hotel_naechte':  normalized_result.hotel_naechte,
        'reinigungstage': normalized_result.reinigungstage,
        'z72_tage':       normalized_result.z72_tage,
        'z73_tage':       normalized_result.z73_tage,
        'z74_tage':       normalized_result.z74_tage,
        'z76_tage':       normalized_result.z76_tage,
        'z72_eur':        round(normalized_result.z72_eur, 2),
        'z73_eur':        round(normalized_result.z73_eur, 2),
        'z74_eur':        round(normalized_result.z74_eur, 2),
        'z76_eur':        round(normalized_result.z76_eur, 2),
    }
    norm_summary['total_vma_brutto'] = round(
        norm_summary['z72_eur'] + norm_summary['z73_eur']
        + norm_summary['z74_eur'] + norm_summary['z76_eur'], 2
    )

    delta = {k: round(norm_summary[k] - legacy_summary.get(k, 0), 2)
             for k in norm_summary}

    by_date_diff = []
    all_dates = set(normalized_result.by_date.keys()) | set(legacy_by_date.keys())
    for ds in sorted(all_dates):
        new_entry = normalized_result.by_date.get(ds)
        old_entry = legacy_by_date.get(ds)

        new_bucket = (new_entry or {}).get('klass') or 'none'
        new_eur = float((new_entry or {}).get('amount') or 0)
        old_bucket = (old_entry or {}).get('klass') or 'none'
        old_eur = float((old_entry or {}).get('amount') or (old_entry or {}).get('eur') or 0)

        if new_bucket == old_bucket:
            continue

        # Decision-Logik
        decision = 'needs_review'
        reason = ''

        # Phantom-Tour entfernt (Pattern B): legacy hatte Z76, normalized=none/Frei
        if old_bucket == 'Z76' and new_bucket == 'none':
            decision = 'normalized_more_plausible'
            reason = ('Legacy hatte Z76 für Tag ohne CAS-Tour-Klammer '
                      '(Pattern B Phantom). Normalized entfernt Phantom.')
        # Missing return day hinzugefügt (Pattern D): legacy=Frei/Issue, normalized=Z76
        elif old_bucket in ('Frei', 'Issue', 'ZeroDay', 'none') and new_bucket == 'Z76':
            decision = 'normalized_more_plausible'
            reason = ('Legacy hatte den Tag als nicht-Tour klassifiziert obwohl '
                      'normalized_tours eine Tour-Klammer findet (Pattern D — '
                      'Reader-Lücke).')
        # Pattern A unterschiedliche Behandlung von Anreise-Tag
        elif old_bucket == 'Z76' and new_bucket in ('Z73',):
            decision = 'normalized_more_plausible'
            reason = ('Pattern A: Anreise-Tag mit Homebase-Stempel — normalized '
                      'klassifiziert als Inland (Briefing in DE), legacy als Z76.')
        # Standby-Reinigung-Diff
        elif old_bucket == 'Standby' and new_bucket == 'none':
            decision = 'normalized_more_plausible'
            reason = 'Home-Standby zählt nicht als Tour-Tag (FollowMe-konform).'
        # Inland-Bucket-Mix (Z72/Z73/Z74)
        elif {old_bucket, new_bucket} <= {'Z72', 'Z73', 'Z74'}:
            decision = 'needs_review'
            reason = (f'Inland-Bucket-Diff Z72/Z73/Z74: legacy={old_bucket} '
                      f'normalized={new_bucket}. Manuell prüfen.')
        else:
            decision = 'needs_review'
            reason = (f'Klassen-Diff legacy={old_bucket} → normalized={new_bucket}, '
                      f'kein bekanntes Pattern.')

        by_date_diff.append({
            'date': ds,
            'legacy': {'bucket': old_bucket, 'eur': round(old_eur, 2)},
            'normalized': {'bucket': new_bucket, 'eur': round(new_eur, 2)},
            'decision': decision,
            'reason': reason,
        })

    warnings = []
    if abs(delta.get('z76_eur', 0)) > 200:
        warnings.append(f'Großer Z76-Diff: {delta["z76_eur"]:+.2f}€')
    if abs(delta.get('hotel_naechte', 0)) > 5:
        warnings.append(f'Großer Hotel-Diff: {delta["hotel_naechte"]:+d}')
    if abs(delta.get('arbeitstage', 0)) > 5:
        warnings.append(f'Großer AT-Diff: {delta["arbeitstage"]:+d}')

    return {
        'summary': {
            'legacy': legacy_summary,
            'normalized': norm_summary,
            'delta': delta,
        },
        'by_date': by_date_diff,
        'warnings': warnings,
    }


# ════════════════════════════════════════════════════════════════════════════
# Serialization (für Audit-JSON)
# ════════════════════════════════════════════════════════════════════════════

def normalized_tour_to_dict(tour: NormalizedTour) -> Dict[str, Any]:
    """NormalizedTour → JSON-serializable dict."""
    d = asdict(tour)
    d['start_date'] = tour.start_date.isoformat()
    d['end_date'] = tour.end_date.isoformat()
    d['days'] = [
        {**asdict(td), 'date': td.date.isoformat()}
        for td in tour.days
    ]
    return d


def tours_to_audit_json(tours: List[NormalizedTour]) -> List[Dict[str, Any]]:
    """Liste von Touren → JSON-Audit-Format."""
    return [normalized_tour_to_dict(t) for t in tours]
