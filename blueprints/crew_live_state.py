"""Crew-Live-State — EINE Wahrheit für Familie/Freunde/Crew (Neubau 2026-07-10).

WARUM (Owner: „baue es komplett neu auf damit es funktioniert, auch Text"):
friends-today, family_watch und iOS rieten den Aufenthalts-/Flugzustand einer
Crew bisher JEDER FÜR SICH aus unterschiedlichen Feld-Kombinationen
(layover/current_city/flight_numbers/flights_live) — mit belegten Lücken:
reine iCal-Freunde haben keine reader_facts.flight_numbers, das
flights_live-Gate in friends-today lieferte für sie IMMER [], iOS fiel auf
lastRouteIATA zurück und zeigte „Basis Frankfurt", während die Crew nachweislich
FRA→ARN flog (Tibor-Diagnose 2026-07-10).

HIER lebt jetzt der EINE pure, testbare Resolver:

    resolve_crew_live_state(sectors, obs_lookup, live_lookup, now, …)
      → {state, current_leg, position, text: {title, subtitle}, confidence}

  · state ∈ home | standby | pre_flight | flying | landed | layover
  · Zeitbasierter Leg-Pick (Vorlage: family_watch._pick_current_sector,
    Root-Fix 2026-07-10) über die ical_sectors des Tages (echt-UTC).
  · Board-Beobachtungen (obs_lookup → _flight_obs_merged-Shape) integriert:
    beobachtete Landung beendet den Leg sofort, beobachtete Verspätung
    verschiebt Fenster, cancelled pinnt die Crew an den Abflughafen.
  · aircraft_live-Gegencheck (live_lookup, GRATIS NAS/FR24-gRPC-Store):
    ein AIRBORNE-Beweis schlägt die Uhr in BEIDE Richtungen — Maschine fliegt
    noch nach Plan-Ankunft → flying; Maschine steht am Boden nahe dep, obwohl
    das Plan-Fenster läuft → pre_flight/landed (wartet). Kein Geister-Flieger.
  · TEXT SERVERSEITIG (Owner „auch Text"): title/subtitle werden HIER gebaut
    („Fliegt gerade", „FRA → ARN · Ankunft 12:30", „Gelandet in Stockholm",
    „Wartet auf LH803 · 13:10", „Layover Barcelona", „Basis Frankfurt" NUR
    wenn wirklich kein Dienst) — iOS zeigt sie 1:1, kein lokales Raten mehr.

PUR & OFFLINE-TESTBAR: alle Außenwelt-Zugriffe kommen als injizierte Callables
(obs_lookup/live_lookup/city_lookup/local_hhmm/status_bucket) — der Resolver
selbst macht NIE einen Netz-/DB-Call und wirft nie. free-first by contract:
die mitgelieferten Factory-Adapter (build_obs_lookup free_only=True,
build_live_lookup = reiner aircraft_live-Read) geben keinen Cent aus.

Consumers: app.py get_friends_today (Feld `crew_state` pro Freund) und
blueprints/family_watch._load_crew_status_for_family (Feld `crew_state` im
Status) — beide ADDITIV, alle Altfelder bleiben für alte Builds unverändert.

PRE-FLIGHT-TIMELINE (Owner 2026-07-12, ADDITIV): im Zustand `pre_flight`
trägt das Ergebnis zusätzlich `pre_phase` + `pre_phase_label` — die
feingranulare Vor-Abflug-Phase, SERVERSEITIG berechnet (iOS zeigt nur an):
  OUTSTATION  checkin_open → crewbus (ab iCal-Pickup) → security (ab Pickup +
              Crewbus-Fahrtzeit-Default) → prep → boarding (nur BEOBACHTET)
  HOMEBASE    checkin_open → commute (ab Report − selbst angegebene Fahrzeit)
              → briefing (ab Report/ical_start) → prep → boarding (beobachtet)
Fehlt ein Baustein (kein Pickup im iCal, keine commute_minutes, kein
Boarding-Signal) wird seine Phase EHRLICH übersprungen — nie geratene Zeiten.
Details/Quellen: siehe Kommentarblock über den PRE_*-Konstanten.
"""
import datetime as _dt
import re as _re

# ── Zustände (Kontrakt mit iOS) ──────────────────────────────────────────────
STATE_HOME = 'home'            # kein Dienst / Feierabend an der Homebase
STATE_STANDBY = 'standby'      # Bereitschaft ohne Legs
STATE_PRE_FLIGHT = 'pre_flight'  # vor dem ERSTEN Leg des Tages (wartet/boarding)
STATE_FLYING = 'flying'        # in der Luft (beobachtet oder Plan-Fenster)
STATE_LANDED = 'landed'        # gelandet — wartet auf den nächsten Leg / frisch da
STATE_LAYOVER = 'layover'      # Übernachtung/Ruhetag fern der Homebase

CONF_OBSERVED = 'observed'     # Board-/Live-Beweis stützt die Entscheidung
CONF_PLAN = 'plan'             # reine Plan-Uhr (kein Gegenbeweis)

_ARR_BUFFER_MIN = 40           # Verspätungs-Puffer ohne jede Beobachtung
_LANDED_RECENT_MIN = 90        # „Gelandet in X" statt „Layover X" nach Landung
_STALE_GROUNDED_MIN = 30       # grounded-Obs nach Plan-Ankunft+30' → Live-Check
_NEAR_AIRPORT_KM = 8.0         # „am Boden nahe Flughafen"-Radius (Gegencheck)

