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
    klass: str           # 'Frei'|'Z72'|'Z73'|'Z74'|'Z76'|'Office'|'Standby'|'ZeroDay'|'Issue'
    eur: float = 0.0
    rate_type: str = 'none'  # 'voll_24h'|'an_abreise'|'tagestrip_8h'|'none'|'unknown'
    country: Optional[str] = None
    bmf_land: Optional[str] = None
    bmf_tagtyp: Optional[str] = None
    is_hotel_night: bool = False
    is_fahrtag: bool = False
    is_arbeitstag: bool = False
    is_reinigungstag: bool = False
    reason: str = ''
    source: str = 'none'
    warnings: List[str] = field(default_factory=list)


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

    # 3. day.target_iata (aus routing)
    # Strategie:
    #   - kein overnight (Same-Day-Tag): LETZTER foreign-IATA = Tagesziel
    #     z.B. ['FRA','MXP','GVA'] → GVA (Schweiz), nicht MXP (Italien transit)
    #   - mit overnight: ERSTER foreign-IATA = Anreise-Ziel (Layover dürfte
    #     Hauptquelle sein, das hier ist Fallback)
    routing = day.get('routing') or []
    if isinstance(routing, list) and routing:
        order = list(reversed(routing)) if not day.get('overnight_after_day') else list(routing)
        for tok in order:
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


# ════════════════════════════════════════════════════════════════════════════
# Regel 6: classify_day — finale Tag-Klassifikation (Z72/Z73/Z74/Z76/...)
# ════════════════════════════════════════════════════════════════════════════

# BMF-Inland-Pauschalen (§9 Abs. 4a EStG, gültig 2023-2026).
_BMF_INLAND = {
    'an_abreise': 14.0,
    'tagestrip_8h': 14.0,
    'voll_24h': 28.0,
}


