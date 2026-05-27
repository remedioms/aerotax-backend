"""CAS Reader V2 — Sonnet-Prompt-Instructions + Schema + Validator.

Diese Datei ist die SINGLE SOURCE OF TRUTH für den Reader-V2 Sonnet-Prompt.
Wenn app.py den CAS-Reader aufruft (Sonnet) und der Flag AEROTAX_CAS_READER_V2=1
aktiv ist, werden diese Instructions an den existing Prompt angehängt.

Pflichtfelder pro Tag — synced mit normalized_tours/cas_postprocessor-Annahmen.

KEINE Hardcodings:
- Keine Tibor-Daten
- Keine FollowMe-Beträge
- Keine Date-Literals
- Nur generische Crew-Logik
"""

# ════════════════════════════════════════════════════════════════════════════
# V2 Prompt Instructions — Append to existing CAS prompt
# ════════════════════════════════════════════════════════════════════════════

V2_PROMPT_INSTRUCTIONS = """

══════════════════════════════════════════════════════════════════════════════
CAS READER V2 — Tour-Context-aware Reading (verpflichtend ab v15.2)
══════════════════════════════════════════════════════════════════════════════

WICHTIG: Du darfst keinen Tag isoliert klassifizieren. Für jeden Tag musst du
mindestens den Vortag und Folgetag berücksichtigen.

REGEL 1 — X / leerer Marker am Heimkehr-Tag
────────────────────────────────────────────
Wenn ein Tag den Marker `X`, leer, `==`, `--`, `OFF` oder `frei` hat, ABER:
  - der Vortag war eine Auslands-Übernachtung (overnight=True + layover_iata foreign), UND
  - der aktuelle Tag führt zur Homebase zurück (routing enthält Homebase ODER
    ends_at_homebase=True)
DANN klassifiziere als:
  - activity_type='tour_return'
  - is_tour_return=true
  - return_from_layover=true
  - origin_iata=previous_layover_iata
  - destination_iata=homebase
  - overnight_after_day=false (User schläft heute zuhause)
  - warning='X marker interpreted as return day from tour context'
NICHT als activity_type='free'.

REGEL 2 — Leerer Marker zwischen Tour-Tagen
─────────────────────────────────────────────
Wenn ein Tag leeren Marker hat UND prev+next beide Tour-Tage sind:
  - activity_type='tour_continuation'
  - is_tour_continuation=true
  - overnight_after_day vom Vortag erben falls passend
  - destination_iata aus Tour-Kontext setzen
NICHT als activity_type='free'.

REGEL 3 — ends_at_homebase Conflict
─────────────────────────────────────
Wenn ends_at_homebase=True bei diesem Tag, aber der Folgetag clearly
Tour-Continuation ist (foreign-layover-overnight, oder Mid-Tour-Marker):
  - ends_at_homebase=false (Tour läuft weiter)
  - warning='ends_at_homebase conflicts with tour continuation'

REGEL 4 — Briefing- und Departure-Zeiten extrahieren
─────────────────────────────────────────────────────
Für JEDEN Anreise-/Briefing-Tag musst du extrahieren:
  - briefing_time: HH:MM (oder null, wenn nicht lesbar)
  - duty_start_time: HH:MM
  - duty_end_time: HH:MM (oder null wenn over-night)
  - departure_time: HH:MM erstes Flug-Departure-am-Tag
  - arrival_time: HH:MM letztes Flug-Arrival-am-Tag
  - debrief_time: HH:MM nach letztem Flug (oder null)

Diese Felder sind kritisch für die Z73-Inland vs Z76-Foreign-Entscheidung
am Anreise-Tag (Briefing-Stunden in DE vs Flug-Stunden im Ausland).

REGEL 5 — Destination Propagation
──────────────────────────────────
Wenn am Anreise-Tag das Routing nur das Start-IATA enthält
(z.B. routing=['FRA']), aber die Tour-Folgetage zeigen das Ziel:
  - destination_iata aus Folgetag/Layover ableiten
  - warning='destination inferred from next tour day'
NICHT destination_iata leer lassen.

REGEL 6 — Overnight korrekt setzen
────────────────────────────────────
overnight_after_day:
  - true: Crew schläft nicht zuhause (foreign Hotel / Layover)
  - false: Crew schläft zuhause (Tour-Ende, Same-Day-Rückkehr)
  - null: aus Daten nicht eindeutig ableitbar (warning setzen)

NICHT einfach false default. Wenn Marker layover_ort enthält oder
Tour-Klammer das nahelegt → true.

REGEL 7 — Routing vs Flight-Numbers
─────────────────────────────────────
Trenne strikt:
  - routing_iatas: nur 3-letter-alphabetische Codes (FRA, BLR, HKG, LCA)
  - flight_numbers: Flugnummern wie LH756, 31591, 49444
Wenn ein Token sowohl wie Flugnummer als auch wie IATA aussieht: bevorzuge
die Flugnummer-Interpretation wenn ≥3 Ziffern.

REGEL 8 — Tour-Context-Hint pro Tag
─────────────────────────────────────
Setze für jeden Tag:
  - tour_context_hint: 'departure' | 'mid_tour' | 'return' | 'continuation' |
                       'home' | 'standby' | 'training' | 'unclear'
  - tour_context_confidence: 'high' | 'medium' | 'low'

Wenn unklar:
  - tour_context_hint='unclear'
  - activity_type='unknown_tour_context' (NICHT 'free')
  - warning='cannot determine tour context from neighbors'

REGEL 9 — Free-Default-Vermeidung
───────────────────────────────────
Du darfst activity_type='free' NUR setzen, wenn:
  - Marker explizit Frei/Urlaub/OFF/krank ist, UND
  - kein Tour-Klammer-Indiz auf Vortag/Folgetag,
  - keine SE-Auslandszeile am Tag oder angrenzendem Tag,
  - keine Routing-Information.
Sonst: activity_type='unknown_tour_context' + warning.

REGEL 10 — Reader Should Not Classify As Free Reason
──────────────────────────────────────────────────────
Wenn du activity_type von 'free' weg ändern würdest (Regeln 1, 2, 9), setze
zusätzlich:
  - reader_should_not_classify_as_free_reason: string mit Begründung
  - neighbor_evidence: list[str] mit konkretem Beleg aus Vortag/Folgetag

══════════════════════════════════════════════════════════════════════════════
"""


