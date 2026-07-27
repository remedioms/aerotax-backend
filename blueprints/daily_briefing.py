"""Daily Briefing — Backend-Berechnung nach Florians Spezifikation
(README_DAILY_BRIEFING_LOGIK.md, LH-Purser, per Mail 26.07.2026).

Das README ist die fachliche Wahrheit. Reihenfolge der Blöcke (fix):
  1. Header (Datum + ggf. Briefing Room)     — Room NUR wenn der erste echte
     Duty-Bestandteil des Tages ein Briefing ist (Hotel-Einträge überspringen)
  2. A/C Changes                             — nur wenn das VORHERIGE Leg in
     COMMON_CREW_ROTATION als aircraftChanged gekennzeichnet ist
  3. Long Transits (>= 80 min)
  4. Crew Changes                            — alle Flüge des Duty-Tages + der
     ERSTE Flug des nächsten Duty-Tages derselben Rotation (Return-Block)
  5. FDZ-Toleranz (immer)                    — MTV / EASA
  6. RZ-Toleranz (nur wenn Hotel am Tagesende)
  7. Hotel + Transferzeit + Pick-up (nur wenn Hotel)
Leere optionale Blöcke fallen weg. Ohne gültige Rotation gibt es KEIN Briefing
(ehrlich nichts, kein Teilergebnis).

DATENQUELLEN (alle am 27.07.2026 gegen echte PROD-Payloads verifiziert,
Umlauf 183706 FRA, A320-Kurzstrecke):
  * COMMON_CREW_ROTATION  rotations[].shifts[].legs[]:
      - `aircraftChanged` sitzt am VORHERIGEN Leg und heißt „nach diesem Leg
        wechselt das Gerät" (LH027 DAIRO→True, nächstes Leg LH332 DAIWJ;
        LH121 DAILC→True, nächstes LH1406 DAIBJ — mehrfach belegt).
      - `transit` = Bodenzeit NACH dem Leg in Minuten (LH075: 80 = 15:20−14:00);
        letztes Leg der Schicht trägt 0. Wir rechnen die Zeit trotzdem selbst
        aus den Leg-Zeiten (Plan-Zeiten in der Rotation können ein paar Minuten
        hinter dem Tagesplan herhinken) und nutzen `transit` nur als Fallback.
      - `hotelName` = LH-Klarname am HINFLUG-Leg zur Layover-Station
        ('Clayton Hotel Düsseldorf', 'IntercityHotel Budapest') — kann fehlen
        (ZAG-Nacht ohne hotelName trotz Hotel-Duty-Event). Das `hotel`-Flag ist
        unbrauchbar (False trotz Name) — nie auswerten.
      - `pickupTime` (UTC) + `pickupTimeLT` sitzen am RÜCKFLUG-Leg.
      - shifts[].attributes = LHs EIGENE Regelwerks-Rechnung, minutengenau
        nachvollzogen (siehe _tolerances): {CAB|COC}_MTV_FDZ/_MTV_MAX/_MTV_RZ/
        _MTV_RZ_ACTUAL + _LAW_FDZ/_LAW_MAX/_LAW_RZ. „LAW" = EASA:
          MTV_FDZ  = Briefing → letzte Landung + Debrief (Kont 30 min)  [belegt:
                     12:10Z→21:40Z + 30 = 600]
          LAW_FDZ  = Report → letzte Landung (EASA-FDP, ohne Debrief) [= 570]
          LAW_MAX  = EASA-Tabellen-Max nach Report-Ortszeit + Sektoren [= 690]
          MTV_RZ_ACTUAL = (Landung + MTV-Debrief) → nächstes Briefing [795 =
                     22:10Z→11:25Z am Folgetag; an 4 Schichten bewiesen]
        Ein *_LAW_RZ_ACTUAL existiert NICHT (siehe RZ-EASA-Hinweis unten).
  * COMMON_CREWLIST (braucht accessCode aus den Duty-Events-_links):
      crewMembers[] mit crewPosition (auch 'CP/TC'!), dutyCode (OD/DH),
      exFlight/toFlight ({flightDesignator, flightDate}) = Zu-/Abbringer.
  * COMMON_FLIGHT_LEG_DETAILS: departurePosition/arrivalPosition (Standplätze,
    können null sein — LIN lieferte null), Gates, STD/STA, Registration.
  * crew_hotel_directory (Endpoint /api/ax/crew-hotels, „ax_crew_hotels"):
    iata/base/hotel/transfer_min/votes — unser PEGMA-Äquivalent
    („Standard Fahrtzeiten"). AIRLINE-GETRENNT über
    app._viewer_airline_and_calendar + _canonical_airline_key +
    _crew_hotel_dir_serve: ein User sieht ausschließlich das Verzeichnis der
    EIGENEN Airline, ohne erkannte Airline kommt NICHTS (fail-closed — dieselbe
    Sicherheitslinie wie _filter_crew_hotels bei den Layover-Recs, die hier
    bewusst gar nicht angefasst werden).

RZ-Toleranz EASA — WARUM n/a: Florian verlangt für EASA eine ANDERE
Debrief-Annahme als MTV. LH liefert kein *_LAW_RZ_ACTUAL, und eine belastbare
Backend-Quelle für LHs EASA-Debrief-Annahme (OM-A/ORO.FTL-Minutenwert) gibt es
nicht — erfinden verboten. Die Formel ist fertig verdrahtet: sobald
_EASA_DEBRIEF_MIN gesetzt ist (Owner/Florian liefert den Wert), rechnet
_tolerances die EASA-Ist-Ruhe aus (letzte Landung + EASA-Debrief → nächstes
Briefing) gegen LAW_RZ. Bis dahin ist der EASA-Teil ehrlich `null`.

KOSTEN (LH-FlightOps-Key, 1.000 Calls/h): ein kalter Lauf des Beispiel-Tages
(4 Legs, 1 Rotation, 1 A/C-Wechsel==Long-Transit-Übergang) = 10 Calls:
1 COMMON_DUTY_EVENTS + 1 COMMON_CREW_ROTATION + 5 COMMON_CREWLIST +
2 COMMON_FLIGHT_LEG_DETAILS + 1 COMMON_CHECK_IN_TIMES (nur an Briefing-Tagen).
Alles zählt automatisch in den lhfo:-Stundenzähler (_api_get →
_flightops_budget_inc, sichtbar in /api/ax/lh-quota). Schutz:
  * Ergebnis-Cache pro (token, date), TTL 10 min (Antworten sind
    ROLLENSPEZIFISCH — nie über User teilen).
  * Stunden-Notbremse wie beim Pickup: lhfo-Stand >= _LHFO_HOUR_CEILING →
    stale Cache oder 503, NIE den Roster-Kernpfad aushungern.
"""
import re
import time
import logging
import threading
import unicodedata
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, request

log = logging.getLogger('aerotax')
daily_briefing_bp = Blueprint('daily_briefing_bp', __name__)

# ── Fachliche Konstanten (Florian) ───────────────────────────────────────────
HOMEBASES = ('FRA', 'MUC')          # Crew-Change-Logik: FRA und MUC sind Homebase
LONG_TRANSIT_MIN = 80               # „sobald die geplante Transitzeit mindestens 80 Minuten beträgt"
POSITION_ORDER = {'CP': 0, 'FO': 1, 'AC': 2, 'P1': 3, 'FB': 4, 'AK': 5}
SYM_BASE_START = '◎'           # ◎ Dienstbeginn Homebase ohne Vorflug
SYM_CONN = '✈︎'           # ✈︎ Anschluss/Zu-/Abbringer am selben Tag
SYM_HOTEL = '⌂'                # ⌂ Hotel/Verbleib Außenstation
SYM_BASE_END = '⚐'             # ⚐ Dienstende Homebase
ARROW = '➜'                    # ➜ (Statuswechsel, Stand-Wechsel)

# EASA-Debrief-Annahme in Minuten (Ende Flugzeit → Ende EASA-Dienstzeit).
# BEWUSST None: LH liefert kein *_LAW_RZ_ACTUAL, und ohne belegte Quelle wird
# hier NICHTS erfunden (Florians eigener Punkt: MTV- und EASA-Debrief-Annahme
# sind verschieden und dürfen nicht vermischt werden). Sobald der echte
# OM-A-Wert vorliegt, diesen setzen — die RZ-EASA-Rechnung geht dann automatisch
# an (siehe _tolerances).
_EASA_DEBRIEF_MIN = None

_LHFO_HOUR_CEILING = 800            # wie lh_flightops._ROT_LHFO_HOUR_CEILING

# Ergebnis-Cache pro (token, date). Kurz: das Briefing lebt von tagesaktuellen
# Ständen/Crews; 10 min glätten nur wiederholtes Öffnen.
_CACHE_TTL_S = 600.0
_cache = {}
_cache_lock = threading.Lock()

_ISO_RE = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}')
_MONTHS = ('JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN',
           'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC')


# ── Zeit-/Format-Helper (pure) ───────────────────────────────────────────────
def _dt(iso):
    """ISO-UTC → aware datetime oder None. Nie raten."""
    s = str(iso or '').strip()
    if not _ISO_RE.match(s):
        return None
    try:
        d = datetime.fromisoformat(s.replace('Z', '+00:00'))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _min_between(a_iso, b_iso):
    a, b = _dt(a_iso), _dt(b_iso)
    if not a or not b:
        return None
    return int(round((b - a).total_seconds() / 60.0))


def _fmt_hm(minutes):
    """Minuten → 'H:MM' (negativ mit '-'). None → 'n/a'."""
    if minutes is None:
        return 'n/a'
    m = int(minutes)
    sign = '-' if m < 0 else ''
    m = abs(m)
    return f'{sign}{m // 60}:{m % 60:02d}'


def _day_label(date_str):
    """'2026-07-27' → '27JUL' (Florians Return-Format)."""
    try:
        d = datetime.strptime(date_str[:10], '%Y-%m-%d')
        return f'{d.day:02d}{_MONTHS[d.month - 1]}'
    except Exception:
        return date_str


def _utc_day(iso):
    d = _dt(iso)
    return d.strftime('%Y-%m-%d') if d else None


def _local_hhmm(iso_utc, iata):
    """Ortszeit 'HH:MM' an einer Station — über app._ics_local_hhmm_at
    (bestehende, getestete TZ-Auflösung). None wenn nicht bestimmbar."""
    try:
        import app as _app
        return _app._ics_local_hhmm_at(iso_utc, (iata or '').upper() or None)
    except Exception:
        return None


def _reg_short(reg):
    """'DAILF'/'D-AILF' → '-ILF' (Florians Beispiel '-ABC': Bindestrich +
    letzte drei Stellen der Registrierung)."""
    r = re.sub(r'[^A-Z0-9]', '', str(reg or '').upper())
    return ('-' + r[-3:]) if len(r) >= 3 else None


# ── COMMON_CREW_ROTATION → normalisierte Shifts/Legs (pure) ─────────────────
def _as_list(v):
    """LH-Known-Issue: Ein-Element-Arrays kommen als Skalar."""
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


_DEP_KEYS = ('depatureDate', 'departureDate')   # LH-Typo, beide lesen


