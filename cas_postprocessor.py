"""CAS Reader V2 — deterministischer Post-Processor.

Aufgabe:
    Tour-Cluster-Heilung auf dem Sonnet-Output. Sonnet kann Tage isoliert
    falsch lesen (X als Frei, leerer Marker als Frei, ends_at_homebase=True
    bei Mid-Tour-Briefings). Dieser Post-Processor schaut Nachbartage an
    und repariert offensichtliche Reader-Lücken deterministisch.

Architektur:
    CAS Reader (Sonnet)  →  normalize_cas_days_v2()  →  normalized_tours
       (raw days)             (geheilte days)             (Tour-Bau)

Harte Regeln:
    1. KEIN Tibor-/Date-Hardcoding
    2. KEINE FollowMe-Beträge
    3. Generische Healing-Rules nur
    4. Jede Heilung muss in `warnings`/`healed_by` audit-bar sein
    5. SE darf NIE allein eine Tour erzeugen — auch nicht hier
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple


def _parse_iso_date(value: Any) -> Optional[date]:
    """Parst YYYY-MM-DD oder ähnliches; gibt None bei Fehler zurück."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value.strip()[:10], '%Y-%m-%d').date()
    except (ValueError, AttributeError):
        return None


def _dates_are_adjacent(prev_date: Any, current_date: Any, max_gap_days: int = 1) -> bool:
    """True wenn prev_date und current_date kalendarisch direkt angrenzen oder
    innerhalb einer kleinen, erlaubten Lücke (≤ max_gap_days) liegen.

    Akzeptiert ISO-Strings oder date-Objekte. Bei nicht-parsbaren Werten False.
    """
    p = prev_date if isinstance(prev_date, date) else _parse_iso_date(prev_date)
    c = current_date if isinstance(current_date, date) else _parse_iso_date(current_date)
    if not p or not c:
        return False
    delta = (c - p).days
    return 1 <= delta <= max_gap_days

# Default-Homebase wenn nicht übergeben (defensiver Fallback)
DEFAULT_HB = 'FRA'

# Inland-IATA-Codes (Synced mit normalized_tours._INLAND_CODES)
_INLAND_CODES: Set[str] = {
    'FRA', 'MUC', 'DUS', 'TXL', 'BER', 'HAM', 'STR', 'CGN', 'HAJ', 'NUE',
    'LEJ', 'BRE', 'DRS', 'PAD', 'FMM', 'FMO', 'SCN', 'FKB', 'FDH', 'NRN',
}

# Standby-Marker (Home + Airport unterscheiden)
_HOME_STANDBY_MARKERS = {'SB_S', 'SB_F', 'SB_M', 'RB', 'RES_SB'}
_AIRPORT_STANDBY_MARKERS = {'SBA', 'SBY'}

# Passive Heimat-Marker (kein Tour-Trigger)
_PASSIVE_MARKERS = {'ORTSTAG', 'FRS', 'OF', 'OFF', 'LMN_AS', 'LMN_CR', 'LMN_HT1'}

# Training/Office-Marker (kein automatisch foreign)
_TRAINING_PREFIXES = ('EM', 'EH', 'TK', 'EMCRM', 'SECCRM', 'EK', 'D4', 'DD',
                     'FL ', 'SIM', 'TRI', 'TRE')

# Frei/Urlaub-Activity-Types
_FREE_ACTIVITIES = {'frei', 'urlaub', 'krank', 'off', 'free'}


# ════════════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════════════

def _is_inland_iata(iata: str) -> bool:
    if not iata or not isinstance(iata, str):
        return False
    return iata.upper().strip() in _INLAND_CODES


def _is_three_letter_iata(token: str) -> bool:
    """True wenn Token ein 3-letter-alphabetischer IATA-Code ist."""
    if not isinstance(token, str):
        return False
    t = token.upper().strip()
    return len(t) == 3 and t.isalpha()


def _is_flight_number(token: str) -> bool:
    """True wenn Token wie eine Flugnummer aussieht (LH123, 1234)."""
    if not isinstance(token, str):
        return False
    t = token.upper().strip()
    if t.startswith('LH'):
        return True
    digits = ''.join(c for c in t if c.isdigit())
    # Reine Flugnummer = mind. 3 Ziffern und Rest nicht alphabetisch
    if len(digits) >= 3 and len([c for c in t if c.isalpha()]) <= 2:
        return True
    return False