# ════════════════════════════════════════════════════════════════════════════
# V2 Schema — JSON-Tool-Schema für Sonnet structured output
# ════════════════════════════════════════════════════════════════════════════

V2_REQUIRED_FIELDS_PER_DAY = (
    'datum',
    'raw_marker',
    'normalized_marker',
    'activity_type',
    'starts_at_homebase',
    'ends_at_homebase',
    'overnight_after_day',
    'routing_iatas',
    'flight_numbers',
    'origin_iata',
    'destination_iata',
    'layover_iata',
    'tour_context_hint',
    'tour_context_confidence',
    'is_tour_departure',
    'is_tour_continuation',
    'is_tour_return',
    'return_from_layover',
    'has_flight_segment',
    'confidence',
    'warnings',
)

V2_OPTIONAL_FIELDS_PER_DAY = (
    'duty_start_time',
    'duty_end_time',
    'duty_minutes',
    'briefing_time',
    'departure_time',
    'arrival_time',
    'debrief_time',
    'previous_layover_iata',
    'next_layover_iata',
    'reader_should_not_classify_as_free_reason',
    'neighbor_evidence',
)

V2_TOUR_CONTEXT_HINTS = (
    'departure', 'mid_tour', 'return', 'continuation',
    'home', 'standby', 'training', 'unclear',
)

V2_ACTIVITY_TYPES = (
    'tour_departure', 'tour_mid', 'tour_return', 'tour_continuation',
    'office', 'training', 'home_standby', 'airport_standby',
    'free', 'urlaub', 'krank', 'sick', 'off',
    'unknown_tour_context',
)


def get_v2_json_schema() -> dict:
    """JSON-Schema für Sonnet structured-output tool."""
    return {
        'type': 'object',
        'properties': {
            'days': {
                'type': 'array',
                'items': {
                    'type': 'object',
                    'properties': {
                        'datum': {'type': 'string', 'description': 'ISO YYYY-MM-DD'},
                        'raw_marker': {'type': 'string'},
                        'normalized_marker': {'type': 'string'},
                        'activity_type': {
                            'type': 'string',
                            'enum': list(V2_ACTIVITY_TYPES),
                        },
                        'duty_start_time': {'type': ['string', 'null'],
                                            'description': 'HH:MM or null'},
                        'duty_end_time': {'type': ['string', 'null']},
                        'duty_minutes': {'type': ['integer', 'null']},
                        'briefing_time': {'type': ['string', 'null']},
                        'departure_time': {'type': ['string', 'null']},
                        'arrival_time': {'type': ['string', 'null']},
                        'debrief_time': {'type': ['string', 'null']},
                        'starts_at_homebase': {'type': 'boolean'},
                        'ends_at_homebase': {'type': 'boolean'},
                        'routing_iatas': {'type': 'array',
                                          'items': {'type': 'string'}},
                        'flight_numbers': {'type': 'array',
                                           'items': {'type': 'string'}},
                        'origin_iata': {'type': ['string', 'null']},
                        'destination_iata': {'type': ['string', 'null']},
                        'layover_iata': {'type': ['string', 'null']},
                        'previous_layover_iata': {'type': ['string', 'null']},
                        'next_layover_iata': {'type': ['string', 'null']},
                        'overnight_after_day': {
                            'type': ['boolean', 'null'],
                            'description': 'null wenn nicht eindeutig',
                        },
                        'tour_context_hint': {
                            'type': 'string',
                            'enum': list(V2_TOUR_CONTEXT_HINTS),
                        },
                        'tour_context_confidence': {
                            'type': 'string',
                            'enum': ['high', 'medium', 'low'],
                        },
                        'is_tour_departure': {'type': 'boolean'},
                        'is_tour_continuation': {'type': 'boolean'},
                        'is_tour_return': {'type': 'boolean'},
                        'return_from_layover': {'type': 'boolean'},
                        'has_flight_segment': {'type': 'boolean'},
                        'reader_should_not_classify_as_free_reason': {
                            'type': ['string', 'null'],
                        },
                        'neighbor_evidence': {
                            'type': 'array', 'items': {'type': 'string'},
                        },
                        'confidence': {
                            'type': 'string',
                            'enum': ['high', 'medium', 'low'],
                        },
                        'warnings': {
                            'type': 'array', 'items': {'type': 'string'},
                        },
                    },
                    'required': list(V2_REQUIRED_FIELDS_PER_DAY),
                },
            },
        },
        'required': ['days'],
    }