# ── PRE-FLIGHT-TIMELINE (Owner 2026-07-12) ──────────────────────────────────
# Feingranulare Phase VOR dem Abflug (nur state == pre_flight; zwischen zwei
# Legs höchstens prep/boarding). Kontrakt mit iOS (ADDITIV — alte Builds
# ignorieren die Felder, iOS-Fallback = exakt bisheriges Verhalten):
#   pre_phase ∈ checkin_open | commute | briefing | crewbus | security |
#               prep | boarding   (None außerhalb von pre_flight)
#   pre_phase_label = fertiger deutscher Anzeige-Text (iOS zeigt 1:1).
# Quellen je Baustein — NICHTS wird erfunden, fehlt ein Baustein wird seine
# Phase übersprungen und die nächste Grenze gilt:
#   • Pickup            echte Hotel-Bus-Zeit aus dem Roh-iCal-Summary
#                       („13:35 LT Pickup BLL" / „Pickup 1430") — Server-
#                       Nachbau der iOS-Referenz-Regexe
#                       RosterLabels.pickupTimeFromSummary (parse_pickup_hhmm),
#                       als aware-UTC via pre_ctx['pickup'] injiziert.
#   • Crewbus-Fahrtzeit KEIN echtes Signal verfügbar → dokumentierte
#                       Default-Konstante _CREWBUS_RIDE_MIN.
#   • Fahrt z. Flughafen pre_ctx['commute_minutes'] — vom Crew-Mitglied SELBST
#                       angegebene Fahrzeit (Profil-Feld commute_minutes,
#                       ÖPNV/Auto). Fehlt sie → Phase wird übersprungen.
#   • Briefing/Report   pre_ctx['report'] (korrigierter ical_start des Tages,
#                       app._corrected_briefing_start_iso hat LT→UTC schon
#                       beim Import gelöst). Nur an der HOMEBASE — am Layover
#                       gibt es kein Briefing (iOS-Outstation-Gate-Parität).
#   • Boarding          NUR BEOBACHTET (Board-Status enthält Boarding/Last
#                       Call/…). „Board schlägt Uhr" in BEIDE Richtungen:
#                       frühes Boarding kippt sofort auf boarding; OHNE Signal
#                       bleibt es bei prep — die Uhr allein macht nie ein
#                       Boarding (nichts erfinden).
PRE_CHECKIN = 'checkin_open'   # ab Beginn des pre_flight-Fensters (= das
                               # bestehende Bordkarten-/Check-in-Fenster:
                               # heutiger Betriebstag bzw. crew_state_next
                               # binnen 24 h vor Abflug — KEIN eigenes Gate)
PRE_COMMUTE = 'commute'        # HOMEBASE: ab Report − commute_minutes
PRE_BRIEFING = 'briefing'      # HOMEBASE: ab Report-/Briefing-Zeit
PRE_CREWBUS = 'crewbus'        # OUTSTATION: ab iCal-Pickup-Zeit
PRE_SECURITY = 'security'      # OUTSTATION: ab Pickup + _CREWBUS_RIDE_MIN
PRE_PREP = 'prep'              # ab eff. Abflug − _PREP_BEFORE_DEP_MIN
PRE_BOARDING = 'boarding'      # nur bei beobachtetem Boarding-Board-Status

PRE_PHASE_LABEL = {
    PRE_CHECKIN: 'Check-in offen',
    PRE_COMMUTE: 'Fahrt zum Flughafen',
    PRE_BRIEFING: 'Briefing',
    PRE_CREWBUS: 'Im Crewbus',
    PRE_SECURITY: 'Durch die Security',
    PRE_PREP: 'Flugvorbereitung',
    PRE_BOARDING: 'Boarding',
}

_CREWBUS_RIDE_MIN = 25         # Default Hotel→Terminal-Fahrtzeit (Minuten) —
                               # bewusst Konstante: es gibt keine echte Quelle
                               # pro Hotel; 25' ist der LH-übliche Richtwert.
_PREP_BEFORE_DEP_MIN = 40      # „Flugvorbereitung" ab eff. Abflug − 40 min
                               # (Zeit-Heuristik NUR fürs Label, s.o.)
_PRE_LEAD_MAX_MIN = 6 * 60     # Plausibilitäts-Fenster wie iOS
                               # RosterLabels.maxLeadWindowMinutes: eine Marke
                               # >6 h vor dem Plan-Abflug ist inkonsistent →
                               # verwerfen statt raten
_REPORT_MIN_LEAD_MIN = 15      # Report <15 min vor Abflug = implausibel

# Boarding-Beobachtung: Board-Status-Tokens, die „Boarding läuft" belegen
# (Vokabular der Board-Normalisierer in app.py: Boarding/Last Call/Gate zu/
# 登机→Boarding). NICHT dabei: gate open/go to gate (Gate offen ≠ Boarding
# gestartet). Deboarding/Ausstieg (Ankunftsseite) wird explizit ausgeschlossen.
_BOARDING_WORDS = ('boarding', 'einsteigen', 'last call', 'final call',
                   'gate closed', 'gate zu', 'letzter aufruf')


def _status_is_boarding(status):
    """True, wenn der Board-Status-String beobachtetes Boarding belegt."""
    s = str(status or '').strip().lower()
    if not s or 'deboard' in s or 'ausstieg' in s:
        return False
    return any(t in s for t in _BOARDING_WORDS)