def _split_routing_tokens(routing: List[str], homebase: str) -> Tuple[List[str], List[str]]:
    """Trennt routing-Liste in (iatas, flight_numbers)."""
    iatas: List[str] = []
    flights: List[str] = []
    for tok in routing or []:
        if not isinstance(tok, str):
            continue
        t = tok.upper().strip()
        if _is_three_letter_iata(t):
            iatas.append(t)
        elif _is_flight_number(t):
            flights.append(t)
    return iatas, flights


def _is_home_standby_marker(marker: str) -> bool:
    if not marker:
        return False
    return marker.upper().strip() in _HOME_STANDBY_MARKERS


def _is_passive_marker(marker: str) -> bool:
    if not marker:
        return False
    return marker.upper().strip() in _PASSIVE_MARKERS


def _is_training_marker(marker: str) -> bool:
    if not marker:
        return False
    m_up = marker.upper().strip()
    return any(m_up.startswith(p) for p in _TRAINING_PREFIXES)


def _is_empty_or_x_marker(marker: str) -> bool:
    if not marker:
        return True
    m = marker.upper().strip()
    return m in ('', 'X', '==', '--', '/', '-')


def _is_frei_activity(day: Dict[str, Any]) -> bool:
    return (day.get('activity_type') or '').lower() in _FREE_ACTIVITIES


def _looks_like_tour_day(day: Dict[str, Any], homebase: str) -> bool:
    """Heuristisch: hat der Tag Tour-Charakter?

    Signale:
      - has_fl=True
      - duty_duration_minutes >= 240 UND nicht reines Standby
      - routing enthält foreign IATA
      - layover_iata foreign
      - overnight_after_day=True UND nicht reines Standby
    """
    marker = (day.get('marker_raw') or day.get('marker') or '').strip()
    if _is_home_standby_marker(marker) or _is_passive_marker(marker):
        return False
    if day.get('has_fl'):
        return True
    routing = day.get('routing') or []
    iatas, _ = _split_routing_tokens(routing, homebase)
    has_foreign_iata = any(
        not _is_inland_iata(i) and i != (homebase or DEFAULT_HB).upper()
        for i in iatas
    )
    if has_foreign_iata:
        return True
    layover = (day.get('layover_iata') or day.get('layover_ort') or '').upper().strip()
    if layover and not _is_inland_iata(layover) and layover != (homebase or DEFAULT_HB).upper():
        return True
    duty = int(day.get('duty_duration_minutes') or 0)
    if duty >= 240 and not _is_home_standby_marker(marker):
        return True
    return False


# ════════════════════════════════════════════════════════════════════════════
# Main entry point
# ════════════════════════════════════════════════════════════════════════════