# ════════════════════════════════════════════════════════════════════════════
# Validator — checkt Reader-Output gegen V2-Regeln
# ════════════════════════════════════════════════════════════════════════════

def validate_cas_reader_v2_day(day: dict) -> dict:
    """Prüft einen V2-Tag-Output gegen die Regeln aus V2_PROMPT_INSTRUCTIONS.

    Returns dict {errors: [...], warnings: [...]}.
    """
    errors = []
    warnings = []

    # Schema-Pflichtfelder
    for f in V2_REQUIRED_FIELDS_PER_DAY:
        if f not in day:
            errors.append(f'missing_required_field:{f}')

    # Regel: tour_return braucht origin/destination
    if day.get('is_tour_return'):
        if not day.get('origin_iata'):
            warnings.append('tour_return_without_origin_iata')
        if not day.get('destination_iata'):
            warnings.append('tour_return_without_destination_iata')

    # Regel: overnight=True braucht layover_iata
    if day.get('overnight_after_day') is True:
        if not day.get('layover_iata'):
            warnings.append('overnight_true_without_layover_iata')

    # Regel: activity_type='free' aber neighbor_evidence foreign-tour
    if (day.get('activity_type') == 'free'
            and day.get('neighbor_evidence')):
        # neighbor_evidence vorhanden — möglicherweise zu früh free
        nb_str = ' '.join(str(e) for e in day.get('neighbor_evidence', []))
        if 'foreign' in nb_str.lower() or 'tour' in nb_str.lower():
            warnings.append('free_classification_despite_neighbor_evidence_foreign_tour')

    # Regel: routing_iatas darf keine Flugnummern enthalten
    for tok in (day.get('routing_iatas') or []):
        if not isinstance(tok, str):
            errors.append('routing_iatas_contains_non_string')
            continue
        t = tok.upper().strip()
        # 3-letter alphabetisch erforderlich
        if not (len(t) == 3 and t.isalpha()):
            errors.append(f'routing_iatas_invalid_iata:{t}')
        # Spezifisch: Flugnummern wie LH756 raus
        if t.startswith('LH') and any(c.isdigit() for c in t):
            errors.append(f'flight_number_in_routing_iatas:{t}')

    # Regel: Wenn raw_marker LH/Flugnummer enthält aber flight_numbers leer
    raw = (day.get('raw_marker') or '').upper()
    has_lh_or_num = (raw.startswith('LH') or
                    sum(c.isdigit() for c in raw) >= 3)
    if has_lh_or_num and not (day.get('flight_numbers') or []):
        warnings.append('flight_number_in_raw_marker_but_missing_in_flight_numbers')

    # Regel: ends_at_homebase=True + next day foreign continuation
    # Diese Prüfung passiert in normalize_cas_days_v2 (cross-day) —
    # hier nur per-day Schema-Check.

    # Regel: duty_minutes > 0 aber duty_start_time/end_time fehlt
    duty = day.get('duty_minutes') or 0
    if duty > 0 and not (day.get('duty_start_time') or day.get('duty_end_time')):
        warnings.append('duty_minutes_without_start_or_end_time')

    # Regel: unknown_tour_context muss warning haben
    if day.get('activity_type') == 'unknown_tour_context':
        if not (day.get('warnings') or []):
            errors.append('unknown_tour_context_without_warning')

    return {'errors': errors, 'warnings': warnings}


def validate_cas_reader_v2_response(response: dict) -> dict:
    """Validiert ganzen Response (mit days[])."""
    if not isinstance(response, dict):
        return {'errors': ['response_not_dict'], 'warnings': []}
    if 'days' not in response:
        return {'errors': ['response_missing_days'], 'warnings': []}
    days = response.get('days') or []
    if not isinstance(days, list):
        return {'errors': ['days_not_list'], 'warnings': []}

    all_errors = []
    all_warnings = []
    for i, day in enumerate(days):
        v = validate_cas_reader_v2_day(day)
        for e in v['errors']:
            all_errors.append(f'day[{i}]:{e}')
        for w in v['warnings']:
            all_warnings.append(f'day[{i}]:{w}')
    return {'errors': all_errors, 'warnings': all_warnings}


# ════════════════════════════════════════════════════════════════════════════
# Feature-Flag-Helper
# ════════════════════════════════════════════════════════════════════════════

def is_v2_enabled() -> bool:
    """True wenn AEROTAX_CAS_READER_V2!=0. Default ON ab R24 (2026-05-27)."""
    import os
    return os.environ.get('AEROTAX_CAS_READER_V2', '1') != '0'