# Pickup-Regexe — SERVER-NACHBAU der iOS-Referenz (RosterLabels.swift
# pickupTimeFromSummary, dort mit Testabdeckung ReportTimeTests): LH schreibt
# Pickup-VEVENTs uneinheitlich, beide Reihenfolgen UND beide Zeitformate
# kommen real vor: „09:30 LT Pickup HND" und „Pickup 1430".
_PICKUP_TIME_PAT = r'(\d{1,2}:\d{2}|\d{3,4})'
_PICKUP_RES = (
    _re.compile(_PICKUP_TIME_PAT + r'\s*(?:LT|UTC|Z|L)?\s*[-–]?\s*Pickup',
                _re.IGNORECASE),
    _re.compile(r'Pickup\s*(?:um|at|:)?\s*' + _PICKUP_TIME_PAT,
                _re.IGNORECASE),
)


def parse_pickup_hhmm(summary):
    """Explizite Pickup-/Hotel-Bus-Zeit aus einem Roster-Summary → (hh, mm)
    oder None. Akzeptiert „HH:MM" und 3–4-stellige Militärzeit („1430"),
    verwirft Unplausibles (h≥24/m≥60, z. B. „Pickup 2599") — nie raten."""
    s = str(summary or '').strip()
    if not s or 'pickup' not in s.lower():
        return None
    for rx in _PICKUP_RES:
        m = rx.search(s)
        if not m:
            continue
        t = m.group(1)
        if ':' in t:
            hh, mm = t.split(':', 1)
        else:
            hh, mm = t[:-2], t[-2:]
        try:
            hh, mm = int(hh), int(mm)
        except ValueError:
            continue
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return (hh, mm)
    return None


def pickup_utc_for_leg(hhmm, dep_iso, tzname):
    """Lokale Pickup-„HH:MM" (Ortszeit an der ABFLUG-Station des ersten Legs —
    dort steht das Hotel) → aware-UTC-Zeitpunkt, verankert am lokalen
    Kalendertag des Abflugs; liegt das Ergebnis NACH dem Abflug
    (Mitternachts-Wrap: Pickup 23:00, Abflug 00:30), wird ein Tag abgezogen.
    Plausibilität wie iOS (maxLeadWindow): Pickup muss binnen 6 h VOR dem
    Plan-Abflug liegen, sonst None (nie geratene Zeiten). Wirft nie."""
    try:
        dep = _parse_iso(dep_iso)
        if dep is None or not tzname or not hhmm:
            return None
        from zoneinfo import ZoneInfo
        dep_local = dep.astimezone(ZoneInfo(str(tzname)))
        p = dep_local.replace(hour=int(hhmm[0]), minute=int(hhmm[1]),
                              second=0, microsecond=0)
        if p > dep_local:
            p -= _dt.timedelta(days=1)
        lead_min = (dep_local - p).total_seconds() / 60.0
        if not (0 <= lead_min <= _PRE_LEAD_MAX_MIN):
            return None
        return p.astimezone(_dt.timezone.utc)
    except Exception:
        return None


def _resolve_pre_phase(leg, now, eff_dep, pre_ctx, hb, first_leg):
    """Feingranulare Vor-Abflug-Phase (siehe PRE_*-Block). Reine
    Zeitvergleiche: alle bekannten Phasen-Startmarken sammeln, die SPÄTESTE
    Marke ≤ now gewinnt. Unbekannte Marken fehlen einfach in der Liste →
    die Phase wird übersprungen, die nächste Grenze gilt (nichts erfinden).
    Boarding ist KEINE Zeitmarke, sondern nur beobachtet (Board schlägt Uhr
    in beide Richtungen). Zwischen zwei Legs (first_leg=False, Turnaround am
    Flugzeug) gibt es keine Checkin-/Anfahrts-Phasen — nur prep/boarding."""
    o = leg.get('obs') or {}
    if _status_is_boarding(o.get('status')):
        return PRE_BOARDING
    prep_start = eff_dep - _dt.timedelta(minutes=_PREP_BEFORE_DEP_MIN)
    if not first_leg:
        return PRE_PREP if now >= prep_start else None
    # (start | None=-inf, rank, phase) — rank bricht Zeit-Gleichstand
    # zugunsten der späteren Timeline-Stufe.
    marks = [(None, 0, PRE_CHECKIN), (prep_start, 5, PRE_PREP)]
    ctx = pre_ctx if isinstance(pre_ctx, dict) else {}
    plan_dep = leg['dep']

    def _lead_ok(mark, min_lead=0):
        lead = (plan_dep - mark).total_seconds() / 60.0
        return min_lead <= lead <= _PRE_LEAD_MAX_MIN

    pickup = _parse_iso(ctx.get('pickup'))
    if pickup is not None and not _lead_ok(pickup):
        pickup = None
    if hb and leg['dep_ap'] == hb:
        # HOMEBASE-Kette: commute → briefing (Report-Zeit nötig; Pickup-Guard:
        # ein Report, der exakt der Pickup-Zeit entspricht, ist der Hotel-Bus-
        # DTSTART, kein Briefing — iOS-Paritaet).
        report = _parse_iso(ctx.get('report'))
        if report is not None and (not _lead_ok(report, _REPORT_MIN_LEAD_MIN)
                                   or (pickup is not None and report == pickup)):
            report = None
        if report is not None:
            marks.append((report, 3, PRE_BRIEFING))
            cm = ctx.get('commute_minutes')
            if isinstance(cm, (int, float)) and not isinstance(cm, bool) \
                    and 0 < cm <= _PRE_LEAD_MAX_MIN:
                marks.append((report - _dt.timedelta(minutes=int(cm)),
                              2, PRE_COMMUTE))
    elif pickup is not None:
        # OUTSTATION-Kette (Abflug nicht an der Homebase — oder Homebase
        # unbekannt, aber ein Hotel-Pickup existiert nur am Layover):
        # crewbus → security.
        marks.append((pickup, 3, PRE_CREWBUS))
        marks.append((pickup + _dt.timedelta(minutes=_CREWBUS_RIDE_MIN),
                      4, PRE_SECURITY))
    best = None
    floor = _dt.datetime.min.replace(tzinfo=_dt.timezone.utc)
    for t, rank, phase in marks:
        if t is not None and t > now:
            continue
        key = (t or floor, rank)
        if best is None or key >= best[0]:
            best = (key, phase)
    return best[1] if best else None