def rotation_shifts(resp):
    """COMMON_CREW_ROTATION-Response → Liste normalisierter Shifts (sortiert
    nach shiftBegin), jeder mit sortierten Legs. Pure/testbar."""
    shifts = []
    for rot in _as_list((resp or {}).get('rotations') if isinstance(resp, dict) else None):
        if not isinstance(rot, dict):
            continue
        rn = str(rot.get('rotationNumber') or '').strip()
        for sh in _as_list(rot.get('shifts')):
            if not isinstance(sh, dict):
                continue
            legs = []
            for lg in _as_list(sh.get('legs')):
                if not isinstance(lg, dict):
                    continue
                dep_iso = ''
                for k in _DEP_KEYS:
                    if str(lg.get(k) or '').strip():
                        dep_iso = str(lg[k]).strip()
                        break
                dep = str(lg.get('departureAirport') or '').upper().strip()
                arr = str(lg.get('arrivalAirport') or '').upper().strip()
                if len(dep) != 3 or len(arr) != 3 or not _ISO_RE.match(dep_iso):
                    continue
                legs.append({
                    'flight': re.sub(r'\s', '',
                                     str(lg.get('flightDesignator') or '').upper()),
                    'dep': dep, 'arr': arr,
                    'dep_iso': dep_iso,
                    'arr_iso': str(lg.get('arrivalDate') or '').strip(),
                    'reg': str(lg.get('aircraftRegistration') or '').strip() or None,
                    'ac_changed': bool(lg.get('aircraftChanged') is True
                                       or str(lg.get('aircraftChanged')).strip().lower() == 'true'),
                    'transit_min': lg.get('transit') if isinstance(lg.get('transit'), int) else None,
                    'duty_code': str(lg.get('dutyCode') or '').strip(),
                    'hotel_name': str(lg.get('hotelName') or '').strip() or None,
                    'pickup_utc': str(lg.get('pickupTime') or '').strip() or None,
                    'pickup_lt': str(lg.get('pickupTimeLT') or '').strip() or None,
                })
            legs.sort(key=lambda x: x['dep_iso'])
            shifts.append({
                'rotation': rn,
                'homebase': str(rot.get('homebase') or '').upper().strip() or None,
                'shift_no': sh.get('shiftNumber'),
                'begin': str(sh.get('shiftBegin') or '').strip() or None,
                'end': str(sh.get('shiftEnd') or '').strip() or None,
                'briefing_cab': str(sh.get('briefingBeginCab') or '').strip() or None,
                'briefing_coc': str(sh.get('briefingBeginCoc') or '').strip() or None,
                'attributes': sh.get('attributes') if isinstance(sh.get('attributes'), dict) else {},
                'legs': legs,
            })
    shifts.sort(key=lambda s: (s['begin'] or (s['legs'][0]['dep_iso'] if s['legs'] else '')))
    return shifts


def shift_for_date(shifts, date_str):
    """Der Duty-Tag = bevorzugt die Schicht, deren ERSTES Leg am UTC-Tag
    `date_str` abgeht. (Adversarialer Review: eine Red-Eye-Schicht des VORTAGS
    hat zwar ein 00:30Z-Leg am Tag — sie ist aber nicht dessen Duty; ohne die
    Bevorzugung verdeckte sie die echte Schicht des Tages und lieferte deren
    FDZ/RZ/Crews falsch.) Nur wenn KEINE Schicht am Tag beginnt, zählt eine
    Schicht mit irgendeinem Leg am Tag (Übernacht-Duty, deren Tail-Leg der
    einzige Dienst des Tages ist). None wenn nichts passt."""
    for sh in shifts:
        if sh['legs'] and _utc_day(sh['legs'][0]['dep_iso']) == date_str:
            return sh
    for sh in shifts:
        if any(_utc_day(lg['dep_iso']) == date_str for lg in sh['legs']):
            return sh
    return None


def day_legs(shift, date_str):
    """Die Legs des Duty-Tages IN Reihenfolge. Eine Schicht kann über
    Mitternacht laufen — es zählen die Legs, die am gewählten Tag ODER später
    in derselben Schicht abgehen (Red-Eye: das 00:30Z-Leg gehört zum Tag der
    Schicht)."""
    if not shift:
        return []
    legs = [lg for lg in shift['legs'] if _utc_day(lg['dep_iso']) == date_str]
    if legs:
        # Folge-Legs derselben Schicht nach Mitternacht gehören zum selben Duty-Tag.
        first_idx = shift['legs'].index(legs[0])
        return shift['legs'][first_idx:]
    return []


def next_shift_after(shifts, shift):
    """Die nächste Schicht derselben Rotation (für den Return-Block) oder None."""
    if not shift:
        return None
    try:
        i = shifts.index(shift)
    except ValueError:
        return None
    for nxt in shifts[i + 1:]:
        if nxt['rotation'] == shift['rotation'] and nxt['legs']:
            return nxt
    return None


# ── Block 2/3: A/C Changes + Long Transits (pure, Details injiziert) ────────
def transitions(legs):
    """[(prev, nxt), …] direkt aufeinanderfolgender Legs."""
    return list(zip(legs, legs[1:]))


def build_ac_changes(legs, details_for):
    """A/C-Change-Einträge. Nur wenn das VORHERIGE Leg als aircraftChanged
    gekennzeichnet ist (Florian; am echten Payload bewiesen: das Flag am Leg i
    heißt „nach Leg i wechselt das Gerät"). Fehlen Ankunfts- oder Abflugstand,
    wird der Eintrag WEGGELASSEN (Florians Fallback), nie geraten.
    `details_for(leg) → dict` ist der (gecachte) COMMON_FLIGHT_LEG_DETAILS-Zugriff."""
    out = []
    for prev, nxt in transitions(legs):
        if not prev['ac_changed']:
            continue
        d_prev = details_for(prev) or {}
        d_nxt = details_for(nxt) or {}
        arr_pos = (d_prev.get('arrivalPosition') or '').strip() or None
        dep_pos = (d_nxt.get('departurePosition') or '').strip() or None
        reg = d_nxt.get('aircraftRegistration') or nxt.get('reg')
        gap = _min_between(prev['arr_iso'], nxt['dep_iso'])
        if gap is None and isinstance(prev.get('transit_min'), int) and prev['transit_min'] > 0:
            gap = prev['transit_min']
        if not (arr_pos and dep_pos and reg and gap is not None):
            continue    # notwendige Leg-Details fehlen → Eintrag weglassen
        out.append({
            'flight': nxt['flight'], 'route': f"{nxt['dep']} - {nxt['arr']}",
            'reg': reg, 'reg_short': _reg_short(reg),
            'arr_position': arr_pos, 'dep_position': dep_pos,
            'gap_min': gap,
            'line': (f"A/C Change | {nxt['flight']}: {nxt['dep']} - {nxt['arr']} | "
                     f"{_reg_short(reg)}: {arr_pos} {ARROW} {dep_pos} in {_fmt_hm(gap)}"),
        })
    return out


def build_long_transits(legs, details_for):
    """Long-Transit-Einträge (>= 80 min, Transit exakt 80 zählt). Bodenzeit wird
    aus den Leg-Zeiten gerechnet; `transit` aus der Rotation nur als Fallback.
    Ohne Abflugstand des Folge-Legs wird der Eintrag weggelassen (Fallback-Regel)."""
    out = []
    for prev, nxt in transitions(legs):
        gap = _min_between(prev['arr_iso'], nxt['dep_iso'])
        if gap is None and isinstance(prev.get('transit_min'), int) and prev['transit_min'] > 0:
            gap = prev['transit_min']
        if gap is None or gap < LONG_TRANSIT_MIN:
            continue
        d_nxt = details_for(nxt) or {}
        dep_pos = (d_nxt.get('departurePosition') or '').strip() or None
        arr_lcl = _local_hhmm(prev['arr_iso'], prev['arr'])
        if not (dep_pos and arr_lcl):
            continue    # notwendige Details fehlen → weglassen
        out.append({
            'flight': nxt['flight'], 'route': f"{nxt['dep']} - {nxt['arr']}",
            'arr_local': arr_lcl, 'duration_min': gap, 'dep_position': dep_pos,
            'line': (f"Long Transit | {nxt['flight']}: {nxt['dep']} - {nxt['arr']} | "
                     f"at {arr_lcl} for {_fmt_hm(gap)} @ {dep_pos}"),
        })
    return out


# ── Block 4: Crew Changes (pure Kernlogik) ──────────────────────────────────
def norm_crewlist(resp):
    """COMMON_CREWLIST → [{pk, pos, pos_raw, last, first, duty, ex, to}].
    ex/to = {'flight', 'date'} des Zu-/Abbringers oder None. Pure/testbar."""
    out = []
    for m in _as_list((resp or {}).get('crewMembers') if isinstance(resp, dict) else None):
        if not isinstance(m, dict):
            continue
        pos_raw = str(m.get('crewPosition') or '').strip().upper()
        pos = pos_raw.split('/')[0].strip()   # 'CP/TC' → 'CP' (Sortier-/Anzeigetoken)

        def _ref(v):
            if not isinstance(v, dict):
                return None
            f = re.sub(r'\s', '', str(v.get('flightDesignator') or '').upper())
            d = str(v.get('flightDate') or '')[:10]
            return {'flight': f, 'date': d} if f else None
        out.append({
            'pk': str(m.get('pkNumber') or '').strip(),
            'pos': pos or '?', 'pos_raw': pos_raw,
            'last': str(m.get('lastName') or '').strip().upper(),
            'first': str(m.get('firstName') or '').strip().upper(),
            'duty': str(m.get('dutyCode') or '').strip().upper() or 'OD',
            'ex': _ref(m.get('exFlight')), 'to': _ref(m.get('toFlight')),
        })
    return out


def _crew_sort_key(e):
    return (POSITION_ORDER.get(e['pos'], 9), e['last'], e['first'])


def _conn_display(sym, ref, route, kind, at_home):
    """Anzeige nach Florians Tabellen: NUR die Homebase-Fälle zeigen
    'Flugnummer - Ort' — an der Außenstation steht das nackte ✈︎. Ist der Ort
    an der Homebase nicht auflösbar, bleibt als Fallback '✈︎ Flugnummer'
    (Florian)."""
    if sym != SYM_CONN or not ref or not at_home:
        return sym
    if route:
        ort = route[0] if kind == 'incoming' else route[1]   # Abflug- bzw. Ankunftsort
        if ort:
            return f"{SYM_CONN} {ref['flight']} - {ort}"
    return f"{SYM_CONN} {ref['flight']}"