def normalize_cas_days_v2(
    structured_days: List[Dict[str, Any]],
    homebase: str = DEFAULT_HB,
    se_rows: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Deterministic post-processor für CAS-Reader-Output.

    Liest structured_days (vom Sonnet-Reader) und repariert Tour-Cluster-Lücken
    durch Nachbartag-Analyse. Liefert geheilte Day-Dicts zurück.

    NEUE Felder pro Tag:
      - normalized_marker:           bereinigter Marker
      - routing_iatas:               nur 3-Letter-IATAs
      - flight_numbers:              nur Flugnummern
      - previous_layover_iata:       aus Vortag
      - next_layover_iata:           aus Folgetag
      - tour_context_hint:           none|departure|mid_tour|return|continuation|unclear
      - tour_context_confidence:     high|medium|low
      - is_tour_continuation:        bool
      - is_tour_return:              bool
      - is_tour_departure:           bool
      - return_from_layover:         bool
      - origin_iata:                 IATA des Ursprungs (Tour-Return)
      - destination_iata:            IATA des Ziels
      - reader_should_not_classify_as_free_reason: str | None
      - neighbor_evidence:           list[str]
      - healed_by:                   list[str] (Regel-IDs die geheilt haben)
      - warnings:                    list[str]
    """
    se_rows = se_rows or []
    hb_up = (homebase or DEFAULT_HB).upper()

    # SE-Stempel-Index nach Datum
    se_by_date: Dict[str, List[Dict[str, Any]]] = {}
    for se in se_rows:
        d = se.get('datum') or se.get('date') or ''
        if d:
            se_by_date.setdefault(d, []).append(se)

    # Sortieren nach Datum
    days = sorted(
        [dict(d) for d in (structured_days or [])],
        key=lambda x: x.get('datum') or x.get('date') or '',
    )

    # Pre-Pass: Routing-Tokens trennen + neighbor-layover-cache
    for d in days:
        marker_raw = (d.get('marker_raw') or d.get('marker') or '').strip()
        d['normalized_marker'] = marker_raw.upper()
        routing = d.get('routing') or []
        iatas, flights = _split_routing_tokens(routing, hb_up)
        d['routing_iatas'] = iatas
        d['flight_numbers'] = flights
        # Initialize neue Felder
        d.setdefault('tour_context_hint', 'unclear')
        d.setdefault('tour_context_confidence', 'low')
        d.setdefault('is_tour_continuation', False)
        d.setdefault('is_tour_return', False)
        d.setdefault('is_tour_departure', False)
        d.setdefault('return_from_layover', False)
        d.setdefault('origin_iata', None)
        d.setdefault('destination_iata', None)
        d.setdefault('previous_layover_iata', None)
        d.setdefault('next_layover_iata', None)
        d.setdefault('reader_should_not_classify_as_free_reason', None)
        d.setdefault('neighbor_evidence', [])
        d.setdefault('healed_by', [])
        d.setdefault('warnings', [])

    # Neighbor-Layover-Pass: setze prev/next layover_iata
    for i, d in enumerate(days):
        if i > 0:
            prev_layover = (days[i-1].get('layover_iata')
                           or days[i-1].get('layover_ort') or '')
            d['previous_layover_iata'] = prev_layover.upper().strip() or None
        if i < len(days) - 1:
            next_layover = (days[i+1].get('layover_iata')
                           or days[i+1].get('layover_ort') or '')
            d['next_layover_iata'] = next_layover.upper().strip() or None

    # ───────────────────────────────────────────────────────────────────────
    # Regel 0: Passive-Marker-Heilung (LMN_HT1, ORTSTAG, FRS, OF, OFF,
    # LMN_AS, LMN_CR). Reader liefert teilweise activity_type='unknown' für
    # diese bekannten LH-Codes — wir heilen das deterministisch zu 'free',
    # damit der Tag nicht als „Unbekannte Kennung" im Chat landet.
    #
    # R29 (2026-05-27): zwei Klassen passiver Marker — Marker-Lexikon trägt
    # Wissen das nicht in den CAS-Feldern steht (z.B. LMN_HT = Online-Training
    # zuhause, auch wenn duty/start_time gesetzt sind = keine Fahrt zum HB).
    #   - _PASSIVE_STRICT = passiv unabhängig von CAS-Feldern
    #     (LMN_HT/LMN_HT1/LMN_AD/LMN_AL/LMN_DS/LMN_FT = Home-Maßnahmen;
    #      OFF/OF = Off-Day; ORTSTAG = lokaler HB-Passiv-Tag)
    #   - _PASSIVE_FIELDS_RULE = passiv NUR wenn auch CAS-Felder leer
    #     (FRS/FRD/LMN_AS/LMN_CR = können auch echte Standort-Termine sein)
    # ───────────────────────────────────────────────────────────────────────
    _PASSIVE_STRICT_LOCAL = {
        'ORTSTAG', 'OF', 'OFF',
        'LMN_HT', 'LMN_HT1', 'LMN_AD', 'LMN_AL', 'LMN_DS', 'LMN_FT',
    }
    for d in days:
        marker = (d.get('normalized_marker') or '').strip().upper()
        if not _is_passive_marker(marker):
            continue
        at = (d.get('activity_type') or '').lower()
        if at in _FREE_ACTIVITIES:
            continue
        duty = int(d.get('duty_duration_minutes') or 0)
        has_fl = bool(d.get('has_fl'))
        routing = d.get('routing_iatas') or []
        start_time = (d.get('start_time') or '').strip()
        end_time = (d.get('end_time') or '').strip()
        overnight = bool(d.get('overnight_after_day'))
        is_strict = marker in _PASSIVE_STRICT_LOCAL

        if is_strict:
            # LMN_HT*/OFF/ORTSTAG: immer passiv (User war zuhause, auch wenn
            # CAS-Felder für Online-Schulung Werte tragen)
            d['activity_type'] = 'free'
            d['healed_by'].append('R0_passive_marker_strict_to_free')
            continue

        # FRS/FRD/LMN_AS/LMN_CR: nur dann free wenn Felder auch leer sind.
        # Bei Briefing-Zeit könnte echter Standort-Termin gemeint sein
        # (z.B. FRS mit start=04:45 = Tour-Start statt Frei-Schicht).
        if duty > 0 or has_fl or routing or start_time or end_time or overnight:
            d['warnings'].append(
                f'R0 skip: marker {marker} passive-default but CAS fields '
                f'active (duty={duty}, start={start_time!r}, routing={routing}, '
                f'overnight={overnight}) — fields win, leaving as unknown'
            )
            continue
        d['activity_type'] = 'free'
        d['healed_by'].append('R0_passive_marker_to_free')

    # ───────────────────────────────────────────────────────────────────────
    # Regel 1: X-Return-Healing
    # ───────────────────────────────────────────────────────────────────────
    for i, d in enumerate(days):
        if i == 0:
            continue
        marker = d.get('normalized_marker') or ''
        if not _is_empty_or_x_marker(marker):
            continue
        if not _is_frei_activity(d):
            continue
        prev = days[i-1]
        # R14 Date-Adjacency: Heilung greift nur, wenn prev und d kalendarisch
        # benachbart sind. Sonst ist „prev" ein anderer Tour-Block.
        cur_ds = d.get('datum') or d.get('date')
        prev_ds = prev.get('datum') or prev.get('date')
        if not _dates_are_adjacent(prev_ds, cur_ds, max_gap_days=1):
            d['warnings'].append(
                f'R1 chain skipped: non-adjacent dates {prev_ds} -> {cur_ds}'
            )
            continue
        prev_layover_raw = (prev.get('layover_iata') or prev.get('layover_ort') or '')
        prev_layover = prev_layover_raw.upper().strip()
        prev_overnight = bool(prev.get('overnight_after_day'))

        # Vortag muss Ausland-Layover gewesen sein
        prev_was_foreign_layover = (
            prev_overnight and prev_layover
            and not _is_inland_iata(prev_layover)
            and prev_layover != hb_up
        )
        if not prev_was_foreign_layover:
            continue

        # Heutiger Tag zeigt Heimkehr: routing enthält HB ODER ends_at_homebase
        today_returns_to_hb = (
            hb_up in (d.get('routing_iatas') or [])
            or bool(d.get('ends_at_homebase'))
        )
        # Auch SE-Beleg: SE-stfrei für Vortag (User schliesst die Tour ab)
        ds = d.get('datum') or d.get('date') or ''
        prev_ds = prev.get('datum') or prev.get('date') or ''
        has_se_for_prev = any(
            float(se.get('stfrei_betrag') or 0) > 0
            for se in se_by_date.get(prev_ds, [])
            if not se.get('storno')
        )

        if today_returns_to_hb or has_se_for_prev:
            # Heilung anwenden
            d['is_tour_return'] = True
            d['return_from_layover'] = True
            d['origin_iata'] = prev_layover
            d['destination_iata'] = hb_up
            d['tour_context_hint'] = 'return'
            d['tour_context_confidence'] = 'medium'
            d['activity_type'] = 'tour_return'
            d['reader_should_not_classify_as_free_reason'] = (
                f'Vortag {prev_ds} hatte Auslands-Layover {prev_layover}; '
                f'heute returnt zu {hb_up}.'
            )
            d['neighbor_evidence'].append(
                f'prev.layover_iata={prev_layover} prev.overnight=True'
            )
            d['healed_by'].append('rule1_x_return_healing')
            d['warnings'].append('healed_x_return_day_from_neighbor_context')

    # ───────────────────────────────────────────────────────────────────────
    # Regel 2: Empty-Marker-Continuation
    # ───────────────────────────────────────────────────────────────────────
    for i in range(1, len(days) - 1):
        d = days[i]
        marker = d.get('normalized_marker') or ''
        # Nur leerer Marker (NICHT X-Heimkehr, die ist schon geheilt)
        if marker not in ('', '==') or d.get('is_tour_return'):
            continue
        if not _is_frei_activity(d):
            continue
        prev = days[i-1]
        nxt = days[i+1]
        # R14 Date-Adjacency: nur heilen, wenn prev UND nxt benachbart sind.
        cur_ds = d.get('datum') or d.get('date')
        prev_ds = prev.get('datum') or prev.get('date')
        nxt_ds = nxt.get('datum') or nxt.get('date')
        if not (_dates_are_adjacent(prev_ds, cur_ds, max_gap_days=1)
                and _dates_are_adjacent(cur_ds, nxt_ds, max_gap_days=1)):
            continue
        prev_is_tour = _looks_like_tour_day(prev, hb_up)
        nxt_is_tour = _looks_like_tour_day(nxt, hb_up)
        if not (prev_is_tour and nxt_is_tour):
            continue
        d['is_tour_continuation'] = True
        d['tour_context_hint'] = 'continuation'
        d['tour_context_confidence'] = 'medium'
        d['activity_type'] = 'tour_continuation'
        d['reader_should_not_classify_as_free_reason'] = (
            f'Prev und Next sind Tour-Tage — leerer Marker = Tour-Continuation.'
        )
        d['neighbor_evidence'].append(
            f'prev.marker={prev.get("normalized_marker")} '
            f'next.marker={nxt.get("normalized_marker")}'
        )
        d['healed_by'].append('rule2_empty_marker_continuation')
        d['warnings'].append('healed_empty_marker_inside_tour_cluster')

    # ───────────────────────────────────────────────────────────────────────
    # Regel 3: ends_at_homebase-Conflict-Detection (STRICT + Date-Adjacency)
    # ───────────────────────────────────────────────────────────────────────
    # Nur greifen wenn:
    #   - heutiger Tag UND morgiger Tag KEINE eigenständige Tour wären
    #   - klare Auslands-Continuation (overnight + foreign layover next day)
    #   - prev_date und current_date sind kalendarisch direkt benachbart
    #     (R14 Date-Adjacency-Fix: sortierte Listen mit grossen Lücken duerfen
    #      nicht automatisch verkettet werden).
    for i, d in enumerate(days):
        if not d.get('ends_at_homebase'):
            continue
        if i >= len(days) - 1:
            continue
        # Wenn heutiger Tag selbst overnight=False UND starts_at_homebase=True,
        # dann ist es Same-Day-Trip — KEINE Continuation nötig.
        if (not d.get('overnight_after_day')) and bool(d.get('starts_at_homebase')):
            # Could be a separate same-day-tour — don't merge
            continue
        nxt = days[i+1]
        # R14: Datums-Adjazenz prüfen — Lücken > 1 Tag duerfen KEINE Tour-Kette
        # bilden.
        cur_ds = d.get('datum') or d.get('date')
        nxt_ds = nxt.get('datum') or nxt.get('date')
        if not _dates_are_adjacent(cur_ds, nxt_ds, max_gap_days=1):
            d['warnings'].append(
                f'R3 chain skipped: non-adjacent dates {cur_ds} -> {nxt_ds}'
            )
            continue
        nxt_marker = nxt.get('normalized_marker') or ''
        # Folgetag clearly Tour-Continuation: foreign-layover-overnight required
        nxt_routing_iatas = nxt.get('routing_iatas') or []
        nxt_has_foreign = any(
            not _is_inland_iata(r) and r != hb_up for r in nxt_routing_iatas
        )
        nxt_layover = (nxt.get('layover_iata') or nxt.get('layover_ort') or '').upper()
        nxt_overnight = bool(nxt.get('overnight_after_day'))
        # STRICT: foreign-layover-overnight ist die einzige starke Evidence
        nxt_continues_tour = (
            nxt_layover and not _is_inland_iata(nxt_layover)
            and nxt_layover != hb_up
            and nxt_overnight
        )
        if not nxt_continues_tour:
            continue
        # Conflict: today.ends_at_homebase=True, but next day continues tour.
        # Aktive Korrektur: ends_at_homebase=False setzen damit Builder die Tour
        # nicht abbricht. Audit-Trail in healed_by/warnings für Transparenz.
        d['ends_at_homebase_original'] = d.get('ends_at_homebase')
        d['ends_at_homebase'] = False
        d['ends_at_homebase_conflict'] = True
        d['warnings'].append('ends_hb_conflicts_with_tour_continuation_corrected')
        d['neighbor_evidence'].append(
            f'next.marker={nxt_marker} next.overnight={nxt_overnight} '
            f'next.layover={nxt_layover}'
        )
        d['healed_by'].append('rule3_ends_hb_correction')

    # ───────────────────────────────────────────────────────────────────────
    # Regel 4: Flight-numbers vs IATAs (bereits in Pre-Pass gemacht — Audit-only)
    # ───────────────────────────────────────────────────────────────────────
    # routing_iatas und flight_numbers sind getrennt. Keine weitere Aktion nötig.

    # ───────────────────────────────────────────────────────────────────────
    # Regel 5: Return-from-Layover-Markierung
    # ───────────────────────────────────────────────────────────────────────
    for i, d in enumerate(days):
        if i == 0:
            continue
        if d.get('return_from_layover'):
            continue  # schon durch Regel 1 geheilt
        prev = days[i-1]
        # R14 Date-Adjacency: Markierung nur, wenn prev und d benachbart sind.
        cur_ds = d.get('datum') or d.get('date')
        prev_ds = prev.get('datum') or prev.get('date')
        if not _dates_are_adjacent(prev_ds, cur_ds, max_gap_days=1):
            continue
        prev_layover = (prev.get('layover_iata') or prev.get('layover_ort') or '').upper().strip()
        prev_overnight = bool(prev.get('overnight_after_day'))
        if not (prev_layover and not _is_inland_iata(prev_layover)
                and prev_layover != hb_up and prev_overnight):
            continue
        today_to_hb = (
            bool(d.get('ends_at_homebase'))
            or hb_up in (d.get('routing_iatas') or [])
        )
        if today_to_hb:
            d['return_from_layover'] = True
            d['origin_iata'] = prev_layover
            d['destination_iata'] = hb_up
            if d.get('tour_context_hint') == 'unclear':
                d['tour_context_hint'] = 'return'
                d['tour_context_confidence'] = 'medium'
            d['healed_by'].append('rule5_return_from_layover_marked')

    # ───────────────────────────────────────────────────────────────────────
    # Tour-Departure-Hint (heuristisch — sichtbar machen wenn klar)
    # ───────────────────────────────────────────────────────────────────────
    for i, d in enumerate(days):
        if d.get('tour_context_hint') != 'unclear':
            continue
        is_dep_signal = (
            bool(d.get('starts_at_homebase'))
            and bool(d.get('overnight_after_day'))
        )
        if is_dep_signal:
            d['tour_context_hint'] = 'departure'
            d['tour_context_confidence'] = 'medium'

    return days


# ════════════════════════════════════════════════════════════════════════════
# Diagnose / Audit
# ════════════════════════════════════════════════════════════════════════════

def diff_pre_post(
    raw_days: List[Dict[str, Any]],
    normalized_days: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Liefert Diff vor/nach Postprocessor — welche Tage wurden geheilt."""
    pre_by_date = {d.get('datum') or d.get('date'): d for d in raw_days}
    post_by_date = {d.get('datum') or d.get('date'): d for d in normalized_days}

    healed = []
    for ds, post in post_by_date.items():
        pre = pre_by_date.get(ds, {})
        if not post.get('healed_by'):
            continue
        healed.append({
            'datum':       ds,
            'before':      {
                'marker':       pre.get('marker_raw') or pre.get('marker'),
                'activity':     pre.get('activity_type'),
                'is_tour':      None,
            },
            'after':       {
                'marker':              post.get('normalized_marker'),
                'activity':            post.get('activity_type'),
                'is_tour_return':      post.get('is_tour_return'),
                'is_tour_continuation': post.get('is_tour_continuation'),
                'tour_context_hint':   post.get('tour_context_hint'),
            },
            'healed_by':   post.get('healed_by'),
            'warnings':    post.get('warnings'),
            'reason':      post.get('reader_should_not_classify_as_free_reason'),
        })
    return {
        'total_days':   len(normalized_days),
        'healed_count': len(healed),
        'by_rule':      _count_by_rule(healed),
        'healed_days':  healed,
    }


def _count_by_rule(healed: List[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for h in healed:
        for rule in h.get('healed_by', []):
            counts[rule] = counts.get(rule, 0) + 1
    return counts