def classify_day(
    day: Dict[str, Any],
    tour: Optional[Tour],
    country: CountryResult,
    hotel_info: Optional[Tuple[bool, str]] = None,
    bmf_auslandj: Optional[Dict[str, Tuple[float, float]]] = None,
    homebase: str = 'FRA',
) -> DayClassification:
    """Klassifiziert einen Tag final in Z72/Z73/Z74/Z76/Frei/Office/Standby/Issue.

    Verantwortung:
      - kein Tour ⇒ Frei/Office/Standby/Issue je nach Marker/Aktivität
      - Tour-Tag ⇒ DayRole + Country → Z-Code + Pauschale

    BMF-Compliance (CLAUDE.md R39):
      - Z72 nur bei echter Auswärtstätigkeit am HB (Same-Day-Inland >8h)
      - Schulung am HB ohne Foreign-Routing → Office (erste Tätigkeitsstätte)
      - Standby (auch aktiviert) → Standby/Z73 nur wenn Auslands-Aktivierung
        in den Reader-Fakten erscheint

    Args:
      day:         Reader-Tag-Dict (datum, marker_raw, routing, layover_ort,
                   overnight_after_day, starts_at_homebase, ends_at_homebase,
                   duty_duration_minutes, start_time, end_time, activity_type)
      tour:        Tour aus build_tours() oder None
      country:     CountryResult aus resolve_country()
      hotel_info:  (is_hotel_night, reason) aus is_hotel_night() — optional
      bmf_auslandj: dict country_key → (voll_24h, an_abreise) für das Jahr.
                   Wenn None: nur Inland-Klassifikation, Ausland-Tage werden
                   Z76 mit eur=0 markiert + warning.
      homebase:    IATA-Code

    Returns:
      DayClassification(klass, eur, rate_type, reason, ...)
    """
    marker_raw = day.get('marker_raw') or day.get('marker') or ''
    marker_kind = classify_marker(marker_raw, day)
    activity = (day.get('activity_type') or '').lower()
    starts_hb = bool(day.get('starts_at_homebase'))
    ends_hb = bool(day.get('ends_at_homebase'))
    overnight = bool(day.get('overnight_after_day'))
    duty = int(day.get('duty_duration_minutes') or 0)
    routing = day.get('routing') or []
    hb_up = (homebase or 'FRA').upper()
    has_foreign_routing = _has_foreign_iata_in_routing(routing, hb_up)
    has_flight = _has_flight_token_in_routing(routing)
    layover = (day.get('layover_ort') or '').upper().strip()
    has_foreign_layover = (
        layover
        and not _is_inland_iata(layover)
        and layover != hb_up
    )

    # ────────────────────────────────────────────────────────────────────
    # Stage 1: Nicht-Tour-Tage
    # ────────────────────────────────────────────────────────────────────
    if tour is None:
        if marker_kind == MarkerKind.STRICT_PASSIVE:
            return DayClassification(klass='Frei', reason='strict_passive_marker')
        if activity in ('frei', 'urlaub', 'krank', 'off') and not (overnight or has_foreign_layover):
            return DayClassification(klass='Frei', reason='activity_frei')
        if marker_kind == MarkerKind.FLEXIBLE_PASSIVE and _cas_fields_are_empty(day):
            return DayClassification(klass='Frei', reason='flexible_passive_empty')
        if marker_kind == MarkerKind.STANDBY_HOME:
            return DayClassification(klass='Standby', reason='standby_home')
        if marker_kind == MarkerKind.STANDBY_AIRPORT:
            return DayClassification(klass='Standby', reason='standby_airport_no_activation')
        if marker_kind == MarkerKind.TRAINING and starts_hb and ends_hb and not has_foreign_routing:
            # Schulung am HB: erste Tätigkeitsstätte (BMF R39) → kein Z72.
            if duty >= 480:
                return DayClassification(
                    klass='Office', reason='training_at_hb_8h_no_z72_bmf_r39')
            return DayClassification(klass='Office', reason='training_at_hb_short')
        if duty == 0 and not (day.get('start_time') or '').strip():
            return DayClassification(klass='ZeroDay', reason='no_duty_no_signal')
        # Office am HB ohne foreign-Routing, >0 duty
        if starts_hb and ends_hb and not has_foreign_routing and not overnight:
            return DayClassification(klass='Office', reason='office_at_hb')
        return DayClassification(klass='Issue', reason='no_tour_no_clear_pattern')

    # ────────────────────────────────────────────────────────────────────
    # Stage 2: Tour-Tag
    # ────────────────────────────────────────────────────────────────────
    role = day_role_in_tour(day, tour, homebase)

    # SAME_DAY_INLAND ohne foreign-Routing: Inland-Same-Day-Tour
    if role == DayRole.SAME_DAY_INLAND:
        if has_foreign_routing or has_foreign_layover:
            # technisch Same-Day-Foreign
            if country.is_foreign and bmf_auslandj:
                rates = bmf_auslandj.get(country.country) or (0.0, 0.0)
                eur = float(rates[1])  # an_abreise
                return DayClassification(
                    klass='Z76', eur=eur, rate_type='an_abreise',
                    reason=f'foreign_same_day_{country.iata}',
                    bmf_land=country.country, bmf_tagtyp='an_abreise')
            return DayClassification(
                klass='Z76', eur=0.0, rate_type='an_abreise',
                reason='foreign_same_day_no_country',
                warnings=['country_unresolved'])
        # Inland-Same-Day: >=8h → Z72, sonst Office
        if duty >= 480:
            return DayClassification(
                klass='Z72', eur=_BMF_INLAND['tagestrip_8h'],
                rate_type='tagestrip_8h',
                reason='inland_same_day_over_8h',
                bmf_land='Deutschland', bmf_tagtyp='tagestrip_8h')
        return DayClassification(klass='Office', reason='inland_same_day_under_8h')

    if role == DayRole.STANDBY_AIRPORT:
        return DayClassification(klass='Standby', reason='standby_airport_in_tour')

    if role == DayRole.OFFICE_AT_HB:
        return DayClassification(klass='Office', reason='office_at_hb_in_tour')

    # DEPARTURE / MID_FULL_AWAY / RETURN
    if country.is_foreign and bmf_auslandj is not None:
        rates = bmf_auslandj.get(country.country)
        if not rates:
            return DayClassification(
                klass='Z76', eur=0.0, rate_type='unknown',
                reason=f'foreign_{role.value}_country_unmapped',
                warnings=[f'bmf_missing_{country.country}'],
                bmf_land=country.country)
        voll_24h, an_abreise = float(rates[0]), float(rates[1])
        if role == DayRole.MID_FULL_AWAY:
            return DayClassification(
                klass='Z76', eur=voll_24h, rate_type='voll_24h',
                reason=f'foreign_mid_full_away_{country.iata}',
                bmf_land=country.country, bmf_tagtyp='voll_24h')
        # DEPARTURE / RETURN: An/Abreise-Pauschale
        return DayClassification(
            klass='Z76', eur=an_abreise, rate_type='an_abreise',
            reason=f'foreign_{role.value}_{country.iata}',
            bmf_land=country.country, bmf_tagtyp='an_abreise')

    if country.is_foreign:
        # bmf_auslandj nicht gegeben → Z76 mit eur=0
        return DayClassification(
            klass='Z76', eur=0.0, rate_type='unknown',
            reason=f'foreign_{role.value}_no_bmf_table',
            warnings=['bmf_table_missing'],
            bmf_land=country.country)

    # Inland-Auswärtstätigkeit (Tour mit Inland-Layover/Mid-Tag)
    if role == DayRole.MID_FULL_AWAY:
        return DayClassification(
            klass='Z74', eur=_BMF_INLAND['voll_24h'], rate_type='voll_24h',
            reason='inland_mid_full_away',
            bmf_land='Deutschland', bmf_tagtyp='voll_24h')
    # DEPARTURE / RETURN inland-Mischfall
    return DayClassification(
        klass='Z73', eur=_BMF_INLAND['an_abreise'], rate_type='an_abreise',
        reason=f'inland_{role.value}',
        bmf_land='Deutschland', bmf_tagtyp='an_abreise')