def crew_change_block(prev_crew, next_crew, ref_leg, briefing_date, resolve_route,
                      from_next_shift=False):
    """Crew-Vergleich EINES Übergangs. Florians Regeln:
      Outgoing  = auf prev, nicht auf next; Incoming = umgekehrt;
      Statuswechsel (Duty ändert sich) erscheint auf BEIDEN Seiten.
      Homebase-Kontext = Abflugstation des nächsten gemeinsamen Legs
      (FRA/MUC); „selber Tag" = Kalendertag; Außenstation ohne Anschluss → ⌂.
    `resolve_route(flight, date) → (dep, arr)|None` (kostenlos, best-effort).
    Rückgabe None, wenn es keinerlei Änderungen gibt (Block fällt weg)."""
    if prev_crew is None or next_crew is None:
        return None
    station = ref_leg['dep']
    at_home = station in HOMEBASES
    ref_day = _utc_day(ref_leg['dep_iso'])
    # Return-Kennzeichnung NUR für den Übergang zur NÄCHSTEN Schicht (über den
    # Layover hinweg, Florians „Return @ …"). Ein Red-Eye-Folge-Leg derselben
    # Schicht nach Mitternacht ist eine direkte Fortsetzung, kein Return
    # (adversarialer Review).
    is_return = bool(from_next_shift and briefing_date and ref_day
                     and ref_day != briefing_date)
    prev_by = {e['pk']: e for e in prev_crew if e['pk']}
    next_by = {e['pk']: e for e in next_crew if e['pk']}

    def _same_day(ref):
        return bool(ref and ref.get('date') and ref_day and ref['date'] == ref_day)

    def _out_entry(e, status_to=None):
        cont = e.get('to') if _same_day(e.get('to')) else None
        if at_home:
            sym = SYM_CONN if cont else SYM_BASE_END
        else:
            sym = SYM_CONN if cont else SYM_HOTEL
        route = resolve_route(cont['flight'], cont['date']) if (cont and at_home) else None
        disp = _conn_display(sym, cont, route, 'outgoing', at_home)
        hint = f" ({e['duty']} {ARROW} {status_to})" if status_to else (
            " (DH)" if e['duty'] == 'DH' else '')
        return {'pk': e['pk'], 'pos': e['pos'], 'name': f"{e['last']}, {e['first']}",
                'duty': e['duty'], 'symbol': sym, 'ref': cont,
                'line': f"{e['pos']} {e['last']}, {e['first']}{hint} {disp}".strip()}

    def _in_entry(e, status_from=None):
        feeder = e.get('ex') if _same_day(e.get('ex')) else None
        if at_home:
            sym = SYM_CONN if feeder else SYM_BASE_START
        else:
            sym = SYM_CONN if feeder else SYM_HOTEL
        route = resolve_route(feeder['flight'], feeder['date']) if (feeder and at_home) else None
        disp = _conn_display(sym, feeder, route, 'incoming', at_home)
        hint = f" ({status_from} {ARROW} {e['duty']})" if status_from else (
            " (DH)" if e['duty'] == 'DH' else '')
        return {'pk': e['pk'], 'pos': e['pos'], 'name': f"{e['last']}, {e['first']}",
                'duty': e['duty'], 'symbol': sym, 'ref': feeder,
                'line': f"{e['pos']} {e['last']}, {e['first']}{hint} {disp}".strip()}

    outgoing, incoming = [], []
    for e in sorted(prev_crew, key=_crew_sort_key):
        if not e['pk']:
            continue
        n = next_by.get(e['pk'])
        if n is None:
            outgoing.append(_out_entry(e))
        elif n['duty'] != e['duty']:
            outgoing.append(_out_entry(e, status_to=n['duty']))   # alter Bezug
    for e in sorted(next_crew, key=_crew_sort_key):
        if not e['pk']:
            continue
        p = prev_by.get(e['pk'])
        if p is None:
            incoming.append(_in_entry(e))
        elif p['duty'] != e['duty']:
            incoming.append(_in_entry(e, status_from=p['duty']))  # neuer Bezug
    if not outgoing and not incoming:
        return None
    ref_label = (f"Return @ {_day_label(ref_day)}" if is_return
                 else f"{ref_leg['flight']}: {ref_leg['dep']} - {ref_leg['arr']}")
    return {'ref': ref_label, 'is_return': is_return,
            'flight': ref_leg['flight'], 'station': station,
            'outgoing': outgoing, 'incoming': incoming}


# ── Block 5/6: FDZ-/RZ-Toleranz aus den Shift-Attributen ────────────────────
def _attr_prefix(attrs):
    """'CAB' oder 'COC' — je nachdem, welche Regelwerks-Keys LH für die Rolle
    des eingeloggten Users liefert. None wenn keine da sind."""
    for p in ('CAB', 'COC'):
        if f'{p}_MTV_FDZ' in (attrs or {}) or f'{p}_LAW_FDZ' in (attrs or {}):
            return p
    return None


def _tolerances(attrs, mtv_debrief_min=30):
    """LHs eigene Regelwerks-Minuten → Toleranzen. Semantik am echten Payload
    minutengenau verifiziert (siehe Modul-Docstring):
      FDZ-Toleranz  MTV  = MTV_MAX − MTV_FDZ     EASA = LAW_MAX − LAW_FDZ
      RZ-Toleranz   MTV  = MTV_RZ_ACTUAL − MTV_RZ
                    EASA = nur mit gesetzter _EASA_DEBRIEF_MIN-Annahme —
                           LH liefert kein LAW_RZ_ACTUAL, und die EASA-Ist-Ruhe
                           hängt an der (anderen!) Debrief-Annahme. Ohne Quelle
                           ehrlich None.
    Werte 0 heißen bei LH „nicht belegt" (DH-only-Schichten tragen 0/0) → None."""
    p = _attr_prefix(attrs)
    if not p:
        return None

    def _v(key):
        v = (attrs or {}).get(f'{p}_{key}')
        return int(v) if isinstance(v, (int, float)) and int(v) > 0 else None
    fdz_mtv = fdz_easa = rz_mtv = None
    if _v('MTV_MAX') is not None and _v('MTV_FDZ') is not None:
        fdz_mtv = _v('MTV_MAX') - _v('MTV_FDZ')
    if _v('LAW_MAX') is not None and _v('LAW_FDZ') is not None:
        fdz_easa = _v('LAW_MAX') - _v('LAW_FDZ')
    if _v('MTV_RZ_ACTUAL') is not None and _v('MTV_RZ') is not None:
        rz_mtv = _v('MTV_RZ_ACTUAL') - _v('MTV_RZ')
    rz_easa = None
    rz_easa_reason = 'easa_debrief_assumption_unavailable'
    if _EASA_DEBRIEF_MIN is not None and _v('LAW_RZ') is not None \
            and _v('MTV_RZ_ACTUAL') is not None:
        # EASA-Ist-Ruhe = MTV-Ist-Ruhe, korrigiert um die Debrief-Differenz
        # (MTV-Ruhe beginnt nach MTV-Debrief; EASA nach EASA-Debrief) — die
        # Differenz der Annahmen verschiebt den Ruhebeginn 1:1.
        # `mtv_debrief_min`: 30 = Kont & Interkont ab Homebase; Interkont VON
        # UNTERWEGS rechnet MTV mit 60 — der Aufrufer muss das beim Aktivieren
        # der EASA-Annahme aus dem Rotations-Kontext mitgeben (nicht hier
        # raten — genau die Vermischung, die Florian verbietet).
        easa_actual = _v('MTV_RZ_ACTUAL') + (int(mtv_debrief_min)
                                             - int(_EASA_DEBRIEF_MIN))
        rz_easa = easa_actual - _v('LAW_RZ')
        rz_easa_reason = None
    return {'crew_category': p,
            'fdz': {'mtv_min': fdz_mtv, 'easa_min': fdz_easa,
                    'line': f"FDZ-Toleranz | MTV {_fmt_hm(fdz_mtv)} / EASA {_fmt_hm(fdz_easa)}"},
            'rz': {'mtv_min': rz_mtv, 'easa_min': rz_easa,
                   'easa_unavailable_reason': rz_easa_reason,
                   'line': f"RZ-Toleranz | MTV {_fmt_hm(rz_mtv)} / EASA {_fmt_hm(rz_easa)}"}}


# ── Block 7: Hotel, Transferzeit, Pick-up ───────────────────────────────────
_GENERIC_HOTEL_TOKENS = {'hotel', 'hotels', 'the', 'by', 'and', 'ehem', 'am', 'an', 'im'}


def _fold(s):
    s = unicodedata.normalize('NFKD', str(s or ''))
    return ''.join(c for c in s if not unicodedata.combining(c)).lower()


def _degeneric(tok):
    """'intercityhotel' → 'intercity'. Ein ANGEKLEBTES generisches Wort am
    ENDE eines Tokens wird abgetrennt.

    Der konkrete Fall (BUD, 27.07.): das Verzeichnis kennt 'Intercity
    Budapest', LH bucht 'IntercityHotel Budapest' — dasselbe Haus, aber die
    Token-Mengen {intercity, budapest} und {intercityhotel, budapest} sind
    ungleich. Bewusst NUR als Suffix und nur für die Wurzeln 'hotel(s)': ein
    Präfix-Strip ('Hotelissimo' → 'issimo') oder eine breitere Wortliste wäre
    genau die Fuzzy-Magie, die hier schon zweimal zwei Häuser verschmolzen hat.
    Der Rest muss ≥ 3 Zeichen behalten UND darf nicht selbst generisch sein
    ('thehotel' → 'the' wäre danach leer und träfe jedes Haus)."""
    for g in ('hotels', 'hotel'):
        if tok.endswith(g) and len(tok) - len(g) >= 3:
            rest = tok[:-len(g)]
            return tok if rest in _GENERIC_HOTEL_TOKENS else rest
    return tok


def _hotel_tokens(name, degeneric=False):
    """Hotelname → signifikante Token-Menge (Klammern raus, Akzente gefaltet).
    Deterministisch — keine Fuzzy-Magie.

    `degeneric` löst zusätzlich angeklebte 'hotel'-Suffixe auf und ist damit
    LOCKERER. Das bleibt bewusst dem Wechsel-Pfad vorbehalten
    (`hotel_supersede_plan`) und wirkt NIE auf `transfer_match`: an einer
    Station mit zwei Crowd-Schreibweisen desselben Hauses ('Parkhotel Bremen'
    und 'Park Hotel Bremen', beide erlaubt) würde die lockere Sicht aus einem
    eindeutigen Treffer zwei machen — und aus einer korrekten Fahrtzeit ein
    N/A (adversarialer Review 27.07.). Der Wechsel-Pfad verträgt das: dort
    heisst „mehrdeutig" schlicht „nichts anfassen"."""
    s = re.sub(r'\([^)]*\)', ' ', str(name or ''))
    toks = {t for t in re.split(r'[^a-z0-9]+', _fold(s)) if t}
    if degeneric:
        toks = {_degeneric(t) for t in toks}
    return toks - _GENERIC_HOTEL_TOKENS


def _paren_tokens(name):
    """Nur der KLAMMER-Inhalt als Token-Menge. Im crowdgesourcten Verzeichnis
    ist die Klammer mal eine Notiz ('(ehem. Nikko)'), mal das einzige
    Unterscheidungsmerkmal zweier Häuser ('(Airport)' vs '(City)')."""
    toks = set()
    for m in re.findall(r'\(([^)]*)\)', str(name or '')):
        toks |= {t for t in re.split(r'[^a-z0-9]+', _fold(m)) if t}
    return toks - _GENERIC_HOTEL_TOKENS


def _same_house_loose(lh_name, dir_name):
    """Wie `_same_house`, löst zusätzlich angeklebte 'hotel'-Suffixe auf
    ('IntercityHotel Budapest' == 'Intercity Budapest'). NUR für den
    Hotelwechsel-Pfad — s. `_hotel_tokens`."""
    lt = _hotel_tokens(lh_name, degeneric=True)
    if not lt or lt != _hotel_tokens(dir_name, degeneric=True):
        return False
    pl, pd = _paren_tokens(lh_name), _paren_tokens(dir_name)
    if pl and pd and pl != pd:
        return False
    return True


def _same_house(lh_name, dir_name):
    """Bezeichnen zwei Schreibweisen DASSELBE Haus? Grundlage ist die Gleichheit
    der signifikanten Token-Mengen (Teilmenge reicht bewusst NICHT, siehe
    transfer_match). Zusätzlich der Klammer-Riegel (adversarialer Review
    27.07.): trägt EINE Seite eine Klammer, ist sie eine Notiz und wird
    ignoriert ('Clayton Hotel Düsseldorf' == 'Clayton Hotel Düsseldorf (ehem.
    Nikko)'). Tragen BEIDE Seiten eine Klammer mit VERSCHIEDENEM Inhalt, ist
    genau sie die Unterscheidung — dann sind es zwei Häuser:
    'Mercure Hotel Frankfurt (Airport)' ist nicht '… (City)'. Ohne diesen
    Riegel lieferte der Abgleich den richtigen Namen mit der Fahrtzeit des
    anderen Hauses, unmarkiert — der teuerste Fehler dieser Funktion."""
    lt = _hotel_tokens(lh_name)
    if not lt or lt != _hotel_tokens(dir_name):
        return False
    pl, pd = _paren_tokens(lh_name), _paren_tokens(dir_name)
    if pl and pd and pl != pd:
        return False
    return True