# Default-Status-Buckets — Consumers reichen app._flight_status_bucket rein
# (DIE eine Wahrheit); diese Listen sind nur der abgespeckte Offline-Fallback.
_LANDED_WORDS = ('landed', 'gelandet', 'arrived', 'angekommen')
_AIRBORNE_WORDS = ('airborne', 'in flight', 'in-flight', 'enroute', 'en route',
                   'departed', 'abgeflogen', 'unterwegs')
_GROUNDED_WORDS = ('boarding', 'gate', 'scheduled', 'delayed', 'verspätet',
                   'check-in', 'checkin', 'on time', 'pünktlich', 'wait')


def _parse_iso(s):
    """Toleranter ISO→aware-UTC-Parser (naiv = UTC). None wenn unparsebar."""
    if not s:
        return None
    if isinstance(s, _dt.datetime):
        d = s
    else:
        try:
            d = _dt.datetime.fromisoformat(str(s).strip().replace('Z', '+00:00'))
        except Exception:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.astimezone(_dt.timezone.utc)


def _iso_z(d):
    return d.strftime('%Y-%m-%dT%H:%M:%SZ') if isinstance(d, _dt.datetime) else None


def _default_bucket(status):
    """Board-Status → 'landed'|'airborne'|'grounded'|None (Fallback-Bucket)."""
    s = str(status or '').strip().lower()
    if not s:
        return None
    for t in _LANDED_WORDS:
        if t in s:
            return 'landed'
    for t in _AIRBORNE_WORDS:
        if t in s:
            return 'airborne'
    for t in _GROUNDED_WORDS:
        if t in s:
            return 'grounded'
    return None


def _default_hhmm(d, _iata=None):
    """Fallback-Zeitformat: UTC HH:MM (Consumers injizieren die Station-lokale
    Variante via build_local_hhmm(airport_tz))."""
    try:
        return d.astimezone(_dt.timezone.utc).strftime('%H:%M')
    except Exception:
        return None


def _norm_legs(sectors):
    """ical_sectors[] → normalisierte Leg-Liste (aware-UTC, defensive).
    dep_iso ist Pflicht; ein fehlendes/unplausibles arr_iso (Red-Eye-Macke:
    arr ≤ dep) wird durch ein 3h-Plan-Fenster ersetzt und als synthetisch
    markiert (kein erfundener Ankunfts-Text)."""
    legs = []
    for s in (sectors or []):
        if not isinstance(s, dict):
            continue
        frm = str(s.get('from') or '').strip().upper()
        to = str(s.get('to') or '').strip().upper()
        dep = _parse_iso(s.get('dep_iso'))
        if not (len(frm) == 3 and frm.isalpha() and dep is not None):
            continue
        if not (len(to) == 3 and to.isalpha()):
            to = None
        arr = _parse_iso(s.get('arr_iso'))
        arr_synth = False
        if arr is None or arr <= dep:
            arr = dep + _dt.timedelta(hours=3)
            arr_synth = True
        fno = str(s.get('flight') or '').replace(' ', '').upper() or None
        legs.append({'dep_ap': frm, 'arr_ap': to, 'flight': fno,
                     'dep': dep, 'arr': arr, 'arr_synth': arr_synth,
                     'tail': (str(s.get('tail') or '').strip().upper() or None)})
    legs.sort(key=lambda l: l['dep'])
    return legs


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _fmt_reg(r):
    """Kanonische Reg-Schreibweise: Boards liefern 'DAIWA', Radar/adsb suchen
    'D-AIWA' — deutsche Regs bekommen den Bindestrich zurueck (Owner-Fund:
    Crew-Tap -> Radar sagte 'aktuell nicht im Radar' wegen Schreibweise)."""
    r = (r or '').strip().upper()
    if not r:
        return None
    if '-' not in r and len(r) == 5 and r[0] == 'D' and r[1:].isalpha():
        return 'D-' + r[1:]
    return r


