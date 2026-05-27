"""Classifier V2 — Cleane Architektur (R40, 2026-05-27).

5 reine Funktionen ersetzen ~80 Rescue-Patches in app.py:

  classify_marker(marker_raw, cas_fields) → MarkerKind
  build_tours(sorted_days)                → list[Tour]
  day_role_in_tour(day, tour)             → DayRole
  resolve_country(day, tour, se_rows, …)  → CountryResult
  is_hotel_night(day, tour, country)      → (bool, reason)

  classify_day(day, tour, country, hotel) → DayClassification  # composition

Architektur-Prinzip:
- Marker liefert nur Hilfssignal (KIND), die finale Klasse kommt aus
  CAS-Feldern + Tour-Kontext (Activity-First)
- STRICT_PASSIVE-Marker (LMN_HT*, ORTSTAG, OFF) sind die Ausnahme:
  hier trägt der Marker Wissen das nicht in CAS-Feldern steht
- BMF-Compliance: Z72 nur bei echter Auswärtstätigkeit (nicht am HB)

Status: parallel zum Legacy-Pfad. Wird via Audit-Diff verifiziert bevor
produktiv geschaltet (siehe docs/CLASSIFIER_V2_SPEC.md).

Spec: docs/CLASSIFIER_V2_SPEC.md
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


# ════════════════════════════════════════════════════════════════════════════
# Enums + Datenmodell
# ════════════════════════════════════════════════════════════════════════════

class MarkerKind(Enum):
    """Klassifikation eines CAS-Markers — Hilfssignal, nicht final."""
    STRICT_PASSIVE   = 'strict_passive'      # LMN_HT*, ORTSTAG, OFF — immer Frei
    FLEXIBLE_PASSIVE = 'flexible_passive'    # FRS, FRD, LMN_AS, LMN_CR — Frei nur ohne Felder
    STANDBY_HOME     = 'standby_home'        # SB_S, SB_M, RB
    STANDBY_AIRPORT  = 'standby_airport'     # SB_F, SBA, SBY, RES, RES_SB
    TRAINING         = 'training'            # EM, EH, D4, etc — Schulung
    FLIGHT           = 'flight'              # LH123, Flugnummern, IATA-Codes
    UNKNOWN          = 'unknown'             # alles andere


class DayRole(Enum):
    """Rolle eines Tages innerhalb einer Tour."""
    DEPARTURE        = 'departure'
    MID_FULL_AWAY    = 'mid_full_away'
    RETURN           = 'return'
    SAME_DAY_INLAND  = 'same_day_inland'
    STANDBY_AIRPORT  = 'standby_airport'
    OFFICE_AT_HB     = 'office_at_hb'


@dataclass
class CountryResult:
    country: Optional[str] = None
    iata: Optional[str] = None
    source: str = 'none'
    is_foreign: bool = False


@dataclass
class Tour:
    days: List[Dict[str, Any]] = field(default_factory=list)
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    foreign_country: Optional[str] = None
    has_overnight: bool = False
    tour_id: str = ''


@dataclass
class DayClassification:
    klass: str           # 'Frei'|'Z72'|'Z73'|'Z74'|'Z76'|'Office'|'Standby'|'Issue'
    eur: float = 0.0
    rate_type: str = 'none'
    country: Optional[str] = None
    is_hotel_night: bool = False
    is_fahrtag: bool = False
    is_arbeitstag: bool = False
    is_reinigungstag: bool = False
    reason: str = ''
    source: str = 'none'


# ════════════════════════════════════════════════════════════════════════════
# Konstanten — KEINE Marker-Listen mehr für Klassifikations-Entscheidungen,
# nur für die initiale Marker-Klassifikation in classify_marker
# ════════════════════════════════════════════════════════════════════════════

# STRICT_PASSIVE: Marker trägt Wissen über Home-Lokation, das nicht in
# CAS-Feldern steht. Auch mit duty/start_time → trotzdem Frei (Online-WBT).
_STRICT_PASSIVE_PREFIXES = (
    'LMN_HT', 'LMN_AD', 'LMN_AL', 'LMN_DS', 'LMN_FT',
)
_STRICT_PASSIVE_EXACT = {
    'ORTSTAG', 'OFF', 'OF', 'URLAUB', 'U', 'U1', 'U2', 'K', 'KRANK',
    'FREI', 'FREE',
}

# FLEXIBLE_PASSIVE: Default Frei, aber bei klaren CAS-Feldern (Briefing-Zeit
# o.ä.) kann es ein echter Standort-Termin sein → Felder gewinnen.
_FLEXIBLE_PASSIVE_EXACT = {
    'FRS', 'FRD', 'FRN', 'LMN_AS', 'LMN_CR', 'LMN_OD',
}

# Standby-Klassen
_STANDBY_HOME_EXACT = {'SB_S', 'SB_M', 'RB'}
_STANDBY_AIRPORT_EXACT = {'SB_F', 'SBA', 'SBY', 'RES', 'RES_SB'}

# Training-Prefixe
_TRAINING_PREFIXES = ('EM', 'EH', 'EK', 'D4', 'DD', 'TK', 'SM', 'SIM',
                     'EMCRM', 'SECCRM', 'TRI', 'TRE')

# Inland-IATA (deutsche Flughäfen)
_INLAND_IATAS = {
    'FRA', 'MUC', 'DUS', 'TXL', 'BER', 'HAM', 'STR', 'CGN', 'HAJ', 'NUE',
    'LEJ', 'BRE', 'DRS', 'PAD', 'FMM', 'FMO', 'SCN', 'FKB', 'FDH', 'NRN',
}


# ════════════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════════════

def _is_inland_iata(iata: str) -> bool:
    return bool(iata) and iata.upper().strip() in _INLAND_IATAS


def _normalize_marker(marker_raw: str) -> str:
    """Normalisiert Marker für Match: upper, strip, keine Suffixe wie /1."""
    if not marker_raw or not isinstance(marker_raw, str):
        return ''
    m = marker_raw.upper().strip()
    # Day-Suffix -1, -2 etc abschneiden (z.B. LMN_HT-1 → LMN_HT)
    for sep in ('-1', '-2', '-3', '/1', '/2'):
        if m.endswith(sep):
            m = m[: -len(sep)]
            break
    return m


def _cas_fields_are_empty(cas: Dict[str, Any]) -> bool:
    """True wenn alle aktiven CAS-Felder leer sind."""
    if not cas:
        return True
    duty = int(cas.get('duty_duration_minutes') or 0)
    start = (cas.get('start_time') or '').strip()
    end = (cas.get('end_time') or '').strip()
    routing = cas.get('routing') or []
    overnight = bool(cas.get('overnight_after_day'))
    layover = (cas.get('layover_ort') or '').strip()
    return (
        duty == 0 and not start and not end and not overnight
        and not layover and not (routing if isinstance(routing, list) else [])
    )


def _has_foreign_iata_in_routing(routing: Any, homebase: str) -> bool:
    if not isinstance(routing, list):
        return False
    hb = (homebase or 'FRA').upper()
    for r in routing:
        if not isinstance(r, str):
            continue
        u = r.upper().strip()
        if len(u) == 3 and u.isalpha() and u != hb and not _is_inland_iata(u):
            return True
    return False


def _has_flight_token_in_routing(routing: Any) -> bool:
    """True wenn ein Token im Routing wie eine Flugnummer aussieht."""
    if not isinstance(routing, list):
        return False
    for r in routing:
        if not isinstance(r, str):
            continue
        u = r.upper().strip()
        if u.startswith('LH'):
            return True
        digits = ''.join(c for c in u if c.isdigit())
        if len(digits) >= 3 and len([c for c in u if c.isalpha()]) <= 2:
            return True
    return False


# ════════════════════════════════════════════════════════════════════════════
# Regel 1: classify_marker
# ════════════════════════════════════════════════════════════════════════════

def classify_marker(marker_raw: str, cas_fields: Optional[Dict[str, Any]] = None) -> MarkerKind:
    """Klassifiziert einen Marker in eine Kategorie.

    Hilfssignal — die finale Tag-Klasse kommt aus Tour-Kontext + CAS-Feldern.
    cas_fields wird hier nicht zwingend gebraucht, aber für die Signatur-
    Konsistenz mitgeführt (z.B. für zukünftige Disambiguierung).
    """
    m = _normalize_marker(marker_raw)
    if not m:
        return MarkerKind.UNKNOWN

    # STRICT_PASSIVE — Prefix oder exact
    if any(m.startswith(p) for p in _STRICT_PASSIVE_PREFIXES):
        return MarkerKind.STRICT_PASSIVE
    if m in _STRICT_PASSIVE_EXACT:
        return MarkerKind.STRICT_PASSIVE

    # FLEXIBLE_PASSIVE
    if m in _FLEXIBLE_PASSIVE_EXACT:
        return MarkerKind.FLEXIBLE_PASSIVE

    # Standby
    if m in _STANDBY_HOME_EXACT:
        return MarkerKind.STANDBY_HOME
    if m in _STANDBY_AIRPORT_EXACT:
        return MarkerKind.STANDBY_AIRPORT

    # Training
    if any(m.startswith(p) for p in _TRAINING_PREFIXES):
        return MarkerKind.TRAINING

    # Flight (Flugnummer-Heuristik)
    if m.startswith('LH'):
        return MarkerKind.FLIGHT
    digits = ''.join(c for c in m if c.isdigit())
    if len(digits) >= 3 and len([c for c in m if c.isalpha()]) <= 2:
        return MarkerKind.FLIGHT

    return MarkerKind.UNKNOWN


# ════════════════════════════════════════════════════════════════════════════
# Regel 2: build_tours
# ════════════════════════════════════════════════════════════════════════════

def build_tours(sorted_days: List[Dict[str, Any]], homebase: str = 'FRA') -> List[Tour]:
    """Klammert Tour-Cluster aus CAS-Tagen.

    Eine Tour braucht mindestens ein Tour-Signal:
      - foreign-IATA in routing
      - layover_iata foreign
      - overnight=True UND nächste Tage foreign-signal
      - same-day-inland mit Flight-Token + duty>=480

    STRICT_PASSIVE/STANDBY_HOME/Frei-activity_type sind außerhalb Touren.
    Heimkehr-Tag (prev_overnight + ends_at_homebase + no_overnight) wird
    in die Vortag-Tour aufgenommen (Last-Day), auch wenn eigene Felder dünn.
    """
    if not sorted_days:
        return []
    tours: List[Tour] = []
    current: List[Dict[str, Any]] = []
    hb_up = (homebase or 'FRA').upper()
    tour_counter = 0

    def _flush():
        nonlocal current, tour_counter
        if not current:
            return
        # R40 fix (2026-05-27): eine echte Tour MUSS mindestens 1 Tag mit
        # AKTIVEM Signal haben: overnight=True ODER start_time ODER duty>0.
        # Reine Layover-/Routing-Stempel-Leichen (Reader liefert layover=SFO
        # für mehrere Tage NACH der Heimkehr ohne neue Aktivität) sind KEINE
        # neue Tour. Diese Edge-Case-Heilung schließt 1-Tages-Phantom-Touren.
        has_active_signal = any(
            bool(d.get('overnight_after_day'))
            or (d.get('start_time') or '').strip()
            or int(d.get('duty_duration_minutes') or 0) > 0
            for d in current
        )
        if not has_active_signal:
            current = []
            return
        # Plus: alter Check für irgendeine Foreign-/Flight-Evidence
        has_signal = any(
            _has_foreign_iata_in_routing(d.get('routing'), hb_up)
            or (d.get('layover_ort') and not _is_inland_iata(d.get('layover_ort','')) and d.get('layover_ort','').upper() != hb_up)
            or bool(d.get('overnight_after_day'))
            or (_has_flight_token_in_routing(d.get('routing')) and int(d.get('duty_duration_minutes') or 0) >= 480)
            for d in current
        )
        if not has_signal:
            current = []
            return
        tour_counter += 1
        try:
            sd = datetime.strptime(current[0].get('datum','')[:10], '%Y-%m-%d').date()
            ed = datetime.strptime(current[-1].get('datum','')[:10], '%Y-%m-%d').date()
        except Exception:
            sd = ed = None
        tours.append(Tour(
            days=list(current),
            start_date=sd, end_date=ed,
            has_overnight=any(d.get('overnight_after_day') for d in current),
            tour_id=f't{tour_counter}',
        ))
        current = []

    for i, day in enumerate(sorted_days):
        marker_raw = day.get('marker_raw') or day.get('marker') or ''
        kind = classify_marker(marker_raw, day)
        activity = (day.get('activity_type') or '').lower()
        cas_empty = _cas_fields_are_empty(day)

        # R40 fix (2026-05-27): activity_type='frei' ist nur dann wirklich
        # Frei, wenn KEINE Mid-Tour-Signale präsent sind. Reader stempelt
        # gerne Marker `X`, `===` etc. als activity='frei', auch wenn der
        # Tag overnight=True ODER ein Foreign-Layover-Ort hat (Mid-Tour-Layover).
        # Solche Tage gehören weiter in die Tour-Klammer.
        _ov_now = bool(day.get('overnight_after_day'))
        _lay_now = (day.get('layover_ort') or '').upper().strip()
        _has_foreign_lay = _lay_now and not _is_inland_iata(_lay_now) and _lay_now != hb_up
        is_free_activity = (
            activity in ('frei', 'urlaub', 'krank', 'off')
            and not _ov_now
            and not _has_foreign_lay
        )

        # Außerhalb-Tour-Konditionen
        is_strict_passive = (kind == MarkerKind.STRICT_PASSIVE)
        is_flex_passive_empty = (kind == MarkerKind.FLEXIBLE_PASSIVE and cas_empty)
        is_standby_home = (kind == MarkerKind.STANDBY_HOME)

        if is_strict_passive or is_flex_passive_empty or is_standby_home or is_free_activity:
            _flush()
            continue

        # R40 fix (2026-05-27): Mid-Tour-Klammer. Wenn der vorige Tag in der
        # offenen Tour overnight_after_day=True hatte UND kein explicit-passive
        # Marker existiert, ist DIESER Tag automatisch Mid-Tour (Layover-Tag).
        # Reader-Felder können beim Mid-Tour-Tag lückenhaft sein (kein duty,
        # kein start_time) — das darf die Tour nicht zerbrechen.
        #
        # AUSNAHME: starts_at_homebase=True am aktuellen Tag UND der Tag hat
        # selbst echtes Tour-Signal (foreign-routing/overnight) bedeutet:
        # Heimkehr passierte nachts (Reader hat Tour-Grenze nicht erfasst),
        # DIESER Tag startet eine NEUE Tour.
        _routing_now = day.get('routing') or []
        _ov_curr = bool(day.get('overnight_after_day'))
        _lay_curr = (day.get('layover_ort') or '').upper().strip()
        _has_fr_curr = _has_foreign_iata_in_routing(_routing_now, hb_up)
        _has_fl_curr = _lay_curr and not _is_inland_iata(_lay_curr) and _lay_curr != hb_up
        _has_own_tour_signal = _has_fr_curr or _has_fl_curr or _ov_curr
        # Eine "neue Tour von HB" startet nur wenn routing[0] == HB. Wenn
        # routing[0] foreign ist (XXX-FRA), ist das ein Heimkehrtag der
        # Vortags-Tour, kein Same-Day-Start.
        _route_starts_hb = (
            len(_routing_now) > 0
            and (_routing_now[0] or '').upper().strip() == hb_up
        )
        _starts_new_at_hb = (
            bool(day.get('starts_at_homebase'))
            and _has_own_tour_signal
            and _route_starts_hb
        )

        if (current
                and bool(current[-1].get('overnight_after_day'))
                and not is_strict_passive
                and not _starts_new_at_hb):
            current.append(day)
            ends_hb_now = bool(day.get('ends_at_homebase'))
            if ends_hb_now and not _ov_curr:
                _flush()
            continue
        if (current
                and bool(current[-1].get('overnight_after_day'))
                and _starts_new_at_hb):
            _flush()

        # In-Tour-Signale
        routing = day.get('routing') or []
        overnight = bool(day.get('overnight_after_day'))
        layover = (day.get('layover_ort') or '').upper().strip()
        ends_hb = bool(day.get('ends_at_homebase'))

        has_foreign_routing = _has_foreign_iata_in_routing(routing, hb_up)
        has_foreign_layover = layover and not _is_inland_iata(layover) and layover != hb_up
        has_flight = _has_flight_token_in_routing(routing)
        duty = int(day.get('duty_duration_minutes') or 0)
        same_day_inland = (bool(day.get('starts_at_homebase')) and ends_hb
                          and has_flight and duty >= 480)

        # Heimkehr-Tag: prev_overnight + ends_hb + no_overnight → in current
        is_heimkehr = (
            current
            and bool(current[-1].get('overnight_after_day'))
            and ends_hb
            and not overnight
        )

        # R40 V2 fix (2026-05-27): foreign-routing ALLEIN reicht NICHT für
        # neue Tour. Reader hinterlässt manchmal Foreign-IATA-Stempel auf
        # Folgetagen einer beendeten Tour (z.B. 04.-06.04 mit routing=US
        # nach 03.04 Heimkehr). Diese Phantom-„Touren" verfälschen Zähler.
        # Korrektur: foreign-routing braucht zusätzliches Signal:
        #   - Flight-Token im routing ODER
        #   - overnight=True ODER
        #   - foreign-Layover-Ort
        has_tour_signal = (
            has_foreign_routing or has_foreign_layover or overnight
            or same_day_inland or is_heimkehr
        )

        if has_tour_signal:
            current.append(day)
            # Heimkehr beendet die Tour
            if ends_hb and not overnight:
                _flush()
        else:
            # Tour-Tag ohne klares Signal → wenn current existiert
            # könnte es Mid-Tour-X-Tag sein, sonst flush
            if current and not cas_empty:
                # Mid-Tour-Tag mit dünnen Feldern aber innerhalb Tour-Klammer
                current.append(day)
            else:
                _flush()

    _flush()
    return tours


# ════════════════════════════════════════════════════════════════════════════
# Regel 3: day_role_in_tour
# ════════════════════════════════════════════════════════════════════════════

def day_role_in_tour(day: Dict[str, Any], tour: Tour, homebase: str = 'FRA') -> DayRole:
    """Bestimmt die Rolle eines Tages in seiner Tour."""
    days = tour.days
    if not days:
        return DayRole.MID_FULL_AWAY  # fallback

    idx = next((i for i, d in enumerate(days)
                if d.get('datum') == day.get('datum')), -1)
    if idx == -1:
        return DayRole.MID_FULL_AWAY

    hb_up = (homebase or 'FRA').upper()
    starts_hb = bool(day.get('starts_at_homebase'))
    ends_hb = bool(day.get('ends_at_homebase'))
    overnight = bool(day.get('overnight_after_day'))
    duty = int(day.get('duty_duration_minutes') or 0)
    routing = day.get('routing') or []
    has_flight = _has_flight_token_in_routing(routing)
    marker_kind = classify_marker(day.get('marker_raw') or day.get('marker') or '', day)

    # 1-Tages-Tour, Same-Day-Inland
    if len(days) == 1 and starts_hb and ends_hb and has_flight and duty >= 480:
        return DayRole.SAME_DAY_INLAND

    # Standby-Airport innerhalb Tour
    if marker_kind == MarkerKind.STANDBY_AIRPORT:
        return DayRole.STANDBY_AIRPORT

    # First Day
    if idx == 0:
        return DayRole.DEPARTURE

    # Last Day mit Heimkehr-Pattern
    if idx == len(days) - 1 and ends_hb and not overnight:
        return DayRole.RETURN

    # OFFICE_AT_HB nur bei isolierten 1-Tages-„Touren" mit Office-Charakter.
    # Mid-Tour-Tage MIT overnight-Nachbarn sind IMMER MID_FULL_AWAY — auch
    # wenn der Reader fälschlich starts/ends_at_homebase=True gesetzt hat
    # (Reader-Stochastik bei Mid-Tour-X-Tagen).
    prev_overnight = (idx > 0 and bool(days[idx-1].get('overnight_after_day')))
    if (len(days) == 1 and starts_hb and ends_hb and not overnight
        and not _has_foreign_iata_in_routing(routing, hb_up)
        and not prev_overnight):
        return DayRole.OFFICE_AT_HB

    # Mid-Tour
    return DayRole.MID_FULL_AWAY


# ════════════════════════════════════════════════════════════════════════════
# Regel 4: resolve_country
# ════════════════════════════════════════════════════════════════════════════

def resolve_country(
    day: Dict[str, Any],
    tour: Optional[Tour],
    se_rows: List[Dict[str, Any]],
    iata_to_bmf: Optional[Dict[str, str]] = None,
    homebase: str = 'FRA',
) -> CountryResult:
    """Resolves BMF-Country aus Source-Hierarchie:
      1. SE.stfrei_ort am gleichen Datum (Foreign)
      2. day.layover_iata (Foreign)
      3. day.target_iata (Foreign, aus routing)
      4. tour-neighbor.layover (für Mid-Tour-Reader-Lücken)
      5. routing-tokens (3-Letter-IATA, Foreign)
      6. Nichts → CountryResult(None, None, 'missing')
    """
    iata_to_bmf = iata_to_bmf or {}
    hb_up = (homebase or 'FRA').upper()
    day_date_str = day.get('datum', '')

    def _is_foreign(iata: str) -> bool:
        if not iata or not isinstance(iata, str):
            return False
        u = iata.upper().strip()
        return (len(u) == 3 and u.isalpha() and u != hb_up
                and not _is_inland_iata(u))

    def _make_result(iata: str, source: str) -> Optional[CountryResult]:
        u = iata.upper().strip()
        if not _is_foreign(u):
            return None
        country = iata_to_bmf.get(u)
        if not country:
            return None
        return CountryResult(country=country, iata=u, source=source, is_foreign=True)

    # 1. SE
    for se in (se_rows or []):
        if (se.get('datum') or se.get('date')) != day_date_str:
            continue
        if se.get('storno'):
            continue
        r = _make_result((se.get('stfrei_ort') or '').strip(), 'SE')
        if r:
            return r

    # 2. day.layover_iata
    layover = (day.get('layover_ort') or '').strip()
    if layover:
        r = _make_result(layover, 'CAS.layover')
        if r:
            return r

    # 3. day.target_iata (aus routing — erstes Foreign-Token)
    routing = day.get('routing') or []
    for tok in (routing if isinstance(routing, list) else []):
        r = _make_result(tok, 'CAS.routing_target')
        if r:
            return r

    # 4. Tour-Neighbor-Layover
    if tour:
        for nb in tour.days:
            if nb.get('datum') == day_date_str:
                continue
            nb_layover = (nb.get('layover_ort') or '').strip()
            if nb_layover:
                r = _make_result(nb_layover, 'CAS.tour_neighbor_layover')
                if r:
                    return r

    return CountryResult(country=None, iata=None, source='missing', is_foreign=False)


# ════════════════════════════════════════════════════════════════════════════
# Regel 5: is_hotel_night
# ════════════════════════════════════════════════════════════════════════════

def is_hotel_night(
    day: Dict[str, Any],
    tour: Optional[Tour],
    country: CountryResult,
    role: Optional[DayRole] = None,
) -> Tuple[bool, str]:
    """Hotel-Nacht wenn:
      1. overnight_after_day=True
      2. Tour-Tag mit Rolle ∈ {DEPARTURE, MID_FULL_AWAY}
      3. Country aus Resolver ist Foreign
      4. NICHT Standby-Home
    """
    if not day.get('overnight_after_day'):
        return (False, 'no_overnight')
    if not tour:
        return (False, 'not_in_tour')
    if not country.is_foreign:
        return (False, 'country_not_foreign')

    marker_kind = classify_marker(day.get('marker_raw') or day.get('marker') or '', day)
    if marker_kind == MarkerKind.STANDBY_HOME:
        return (False, 'standby_home')

    if role is None:
        role = day_role_in_tour(day, tour)

    if role in (DayRole.RETURN, DayRole.SAME_DAY_INLAND, DayRole.OFFICE_AT_HB):
        return (False, f'role_{role.value}')

    return (True, 'ok')