def transfer_match(iata, lh_name, directory):
    """Florians vier Zuordnungsregeln, 1:1:
      1. passendes Hotel → dessen Zeit (LHs Klarname ist der primäre Schlüssel)
      2. Destination ohne Eintrag → N/A
      3. Destination bekannt, Hotel nicht eindeutig zuordenbar (andere
         Schreibweise/kein LH-Name) → allgemeine Destinations-Zeit mit '*' —
         NUR wenn die Destination genau EIN Hotel im Verzeichnis hat
      4. Destination mit MEHREREN Hotels → ausschließlich eindeutiger Treffer,
         sonst N/A
    „passend" = normalisierte Token-Mengen GLEICH (Klammern/Akzente/generische
    Wörter wie 'Hotel' zählen nicht — 'Leonardo Royal Venice Mestre' trifft
    'Leonardo Royal Hotel Venice Mestre'). Eine bloße TEILMENGE gilt bewusst
    NICHT als Treffer: 'Hilton Frankfurt Airport' ist ein ANDERES Haus als
    'Hilton Garden Inn Frankfurt Airport' (adversarialer Review — der
    Teilmengen-Match hätte hier das falsche Hotel als Regel-1-Treffer
    ausgegeben). transfer_min <= 0 gilt als „keine Zeit hinterlegt"
    (crowdsource-Default 0) → N/A.

    `matched` ist die EINZIGE Aussage darüber, ob LHs Name dieses Haus wirklich
    bezeichnet — nur dann darf angereichert werden. Der Reason allein reicht
    nicht: 'no_time_recorded' konnte früher BEIDES heißen (Namens-Treffer ohne
    Zeit ODER Regel-3-Rückfall ohne Zeit), und der Anreicherer hat den Rückfall
    als Treffer gelesen. Live-Folge auf dem echten Prod-Payload: LHs
    'IntercityHotel Budapest' wurde auf die BUD-Zeile 'Hilton Garden Inn
    Budapest City Centre' geschrieben — zwei Häuser verschmolzen (adversarialer
    Review 27.07.). Darum getrennte Reasons + explizites Flag.
    → {'row', 'transfer_min', 'marker', 'matched', 'reason'}"""
    cands = [r for r in (directory or [])
             if str(r.get('iata') or '').upper() == (iata or '').upper()
             and str(r.get('hotel') or '').strip()]
    if not cands:
        return {'row': None, 'transfer_min': None, 'marker': None,
                'matched': False, 'reason': 'no_entry'}

    def _usable(row):
        try:
            return int(row.get('transfer_min') or 0) > 0
        except Exception:
            return False
    if lh_name:
        matches = [r for r in cands if _same_house(lh_name, r.get('hotel'))]
        if len(matches) == 1:
            hit = matches[0]
            if not _usable(hit):
                return {'row': hit, 'transfer_min': None, 'marker': None,
                        'matched': True, 'reason': 'no_time_recorded'}
            return {'row': hit, 'transfer_min': int(hit['transfer_min']),
                    'marker': '', 'matched': True, 'reason': 'exact'}
        # kein (oder mehrdeutiger) Namens-Treffer → Regel 3/4
    if len(cands) == 1:
        r = cands[0]
        if not _usable(r):
            return {'row': r, 'transfer_min': None, 'marker': None,
                    'matched': False, 'reason': 'destination_general_no_time'}
        return {'row': r, 'transfer_min': int(r['transfer_min']),
                'marker': '*', 'matched': False, 'reason': 'destination_general'}
    return {'row': None, 'transfer_min': None, 'marker': None,
            'matched': False, 'reason': 'ambiguous_multi_hotel'}


_LH_HOTEL_PLACEHOLDER_RE = re.compile(r'^(H\d{4,}|N/?A|NA|TBD|UNKNOWN|[-.]+)$',
                                      re.IGNORECASE)


def _valid_lh_hotel_name(name):
    """True nur für echte Klarnamen. LH-interne Hotel-Codes ('H9941671', so in
    der Doku-Fixture), literale Platzhalter ('N/A' — beim briefingRoom live
    gesehen, hier vorsorglich genauso behandelt) und Kurz-Müll fallen durch.
    Gilt für BEIDE Wege: Anzeige (hotel_block) und Verzeichnis-Schreibpfad
    (_sync_official_name). Ohne den Anzeige-Gate stand am Layover-Abend
    „Hotel | N/A (0:30*)" auf der Karte — ein Platzhalter, der wie ein
    Hotelname aussieht (adversarialer Review 27.07.)."""
    s = str(name or '').strip()
    if len(s) < 3 or len(s) > 160:
        return False
    if _LH_HOTEL_PLACEHOLDER_RE.match(re.sub(r'\s', '', s)):
        return False
    return bool(re.search(r'[A-Za-zÀ-ÿ]{3}', s))


def hotel_block(shift, legs_today, all_legs, directory, hotel_event_days):
    """Hotel-Block NUR, wenn die Rotation nach dem letzten Leg des Tages in ein
    Hotel führt. Pick-up = der Wert am RÜCKFLUG-Leg, also dem ERSTEN Leg, das
    NACH der Ankunft wieder an der Layover-Station startet — bei mehrtägigem
    Layover ist das automatisch der Abholtag am ENDE der Hotelphase (Florians
    Kernpunkt; hier wird aus ECHTEN Folge-Legs gebaut, nie aus einer
    Tage-Annahme — die Tibor-„Tag 2/2"-Fehlerklasse ist damit konstruktiv
    ausgeschlossen). Kein Rückflug im Fenster → Pick-up ehrlich None."""
    if not legs_today:
        return None
    last = legs_today[-1]
    station = last['arr']
    lh_name = last.get('hotel_name')
    # Platzhalter sind KEIN Klarname: LH schickt in Namensfeldern literal 'N/A'
    # (beim briefingRoom belegt) bzw. interne Codes ('H9941671'). Ungegated
    # rutschte das als „Hotel | N/A (0:30*)" auf die Karte — ein Platzhalter mit
    # echter Transferzeit daneben. Ausgefiltert VOR allem anderen, damit auch
    # der Rückfall-Scan und die Hotel-Evidenz nur echte Namen sehen.
    if not _valid_lh_hotel_name(lh_name):
        lh_name = None
    if not lh_name:
        # LH hängt den Namen an den HINFLUG zur Station. Bei ZWEI Besuchen
        # derselben Station zählt der SPÄTESTE Hinflug vor dieser Nacht, nicht
        # der erste (adversarialer Review); notfalls irgendein Besuch.
        best = None
        for lg in all_legs:
            if lg['arr'] == station and _valid_lh_hotel_name(lg.get('hotel_name')):
                if lg.get('arr_iso') and lg['arr_iso'] <= (last.get('arr_iso') or '~'):
                    if best is None or lg['arr_iso'] > best['arr_iso']:
                        best = lg
        if best is None:
            best = next((lg for lg in all_legs
                         if lg['arr'] == station
                         and _valid_lh_hotel_name(lg.get('hotel_name'))), None)
        lh_name = best['hotel_name'] if best else None
    has_hotel = bool(lh_name) or any(d[1] == station for d in hotel_event_days)
    # Unterdrückt wird der Block an der ECHTEN Homebase des Umlaufs (Rotation
    # trägt sie); nur ohne diesen Wert fällt die Prüfung auf FRA/MUC zurück.
    # Eine MUC-Crew mit Nightstop FRA behält Hotel- und RZ-Block
    # (adversarialer Review — README kennt keine Homebase-Ausnahme fürs Hotel,
    # aber an der eigenen Base gibt es schlicht kein Crew-Hotel).
    hb_station = (shift or {}).get('homebase')
    at_base = (station == hb_station) if hb_station else (station in HOMEBASES)
    if not has_hotel or at_base:
        return None
    # Rückflug-Leg = erstes Leg ab `station` nach der Ankunft (ganze Rotation).
    ret = None
    for lg in all_legs:
        if lg['dep'] == station and lg['dep_iso'] > last['arr_iso']:
            ret = lg
            break
    pickup_utc = (ret or {}).get('pickup_utc')
    pickup_lt = _local_hhmm(pickup_utc, station) if pickup_utc else None
    if not pickup_lt and (ret or {}).get('pickup_lt'):
        m = re.search(r'(\d{1,2}):(\d{2})', str(ret['pickup_lt']).split('T')[-1])
        if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
            pickup_lt = f"{int(m.group(1)):02d}:{m.group(2)}"
    tm = transfer_match(station, lh_name, directory)
    display_name = lh_name or ((tm['row'] or {}).get('hotel') if tm['reason'] in (
        'destination_general', 'destination_general_no_time',
        'no_time_recorded') else None)
    transfer_txt = ('N/A' if tm['transfer_min'] is None
                    else _fmt_hm(tm['transfer_min']) + (tm['marker'] or ''))
    pu_txt = f"PU @ {pickup_lt}lcl" if pickup_lt else 'PU n/a'
    return {
        'station': station, 'hotel': display_name,
        'hotel_source': ('lh' if lh_name else ('directory' if display_name else None)),
        'transfer_min': tm['transfer_min'], 'transfer_marker': tm['marker'],
        'transfer_reason': tm['reason'],
        # Die NACHT, in der die Crew hier schläft (Ankunft des letzten Legs).
        # Evidenz-Schlüssel des Hotelwechsel-Pfads: gezählt werden Nächte,
        # nicht Payloads — acht Crews derselben Rotation sind EIN Ereignis.
        'night_of': _utc_day(last.get('arr_iso')),
        'pickup_utc': pickup_utc, 'pickup_local': pickup_lt,
        'pickup_day': _utc_day((ret or {}).get('dep_iso')) if ret else None,
        'return_flight': (ret or {}).get('flight'),
        'line': f"Hotel | {display_name or station} ({transfer_txt}) | {pu_txt}",
    }


# ── LH-Klarname → Verzeichnis-Anreicherung (Owner-Freigabe 27.07.2026) ──────
# Zwei Fragen, strikt getrennt: „Welches Hotel?" beantwortet LH — der Klarname
# wird IMMER angezeigt, auch wenn er im Verzeichnis fehlt. „Wie lange dauert
# der Transfer?" beantwortet NUR ein sicherer Verzeichnis-Treffer. Wo beide auf
# dasselbe Haus zeigen, wird der Verzeichnis-Eintrag um den offiziellen Namen
# ERGÄNZT (Spalte official_name, Migration 20260727) — nie ersetzt. Harte
# Regeln: transfer_min/votes/status niemals anfassen, approved nie zurückstufen,
# nie zwei Häuser zusammenführen, Herkunft mitführen, jede Anreicherung loggen.


def _clean_hotel_name(name):
    """Der Name, wie er ins Verzeichnis geschrieben wird. Zeichen ausserhalb der
    Whitelist (identisch zu /api/ax/crew-hotels/suggest, inkl. der SQL-LIKE-
    Wildcards % und _) werden durch ein LEERZEICHEN ersetzt, nicht gelöscht:
    typografisches ’ und – sind in Hotelnamen normal, und ersatzloses Löschen
    verklebte Wörter ('l’Opéra' → 'lOpéra').

    Muss ÜBERALL benutzt werden, wo der geschriebene Wert gemeint ist — der
    Idempotenz-Vergleich in official_name_action lief sonst gegen den ROHEN
    Namen und meldete bei jedem Lauf erneut 'enrich' für einen bereits
    angereicherten Eintrag."""
    s = re.sub(r"[^0-9A-Za-zÀ-ÿ .,()&'\-/]", ' ', str(name or ''))
    return re.sub(r'\s+', ' ', s).strip()