def resolve_crew_live_state(sectors, obs_lookup, live_lookup, now,
                            homebase=None, layover_iata=None, duty=None,
                            city_lookup=None, local_hhmm=None,
                            status_bucket=None, pre_ctx=None):
    """DER Crew-Live-State-Resolver (pur, wirft nie — siehe Modul-Docstring).

    Args:
      sectors:     ical_sectors[] des Tages ({from,to,flight,dep_iso,arr_iso}).
      obs_lookup:  callable(flight_no, dep_iata, arr_iata) → merged Board-Obs
                   (Shape wie app._flight_obs_merged: status/cancelled/
                   dep_delay_min/arr_delay_min/delay_min/reg) | None.
      live_lookup: callable(flight_no, dep_iata, arr_iata) → GRATIS-Positions-
                   Gegencheck {'lat','lon','ts','source','on_ground',
                   'near_dep','near_arr'} | None (siehe build_live_lookup).
      now:         aware-UTC datetime (alle Vergleiche in UTC).
      homebase:    IATA der Homebase (für home/layover-Entscheid + Text).
      layover_iata: geplanter Aufenthaltsort an Leg-losen Tagen (Ruhetag).
      duty:        'standby' für Bereitschafts-Tage ohne Legs (optional).
      city_lookup: callable(iata) → Städtename (Fallback: IATA-Code selbst).
      local_hhmm:  callable(aware_dt, iata) → 'HH:MM' Station-lokal
                   (Fallback: UTC).
      status_bucket: callable(status_str) → 'landed'|'airborne'|'grounded'|None
                   (Consumers reichen app._flight_status_bucket — EINE Wahrheit).
      pre_ctx:     optionale PRE-FLIGHT-TIMELINE-Bausteine (siehe PRE_*-Block):
                   {'pickup': aware-UTC/ISO|None (iCal-Hotel-Bus),
                    'report': aware-UTC/ISO|None (korrigierter Briefing-Start),
                    'commute_minutes': int|None (selbst angegebene Fahrzeit)}.
                   None/leer = Phasen ehrlich reduziert (checkin/prep/boarding).

    Returns dict:
      {state, leg_index, current_leg: {dep,arr,flight_no,dep_iso,arr_iso,reg}
       | None, position: {lat,lon,ts,source} | None,
       text: {title, subtitle}, confidence: 'observed'|'plan',
       pre_phase: PRE_*|None, pre_phase_label: str|None  (ADDITIV 2026-07-12)}
    """
    now = _parse_iso(now) or _dt.datetime.now(_dt.timezone.utc)
    bucket_of = status_bucket if callable(status_bucket) else _default_bucket
    hhmm = local_hhmm if callable(local_hhmm) else _default_hhmm

    def city(c):
        """IATA → Städtename via city_lookup; ehrlicher Fallback: der Code."""
        if not c:
            return ''
        if callable(city_lookup):
            try:
                return city_lookup(c) or c
            except Exception:
                return c
        return c
    hb = str(homebase or '').strip().upper() or None
    legs = _norm_legs(sectors)

    def _obs(leg):
        if 'obs' not in leg:
            o = None
            try:
                if callable(obs_lookup) and leg['flight']:
                    o = obs_lookup(leg['flight'], leg['dep_ap'], leg['arr_ap'])
            except Exception:
                o = None
            leg['obs'] = o if isinstance(o, dict) else None
        return leg['obs']

    def _live(leg):
        if 'live' not in leg:
            v = None
            try:
                if callable(live_lookup) and leg['flight']:
                    v = live_lookup(leg['flight'], leg['dep_ap'], leg['arr_ap'])
            except Exception:
                v = None
            leg['live'] = v if isinstance(v, dict) else None
        return leg['live']

    def _current_leg(leg):
        o = leg.get('obs') or {}
        # ANREICHERUNG (Owner 2026-07-12, Crew-Feed-Härtung #2): Ist-Zeiten/
        # Delay/Status/Annulliert aus den SCHON GELADENEN Board-obs mitgeben —
        # Key-Namen EXAKT wie flights_live, damit der iOS-FlightLiveEntry-
        # Decoder sie direkt liest. Vorher war der Leg mager (nur Soll) und
        # VERSCHATTETE die reichere Tafel-Quelle: Bordkarten zeigten nie
        # „+X min", ein annullierter Flug wirkte als „Check-in offen".
        # KEIN neuer Lookup hier (leg.get('obs') only) — Kosten-Gates bleiben.
        dep_delay = _num(o.get('dep_delay_min'))
        arr_delay = _num(o.get('arr_delay_min'))
        if arr_delay is None:
            arr_delay = _num(o.get('delay_min'))
        est_dep = (leg['dep'] + _dt.timedelta(minutes=dep_delay)
                   if dep_delay is not None else None)
        est_arr = (leg['arr'] + _dt.timedelta(minutes=arr_delay)
                   if (arr_delay is not None and not leg['arr_synth']) else None)
        delay = arr_delay if arr_delay is not None else dep_delay
        return {
            'dep': leg['dep_ap'], 'arr': leg['arr_ap'],
            'flight_no': leg['flight'],
            'dep_iso': _iso_z(leg['dep']),
            'arr_iso': None if leg['arr_synth'] else _iso_z(leg['arr']),
            'reg': _fmt_reg(str(o.get('reg') or '').strip().upper()
                            or leg.get('tail')),
            'est_dep_iso': _iso_z(est_dep) if est_dep else None,
            'est_arr_iso': _iso_z(est_arr) if est_arr else None,
            'delay_min': int(round(delay)) if delay is not None else None,
            'delay_side': ('arr' if arr_delay is not None
                           else ('dep' if dep_delay is not None else None)),
            'delay_known': (arr_delay is not None or dep_delay is not None),
            'status': (str(o.get('status')).strip() or None)
                      if o.get('status') else None,
            'cancelled': True if o.get('cancelled') else None,
        }

    def _position(leg):
        lv = leg.get('live')
        if not lv or lv.get('on_ground') or lv.get('lat') is None or lv.get('lon') is None:
            return None
        ts = lv.get('ts')
        if isinstance(ts, (int, float)) and not isinstance(ts, bool):
            ts = _iso_z(_dt.datetime.fromtimestamp(float(ts), _dt.timezone.utc))
        else:
            ts = _iso_z(_parse_iso(ts)) if ts else None
        return {'lat': lv['lat'], 'lon': lv['lon'], 'ts': ts,
                'source': lv.get('source') or 'aircraft_live'}

    def _result(state, leg=None, idx=None, position=None, title=None,
                subtitle=None, confidence=CONF_PLAN, pre_phase=None):
        return {
            'state': state,
            'leg_index': idx,
            'current_leg': _current_leg(leg) if leg else None,
            'position': position,
            'text': {'title': title, 'subtitle': subtitle},
            'confidence': confidence,
            # PRE-FLIGHT-TIMELINE (ADDITIV 2026-07-12): Phase + fertiger
            # Anzeigetext; None außerhalb von pre_flight/Turnaround-prep.
            'pre_phase': pre_phase,
            'pre_phase_label': PRE_PHASE_LABEL.get(pre_phase),
        }

    # ── Leg-loser Tag: standby / Layover-Ruhetag / wirklich kein Dienst ─────
    if not legs:
        lay = str(layover_iata or '').strip().upper() or None
        if lay and hb and lay == hb:
            lay = None            # „Layover an der Homebase" gibt es nicht
        if str(duty or '').strip().lower() in ('standby', 'sby', 'reserve'):
            sub = f'Basis {city(hb)}' if hb else None
            return _result(STATE_STANDBY, title='Standby', subtitle=sub)
        if lay:
            return _result(STATE_LAYOVER, title=f'Layover {city(lay)}')
        # „Basis Frankfurt" NUR wenn wirklich kein Dienst (Owner-Vorgabe).
        title = f'Basis {city(hb)}' if hb else 'Kein Dienst'
        return _result(STATE_HOME, title=title)

    # ── Zeitbasierter Leg-Pick mit Board-Obs + Live-Gegencheck ──────────────
    # (Vorlage: family_watch._pick_current_sector; hier zusätzlich mit dem
    # aircraft_live-Beweis in beide Richtungen.)
    picked = None          # (kind, idx, confidence); kind ∈ flying|waiting|cancelled
    last_flown_observed = False
    for idx, leg in enumerate(legs):
        o = _obs(leg) or {}
        b = bucket_of(o.get('status')) if o else None
        dep_delay = _num(o.get('dep_delay_min'))
        arr_delay = _num(o.get('arr_delay_min'))
        if arr_delay is None:
            arr_delay = _num(o.get('delay_min'))
        eff_dep = leg['dep'] + _dt.timedelta(minutes=max(0.0, dep_delay or 0.0))
        eff_arr = leg['arr'] + _dt.timedelta(minutes=max(0.0, arr_delay or 0.0))
        leg['eff_arr'] = eff_arr

        if o.get('cancelled'):
            # Annulliert schlägt alles: Crew ist nie losgeflogen.
            picked = ('cancelled', idx, CONF_OBSERVED)
            break
        if b == 'landed':
            # Board schlägt Uhr: Leg beendet → nächsten prüfen.
            leg['flown'] = True
            last_flown_observed = True
            continue
        if b == 'airborne':
            # Dep-seitiges „Abgeflogen"/airborne beweist den ABFLUG — nicht
            # ewiges Fliegen. Outstations ohne Ankunfts-Board (ARN!) melden nie
            # 'landed' → ohne Zeit-Deckel klebte der Status auf diesem Leg,
            # obwohl die Maschine laengst den NAECHSTEN Sektor fliegt
            # (Tibor LH802 „Ankunft 12:30" um 13:39). Ab Plan-Ankunft+Puffer
            # gilt der Leg als geflogen und der naechste wird geprueft.
            # Audit B6: dep-seitige „Abgeflogen"-Rows tragen meist NUR
            # dep_delay (kein arr_delay) — eff_arr allein unterschaetzt dann
            # die echte Ankunft um die Start-Verspaetung: >30 min verspaetete
            # Starts kippten ab Plan-Ankunft+Puffer faelschlich auf 'flown'.
            # Deckel = spaeteste plausible Ankunft: eff_arr ODER
            # eff_dep + Plan-Flugzeit (arr - dep), je nachdem was spaeter ist.
            cap_arr = max(eff_arr, eff_dep + (leg['arr'] - leg['dep']))
            if now >= cap_arr + _dt.timedelta(minutes=_STALE_GROUNDED_MIN):
                leg['flown'] = True
                last_flown_observed = True
                continue
            picked = ('flying', idx, CONF_OBSERVED)
            break
        if b == 'grounded':
            # Echtes „noch nicht abgeflogen" — Ewig-Pin, AUSSER die Obs ist
            # nachweislich stale (Plan-Ankunft lange vorbei UND der GRATIS
            # aircraft_live-Store beweist das Gegenteil — Tibor-LH1139-Muster).
            if now >= eff_arr + _dt.timedelta(minutes=_STALE_GROUNDED_MIN):
                lv = _live(leg)
                if lv and not lv.get('on_ground'):
                    picked = ('flying', idx, CONF_OBSERVED)
                    break
                if lv and lv.get('on_ground') and lv.get('near_arr'):
                    leg['flown'] = True
                    last_flown_observed = True
                    continue
            picked = ('waiting', idx, CONF_OBSERVED)
            break
        # Kein Board-Signal → Uhr, mit Live-Gegencheck an den Kipp-Punkten.
        if now < eff_dep:
            # observed wenn ein ECHTES Signal die Lage stützt (beobachteter
            # Abflug-Delay dieses Legs ODER beobachtete Landung des vorigen).
            picked = ('waiting', idx,
                      CONF_OBSERVED if (dep_delay is not None
                                        or last_flown_observed) else CONF_PLAN)
            break
        window_end = eff_arr if arr_delay is not None else (
            eff_arr + _dt.timedelta(minutes=_ARR_BUFFER_MIN))
        if now < window_end:
            # Plan sagt „fliegt" — Live-Beweis darf in BEIDE Richtungen kippen.
            lv = _live(leg)
            if lv:
                if not lv.get('on_ground'):
                    picked = ('flying', idx, CONF_OBSERVED)
                    break
                if lv.get('near_dep'):
                    # Maschine steht noch am Abflug → wartet (kein Geist).
                    picked = ('waiting', idx, CONF_OBSERVED)
                    break
                if lv.get('near_arr'):
                    leg['flown'] = True
                    last_flown_observed = True
                    continue
            picked = ('flying', idx, CONF_PLAN)
            break
        # Uhr sagt „vorbei" — fliegt sie NACHWEISLICH noch (Delay ohne Obs)?
        lv = _live(leg)
        if lv:
            if not lv.get('on_ground'):
                picked = ('flying', idx, CONF_OBSERVED)
                break
            if lv.get('near_dep'):
                picked = ('waiting', idx, CONF_OBSERVED)
                break
        leg['flown'] = True
        last_flown_observed = False
        continue

    # ── Zustand + Text ───────────────────────────────────────────────────────
    if picked is None:
        # Alle Legs geflogen → am Tagesziel.
        leg = legs[-1]
        dest = leg['arr_ap'] or (hb or '')
        eff_arr = leg.get('eff_arr') or leg['arr']
        conf = CONF_OBSERVED if last_flown_observed else CONF_PLAN
        recent = (now - eff_arr) <= _dt.timedelta(minutes=_LANDED_RECENT_MIN)
        landed_sub = (f'Gelandet {hhmm(eff_arr, dest)}'
                      if not leg['arr_synth'] else None)
        if hb and dest == hb:
            if recent:
                return _result(STATE_LANDED, leg=leg, idx=len(legs) - 1,
                               title=f'Gelandet in {city(dest)}',
                               subtitle='Feierabend', confidence=conf)
            return _result(STATE_HOME, leg=leg, idx=len(legs) - 1,
                           title='Feierabend',
                           subtitle=f'Gelandet in {city(dest)}', confidence=conf)
        if recent:
            return _result(STATE_LANDED, leg=leg, idx=len(legs) - 1,
                           title=f'Gelandet in {city(dest)}',
                           subtitle=(f'Layover {city(dest)}'
                                     if dest != hb else None), confidence=conf)
        return _result(STATE_LAYOVER, leg=leg, idx=len(legs) - 1,
                       title=f'Layover {city(dest)}', subtitle=landed_sub,
                       confidence=conf)

    kind, idx, conf = picked
    leg = legs[idx]

    if kind == 'flying':
        # Position IMMER nachziehen: der Board-airborne-Zweig pickt 'flying',
        # ohne _live(leg) je gerufen zu haben — _position(leg) war dann leer
        # und iOS fiel auf die (verbotene) Grosskreis-Simulation zurueck,
        # obwohl die ECHTE Position im aircraft_live-Store lag (Tibor-LH803:
        # real ueber Schweden, Karte malte Strasbourg).
        _live(leg)
        o = leg.get('obs') or {}
        arr_delay = _num(o.get('arr_delay_min'))
        if arr_delay is None:
            arr_delay = _num(o.get('delay_min'))
        eff_arr = leg['arr'] + _dt.timedelta(minutes=max(0.0, arr_delay or 0.0))
        route = f"{leg['dep_ap']} → {leg['arr_ap'] or '?'}"
        sub = route
        if not leg['arr_synth']:
            t = hhmm(eff_arr, leg['arr_ap'] or leg['dep_ap'])
            if t:
                sub = f'{route} · Ankunft {t}'
        return _result(STATE_FLYING, leg=leg, idx=idx,
                       position=_position(leg), title='Fliegt gerade',
                       subtitle=sub, confidence=conf)

    if kind == 'cancelled':
        here = leg['dep_ap']
        return _result(STATE_PRE_FLIGHT, leg=leg, idx=idx,
                       title=f"{leg['flight'] or 'Flug'} annulliert",
                       subtitle=f'In {city(here)}', confidence=CONF_OBSERVED)

    # kind == 'waiting': vor Leg idx — physisch am Abflughafen dieses Legs.
    o = leg.get('obs') or {}
    dep_delay = _num(o.get('dep_delay_min'))
    eff_dep = leg['dep'] + _dt.timedelta(minutes=max(0.0, dep_delay or 0.0))
    t = hhmm(eff_dep, leg['dep_ap'])
    wait_txt = (f"Wartet auf {leg['flight']} · {t}" if leg['flight'] and t
                else (f'Wartet auf den Abflug · {t}' if t else 'Wartet auf den Abflug'))
    # PRE-FLIGHT-TIMELINE (Owner 2026-07-12): feingranulare Phase aus reinen
    # Zeitvergleichen (now vs. berechnete Marken) + Boarding-Beobachtung.
    pre = _resolve_pre_phase(leg, now, eff_dep, pre_ctx, hb,
                             first_leg=(idx == 0))
    if idx == 0:
        route = f"{leg['dep_ap']} → {leg['arr_ap'] or '?'}"
        # Fertiger Text in der Subtitle (Owner-Spez): alte Builds zeigen die
        # angereicherte Prosa, neue lesen zusätzlich pre_phase(_label).
        sub = f'{route} · {PRE_PHASE_LABEL[pre]}' if pre else route
        return _result(STATE_PRE_FLIGHT, leg=leg, idx=idx, title=wait_txt,
                       subtitle=sub, confidence=conf, pre_phase=pre)
    # Zwischen zwei Legs: gelandet am Abflughafen des kommenden Legs.
    # pre ist hier höchstens prep/boarding (Turnaround am Flugzeug).
    return _result(STATE_LANDED, leg=leg, idx=idx,
                   title=f"Gelandet in {city(leg['dep_ap'])}",
                   subtitle=wait_txt, confidence=conf, pre_phase=pre)