# ════════════════════════════════════════════════════════════════════════════
# Orchestrator: classify_pipeline (kombiniert alle 6 Regeln)
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class PipelineResult:
    """Output des V2-Klassifikations-Orchestrators.

    Bietet alles was app.py braucht um den Legacy-Pfad zu ersetzen oder
    parallel zu auditieren: tag-für-tag-Klassen, KPI-Counter, Tour-Liste.
    """
    tage_detail: List[Dict[str, Any]] = field(default_factory=list)
    tours_count: int = 0
    fahrtage: int = 0
    arbeitstage: int = 0
    hotel_naechte: int = 0
    reinigungstage: int = 0
    z72_eur: float = 0.0
    z73_eur: float = 0.0
    z74_eur: float = 0.0
    z76_eur: float = 0.0
    z72_tage: int = 0
    z73_tage: int = 0
    z74_tage: int = 0
    z76_tage: int = 0
    warnings: List[str] = field(default_factory=list)
    diagnostics: Dict[str, Any] = field(default_factory=dict)


def classify_pipeline(
    cas_days: List[Dict[str, Any]],
    se_rows: Optional[List[Dict[str, Any]]] = None,
    year: int = 2025,
    homebase: str = 'FRA',
    iata_to_bmf: Optional[Dict[str, str]] = None,
    bmf_auslandj: Optional[Dict[str, Tuple[float, float]]] = None,
) -> PipelineResult:
    """Orchestriert die 6 V2-Regeln zu einem End-to-End-Ergebnis.

    Pipeline:
      sorted_days → build_tours → für jeden Tag:
        day_role_in_tour + resolve_country + is_hotel_night + classify_day
      → counters aggregieren → PipelineResult

    Args:
      cas_days:    Reader-Tage (vom Reader V2 normalisiert)
      se_rows:     SE-Buchungs-Zeilen (für Country-Resolver)
      year:        Steuerjahr (für BMF-Lookup, wenn bmf_auslandj=None)
      homebase:    IATA-Code
      iata_to_bmf: IATA→Country-Map (für resolve_country)
      bmf_auslandj: country_key → (voll_24h, an_abreise)

    Returns:
      PipelineResult mit tage_detail + aggregierten Countern.

    Side-effect-frei. Keine I/O. Deterministisch.
    """
    se_rows = se_rows or []
    iata_to_bmf = iata_to_bmf or {}
    sorted_days = sorted(cas_days or [], key=lambda d: str(d.get('datum') or ''))
    tours = build_tours(sorted_days, homebase=homebase)
    # Index: datum → tour
    tour_by_date: Dict[str, Tour] = {}
    for t in tours:
        for d in t.days:
            ds = d.get('datum')
            if ds:
                tour_by_date[ds] = t

    result = PipelineResult(tours_count=len(tours))
    result.fahrtage = len(tours)  # Definition: Tour-Start = Fahrtag

    for day in sorted_days:
        ds = day.get('datum') or ''
        tour = tour_by_date.get(ds)
        country = resolve_country(day, tour, se_rows, iata_to_bmf, homebase)
        role = day_role_in_tour(day, tour, homebase) if tour else None
        hotel_flag, hotel_reason = (False, 'no_tour')
        if tour:
            hotel_flag, hotel_reason = is_hotel_night(day, tour, country, role)
        cls = classify_day(
            day, tour, country,
            hotel_info=(hotel_flag, hotel_reason),
            bmf_auslandj=bmf_auslandj,
            homebase=homebase,
        )
        # Counter-Logik
        is_workday = cls.klass in ('Z72', 'Z73', 'Z74', 'Z76', 'Office', 'Standby')
        is_reinigungstag = cls.klass in ('Z72', 'Z73', 'Z74', 'Z76', 'Office')
        if is_workday:
            result.arbeitstage += 1
        if is_reinigungstag:
            result.reinigungstage += 1
        if hotel_flag:
            result.hotel_naechte += 1
        if cls.klass == 'Z72':
            result.z72_eur += cls.eur
            result.z72_tage += 1
        elif cls.klass == 'Z73':
            result.z73_eur += cls.eur
            result.z73_tage += 1
        elif cls.klass == 'Z74':
            result.z74_eur += cls.eur
            result.z74_tage += 1
        elif cls.klass == 'Z76':
            result.z76_eur += cls.eur
            result.z76_tage += 1

        result.tage_detail.append({
            'datum':        ds,
            'klass':        cls.klass,
            'eur':          round(cls.eur, 2),
            'rate_type':    cls.rate_type,
            'role':         role.value if role else 'no_tour',
            'country':      country.country,
            'country_iata': country.iata,
            'country_src':  country.source,
            'is_hotel':     hotel_flag,
            'reason':       cls.reason,
            'bmf_land':     cls.bmf_land,
            'bmf_tagtyp':   cls.bmf_tagtyp,
            'warnings':     list(cls.warnings or []),
        })
        if cls.warnings:
            result.warnings.extend(cls.warnings)

    result.z72_eur = round(result.z72_eur, 2)
    result.z73_eur = round(result.z73_eur, 2)
    result.z74_eur = round(result.z74_eur, 2)
    result.z76_eur = round(result.z76_eur, 2)
    result.diagnostics = {
        'tour_count': len(tours),
        'days_processed': len(sorted_days),
        'days_in_tour': sum(len(t.days) for t in tours),
        'unresolved_country': sum(
            1 for e in result.tage_detail
            if e['klass'] == 'Z76' and 'country_unresolved' in (e.get('warnings') or [])
        ),
    }
    return result