def official_name_action(lh_name, match):
    """(action, conflict) für einen LH-Klarnamen gegen das Match-Ergebnis von
    transfer_match. Pure/testbar.
      'enrich'  — eindeutiger NAMENS-Treffer, Schreibweise weicht ab →
                  offiziellen Namen am Eintrag ergänzen (gleiches Haus).
      'suggest' — Station hat GAR KEINEN Eintrag → über den bestehenden
                  Vorschlags-Weg als `suggested` anlegen (ohne erfundene Zeit).
      'contest' — Station hat Einträge, LHs Name trifft aber keinen davon.
                  OWNER-KORREKTUR 27.07.2026 (LH-Kabinencrew): „wenn LH ein
                  anderes Hotel liefert, ist es wahrscheinlich ein neues
                  Hotel." LHs Buchung ist die Wahrheit darüber, WO die Crew
                  schläft — der Verzeichnis-Eintrag ist im Konfliktfall
                  vermutlich veraltet. Trotzdem kippt EIN Payload nichts:
                  'contest' legt nur Evidenz an (s. `_record_lh_hotel_evidence`),
                  gekippt wird erst bei wiederholter Buchung über mehrere
                  getrennte Layover-Nächte (`hotel_change_decision`).
      None      — nichts tun (Platzhalter, oder Name schon identisch).
    conflict=True begleitet 'contest' — das Signal für einen möglichen
    Hotelwechsel.

    Angereichert wird ausschliesslich bei `matched` — nie aufgrund des Reasons
    allein (siehe transfer_match: der Regel-3-Rückfall sah früher aus wie ein
    Treffer und verschmolz zwei Häuser)."""
    if not _valid_lh_hotel_name(lh_name):
        return None, False
    reason = (match or {}).get('reason')
    row = (match or {}).get('row') or {}
    if (match or {}).get('matched'):
        # Verglichen wird gegen den Namen, wie er GESCHRIEBEN würde — sonst
        # gilt ein bereits angereicherter Eintrag ewig als anreicherungsbedürftig.
        want = _clean_hotel_name(lh_name)
        shown = str(row.get('hotel') or '').strip()
        crowd = str(row.get('hotel_crowd') or row.get('hotel') or '').strip()
        if want in (shown, crowd) and row.get('official'):
            return None, False          # bereits (identisch) angereichert
        if want == crowd and not row.get('official'):
            return None, False          # Crowd-Name ist schon exakt LHs Name
        return 'enrich', False
    if reason == 'no_entry':
        return 'suggest', False
    return 'contest', True


# ── Hotelwechsel: LH gewinnt, aber erst mit Evidenz (Owner 27.07.2026) ──────
# „Wenn LH ein anderes Hotel liefert, ist es wahrscheinlich ein neues Hotel."
# Zwei Dinge bleiben davon unberührt:
#   1. Die FAHRTZEIT wandert NIE mit. Ein neues Haus startet ohne Zeit → die
#      bestehenden Regeln von `transfer_match` liefern N/A bzw. (an einer
#      Station mit genau EINEM Eintrag) die allgemeine Destinations-Zeit mit
#      '*'. Ein richtiger Name mit fremder Fahrtzeit ist schlimmer als gar
#      keine Zeit — genau dieser Fehler wurde am 27.07. zweimal gefunden
#      (BUD, Mercure Frankfurt).
#   2. Ein EINZELNER Payload kippt nichts. Crews werden auch kurzfristig
#      umgebucht (Überbuchung, Messe-Wochen).
#
# SCHWELLE: 3 verschiedene Layover-NÄCHTE, die zusammen ≥ 7 Tage auseinander
# liegen, innerhalb der letzten 120 Tage. Begründung:
#   • 1 Nacht  = ein einzelner Payload (ausgeschlossen, s.o.).
#   • 2 Nächte = kann eine einzige zweitägige Layover-Phase sein oder zwei
#     Crews derselben Umbuchungswelle.
#   • Gezählt werden NÄCHTE, nicht Payloads — acht Crews derselben Rotation
#     sind EIN Ereignis, nicht acht.
#   • Die 7-Tage-Spanne ist der eigentliche Filter gegen die Umbuchung:
#     „Stammhaus war während der Messe voll" verteilt sich auf aufeinander-
#     folgende Tage, ein geänderter Hotelvertrag auf Wochen.
# Der Preis der Vorsicht ist klein: die Briefing-Karte zeigt LHs Namen ohnehin
# SOFORT (`hotel_block`: LH-Name schlägt Verzeichnis-Name). Die Schwelle
# verzögert nur das Umschreiben des Verzeichnisses — und die ist rücknehmbar.
_LH_CONTEST_STATUS = 'lh_contested'
_EVIDENCE_PREFIX = 'lh_evidence:'
_CONTEST_MIN_NIGHTS = 3
_CONTEST_MIN_SPAN_DAYS = 7
_CONTEST_WINDOW_DAYS = 120


def _parse_nights(raw):
    """'lh_evidence:2026-07-20,2026-07-24' → ['2026-07-20', '2026-07-24'].
    Alles Unbekannte → []. Wirft nie."""
    s = str(raw or '')
    if not s.startswith(_EVIDENCE_PREFIX):
        return []
    out = []
    for part in s[len(_EVIDENCE_PREFIX):].split(','):
        p = part.strip()[:10]
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', p):
            out.append(p)
    return sorted(set(out))


def _fmt_nights(nights):
    return _EVIDENCE_PREFIX + ','.join(sorted(set(nights)))


def hotel_change_decision(nights, today=None):
    """Reicht die gesammelte Evidenz, um das Verzeichnis umzuschreiben? Pure.

    `nights` = ISO-Daten der Layover-NÄCHTE, an denen LH dasselbe neue Haus
    gebucht hat. Returns (flip, kept, reason) — `kept` ist die auf das
    120-Tage-Fenster beschnittene, sortierte Liste (und damit das, was
    zurückgeschrieben wird; alte Evidenz verfällt von selbst)."""
    from datetime import date as _date
    if today is None:
        today = datetime.now(timezone.utc).date()
    elif isinstance(today, str):
        today = _date.fromisoformat(today[:10])
    kept = []
    for n in sorted(set(nights or [])):
        try:
            d = _date.fromisoformat(n[:10])
        except Exception:
            continue
        if 0 <= (today - d).days <= _CONTEST_WINDOW_DAYS:
            kept.append(n[:10])
        elif (today - d).days < 0:
            kept.append(n[:10])      # Zukunfts-Layover ist normale Planung
    if len(kept) < _CONTEST_MIN_NIGHTS:
        return False, kept, 'need_more_nights'
    span = (_date.fromisoformat(kept[-1]) - _date.fromisoformat(kept[0])).days
    if span < _CONTEST_MIN_SPAN_DAYS:
        return False, kept, 'need_wider_span'
    return True, kept, 'flip'


def hotel_supersede_plan(rows, lh_name):
    """Was genau passiert, wenn die Schwelle erreicht ist. Pure/testbar.

    `rows` = ALLE Verzeichnis-Zeilen dieser (airline, iata) — auch inaktive und
    andere Status. Returns dict:
      {'resurrect': row|None, 'insert': bool, 'deactivate': [row, …],
       'reason': str}

    Zwei Regeln, die der Owner explizit gezogen hat:
      • Kennt das Verzeichnis DIESES Haus schon (auch als stillgelegte Zeile),
        wird GENAU DIESE Zeile reaktiviert — mit ihrer eigenen `transfer_min`
        und ihren `votes`. Das ist keine wandernde Fahrtzeit, sondern die Zeit
        DES HAUSES, das zurückkommt (BUD: 'Intercity Budapest', 35 min, am
        18.07. stillgelegt — und genau dorthin bucht LH wieder).
      • Stillgelegt wird nur, wenn die Station bisher GENAU EIN aktives Hotel
        hatte. Bei mehreren aktiven Häusern sind das bewusste Optionen
        (`/api/admin/crew-hotels/approve` kollabiert sie auch nicht) — dort
        kommt LHs Haus dazu, statt die anderen zu löschen.

    Zwei Riegel aus dem adversarialen Review 27.07.:
      • Verglichen wird NUR gegen die Spalte `hotel`. Über `official_name` zu
        matchen hätte eine (früher live vorgekommene) vergiftete Zeile
        — offizieller Name des einen Hauses auf der Zeile eines anderen —
        samt deren fremder Fahrtzeit wieder aktiviert.
      • Reaktiviert wird nur eine `approved`-Zeile. Eine `suggested`-Zeile
        trägt die frei eingegebene Zeit EINER Person; sie über den
        Wechsel-Pfad hochzuheben umginge die Zwei-Stimmen-Regel von
        /api/ax/crew-hotels/suggest. In dem Fall: nichts tun, der Mensch
        entscheidet."""
    rows = [r for r in (rows or [])
            if str(r.get('status') or '') != _LH_CONTEST_STATUS]
    same = [r for r in rows if _same_house_loose(lh_name, r.get('hotel'))]
    if len(same) > 1:
        # Nicht eindeutig adressierbar → NICHTS anfassen (dieselbe Linie wie
        # der 'enrich'-Pfad). Ein Mensch entscheidet.
        return {'resurrect': None, 'insert': False, 'deactivate': [],
                'reason': 'ambiguous_same_house'}
    if same and str(same[0].get('status') or '') != 'approved':
        return {'resurrect': None, 'insert': False, 'deactivate': [],
                'reason': 'pending_human_vote'}
    same_ids = {r.get('id') for r in same}
    active = [r for r in rows
              if str(r.get('status') or '') == 'approved' and r.get('active')
              and r.get('id') not in same_ids]
    deactivate = active if len(active) == 1 else []
    reason = 'replace' if deactivate else (
        'added_as_option' if active else 'added_to_empty')
    if same:
        hit = same[0]
        if (str(hit.get('status') or '') == 'approved' and hit.get('active')
                and not deactivate):
            return {'resurrect': None, 'insert': False, 'deactivate': [],
                    'reason': 'already_current'}
        return {'resurrect': hit, 'insert': False,
                'deactivate': deactivate, 'reason': reason}
    return {'resurrect': None, 'insert': True, 'deactivate': deactivate,
            'reason': reason}


# Doppel-Schreib-Schutz pro Prozess: derselbe abweichende Name erzeugt nie
# zweimal hintereinander einen Vorschlag/ein Update (zusätzlich prüft der
# Writer den DB-Bestand — der Memo spart nur die Roundtrips).
_dir_sync_memo = {}
_DIR_SYNC_TTL_S = 6 * 3600.0

# LH-FlightOps ist LH-Group-only (siehe blueprints/lh_flightops.py). Nur diese
# Buckets dürfen aus dem Anreicherungs-Pfad beschrieben werden.
_LHFO_AIRLINE_BUCKETS = ('LUFTHANSA', 'LUFTHANSA CITY')

# Sentinel statt User-Hash in `suggested_by`: markiert maschinell angelegte
# Vorschläge und hält sie aus der menschlichen Vote-Promotion heraus.
_SUGGESTED_BY_MACHINE = 'lh_flightops:auto'