# ── Frische-Wahl: Briefing schlägt stalen Snapshot ───────────────────────────

def pick_fresher_sectors(snap_sectors, snap_ts, brief_sectors, brief_ts):
    """Sektor-Quelle wählen (PUR): das FRISCHERE Briefing (user_ical_briefings,
    ical_imported_at/updated_at) SCHLÄGT einen stalen Roster-Snapshot
    (roster_snapshots.taken_at) — Diagnose 2026-07-10: iCal-Freunde froren auf
    dem letzten Push-Snapshot ein, obwohl der serverseitige Kalender-Refresh
    längst frischere Sektoren hatte. → (sectors|None, 'snapshot'|'briefing')."""
    snap_ok = isinstance(snap_sectors, list) and any(
        isinstance(s, dict) for s in snap_sectors)
    brief_ok = isinstance(brief_sectors, list) and any(
        isinstance(s, dict) for s in brief_sectors)
    if not brief_ok:
        return (snap_sectors if snap_ok else None), 'snapshot'
    if not snap_ok:
        return brief_sectors, 'briefing'
    s_ts, b_ts = _parse_iso(snap_ts), _parse_iso(brief_ts)
    if b_ts is not None and (s_ts is None or b_ts > s_ts):
        return brief_sectors, 'briefing'
    return snap_sectors, 'snapshot'


# ── Adapter-Factories (impure, von den Consumers genutzt) ────────────────────

def build_obs_lookup(resolver, datum):
    """obs_lookup-Adapter um app._flight_obs_merged (free_only=True — der
    Fan-out über viele Freunde/Watcher darf NIE Paid-Spend auslösen; der
    Resolver ist upstream memoisiert). Wirft nie."""
    def _lookup(flight_no, dep_iata, arr_iata):
        if not (callable(resolver) and flight_no):
            return None
        try:
            return resolver(flight_no, date=datum, dep_iata=dep_iata,
                            arr_iata=(arr_iata or None), free_only=True)
        except Exception:
            return None
    return _lookup


def _haversine_km(lat1, lon1, lat2, lon2):
    import math
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


def build_live_lookup():
    """live_lookup-Adapter um den GRATIS aircraft_live-Store (NAS-Harvester/
    FR24-gRPC → Supabase; reiner Read, kein Paid-Ping). Route-konsistent
    (dest == arr des Legs), on_ground-Nähe zu dep/arr via Referenz-DB. Wirft nie."""
    def _lookup(flight_no, dep_iata, arr_iata):
        try:
            from blueprints.aerox_data_blueprint import (_aircraft_live_pos,
                                                         _iata_latlon)
        except Exception:
            return None
        try:
            pos, _rt, reg, _ty = _aircraft_live_pos(flight=flight_no,
                                                    dep=arr_iata)
            if not pos or pos.get('lat') is None or pos.get('lon') is None:
                return None
            out = {'lat': float(pos['lat']), 'lon': float(pos['lon']),
                   'track': pos.get('track'), 'gs': pos.get('gs'),
                   'ts': pos.get('seen_ts'),
                   'source': pos.get('source') or 'aircraft_live',
                   'on_ground': bool(pos.get('on_ground')),
                   'near_dep': False, 'near_arr': False, 'reg': reg}
            if out['on_ground']:
                for ap, key in ((dep_iata, 'near_dep'), (arr_iata, 'near_arr')):
                    try:
                        ll = _iata_latlon(ap)
                        if ll and _haversine_km(out['lat'], out['lon'],
                                                ll[0], ll[1]) <= _NEAR_AIRPORT_KM:
                            out[key] = True
                    except Exception:
                        continue
            return out
        except Exception:
            return None
    return _lookup


def build_local_hhmm(airport_tz_fn):
    """local_hhmm-Adapter: aware-UTC → 'HH:MM' in der STATIONS-Ortszeit
    (airport_tz; FRA/EDDF-Fallback Europe/Berlin wie app._board_local_to_utc_iso).
    Unbekannte TZ → UTC (nie None-Zeit wegen TZ-Lücke)."""
    def _fmt(d, iata):
        try:
            ap = str(iata or '').strip().upper()
            tzname = airport_tz_fn(ap) if callable(airport_tz_fn) else None
            if not tzname and ap in ('FRA', 'EDDF'):
                tzname = 'Europe/Berlin'
            if tzname:
                from zoneinfo import ZoneInfo
                return d.astimezone(ZoneInfo(tzname)).strftime('%H:%M')
        except Exception:
            pass
        return _default_hhmm(d)
    return _fmt