def _sync_official_name(token, station, lh_name, directory, night=None):
    """Best-effort-Anreicherung nach einem gebauten Hotel-Block. Wirft nie und
    blockiert das Briefing nie. Airline fail-closed über das Profil des Tokens
    (dieselbe Linie wie _crew_hotel_dir_serve; _filter_crew_hotels der
    Layover-Recs bleibt komplett unberührt).

    `night` = ISO-Datum der Layover-Nacht. Es ist der EVIDENZ-Schlüssel des
    Hotelwechsel-Pfads: gezählt werden Nächte, nicht Payloads — acht Crews
    derselben Rotation sind EIN Ereignis. Ohne `night` wird keine Evidenz
    gesammelt (der Konflikt wird dann nur wie früher gemeldet)."""
    try:
        match = transfer_match(station, lh_name, directory)
        action, conflict = official_name_action(lh_name, match)
        if not action:
            return None
        import app as _app
        raw_airline, _hc = _app._viewer_airline_and_calendar(token)
        airline = _app._canonical_airline_key(raw_airline)
        if not airline or not getattr(_app, 'SB_AVAILABLE', False):
            return None
        # Die Daten stammen aus LHs FlightOps-API — sie dürfen NUR in einen
        # Lufthansa-Bucket. `airline` kommt aus dem SELBSTGESETZTEN Profilfeld
        # (_viewer_airline_and_calendar liest profile.airline); ein User mit
        # LH-Grant und Profil „SWISS" hätte sonst LH-Hotelnamen in den
        # SWISS-Bucket geschrieben, wo sie echter SWISS-Crew angezeigt werden.
        # Beim LESEN war die falsche Airline nur nutzlos, beim SCHREIBEN ist
        # sie Datenvergiftung (adversarialer Review 27.07.).
        if airline not in _LHFO_AIRLINE_BUCKETS:
            log.warning('[daily_briefing] official-name-sync: Bucket "%s" ist '
                        'keine LH-Group-Airline — kein Schreibzugriff', airline)
            return None
        clean = _clean_hotel_name(lh_name)
        if len(clean) < 3:
            return None
        # Die NACHT gehört in den Memo-Key: ohne sie hätte der 6-h-Schutz eine
        # zweite Layover-Nacht verschluckt, die kurz nach Mitternacht auf die
        # erste folgt — und genau die Nächte sind die Evidenz.
        mk = (airline, (station or '').upper(), clean.lower(), action,
              str(night or '')[:10])
        now = time.time()
        if (now - _dir_sync_memo.get(mk, 0)) < _DIR_SYNC_TTL_S:
            return None
        if len(_dir_sync_memo) > 5000:          # unbegrenztes Wachstum vermeiden
            for k, ts in list(_dir_sync_memo.items()):
                if (now - ts) >= _DIR_SYNC_TTL_S:
                    _dir_sync_memo.pop(k, None)
        _dir_sync_memo[mk] = now
        from datetime import datetime as _d, timezone as _tz
        now_iso = _d.now(_tz.utc).isoformat()
        sbc = _app.sb
        if action == 'contest':
            # Fall 2/4: die Station HAT Einträge, LHs Name trifft keinen davon.
            # LH gewinnt (Owner 27.07.) — aber erst mit Evidenz über mehrere
            # getrennte Layover-Nächte, s. `hotel_change_decision`.
            log.warning('[daily_briefing] hotel-name-conflict station=%s '
                        'lh="%s" verzeichnis=%s (Evidenz sammeln)',
                        station, clean,
                        [r.get('hotel') for r in directory
                         if str(r.get('iata') or '').upper() == (station or '').upper()])
            if not night:
                return 'reported'    # ohne Nacht kein Evidenz-Schlüssel
            return _record_lh_hotel_evidence(sbc, airline,
                                             (station or '').upper(), clean,
                                             str(night)[:10], now_iso)
        if action == 'enrich':
            crowd = str((match['row'] or {}).get('hotel_crowd')
                        or (match['row'] or {}).get('hotel') or '').strip()
            rows = (sbc.table('crew_hotel_directory')
                    .select('id,official_name,hotel')
                    .eq('airline', airline).eq('iata', (station or '').upper())
                    .eq('hotel', crowd).eq('status', 'approved')
                    .eq('active', True)
                    .limit(2).execute().data) or []
            if len(rows) != 1:
                return None      # nicht eindeutig adressierbar → nicht anfassen
            if (rows[0].get('official_name') or '').strip() == clean:
                return None      # idempotent: schon angereichert
            sbc.table('crew_hotel_directory').update({
                'official_name': clean,
                'official_name_source': 'lh_flightops',
                'official_name_at': now_iso,
            }).eq('id', rows[0]['id']).execute()
            log.info('[daily_briefing] hotel-official-enrich %s/%s "%s" -> '
                     'official "%s"', airline, station, crowd, clean)
            return 'enriched'
        # action == 'suggest': NUR an einer Station GANZ OHNE Eintrag (Fall 3).
        # Als `suggested` (NIE auto-approve aus einem automatischen Pfad), ohne
        # erfundene transfer_min. Dedupe gegen hotel UND official_name.
        for col, val in (('hotel', clean), ('official_name', clean)):
            try:
                ex = (sbc.table('crew_hotel_directory').select('id')
                      .eq('airline', airline).eq('iata', (station or '').upper())
                      .ilike(col, val).limit(1).execute().data) or []
            except Exception:
                ex = []
            if ex:
                return None      # kein doppelter Vorschlag
        sbc.table('crew_hotel_directory').insert({
            'airline': airline, 'iata': (station or '').upper(), 'base': None,
            'hotel': clean, 'transfer_min': 0, 'status': 'suggested',
            # Herkunft EHRLICH: die Maschine hat geschrieben, nicht ein Mensch.
            # Mit einem User-Hash hier hätte die Vote-Promotion in
            # /api/ax/crew-hotels/suggest diese Zeile als „erste Stimme" gelesen
            # und dem echten Melder ausserdem sein Selbst-Approve-Verbot
            # aufgehalst (adversarialer Review 27.07.).
            'suggested_by': _SUGGESTED_BY_MACHINE, 'votes': 1,
            'active': True,
            'official_name': clean, 'official_name_source': 'lh_flightops',
            'official_name_at': now_iso,
        }).execute()
        log.info('[daily_briefing] hotel-official-suggest %s/%s "%s"',
                 airline, station, clean)
        return 'suggested'
    except Exception as e:
        log.warning('[daily_briefing] official-name-sync: %s', type(e).__name__)
        return None


_DIR_TABLE = 'crew_hotel_directory'


def _record_lh_hotel_evidence(sbc, airline, station, clean, night, now_iso):
    """Eine Layover-Nacht als Evidenz für „LH bucht hier ein anderes Haus"
    festhalten und — sobald die Schwelle steht — den Wechsel vollziehen.

    SPEICHERORT ist bewusst `crew_hotel_directory` selbst, mit dem eigenen
    Status `lh_contested`: keine neue Tabelle, also kein Migrations-Schritt
    zwischen Code und Wirkung (Migrationen laufen hier von Hand über den
    Supabase-SQL-Editor, s. RUNBOOK). Alle bestehenden Lesepfade filtern hart
    auf status='approved' bzw. 'suggested' — eine contested-Zeile ist für
    Serve, Vorschlags-Liste und Vote-Promotion unsichtbar.
    Feldbelegung: `hotel`/`official_name` = LHs Klarname, `votes` = Zahl der
    Nächte, `official_name_source` = 'lh_evidence:<datum>,<datum>,…'.
    Wirft nie."""
    try:
        # `active=True` ist Pflicht, nicht Kosmetik: nach einem vollzogenen
        # Wechsel wird die Evidenz-Zeile stillgelegt, bleibt aber als Protokoll
        # stehen. Ohne den Filter fände der nächste Payload sie wieder — mit
        # bereits erreichter Schwelle — und würde eine vom Owner
        # zurückgenommene Entscheidung sofort wieder überstimmen
        # (adversarialer Review 27.07.).
        rows = (sbc.table(_DIR_TABLE)
                .select('id,official_name_source,votes')
                .eq('airline', airline).eq('iata', station)
                .eq('status', _LH_CONTEST_STATUS).eq('active', True)
                .ilike('hotel', clean).limit(5).execute().data) or []
    except Exception as e:
        log.warning('[daily_briefing] hotel-evidence read: %s', type(e).__name__)
        return 'reported'
    # DUPLIKATE ZUSAMMENFÜHREN statt aufgeben: es gibt keine Unique-Constraint
    # und `_dir_sync_memo` ist prozesslokal — zwei gunicorn-Worker können
    # dieselbe Nacht gleichzeitig einfügen. Würde man bei >1 Zeile aufgeben,
    # wäre das Feature ab dem ersten Rennen dauerhaft tot und die Tabelle
    # wüchse pro Nacht weiter (adversarialer Review 27.07.).
    row = rows[0] if rows else None
    nights = set()
    for r in rows:
        nights |= set(_parse_nights(r.get('official_name_source')))
    if night in nights and row is not None and len(rows) == 1:
        return 'evidence_known'          # diese Nacht zählt genau einmal
    nights.add(night)
    flip, kept, reason = hotel_change_decision(nights)
    payload = {'official_name_source': _fmt_nights(kept),
               'votes': len(kept), 'official_name_at': now_iso,
               'updated_at': now_iso}
    try:
        if row is None:
            sbc.table(_DIR_TABLE).insert(dict(payload, **{
                'airline': airline, 'iata': station, 'base': None,
                'hotel': clean, 'official_name': clean, 'transfer_min': 0,
                'status': _LH_CONTEST_STATUS,
                'suggested_by': _SUGGESTED_BY_MACHINE, 'active': True,
            })).execute()
        else:
            sbc.table(_DIR_TABLE).update(payload).eq('id', row['id']).execute()
            for dupe in rows[1:]:        # Rennen-Duplikate einsammeln
                sbc.table(_DIR_TABLE).update(
                    {'active': False, 'updated_at': now_iso}
                ).eq('id', dupe['id']).execute()
    except Exception as e:
        log.warning('[daily_briefing] hotel-evidence write: %s', type(e).__name__)
        return 'reported'
    log.warning('[daily_briefing] hotel-evidence %s/%s "%s": %d Nacht/Nächte '
                '%s -> %s', airline, station, clean, len(kept), kept, reason)
    if not flip:
        return 'evidence_recorded'
    return _apply_lh_hotel_change(sbc, airline, station, clean, kept,
                                  (row or {}).get('id'), now_iso)


def _apply_lh_hotel_change(sbc, airline, station, clean, nights, evidence_id,
                           now_iso):
    """Den Wechsel vollziehen: LHs Haus wird der aktuelle Eintrag, das bisherige
    wird STILLGELEGT (active=False) — nie gelöscht, nie umgeschrieben.
    `transfer_min`, `votes` und `status` der alten Zeile bleiben unangetastet,
    damit ein Rückweg über /api/admin/crew-hotels/approve genügt und die Zeit
    wieder gilt, falls das Haus zurückkommt.
    Eine FAHRTZEIT wird nie erfunden: ein neu angelegtes Haus startet mit
    transfer_min=0, was `transfer_match` als „keine Zeit hinterlegt" liest
    (→ N/A bzw. Regel 3). Wirft nie."""
    try:
        rows = (sbc.table(_DIR_TABLE)
                .select('id,hotel,official_name,status,active,transfer_min,votes')
                .eq('airline', airline).eq('iata', station)
                .limit(200).execute().data) or []
    except Exception as e:
        log.warning('[daily_briefing] hotel-change read: %s', type(e).__name__)
        return 'evidence_recorded'
    plan = hotel_supersede_plan(rows, clean)
    if not (plan['resurrect'] or plan['insert'] or plan['deactivate']):
        log.warning('[daily_briefing] hotel-change %s/%s "%s": kein Plan (%s)',
                    airline, station, clean, plan['reason'])
        return 'evidence_recorded'
    try:
        for old in plan['deactivate']:
            # NUR active umlegen. transfer_min/votes/status bleiben stehen —
            # die Zeit gehört dem Haus, nicht der Station.
            sbc.table(_DIR_TABLE).update(
                {'active': False, 'updated_at': now_iso}
            ).eq('id', old['id']).execute()
        if plan['resurrect'] is not None:
            sbc.table(_DIR_TABLE).update({
                'status': 'approved', 'active': True,
                'official_name': clean, 'official_name_source': 'lh_flightops',
                'official_name_at': now_iso, 'updated_at': now_iso,
            }).eq('id', plan['resurrect']['id']).execute()
        elif plan['insert']:
            sbc.table(_DIR_TABLE).insert({
                'airline': airline, 'iata': station, 'base': None,
                'hotel': clean, 'transfer_min': 0, 'status': 'approved',
                'suggested_by': _SUGGESTED_BY_MACHINE, 'votes': len(nights),
                'active': True, 'official_name': clean,
                'official_name_source': 'lh_flightops',
                'official_name_at': now_iso,
            }).execute()
        if evidence_id:
            # Evidenz-Zeile stilllegen, nicht löschen — sie ist das Protokoll,
            # warum gewechselt wurde.
            sbc.table(_DIR_TABLE).update(
                {'active': False, 'updated_at': now_iso}
            ).eq('id', evidence_id).execute()
    except Exception as e:
        log.warning('[daily_briefing] hotel-change write: %s', type(e).__name__)
        return 'evidence_recorded'
    log.warning('[daily_briefing] HOTELWECHSEL %s/%s -> "%s" (%s, %d Nächte %s)'
                ' · stillgelegt: %s · reaktiviert: %s · Rückweg: POST '
                '/api/admin/crew-hotels/approve {"id":"<alte id>"} + '
                '/deactivate {"id":"<neue id>"}',
                airline, station, clean, plan['reason'], len(nights), nights,
                [(o.get('id'), o.get('hotel'), o.get('transfer_min'))
                 for o in plan['deactivate']],
                (plan['resurrect'] or {}).get('id'))
    return 'hotel_changed'


# ── Header (Briefing Room) ──────────────────────────────────────────────────
def briefing_room_decision(duty_day_events, checkin_fetch):
    """Room-Regel: NUR wenn der erste echte Duty-Bestandteil des Tages ein
    Briefing ist. Hotel-Einträge am Tagesanfang (Layover-Morgen) werden
    übersprungen. Am echten Payload: LH emittiert das BRIEFING-Duty-Event genau
    am Homebase-Report-Tag — Außenstations-Tage beginnen (nach Hotel) mit dem
    Flug-Event und bekommen daher KEINE Raumangabe, exakt Florians Regel.
    Raum aus COMMON_CHECK_IN_TIMES.briefingRoom; LH liefert dort auch literal
    'N/A' → dann (und ohne Raum) 'Cabin OD'."""
    first_duty = None
    first_flight = None
    for ev in duty_day_events:
        et = ev.get('type')
        if et == 'hotel':
            continue                      # Hotel am Tagesanfang überspringen
        if first_duty is None:
            first_duty = ev
        if et == 'flight' and first_flight is None:
            first_flight = ev
    if not first_duty or first_duty.get('type') != 'briefing':
        return {'room': None, 'has_briefing': False}
    room = None
    if first_flight and callable(checkin_fetch):
        times = checkin_fetch(first_flight) or {}
        r = str(times.get('briefingRoom') or '').strip()
        if r and r.upper() not in ('N/A', 'NA', '-'):
            room = r
    return {'room': room or 'Cabin OD', 'has_briefing': True,
            'room_known': bool(room)}


# ── Duty-Events des Tages → schlanke Event-Liste (pure) ─────────────────────
def duty_day_events(de_resp, date_str):
    """COMMON_DUTY_EVENTS → [{type, start, from, to, details}] des Tages, in
    Roster-Reihenfolge. type ∈ flight|briefing|hotel|other."""
    out = []
    for d in _as_list((de_resp or {}).get('rosterDays') if isinstance(de_resp, dict) else None):
        if not isinstance(d, dict) or (d.get('day') or '')[:10] != date_str:
            continue
        for ev in _as_list(d.get('events')):
            if not isinstance(ev, dict):
                continue
            cat = re.sub(r'[_\s]', '', (ev.get('eventCategory') or '').lower())
            et = re.sub(r'[_\s]', '', (ev.get('eventType') or '').lower())
            if et == 'flight' or cat in ('flight', 'flightother'):
                typ = 'flight'
            elif et == 'briefing':
                typ = 'briefing'
            elif et == 'hotel' or cat == 'hotel':
                typ = 'hotel'
            else:
                typ = 'other'
            out.append({'type': typ,
                        'start': str(ev.get('startTime') or '').strip() or None,
                        'from': (ev.get('startLocation') or '').upper().strip() or None,
                        'to': (ev.get('endLocation') or '').upper().strip() or None,
                        'details': (ev.get('eventDetails') or '').strip() or None})
    return out


def hotel_days(de_resp):
    """[(day, station)] aller Hotel-Duty-Events der Response."""
    out = []
    for d in _as_list((de_resp or {}).get('rosterDays') if isinstance(de_resp, dict) else None):
        if not isinstance(d, dict):
            continue
        day = (d.get('day') or '')[:10]
        for ev in _as_list(d.get('events')):
            if not isinstance(ev, dict):
                continue
            cat = re.sub(r'[_\s]', '', (ev.get('eventCategory') or '').lower())
            et = re.sub(r'[_\s]', '', (ev.get('eventType') or '').lower())
            if et == 'hotel' or cat == 'hotel':
                stn = ((ev.get('endLocation') or ev.get('startLocation') or '')
                       .upper().strip())
                if len(stn) == 3 and day:
                    out.append((day, stn))
    return out


def rotation_ids_for_date(de_resp, date_str):
    """rotationIds der Flug- UND Hotel-Events des Tages (dedupliziert, in
    Reihenfolge). Hotel-Events zählen mit, damit ein reiner Hoteltag eines
    mehrtägigen Layovers als Rotations-Tag erkannt wird (adversarialer Review —
    live tragen Hotel-Events dieselbe rotationId)."""
    out = []
    for d in _as_list((de_resp or {}).get('rosterDays') if isinstance(de_resp, dict) else None):
        if not isinstance(d, dict) or (d.get('day') or '')[:10] != date_str:
            continue
        for ev in _as_list(d.get('events')):
            if not isinstance(ev, dict):
                continue
            cat = re.sub(r'[_\s]', '', (ev.get('eventCategory') or '').lower())
            et = re.sub(r'[_\s]', '', (ev.get('eventType') or '').lower())
            if not (et in ('flight', 'hotel')
                    or cat in ('flight', 'flightother', 'hotel')):
                continue
            for ea in _as_list(ev.get('eventAttributes')):
                if isinstance(ea, dict) and ea.get('rotationId') not in (None, ''):
                    rid = str(ea['rotationId'])
                    if rid not in out:
                        out.append(rid)
    return out


# ── Reiner Hoteltag (mehrtägiger Layover) ───────────────────────────────────
def _hotel_day_briefing(date_str, shifts, station, directory, hotel_event_days):
    """Reduziertes Briefing für einen Tag OHNE Legs mitten im Layover:
    Header (ohne Room), Hotel/Pick-up (der Pick-up ist automatisch der des
    Abholtags am ENDE der Phase — er hängt am echten Rückflug-Leg) und die
    RZ-Toleranz der LAUFENDEN Ruhe (Attribute der Schicht, deren letztes Leg
    die Crew zur Station gebracht hat). FDZ ehrlich n/a — an einem Ruhetag
    gibt es keine Flight Duty. None, wenn kein Anreise-Leg auffindbar ist."""
    all_legs = [lg for sh in shifts for lg in sh['legs']]
    upper = date_str + 'T23:59:59Z'
    prev_shift, last = None, None
    for sh in shifts:
        for lg in sh['legs']:
            if lg['arr'] == station and lg.get('arr_iso') \
                    and lg['arr_iso'] <= upper:
                if last is None or lg['arr_iso'] > last['arr_iso']:
                    prev_shift, last = sh, lg
    if last is None:
        return None
    hotel = hotel_block(prev_shift, [last], all_legs, directory, hotel_event_days)
    if not hotel:
        return None
    tol = _tolerances(prev_shift.get('attributes'))
    rz = (tol or {}).get('rz') or {
        'mtv_min': None, 'easa_min': None,
        'easa_unavailable_reason': 'no_attributes',
        'line': 'RZ-Toleranz | MTV n/a / EASA n/a'}
    fdz = {'mtv_min': None, 'easa_min': None,
           'line': 'FDZ-Toleranz | MTV n/a / EASA n/a'}
    lines = [f"Daily Briefing {_day_label(date_str)}", fdz['line'],
             rz['line'], hotel['line']]
    return {'date': date_str, 'date_label': _day_label(date_str),
            'rotation': prev_shift['rotation'], 'shift_no': prev_shift['shift_no'],
            'crew_category': (tol or {}).get('crew_category'),
            'hotel_day': True,
            'header': {'room': None, 'has_briefing': False},
            'ac_changes': [], 'long_transits': [], 'crew_changes': [],
            'fdz': fdz, 'rz': rz, 'hotel': hotel,
            'text': '\n'.join(lines)}


# ── Gesamt-Assembly (pure — alle Fetches injiziert) ─────────────────────────
def assemble_briefing(date_str, de_resp, rot_resp, fetchers, directory):
    """Baut das komplette Briefing. `fetchers` = {'crewlist': fn(leg)->resp|None,
    'legdetails': fn(leg)->dict|None, 'checkin': fn(ev)->dict|None,
    'resolve_route': fn(flight, date)->(dep,arr)|None}.
    Rückgabe (briefing_dict, errors_list). Notwendige Phasen, die scheitern,
    landen als Fehler mit Phasen-Zuordnung — nie ein stilles Teilergebnis."""
    errors = []
    shifts = rotation_shifts(rot_resp)
    shift = shift_for_date(shifts, date_str)
    legs = day_legs(shift, date_str) if shift else []
    if not legs:
        # Reiner Hoteltag eines mehrtägigen Layovers (adversarialer Review):
        # der Tag gehört zur Rotation, hat aber keine Legs — dann gibt es ein
        # reduziertes Briefing (Header + Hotel/Pick-up + RZ der laufenden Ruhe),
        # statt fälschlich „keine Rotation".
        hotel_today = [stn for (d, stn) in hotel_days(de_resp) if d == date_str]
        if hotel_today:
            hd = _hotel_day_briefing(date_str, shifts, hotel_today[0],
                                     directory, hotel_days(de_resp))
            if hd:
                return hd, []
        return None, [{'phase': 'rotation_day_match',
                       'error': 'no_shift_for_date' if not shift
                       else 'no_legs_for_date'}]
    all_legs = [lg for sh in shifts if sh['rotation'] == shift['rotation']
                for lg in sh['legs']]

    d_events = duty_day_events(de_resp, date_str)
    header = briefing_room_decision(d_events, fetchers.get('checkin'))

    details_memo = {}

    def details_for(leg):
        k = (leg['flight'], _utc_day(leg['dep_iso']), leg['dep'])
        if k not in details_memo:
            try:
                details_memo[k] = fetchers['legdetails'](leg)
            except Exception:
                details_memo[k] = None
        return details_memo[k]

    ac_changes = build_ac_changes(legs, details_for)
    long_transits = build_long_transits(legs, details_for)

    # Crew Changes: Legs des Tages + erstes Leg der nächsten Schicht.
    nxt_shift = next_shift_after(shifts, shift)
    compare_legs = list(legs) + ([nxt_shift['legs'][0]] if nxt_shift else [])
    crew_lists = []
    for lg in compare_legs:
        try:
            resp = fetchers['crewlist'](lg)
        except Exception:
            resp = None
        crew = norm_crewlist(resp) if resp is not None else None
        if not crew:
            # Auch eine LEERE Liste ist ein Datenloch: ein operierender Flug
            # hat nie 0 Crew. Ohne diesen Riegel würde „Liste fehlt" als
            # „alle steigen aus" gerendert (adversarialer Review) — sichtbarer
            # Fehler statt stilles Phantom.
            crew_lists.append(None)
            errors.append({'phase': 'crewlist', 'flight': lg['flight'],
                           'error': ('crewlist_unavailable' if resp is None
                                     else 'crewlist_empty')})
        else:
            crew_lists.append(crew)
    crew_changes = []
    for i in range(len(compare_legs) - 1):
        blk = crew_change_block(crew_lists[i], crew_lists[i + 1],
                                compare_legs[i + 1], date_str,
                                fetchers.get('resolve_route') or (lambda f, d: None),
                                from_next_shift=bool(
                                    nxt_shift and i + 1 == len(compare_legs) - 1))
        if blk:
            crew_changes.append(blk)

    tol = _tolerances(shift.get('attributes')) or {
        'crew_category': None,
        'fdz': {'mtv_min': None, 'easa_min': None,
                'line': 'FDZ-Toleranz | MTV n/a / EASA n/a'},
        'rz': {'mtv_min': None, 'easa_min': None,
               'easa_unavailable_reason': 'no_attributes',
               'line': 'RZ-Toleranz | MTV n/a / EASA n/a'}}

    hotel = hotel_block(shift, legs, all_legs, directory, hotel_days(de_resp))

    # ── Text-Render in Florians Block-Reihenfolge; leere Blöcke fallen weg ──
    lines = []
    hdr = f"Daily Briefing {_day_label(date_str)}"
    if header.get('has_briefing'):
        hdr += (f" | Room {header['room']}" if header.get('room_known')
                else f" | {header['room']}")
    lines.append(hdr)
    lines += [e['line'] for e in ac_changes]
    lines += [e['line'] for e in long_transits]
    for blk in crew_changes:
        lines.append(f"Crew Change | {blk['ref']}")
        lines += ['  ' + e['line'] for e in blk['outgoing']]
        lines += ['  ' + e['line'] for e in blk['incoming']]
    lines.append(tol['fdz']['line'])          # FDZ immer
    if hotel:
        lines.append(tol['rz']['line'])       # RZ nur mit Hotel am Tagesende
        lines.append(hotel['line'])

    briefing = {
        'date': date_str, 'date_label': _day_label(date_str),
        'rotation': shift['rotation'], 'shift_no': shift['shift_no'],
        'crew_category': tol.get('crew_category'),
        'header': header,
        'ac_changes': ac_changes,
        'long_transits': long_transits,
        'crew_changes': crew_changes,
        'fdz': tol['fdz'],
        'rz': (tol['rz'] if hotel else None),
        'hotel': hotel,
        'text': '\n'.join(lines),
    }
    return briefing, errors


# ── Endpoint ────────────────────────────────────────────────────────────────
def _lh_calls_counter():
    return {'duty_events': 0, 'rotation': 0, 'crewlist': 0,
            'legdetails': 0, 'checkin': 0}


@daily_briefing_bp.route('/api/ax/daily-briefing/<token>', methods=['GET'])
def ax_daily_briefing(token):
    """Daily Briefing für den eingeloggten FlightOps-User.
    Query: ?date=YYYY-MM-DD (Default: heute, Europe/Berlin — derselbe
    Tages-Bucket wie der Roster-Import). Antwort ist rollenspezifisch und wird
    pro (token, date) 10 min gecacht. Ohne gültige Rotation: ehrlich
    available:false — nie ein Teilergebnis."""
    from blueprints import lh_flightops as fo

    date_str = (request.args.get('date') or '').strip()[:10]
    if not date_str:
        try:
            from zoneinfo import ZoneInfo
            date_str = datetime.now(ZoneInfo('Europe/Berlin')).strftime('%Y-%m-%d')
        except Exception:
            date_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', date_str):
        return jsonify({'ok': False, 'error': 'invalid_date'}), 400

    # Auth VOR dem Cache: nach Grant-Widerruf darf auch der Cache nichts mehr
    # servieren (adversarialer Review). Das Bearer==Pfad-Token-Binding erzwingt
    # zusätzlich das globale BUG004-GET-Gate (app.py, Prefix
    # '/api/ax/daily-briefing/').
    if not fo.flightops_connected(token):
        return jsonify({'ok': False, 'error': 'not_connected',
                        'phase': 'auth'}), 401

    ck = (token, date_str)
    now = time.time()
    with _cache_lock:
        hit = _cache.get(ck)
    if hit and (now - hit[0]) < _CACHE_TTL_S:
        return jsonify(hit[1])

    # Stunden-Notbremse (wie Pickup): das Briefing ist On-Demand-Komfort und
    # darf den Roster-Kernpfad nie aushungern. Stale Cache ist dann erlaubt.
    used = fo._rot_hour_used()
    if used >= _LHFO_HOUR_CEILING:
        if hit:
            return jsonify({**hit[1], 'stale': True})
        return jsonify({'ok': False, 'error': 'lh_quota_deferred',
                        'phase': 'quota', 'lhfo_hour_used': used}), 503

    calls = _lh_calls_counter()

    day_before = (datetime.strptime(date_str, '%Y-%m-%d')
                  - timedelta(days=1)).strftime('%Y-%m-%d')
    day_after = (datetime.strptime(date_str, '%Y-%m-%d')
                 + timedelta(days=1)).strftime('%Y-%m-%d')
    calls['duty_events'] += 1
    de = fo.duty_events(token, day_before, day_after)
    if not isinstance(de, dict):
        return jsonify({'ok': False, 'error': 'duty_events_failed',
                        'phase': 'duty_events'}), 502
    # accessCodes/Links dieser Response gleich mitnehmen (kostenlos).
    try:
        links = fo.extract_duty_links(de)
        if links:
            merged = [l for l in fo._links_load(token)
                      if not any(l == g for g in links)] + links
            fo._links_save(token, merged[-800:])
    except Exception:
        pass

    rns = rotation_ids_for_date(de, date_str)
    if not rns:
        out = {'ok': True, 'available': False, 'date': date_str,
               'reason': 'no_rotation',
               'lh_calls': calls}
        with _cache_lock:
            _cache[ck] = (now, out)
        return jsonify(out)

    calls['rotation'] += 1
    rot = fo.crew_rotation(token, *rns[:6])
    if not isinstance(rot, dict) or rot.get('processingErrors'):
        return jsonify({'ok': False, 'error': 'rotation_failed',
                        'phase': 'rotation'}), 502

    # Hotel-Verzeichnis der EIGENEN Airline (fail-closed, siehe Modul-Doc).
    directory = []
    try:
        import app as _app
        raw_airline, _hc = _app._viewer_airline_and_calendar(token)
        airline = _app._canonical_airline_key(raw_airline)
        directory = _app._crew_hotel_dir_serve(airline) if airline else []
    except Exception as e:
        log.warning('[daily_briefing] directory: %s', type(e).__name__)
        directory = []

    # ── Fetcher (mit Zähler + Memo) ─────────────────────────────────────────
    def _crewlist(leg):
        d = _utc_day(leg['dep_iso'])
        p = fo._resolve_link_params(token, 'crewlist', leg['flight'], d,
                                    leg['dep'], leg['arr'])
        if not p or not p.get('accessCode'):
            return None
        calls['crewlist'] += 1
        resp = fo.crew_list(token, leg['flight'], d, leg['dep'], leg['arr'],
                            p['accessCode'])
        if not isinstance(resp, dict) or resp.get('processingErrors'):
            return None
        return resp

    def _legdetails(leg):
        calls['legdetails'] += 1
        r = fo.flight_leg_details(token, leg['flight'], _utc_day(leg['dep_iso']),
                                  leg['dep'], leg['arr'])
        return r if isinstance(r, dict) and not r.get('processingErrors') else None

    def _checkin(flight_ev):
        # Nur am Briefing-Tag gerufen. crewCategory aus den Rotations-Attributen.
        flt = None
        m = re.search(r'\b([A-Z]{2}|\d[A-Z])\s?\d{1,4}[A-Z]?\b',
                      (flight_ev.get('details') or '').upper())
        if m:
            flt = m.group(0).replace(' ', '')
        if not flt:
            return None
        d = _utc_day(flight_ev.get('start'))
        p = fo._resolve_link_params(token, 'checkintimes', flt, d,
                                    flight_ev.get('from'), flight_ev.get('to'))
        calls['checkin'] += 1
        if p:
            r = fo.service_get(token, 'COMMON_CHECK_IN_TIMES', p)
        else:
            cat = 'CAB'
            try:
                cat = _attr_prefix(rotation_shifts(rot)[0].get('attributes')) or 'CAB'
            except Exception:
                pass
            r = fo.check_in_times(token, flt, d, flight_ev.get('from'),
                                  flight_ev.get('to'), duty_type='OD',
                                  crew_category=cat)
        return fo.parse_check_in_times(r)

    # Kostenlose Routen-Auflösung für '✈︎ Flugnummer - Ort' (Homebase-Fälle):
    # 1) eigene Rotations-Legs, 2) freier Obs-Merge (eigene DB + freie Boards,
    # KEIN Paid-Spend), gekappt auf 8 Lookups. Nicht auflösbar → Florians
    # Fallback (nackte Flugnummer) greift automatisch.
    _route_memo = {}
    _route_budget = [8]
    _rot_routes = {}
    for sh in rotation_shifts(rot):
        for lg in sh['legs']:
            _rot_routes[(lg['flight'], _utc_day(lg['dep_iso']))] = (lg['dep'], lg['arr'])

    def _resolve_route(flight, date):
        k = (flight, date)
        if k in _rot_routes:
            return _rot_routes[k]
        if k in _route_memo:
            return _route_memo[k]
        if _route_budget[0] <= 0:
            return None
        _route_budget[0] -= 1
        route = None
        try:
            import app as _app
            m = _app._flight_obs_merged(flight, date=date, free_only=True)
            if isinstance(m, dict) and m.get('dep_iata') and m.get('arr_iata'):
                route = (str(m['dep_iata']).upper(), str(m['arr_iata']).upper())
        except Exception:
            route = None
        _route_memo[k] = route
        return route

    fetchers = {'crewlist': _crewlist, 'legdetails': _legdetails,
                'checkin': _checkin, 'resolve_route': _resolve_route}
    try:
        briefing, errors = assemble_briefing(date_str, de, rot, fetchers, directory)
    except Exception as e:
        log.warning('[daily_briefing] assemble: %s', type(e).__name__)
        return jsonify({'ok': False, 'error': 'assemble_failed',
                        'phase': 'assemble'}), 500
    if briefing is None:
        # Duty-Events sahen eine Rotation, aber die Rotations-Details decken den
        # Tag nicht — KEIN Teilergebnis (Florians Fehlerverhalten).
        return jsonify({'ok': False, 'error': 'rotation_day_mismatch',
                        'phase': (errors[0]['phase'] if errors else 'assemble'),
                        'detail': errors}), 502

    # Verzeichnis-Anreicherung (Owner 27.07.): LHs Klarname ERGÄNZT den
    # crowdgesourcten Eintrag (gleiches Haus), wird an einer leeren Station als
    # `suggested` vorgeschlagen — und an einer belegten Station mit anderem
    # Namen als Hotelwechsel-Evidenz gezählt (LH gewinnt, aber erst nach
    # mehreren getrennten Nächten). Nur wenn der Name wirklich von LH kommt
    # (ein aus dem Verzeichnis gezogener Crowd-Name darf nie als „offiziell"
    # zurücklaufen); best-effort, blockiert das Briefing nie.
    hb = briefing.get('hotel') or {}
    if hb.get('hotel') and hb.get('hotel_source') == 'lh':
        _sync_official_name(token, hb.get('station'), hb.get('hotel'),
                            directory, night=hb.get('night_of'))

    out = {'ok': True, 'available': True, 'briefing': briefing,
           'complete': not errors, 'errors': errors, 'lh_calls': calls}
    with _cache_lock:
        _cache[ck] = (now, out)
        if len(_cache) > 2000:
            for k in sorted(_cache, key=lambda k: _cache[k][0])[:1000]:
                _cache.pop(k, None)
    return jsonify(out)
