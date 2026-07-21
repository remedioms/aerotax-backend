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
_STALE_GROUNDED_LANDED_MIN = 6 * 60  # grounded-Obs, deren Plan-Ankunft > 6 h vorbei
                               # ist, ist Scraper-Müll auf einem alten Leg → als
                               # geflogen altern (LH1126, Museum 2026-07-16); der
                               # Owner-„Board schlägt Uhr" gilt nur für heutige Legs.
_NEAR_AIRPORT_KM = 8.0         # „am Boden nahe Flughafen"-Radius (Gegencheck)
_DEP_LIFTOFF_GRACE_MIN = 15    # Zeit-Physik-Umschalter (Julien/Tibor 2026-07-16):
                               # steht der Flug nach eff_dep + dieser Spanne immer
                               # noch auf „grounded", OHNE echten Boden-Beweis am
                               # ABFLUGORT, ist die grounded-Obs arr-seitig/stale →
                               # die Uhr übernimmt (enroute), Position bleibt leer
                               # bis aircraft_live liefert. Klein: der Abflug-Moment
                               # ist die ehrliche Grenze zwischen Warten und Fliegen.

# ── PRE-FLIGHT-TIMELINE (Owner 2026-07-12) ──────────────────────────────────
# Feingranulare Phase VOR dem Abflug (nur state == pre_flight; zwischen zwei
# Legs höchstens prep/boarding). Kontrakt mit iOS (ADDITIV — alte Builds
# ignorieren die Felder, iOS-Fallback = exakt bisheriges Verhalten):
#   pre_phase ∈ checkin_open | commute | briefing | crewbus | security |
#               prep | boarding | delayed   (None außerhalb von pre_flight)
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
PRE_DELAYED = 'delayed'        # bekannte Start-Verspätung, Abflug NOCH nicht
                               # bewiesen (Owner 2026-07-13, Basti-Fall): ehrlich
                               # „Verspätet" statt einer ewig hängenden
                               # „Flugvorbereitung". Ersetzt PRE_PREP, sobald der
                               # Board-/Est-Delay einen späteren Abflug belegt —
                               # gilt VOR und NACH Erreichen von est_dep, bis ein
                               # echtes Abflug-/Fly-/Land-Signal es ablöst. Keine
                               # erfundene Position/Glyph — nur der Text wird ehrlich.

PRE_PHASE_LABEL = {
    PRE_CHECKIN: 'Check-in offen',
    PRE_COMMUTE: 'Fahrt zum Flughafen',
    PRE_BRIEFING: 'Briefing',
    PRE_CREWBUS: 'Im Crewbus',
    PRE_SECURITY: 'Durch die Security',
    PRE_PREP: 'Flugvorbereitung',
    PRE_BOARDING: 'Boarding',
    PRE_DELAYED: 'Verspätet',
}

_CREWBUS_RIDE_MIN = 25         # Default Hotel→Terminal-Fahrtzeit (Minuten) —
                               # bewusst Konstante: es gibt keine echte Quelle
                               # pro Hotel; 25' ist der LH-übliche Richtwert.
_PREP_BEFORE_DEP_MIN = 40      # „Flugvorbereitung" ab eff. Abflug − 40 min
                               # (Zeit-Heuristik NUR fürs Label, s.o.)
_PREP_CAP_GRACE_MIN = 5        # Gnadenspanne, um die die zeitbasierte Vor-Abflug-
                               # Phase über den (delay-korrigierten) eff_dep
                               # hinaus stehen bleibt (Uhr-/Board-Rundung), bevor
                               # sie ohne Delay-Wissen gekappt wird (Owner
                               # 2026-07-13, Basti-Fall). Klein gehalten: der
                               # Abflug-Moment ist die ehrliche Obergrenze.
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
                   'gate closed', 'gate zu', 'geschlossen', 'letzter aufruf')


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


def _resolve_pre_phase(leg, now, eff_dep, pre_ctx, hb, first_leg,
                       delay_known=False):
    """Feingranulare Vor-Abflug-Phase (siehe PRE_*-Block). Reine
    Zeitvergleiche: alle bekannten Phasen-Startmarken sammeln, die SPÄTESTE
    Marke ≤ now gewinnt. Unbekannte Marken fehlen einfach in der Liste →
    die Phase wird übersprungen, die nächste Grenze gilt (nichts erfinden).
    Boarding ist KEINE Zeitmarke, sondern nur beobachtet (Board schlägt Uhr
    in beide Richtungen). Zwischen zwei Legs (first_leg=False, Turnaround am
    Flugzeug) gibt es keine Checkin-/Anfahrts-Phasen — nur prep/boarding.

    KAPPUNG + Verspätung (Owner 2026-07-13, Basti-Fall LH900): die
    zeitbasierte „Flugvorbereitung" (prep, ab eff_dep−40) hatte KEINEN oberen
    Deckel — sie hing ewig, auch wenn der (schon delay-korrigierte) eff_dep
    längst vorbei war und weder Board noch Live den Abflug bewiesen. eff_dep
    ist der EHRLICHE Abflug-Moment (Soll + bekannter Delay); ab da ist eine
    Vor-Abflug-Prosa irreführend. Deshalb:
      • Ist der Delay BEKANNT (est_dep > sched_dep), gewinnt PRE_DELAYED
        („Verspätet") über PRE_PREP — VOR und NACH Erreichen von eff_dep,
        bis ein Abflug-/Fly-/Land-Signal den Zustand ohnehin ablöst. Der Text
        zeigt dann die verspätete Abflugzeit (eff_dep) statt „Flugvorbereitung".
      • Ohne bekannten Delay wird prep bei now ≥ eff_dep (kleine Gnadenspanne
        _PREP_CAP_GRACE_MIN gegen Uhr-/Board-Rundung) gekappt → None
        (neutraler Text, nichts erfunden). Boarding (beobachtet) bleibt
        unberührt, es ist ein echtes Signal.
    """
    o = leg.get('obs') or {}
    if _status_is_boarding(o.get('status')):
        return PRE_BOARDING
    prep_start = eff_dep - _dt.timedelta(minutes=_PREP_BEFORE_DEP_MIN)
    # Deckel: Vor-Abflug-Phase endet am (delay-korrigierten) eff_dep. Kleine
    # Gnadenspanne für Uhr-/Board-Rundung, damit die Phase nicht 1-2 min VOR
    # dem realen Abheben verschwindet.
    prep_cap = eff_dep + _dt.timedelta(minutes=_PREP_CAP_GRACE_MIN)
    if now >= prep_start:
        if delay_known:
            return PRE_DELAYED         # ehrlich „Verspätet" statt „Flugvorbereitung"
        if now < prep_cap:
            return PRE_PREP
        return None                    # gekappt: Abflug-Moment vorbei, kein Beweis
    if not first_leg:
        # Turnaround-Leg vor prep: „Verspätet" nur wenn der Soll-Abflug schon
        # durch ist (überfällig) — sonst keine Phase.
        return PRE_DELAYED if (delay_known and now >= leg['dep']) else None
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
    if best is None:
        return None
    # BEKANNTER DELAY schlägt die gegen die STALE Soll-Abflugzeit gerechneten
    # Timeline-Phasen (Owner 2026-07-13, Tibor: 2 h VOR verspätetem Abflug
    # stand „Briefing", weil die Marken relativ zur Soll-Zeit 08:25 liegen —
    # obwohl der Flug erst 13:20 geht). „Verspätet · Abflug HH:MM" ist die
    # ehrliche Schlagzeile. Aus einem ECHTEN iCal-Pickup abgeleitete Phasen
    # (crewbus/security) bleiben — sie sind ein reales, nicht gegen die Soll-
    # Zeit gerechnetes Signal; Boarding hat oben schon gewonnen.
    # NUR wenn der SOLL-Abflug schon durch ist (now >= sched_dep) — der Flug ist
    # „überfällig" und nachweislich verspätet. VOR dem Soll-Abflug bleibt die
    # normale Timeline (checkin/briefing), kein alarmierender Dauer-„Verspätet"
    # den ganzen Tag (Basti-Fall: kleiner Delay, früh morgens = checkin).
    if (delay_known and now >= leg['dep']
            and best[1] in (PRE_CHECKIN, PRE_COMMUTE, PRE_BRIEFING, PRE_PREP)):
        return PRE_DELAYED
    return best[1]


# Default-Status-Buckets — Consumers reichen app._flight_status_bucket rein
# (DIE eine Wahrheit); diese Listen sind nur der abgespeckte Offline-Fallback.
_LANDED_WORDS = ('landed', 'gelandet', 'arrived', 'angekommen')
_AIRBORNE_WORDS = ('airborne', 'in flight', 'in-flight', 'enroute', 'en route',
                   'departed', 'abgeflogen', 'unterwegs', 'approach',
                   'approaching', 'final approach', 'on final', 'im anflug')
_GROUNDED_WORDS = ('boarding', 'gate', 'scheduled', 'delayed', 'verspätet',
                   'check-in', 'checkin', 'on time', 'pünktlich', 'wait',
                   'closed', 'geschlossen', 'gate zu')


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


# Board-Status-Tokens, die BODEN-Aktivität am ABFLUG-Flughafen belegen (der Flug
# ist nachweislich noch nicht los): Boarding/Gate-Aktivität/Gate zu. NICHT dabei:
# reines „Verspätet"/„Delayed" (kann von der ANKUNFTS-Tafel kommen — ein
# verspäteter Flug fliegt längst) und „Scheduled"/„On time" (nur Plan-Echo).
_DEP_GROUND_WORDS = ('boarding', 'einsteigen', 'last call', 'final call',
                     'letzter aufruf', 'gate', 'closed', 'geschlossen',
                     'check-in', 'checkin', 'go to gate', 'on time', 'ontime',
                     'pünktlich', 'puenktlich', 'scheduled', 'planmäßig',
                     'planmaessig')
# Zwei BEWEIS-ARTEN (Julien LH423 2026-07-16, BOS postet „Delayed 75 Minutes"
# und danach NIE „Departed"): aktive GATE-Wörter belegen Boden-Kontakt hart —
# ein bloßes Plan-Echo/„Delayed" des Abflug-Boards ist dagegen nur so lange
# ein Beweis, wie der angekündigte Abflug (inkl. gemeldetem Delay) + Karenz
# nicht verstrichen ist. Wäre die Maschine dann noch am Boden, stünde ein
# GRÖSSERER Delay am Board — ein eingefrorener Delay-Text heißt: sie ist weg.
_DEP_GATE_WORDS = ('boarding', 'einsteigen', 'last call', 'final call',
                   'letzter aufruf', 'gate', 'closed', 'geschlossen',
                   'check-in', 'checkin', 'go to gate')
_DEP_PLAN_ECHO_WORDS = ('on time', 'ontime', 'pünktlich', 'puenktlich',
                        'scheduled', 'planmäßig', 'planmaessig')
_DEP_DELAY_MIN_RE = _re.compile(r'(\d+)\s*min', _re.IGNORECASE)


def _announced_dep_delay_min(o):
    """Gemeldeter ABFLUG-Delay in Minuten: explizites dep_delay_min oder aus
    dem status_dep-Text geparst („Delayed 75 Minutes" → 75). None wenn keins."""
    try:
        v = _num(o.get('dep_delay_min'))
        if v is not None:
            return max(0, int(v))
        m = _DEP_DELAY_MIN_RE.search(str(o.get('status_dep') or ''))
        if m:
            return max(0, int(m.group(1)))
    except Exception:
        pass
    return None
# Reine VERSPÄTUNGS-Wörter: mehrdeutig (Abflug- ODER Ankunfts-Tafel). Sie zählen
# NUR als Boden-Beweis, wenn sie NACHWEISLICH abflugseitig sind (status_dep /
# dep_delay_min / delay_side=='dep') — ein bloßes gemergtes „Verspätet" kommt bei
# einem Über-Ozean-Flug von der Ankunfts-Tafel (Julien/Tibor 2026-07-16).
_DELAY_ONLY_WORDS = ('delayed', 'verspätet', 'verspaetet', 'delay')


def _dep_ground_proof(o, bucket_of=None):
    """PUR: belegt der Board-Record ECHTEN Boden-Kontakt am ABFLUG-Flughafen?
    (Julien/Tibor 2026-07-16). Der crew obs_lookup mergt beide Tafel-Seiten; der
    gemergte `status` ist „arr gewinnt", also trägt er bei einem Über-Ozean-Flug
    OHNE Abflug-Board die ANKUNFTS-Tafel-Meldung (FRA: „Verspätet") — das ist KEIN
    Beweis, dass die Maschine noch am Boden steht (sie fliegt seit Stunden). Nur
    diese Signale zählen als „noch nicht abgeflogen":
      • ein DEP-seitiger Board-Status (`status_dep`) mit Gate-/Boarding-Wort, ODER
      • ein ABFLUG-Delay (`dep_delay_min` gesetzt bzw. `delay_side == 'dep'`) —
        das ABFLUG-Board meldet die Verspätung, der Flug steht noch am Gate
        (Basti-Fall LH900: est_dep verschoben, aber noch nicht los), ODER
      • ein beobachtetes Boarding (status_dep-Boarding-Wort).
    Ein rein arr-seitiges „Verspätet"/„Delayed" (delay_side == 'arr' bzw. nur
    `arr_delay_min`/`delay_min`, KEIN status_dep, KEIN dep_delay_min) ist KEIN
    Boden-Beweis — das ist die ANKUNFTS-Tafel eines längst fliegenden Fluges
    (Julien/Tibor-Über-Ozean-Fall). Wirft nie."""
    return _dep_ground_proof_kind(o, bucket_of) is not None


def _dep_ground_proof_kind(o, bucket_of=None):
    """Wie `_dep_ground_proof`, aber mit BEWEIS-ART: 'gate' = aktive Gate-/
    Boarding-Aktivität (harter Beweis, Board schlägt Uhr), 'delay' = gemeldeter
    Abflug-Delay/Plan-Echo (zeitlich begrenzter Beweis — gilt nur bis Soll-
    Abflug + gemeldetem Delay + Karenz; Julien LH423: BOS fror auf „Delayed 75
    Minutes" ein und postete nie „Departed"). None = kein Boden-Beweis."""
    try:
        if not isinstance(o, dict):
            return None
        sd = str(o.get('status_dep') or '').strip().lower()
        if sd and not ('deboard' in sd or 'ausstieg' in sd):
            if any(t in sd for t in _DEP_GATE_WORDS):
                return 'gate'
            # Plan-Echo („on time"/„scheduled") bleibt HARTER Beweis (Owner-
            # Regel 2026-07-13, Sebastian: Board schlägt Uhr — gepinnt in
            # test_past_dep_ohne_delay_neutral…). Nur DELAY-Meldungen sind
            # zeitbegrenzt: ein eingefrorenes „Delayed 75 Minutes" ohne je ein
            # „Departed" (Julien LH423, BOS) darf nicht ewig pinnen.
            if any(t in sd for t in _DEP_PLAN_ECHO_WORDS):
                return 'gate'
            bkt = bucket_of if callable(bucket_of) else _default_bucket
            if bkt(o.get('status_dep')) == 'grounded':
                # z.B. „Delayed 75 Minutes": dep-seitig, aber nur zeitbegrenzt.
                return 'delay'
        # ABFLUG-Delay → das Abflug-Board hat die Verspätung gemeldet, der Flug
        # steht noch am Gate (Basti-Fall LH900). Ein expliziter dep_delay_min
        # ODER delay_side=='dep' zählt; rein arr-seitiger Delay NICHT.
        if _num(o.get('dep_delay_min')) is not None:
            return 'delay'
        if str(o.get('delay_side') or '').strip().lower() == 'dep':
            return 'delay'
        # Gemergter `status` OHNE Seiten-Split: ein eindeutiges Boden-Aktivitäts-
        # Wort (Boarding/Gate/…) belegt den Abflug-Boden hart; Plan-Echo („on
        # time"/„scheduled", Basti-Fall) nur zeitbegrenzt. Ein bloßes
        # „Verspätet"/„Delayed" (mehrdeutig, oft ARR-Tafel) zählt hier NICHT —
        # das ist genau der Julien/Tibor-Über-Ozean-Fall.
        s = str(o.get('status') or '').strip().lower()
        if s and not ('deboard' in s or 'ausstieg' in s):
            if any(t in s for t in _DELAY_ONLY_WORDS):
                return None
            if any(t in s for t in _DEP_GATE_WORDS):
                return 'gate'
            if any(t in s for t in _DEP_PLAN_ECHO_WORDS):
                return 'gate'   # Owner-Regel: „on time"-Board schlägt Uhr
        return None
    except Exception:
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
        if arr is not None and arr <= dep:
            # ARRIVAL-DAY-gekeyte iCal-Legs (Julien LH423 BOS→FRA 2026-07-16):
            # LH keyt Über-Nacht-Legs am ANKUNFTS-Tag — Tages-Datum + lokale
            # ABFLUG-Zeit des Vortags ergibt dep NACH arr (dep „16.07 18:40 EDT",
            # arr 16.07 06:50 CEST). Der heutige Resolver-Lauf sah dadurch einen
            # Zukunfts-Abflug („Nächster Flug 18:40") und blockte den Overnight-
            # Carry. Physik-Reparatur: dep um 1 Tag zurück, wenn GENAU das eine
            # plausible Leg-Dauer (0 < Dauer ≤ 20 h) ergibt — sonst wie gehabt
            # das synthetische Fenster (keine erfundene Identität).
            _cand = dep - _dt.timedelta(days=1)
            _dur_h = (arr - _cand).total_seconds() / 3600.0
            if 0 < _dur_h <= 20:
                dep = _cand
        if arr is None or arr <= dep:
            arr = dep + _dt.timedelta(hours=3)
            arr_synth = True
        fno = str(s.get('flight') or '').replace(' ', '').upper() or None
        # TAIL/REG tolerant lesen (Julien/Tibor 2026-07-16): der iOS-Roster-Push
        # keyt die Maschine je nach Reader-Pfad uneinheitlich — die iOS-Bordkarte
        # zeigt die Reg (DABYT), das Feld liegt im Sektor-Datensatz aber mal als
        # 'tail', mal als 'reg'/'ac_reg'/'registration'/'aircraft_reg'. Erste
        # nicht-leere Variante gewinnt, damit der aircraft_live-Positions-Tier
        # (Reg-Match) die Maschine auch findet, wenn nur EIN Alias gesetzt ist.
        _tail = None
        for _k in ('tail', 'reg', 'ac_reg', 'registration', 'aircraft_reg'):
            _v = str(s.get(_k) or '').strip().upper()
            if _v:
                _tail = _v
                break
        legs.append({'dep_ap': frm, 'arr_ap': to, 'flight': fno,
                     'dep': dep, 'arr': arr, 'arr_synth': arr_synth,
                     'tail': _tail})
    legs.sort(key=lambda l: l['dep'])
    return legs


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _eff_arr(leg, o):
    """Effektive (delay-korrigierte) Ankunft EINES Legs → (eff_dt, delay_min).

    EINE Wahrheit mit dem Radar (Owner 2026-07-13 „Live-Karte 8:40, Radar paar
    Min später"): der Radar zeigt die absolute est_arr aus dem Warehouse
    (flights.est_arr / <arr>#ARR, via _board_local_to_utc_iso). Der Crew-State
    kannte bisher nur `sched_arr + arr_delay_min` und fiel bei UNBEKANNTEM
    (aber existierendem) Board-esti auf die Plan-Zeit zurück → Divergenz.
    Deshalb: das absolute `est_arr_iso` aus den obs SCHLÄGT `sched + delay`
    (gleiche Quelle wie der Radar). Fehlt es, exakt das alte Verhalten
    (`arr_delay_min`/`delay_min` auf die Roster-Soll-Ankunft). Ein synthetisches
    arr (arr_synth) hat keine echte Soll-Ankunft → keine eff-Ankunft.
    Wirft nie. Returns (aware-UTC | None, int-min | None)."""
    o = o or {}
    arr_delay = _num(o.get('arr_delay_min'))
    if arr_delay is None:
        arr_delay = _num(o.get('delay_min'))
    if leg.get('arr_synth'):
        return None, (int(round(arr_delay)) if arr_delay is not None else None)
    est_abs = _parse_iso(o.get('est_arr_iso'))
    if est_abs is not None:
        d = round((est_abs - leg['arr']).total_seconds() / 60.0)
        return est_abs, int(d)
    if arr_delay is not None:
        return leg['arr'] + _dt.timedelta(minutes=arr_delay), int(round(arr_delay))
    return None, None


def _eff_dep(leg, o):
    """Effektiver (delay-korrigierter) Abflug EINES Legs → (eff_dt, delay_min).
    Symmetrisch zu _eff_arr: absolute `est_dep_iso` aus den obs schlägt
    `sched_dep + dep_delay_min`. Wirft nie. Returns (aware-UTC, int-min|None)."""
    o = o or {}
    dep_delay = _num(o.get('dep_delay_min'))
    est_abs = _parse_iso(o.get('est_dep_iso'))
    if est_abs is not None:
        d = round((est_abs - leg['dep']).total_seconds() / 60.0)
        return est_abs, int(d)
    if dep_delay is not None:
        return leg['dep'] + _dt.timedelta(minutes=dep_delay), int(round(dep_delay))
    return leg['dep'], None


# Sicherheitsmarge, um die eine ECHTE (delay-korrigierte) Ankunft ihren eigenen
# Abflug frühestens unterschreiten dürfte, bevor wir die Obs als „falscher Tag"
# verwerfen. Ein Flug landet NIE vor seinem eigenen Abflug — nur ein Board-Record
# einer ANDEREN Tagesbetriebs-Instanz derselben Flugnummer/Strecke kann das.
_WRONG_DAY_MARGIN_MIN = 60


def _obs_is_wrong_day(leg, o):
    """PUR: gehört der (datums-agnostisch gematchte) Board-Record `o` zu einer
    ANDEREN Tagesbetriebs-Instanz als dieses iCal-Leg?

    Der crew obs_lookup matcht NUR über (flight_no, dep_iata, arr_iata) — ohne
    Datum. Bei einem täglichen Flug (LH423 BOS→FRA) liefert er den ZULETZT
    beobachteten Lauf, der am Rückflugtag längst gelandet ist, während das echte
    Leg der Crew erst am Abend abfliegt. Diese Vortags-Landung („status=LANDED,
    est_arr heute früh") markierte das noch nicht abgeflogene Leg als „flown" →
    picked=None → „Gelandet in Frankfurt / Feierabend" (Tibor 2026-07-15,
    Julien/Nico BOS-Layover).

    Signal, unmissverständlich und physikalisch: die BEOBACHTETE Ankunft der Obs
    liegt VOR dem eigenen (Soll-)Abflug des Legs — plus Sicherheitsmarge. Ein
    korrekter Lauf kann NIE vor seinem Abflug landen; nur ein Fremd-Tag-Match
    kann das. Wirft nie."""
    try:
        if not isinstance(o, dict):
            return False
        # Absolute beobachtete Ankunft bevorzugt; sonst sched+arr_delay.
        obs_arr = _parse_iso(o.get('est_arr_iso'))
        if obs_arr is None:
            arr_delay = _num(o.get('arr_delay_min'))
            if arr_delay is None:
                arr_delay = _num(o.get('delay_min'))
            sched_arr = _parse_iso(o.get('sched_arr_iso'))
            if sched_arr is not None and arr_delay is not None:
                obs_arr = sched_arr + _dt.timedelta(minutes=arr_delay)
            elif sched_arr is not None:
                obs_arr = sched_arr
        if obs_arr is None:
            return False
        dep = leg.get('dep')
        if not isinstance(dep, _dt.datetime):
            return False
        return obs_arr < dep - _dt.timedelta(minutes=_WRONG_DAY_MARGIN_MIN)
    except Exception:
        return False


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


# ── FLIGHTSTATE-ENGINE als FLUG-AUTORITÄT eines Legs ─────────────────────────
# INTEGRATIONSPLAN (Owner 2026-07-13): die FLUG-Phase eines Kandidaten-Legs
# (fliegt / gelandet / noch nicht abgeflogen) kommt aus der EINEN Engine
# (blueprints/flight_state.resolve_flight_state) statt aus den crew-eigenen
# Heuristiken (b-Buckets/eff_dep-airborne). Die Engine kennt DIE Regel „fliegen
# muss verdient sein" (Airborne-Gate), die Landungs-MONOTONIE (Terminal-Phase
# kippt nie zurück) und die side-aware Board-Klassifikation (dep-„Abgeflogen" =
# TAXI_OUT/off-block, NICHT airborne). GENAU DAS löst den Sebastian-Fall: das
# ARR-seitige „gelandet 12:27" ist ein HARD-Signal → LANDED (terminal) → Leg
# geflogen, eine stale eff_arr-Schätzung (13:28) kann es NICHT „un-landen" und
# zeigt nie „fliegt · 13:06".
#
# KEIN NEUER I/O: die Observations kommen AUSSCHLIESSLICH aus den schon
# geladenen Leaf-Reads des Resolvers (leg['obs'] = der gemergte Board-Record,
# leg['live'] = der aircraft_live-Fix). Der Bridge macht NIE einen Netz-/DB-Call
# und wirft nie — bei jedem Fehler liefert er None und der Aufrufer fällt auf
# die (unveränderte) Uhr-/Live-Kaskade zurück.
#
# Die Engine-Phase wird in eine crew-Leg-Entscheidung übersetzt:
#   CANCELLED                     → 'cancelled'
#   LANDED | ARRIVED | DIVERTED   → 'flown'   (Leg beendet, nächsten prüfen —
#                                   DIVERTED = woanders gelandet, NICHT airborne;
#                                   Feinschliff 2, 100%-Gleichlaut 2026-07-13)
#   AIRBORNE | APPROACH           → 'flying'
#   TAXI_OUT                      → 'departed' (off-block; crew: der Leg ist
#                                   unterwegs — die Leg-Auswahl behandelt ihn wie
#                                   'flying', ABER die Widerspruchs-/Stale-Gates
#                                   des Aufrufers dürfen ihn noch entwerten)
#   BOARDING | SCHEDULED | UNKNOWN → None    (kein hartes Flug-Signal → der
#                                   Aufrufer entscheidet über Uhr + Live-Check)

# Engine-Phase-Tokens (String-Konstanten, hier gespiegelt um einen harten
# Import-Zwang im reinen Modul zu vermeiden — die Engine ist die Quelle).
_ENG_CANCELLED = 'CANCELLED'
_ENG_LANDED = 'LANDED'
_ENG_ARRIVED = 'ARRIVED'
_ENG_AIRBORNE = 'AIRBORNE'
_ENG_APPROACH = 'APPROACH'
_ENG_DIVERTED = 'DIVERTED'
_ENG_TAXI_OUT = 'TAXI_OUT'
_ENG_SCHEDULED = 'SCHEDULED'
_ENG_BOARDING = 'BOARDING'
# Die Grenze „off-block zu lange ⇒ airborne (Zeit-Evidenz, estimated)" lebt jetzt
# AUSSCHLIESSLICH in der Engine (flight_state.TAXI_OUT_MAX_S) — crew_state
# spiegelt die Engine-Phase 1:1: TAXI_OUT → „Startet gerade" (kein Live), sobald
# die Engine hebt kommt kind='flying'. So sind alle Projektionen konsistent.
# (Der frühere crew-eigene 25-min-Deckel _TAXI_OUT_MAX_MIN ist entfernt.)


def _engine_leg_flight(leg, o, live, now, dep_ll=None, arr_ll=None,
                       dep_elev_ft=None, prior=None, bucket_of=None):
    """FLUG-Autorität EINES Legs aus der FlightState-Engine — reuse der schon
    geladenen obs (`o`) + Live-Fix (`live`), KEIN neuer I/O. Wirft nie.

    Returns (kind, phase, phase_conf, live_pos) oder None:
      kind ∈ 'cancelled'|'flown'|'flying'|'departed'|None (siehe Mapping oben).
      phase/phase_conf = die kanonische Engine-Phase (für Konfidenz/Debug).
      live_pos = die Engine-gegatete Position (dict|None) — NUR gesetzt, wenn die
                 Engine sie rendert (airborne-Gate bestanden); sonst None.

    Die Observations werden aus dem gemergten Board-Record `o` gebaut:
      • status_dep/status_arr (echter Merge-Shape) → side-aware Hard/Soft-Phasen
        via obs_from_board_merged; das ARR-seitige „gelandet" ist hier der
        Sebastian-Hebel (Board schlägt jede stale Schätzung).
      • fehlen status_dep/status_arr (Test-/Alt-Shape mit nur `status`), wird der
        EINE Merge-Status über die crew-Vokabular-Klassifikation (`bucket_of` =
        app._flight_status_bucket, DIE eine Wahrheit) auf die richtige Seite
        gelegt: 'landed' → status_arr (Engine LANDED, terminal), 'airborne' →
        status_dep='airborne' (Engine AIRBORNE, dep-enroute-proven). 'grounded'/
        None erzeugt KEINE Phasen-Observation (Engine → SCHEDULED/BOARDING) →
        der Aufrufer entscheidet über Uhr/Live. So bleibt die crew-Vokabular-
        Grenze (z. B. „Deboarding" = KEIN Landungssignal DIESES Legs) exakt
        erhalten, während Landung-Monotonie + Airborne-Gate aus der Engine kommen.
      • cancelled/est_dep_iso/est_arr_iso/dep_delay_min/arr_delay_min fließen als
        Zeiten/Delay-Observations ein (die Engine braucht sie fürs ETA/Delay).
      • der Live-Fix (`live`) wird zur Positions-Observation (aircraft_live).
    """
    try:
        from blueprints.flight_state import (resolve_flight_state,
                                             remember_state)
        from blueprints.flight_state_collectors import (build_keys,
                                                        obs_from_board_merged,
                                                        Observation)
    except Exception:
        return None
    try:
        now_ts = now.timestamp() if isinstance(now, _dt.datetime) else None
        keys = build_keys(
            leg.get('flight'), None, leg['dep_ap'], leg['arr_ap'],
            roster_tail=leg.get('tail'),
            sched_dep_iso=_iso_z(leg['dep']),
            sched_arr_iso=(None if leg.get('arr_synth') else _iso_z(leg['arr'])),
            dep_ll=dep_ll, arr_ll=arr_ll, dep_elev_ft=dep_elev_ft)
        o = o if isinstance(o, dict) else {}
        bkt = bucket_of if callable(bucket_of) else _default_bucket
        # Board-Observations: echter Merge-Shape (status_dep/status_arr) direkt an
        # die Engine (die side-aware Klassifikation + der arr-seitige Landungs-
        # Hebel = Sebastian). Fehlen die Seiten-Felder (nur `status`), wird der
        # EINE Merge-Status über die CREW-Vokabular-Klassifikation seiten-korrekt
        # synthetisiert — so bleibt die crew-Grenze (Deboarding ≠ Landung) exakt.
        m = dict(o)
        if not (o.get('status_dep') or o.get('status_arr')) and o.get('status'):
            _cb = bkt(o['status'])
            if _cb == 'landed':
                m['status_arr'] = o['status']        # → Engine LANDED (terminal)
            elif _cb == 'airborne':
                m['status_dep'] = 'airborne'         # → Engine AIRBORNE (proven)
            # 'grounded'/None: KEINE Phasen-Observation (Engine SCHEDULED) — die
            # Uhr-/Live-Kaskade des Aufrufers entscheidet (crew-Semantik).
        # SHAPE-ANGLEICH an flights_live (Owner 2026-07-13, Divergenz A): der crew
        # obs_lookup trägt die REVIDIERTEN Board-Zeiten als absolute ISO-Strings
        # (`est_dep_iso`/`est_arr_iso`) und die Delays als `dep_delay_min`/
        # `arr_delay_min`/`delay_min` — die flights_live-Projektion füttert die
        # Engine mit denselben Fakten über `esti_dep`/`esti_arr`+`delay_known`.
        # obs_from_board_merged liest genau diese Merge-Keys. Ohne board_to_iso
        # laufen die ISO-Strings 1:1 durch. NÖTIG fürs korrekte off-block-Anker
        # (est_dep) + expected-arr (est_arr / sched+delay) der TAXI_OUT→AIRBORNE-
        # Zeit-Regel — sonst ankert die Engine den Abflug an der reinen Soll-Zeit
        # und hält einen frisch off-block-Flug fälschlich für „lange unterwegs".
        if o.get('est_dep_iso') and not m.get('esti_dep'):
            m['esti_dep'] = o['est_dep_iso']
        if o.get('est_arr_iso') and not m.get('esti_arr'):
            m['esti_arr'] = o['est_arr_iso']
        _dep_dl = _num(o.get('dep_delay_min'))
        _arr_dl = _num(o.get('arr_delay_min'))
        if _arr_dl is None:
            _arr_dl = _num(o.get('delay_min'))
        if not m.get('delay_known') and (_dep_dl is not None or _arr_dl is not None):
            m['delay_known'] = True
            if m.get('dep_delay_min') is None:
                m['dep_delay_min'] = _dep_dl
            if m.get('arr_delay_min') is None:
                m['arr_delay_min'] = _arr_dl
        obs = obs_from_board_merged(m, keys, now=now_ts)
        # Positions-Observation aus dem schon geladenen Live-Fix (aircraft_live).
        # Nur wenn Koordinaten da sind; on_ground_raw wird von der Engine ignoriert
        # (das Airborne-Gate entscheidet). seen_ts/track/gs/alt so weit vorhanden.
        if isinstance(live, dict) and live.get('lat') is not None \
                and live.get('lon') is not None:
            obs.append(Observation('position', {
                'lat': live.get('lat'), 'lon': live.get('lon'),
                'track': live.get('track'), 'gs_kt': live.get('gs'),
                'alt_ft': live.get('alt'),
                'on_ground_raw': live.get('on_ground'),
                'position_source': 3,
            }, 'aircraft_live',
                _iso_or_epoch_ts(live.get('ts')) or now_ts or 0.0))
        fs = resolve_flight_state(keys, obs, now=now_ts, prior=prior)
        try:
            remember_state(fs, now=now_ts)
        except Exception:
            pass
        phase = fs.get('phase')
        conf = fs.get('phase_conf')
        live_pos = fs.get('live')
        if phase == _ENG_CANCELLED:
            kind = 'cancelled'
        elif phase in (_ENG_LANDED, _ENG_ARRIVED, _ENG_DIVERTED):
            kind = 'flown'          # DIVERTED = woanders gelandet, Leg beendet
        elif phase in (_ENG_AIRBORNE, _ENG_APPROACH):
            kind = 'flying'
        elif phase == _ENG_TAXI_OUT:
            kind = 'departed'
        else:
            kind = None
        return kind, phase, conf, live_pos
    except Exception:
        return None


def _iso_or_epoch_ts(v):
    """Live-Fix-ts → UTC-Epoch (float) oder None. Akzeptiert Epoch (int/float)
    und ISO-String; None bei unparsebar. Wirft nie."""
    if v is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    d = _parse_iso(v)
    return d.timestamp() if isinstance(d, _dt.datetime) else None


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
                    # Roster-Tail als REG mitgeben (Julien/Tibor 2026-07-16): der
                    # aircraft_live-Store keyt die Reg OHNE Bindestrich (DABYN);
                    # der Store-Read normalisiert 'D-ABYN' → 'DABYN' selbst und
                    # kann so die Maschine per Reg finden, wenn der Welt-Harvester
                    # den Korridor noch nicht per Flugnummer erfasst hat.
                    v = live_lookup(leg['flight'], leg['dep_ap'], leg['arr_ap'],
                                    leg.get('tail'))
            except TypeError:
                # Alt-Callsite/Test-Lookup ohne reg-Parameter → 3-arg-Fallback.
                try:
                    v = live_lookup(leg['flight'], leg['dep_ap'], leg['arr_ap'])
                except Exception:
                    v = None
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
        # est_dep/est_arr: absolute Warehouse-esti schlägt sched+delay (EINE
        # Wahrheit mit dem Radar — s. _eff_arr/_eff_dep). Fehlt das absolute
        # est, exakt das alte Verhalten (nur sched+delay). Das Anzeige-est darf
        # frischer sein als der (evtl. noch unbekannte) explizite delay_min —
        # deshalb hier über die Helfer, aber die delay_min/known-Metrik unten
        # bleibt an den EXPLIZITEN obs-Delays hängen (Contract stabil).
        _ed, _ = _eff_dep(leg, o)
        est_dep = _ed if (o.get('est_dep_iso') or dep_delay is not None) else None
        est_arr, _ = _eff_arr(leg, o)
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
        # track/gs MITGEBEN (Owner 2026-07-12, „Glyph schief"): der Store
        # (build_live_lookup) hat Kurs+Speed längst — ohne sie stand das
        # Flieger-Symbol der Crew-Live-Mini-Map in Ruhelage (nie in
        # Flugrichtung gedreht) und das Dead-Reckoning der Karte lief leer.
        # ADDITIV: iOS-Decoder (AXLifecycleLive) kennt die Keys bereits.
        return {'lat': lv['lat'], 'lon': lv['lon'], 'ts': ts,
                'track': _num(lv.get('track')), 'gs': _num(lv.get('gs')),
                'on_ground': False,
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

    # ── Leg-loser Tag: standby / Layover-Ruhetag / frei / kein Dienst ────────
    if not legs:
        lay = str(layover_iata or '').strip().upper() or None
        if lay and hb and lay == hb:
            lay = None            # „Layover an der Homebase" gibt es nicht
        d = str(duty or '').strip().lower()
        if d in ('standby', 'sby', 'reserve'):
            sub = f'Basis {city(hb)}' if hb else None
            return _result(STATE_STANDBY, title='Standby', subtitle=sub)
        if lay:
            return _result(STATE_LAYOVER, title=f'Layover {city(lay)}')
        # FREI/URLAUB (B2 Tibor 2026-07-12, „Wieso steht bei euch nicht das
        # Gleiche"): der Resolver kannte nur Sektoren — ein Roster-FREI-Tag
        # wurde „Basis Frankfurt", während iOS lokal „heute frei" ableitete →
        # ZWEI Texte für dieselbe Person je nach Screen/Build. Jetzt liefert
        # der SERVER den Frei-Text (duty='free'|'vacation' aus klass/marker,
        # siehe app._crew_state_for_day) — EINE Textquelle. Bewusst OHNE
        # „Basis X"-Subtitle: wo jemand seinen freien Tag verbringt, wissen
        # wir nicht (nichts erfinden).
        if d in ('vacation', 'urlaub', 'vac'):
            return _result(STATE_HOME, title='Im Urlaub')
        if d in ('visa', 'visum'):
            # Administrativer Termin (Visum/Botschaft) — KEIN Dienst, kein
            # Airport-Weg. STATE_HOME statt eines Dienst-Zustands ⇒ kein Pickup.
            return _result(STATE_HOME, title='Visum-Termin')
        if d in ('free', 'frei', 'off'):
            return _result(STATE_HOME, title='Heute frei')
        # „Basis Frankfurt" NUR wenn wirklich kein Dienst (Owner-Vorgabe).
        title = f'Basis {city(hb)}' if hb else 'Kein Dienst'
        return _result(STATE_HOME, title=title)

    # ── Zeitbasierter Leg-Pick mit Board-Obs + Live-Gegencheck ──────────────
    # (Vorlage: family_watch._pick_current_sector; hier zusätzlich mit dem
    # aircraft_live-Beweis in beide Richtungen.)
    picked = None          # (kind, idx, confidence); kind ∈ flying|waiting|cancelled
    last_flown_observed = False
    # Engine-prior pro (flight) INNERHALB dieses Laufs: die Landung-Monotonie
    # (LANDED regressiert nie zurück) wirkt schon innerhalb EINES resolve, dieser
    # Cache trägt sie zusätzlich über wiederholte Bewertungen desselben Fluges
    # in einem Multi-Leg-Tag. Rein prozesslokal, wirft nie.
    _eng_prior = {}
    for idx, leg in enumerate(legs):
        o = _obs(leg) or {}
        # FALSCHER-TAG-OBS-GATE (Tibor 2026-07-15, Julien/Nico BOS-Layover): der
        # obs_lookup matcht datums-AGNOSTISCH über (flight_no, dep, arr). Bei einem
        # täglichen Flug (LH423 BOS→FRA) hängt so die HEUTE-FRÜH gelandete Instanz
        # am ABEND-Leg der Crew — deren Ankunft liegt vor dem eigenen Abflug. Diese
        # Fremd-Tag-Landung markierte das noch nicht abgeflogene Leg als „flown"
        # → picked=None → „Gelandet in Frankfurt / Feierabend" statt „pre_flight".
        # Physikalisch unmöglich (nie Ankunft vor Abflug) → Obs verwerfen, damit
        # Engine/Buckets/eff_arr nur die eigene iCal-Uhr des Legs sehen.
        if o and _obs_is_wrong_day(leg, o):
            leg['obs'] = None
            o = {}
        b = bucket_of(o.get('status')) if o else None
        dep_delay = _num(o.get('dep_delay_min'))
        arr_delay = _num(o.get('arr_delay_min'))
        if arr_delay is None:
            arr_delay = _num(o.get('delay_min'))
        # ANZEIGE==ENTSCHEIDUNG (Owner 2026-07-13, Tibor „zu früh live vor
        # Abflug"): die absolute revidierte Board-Abflugzeit (est_dep_iso)
        # schlägt sched+dep_delay_min. Vorher blieb eff_dep bei der Soll-Zeit,
        # weil dep_delay_min oft None ist, obwohl das Board eine konkrete
        # esti trägt (z.B. FRA→SFO 08:25 → esti 09:10) → now>Soll → fälschlich
        # 'flying', obwohl der Flieger noch gar nicht los ist. _eff_dep liefert
        # dieselbe est-Zeit wie die Live-Karten-Anzeige (current_leg.est_dep).
        _eff_dep_dt, _eff_dep_delay = _eff_dep(leg, o)
        eff_dep = _eff_dep_dt
        if _eff_dep_delay is not None:
            dep_delay = _eff_dep_delay
        eff_arr = leg['arr'] + _dt.timedelta(minutes=max(0.0, arr_delay or 0.0))
        leg['eff_arr'] = eff_arr

        # ── DIE FLUG-ENTSCHEIDUNG kommt aus der FlightState-Engine ───────────
        # (Integrationsplan Owner 2026-07-13, siehe _engine_leg_flight): die
        # Engine ist die Autorität für die HARTEN Signale — Landung (terminal,
        # monoton), Airborne-Gate („fliegen muss verdient sein"), side-aware
        # Board-Klassifikation (dep-„Abgeflogen" = off-block, NICHT airborne).
        # Reuse der schon geladenen obs (`o`) + Live-Fix (`_live(leg)`) — KEIN
        # neuer I/O. None ⇒ Engine sah kein hartes Flug-Signal → die crew-
        # eigene grounded-/Uhr-/Live-Kaskade unten entscheidet (unverändert).
        # Airport-Koordinaten (+ dep-Elevation) an die Engine reichen. OHNE sie
        # greift der Arrival-Physik-Boden (flight_state §1.5) NICHT — eine stale
        # Vortags-Ankunft „Arrived" landet dann einen noch fliegenden Flieger und
        # die Crew steht fälschlich am Ziel-Pin (Owner 2026-07-13, LH454→SFO:
        # „sehe Tibor immer noch in SFO"). flights_live/flight-live reichten die
        # Koordinaten schon durch — crew_state tat es nicht. Best-effort, wirft nie.
        _dep_ll = _arr_ll = _dep_elev = None
        try:
            from blueprints.aerox_data_blueprint import _iata_latlon as _clL
            _dep_ll = _clL(leg.get('dep_ap'))
            _arr_ll = _clL(leg.get('arr_ap'))
        except Exception:
            pass
        try:
            from blueprints.aerox_data_blueprint import _iata_elev_ft as _clE
            _dep_elev = _clE(leg.get('dep_ap'))
        except Exception:
            pass
        _eng = _engine_leg_flight(
            leg, o, _live(leg), now, dep_ll=_dep_ll, arr_ll=_arr_ll,
            dep_elev_ft=_dep_elev, prior=_eng_prior.get(leg.get('flight')),
            bucket_of=bucket_of)
        eng_kind = eng_phase = None
        eng_live = None
        if _eng:
            eng_kind, eng_phase, _eng_conf, eng_live = _eng
            if leg.get('flight') and eng_phase:
                _eng_prior[leg['flight']] = {
                    'phase': eng_phase, 'conf': _eng_conf,
                    'source': 'crew_leg', 'obs_ts': None,
                    'sticky_airborne': eng_phase in (_ENG_AIRBORNE,
                                                     _ENG_APPROACH)}
        # Engine liefert die gerenderte Position mit (airborne-Gate bestanden);
        # der 'flying'-Zweig unten nutzt sie, damit die crew-Live-Karte dieselbe
        # gegatete Position zeigt wie die Engine (kein Geister-Dot).
        leg['_eng_live'] = eng_live

        # WIDERSPRUCH-GATE (Owner 2026-07-13, Tibor): ein „Abgeflogen"/airborne-/
        # off-block-Signal, das der EIGENEN (delay-korrigierten) est-Abflugzeit
        # widerspricht — now < eff_dep, laut Board geht der Flug erst später —
        # ist stale/inkonsistent (Board trug „Abgeflogen" aus einem alten Stand
        # UND einen frischen +175-min-esti). NICHT als Abflug-Beweis werten,
        # sonst „fliegt gerade" obwohl er noch nicht los ist. Die Engine kennt
        # diesen crew-Zeit-Widerspruch nicht (sie sieht dep-Abgeflogen als
        # TAXI_OUT, würde die Leg-Auswahl aber trotzdem „departed" nennen) →
        # der Gate bleibt hier als crew-Leg-Auswahl-Regel. landed/grounded
        # bleiben unberührt (nur der Abflug-Beweis wird entwertet).
        if eng_kind in ('flying', 'departed') and now < eff_dep:
            eng_kind = None
            b = None
        else:
            b = eng_kind      # ab hier steuert die Engine-Entscheidung die Zweige

        if o.get('cancelled') or eng_kind == 'cancelled':
            # Annulliert schlägt alles: Crew ist nie losgeflogen.
            picked = ('cancelled', idx, CONF_OBSERVED)
            break
        if b == 'flown':
            # Engine LANDED/ARRIVED (arr-seitiges Hard-Landing, MONOTON — der
            # Sebastian-Fall): Leg beendet → nächsten prüfen. Eine stale eff_arr-
            # Schätzung kann das nicht „un-landen".
            leg['flown'] = True
            last_flown_observed = True
            continue
        if b in ('flying', 'departed'):
            # Engine AIRBORNE/APPROACH (bewiesenes Fliegen) ODER TAXI_OUT
            # (dep-seitiges „Abgeflogen"/off-block, Abflug bewiesen). Beides
            # beweist den ABFLUG — nicht ewiges Fliegen. Outstations ohne
            # Ankunfts-Board (ARN!) melden nie 'landed' → ohne Zeit-Deckel klebte
            # der Status auf diesem Leg, obwohl die Maschine laengst den
            # NAECHSTEN Sektor fliegt (Tibor LH802 „Ankunft 12:30" um 13:39).
            # Ab Plan-Ankunft+Puffer gilt der Leg als geflogen und der naechste
            # wird geprueft. Deckel = spaeteste plausible Ankunft: eff_arr ODER
            # eff_dep + Plan-Flugzeit (arr - dep), je nachdem was spaeter ist
            # (dep-seitige „Abgeflogen"-Rows tragen oft nur dep_delay — eff_arr
            # allein unterschaetzt dann die echte Ankunft um die Start-Verspaetung).
            cap_arr = max(eff_arr, eff_dep + (leg['arr'] - leg['dep']))
            if now >= cap_arr + _dt.timedelta(minutes=_STALE_GROUNDED_MIN):
                leg['flown'] = True
                last_flown_observed = True
                continue
            if b == 'departed':
                # TAXI_OUT (kind='departed'): Board „Abgeflogen"=off-block, aber
                # die EINE Engine hat es NICHT auf AIRBORNE gehoben — d.h. entweder
                # frisch raus (< plausible Taxi-Zeit) ODER eine Live-Position zeigt
                # den Flieger sichtbar am Boden. In BEIDEN Fällen rollt die
                # Maschine zur Startbahn, ist NICHT airborne (kein „Fliegt gerade",
                # keine Live-Position). crew_state SPIEGELT die Engine-Phase 1:1 —
                # der frühere 25-min-crew-Deckel ist RAUS, die Grenze „off-block zu
                # lange ⇒ airborne" lebt jetzt AUSSCHLIESSLICH in der Engine
                # (flight_state TAXI_OUT_MAX_S, Zeit-Evidenz = estimated, keine
                # erfundene Position). So zeigen flights_live/crew/family/my-status
                # für denselben Flug DIESELBE Phase. Ein langes off-block liefert
                # die Engine schon als kind='flying' (AIRBORNE/estimated) → dieser
                # Zweig wird dann gar nicht erreicht.
                picked = ('taxiing', idx, CONF_OBSERVED)
                break
            picked = ('flying', idx, CONF_OBSERVED)
            break
        # Engine sah kein hartes Flug-Signal (BOARDING/SCHEDULED/UNKNOWN). Der
        # crew-Bucket unterscheidet jetzt „grounded" (Board sagt aktiv „noch
        # nicht abgeflogen": boarding/delayed/on-time/gate/closed) von „kein
        # Board-Signal".
        _b_raw = bucket_of(o.get('status')) if o else None
        # BOARD-BELEGT „nicht abgeflogen": sagt das Board Gate-Aktivität (gate/
        # closed/boarding) ODER die Engine SCHEDULED/BOARDING observed, ist der
        # Flug nachweislich NOCH NICHT los → wie 'grounded' behandeln, damit die
        # reine Plan-Uhr NICHT auf „fliegt" kippt (Owner 2026-07-13, Sebastian
        # LH862 Gate „closed": das Board weiß es besser als das Plan-Fenster).
        # Pure-Plan-SCHEDULED (kein solcher Board-String, conf=estimated) bleibt
        # der Uhr überlassen — sonst würden Flüge ohne jede Board-Abdeckung im
        # Plan-Fenster fälschlich „warten".
        _st_low = str(o.get('status') or '').lower()
        if (_b_raw != 'grounded'
                and eng_phase in (_ENG_SCHEDULED, _ENG_BOARDING)
                and ('clos' in _st_low or 'geschloss' in _st_low
                     or 'gate' in _st_low
                     or 'boarding' in _st_low or _eng_conf == 'observed')):
            _b_raw = 'grounded'
        if _b_raw == 'grounded':
            # ZEIT-PHYSIK-UMSCHALTER bei arr-seitiger Beobachtung (Julien/Tibor
            # 2026-07-16, Über-Ozean-Nachtflug): der gemergte `status` ist „arr
            # gewinnt" → ein Flug OHNE Abflug-Board (BOS/SFO) trägt die FRA-
            # ANKUNFTS-Tafel „Verspätet" und wurde damit als „grounded" gelesen
            # und ewig auf „Nächster Flug" gepinnt, obwohl er seit ~2 h fliegt.
            # Ein rein arr-seitiges „Verspätet" ist KEIN Boden-Beweis am Abflug
            # (_dep_ground_proof). Steht der Flug nach dem EFFEKTIVEN Abflug +
            # Gnadenspanne immer noch nur wegen einer solchen arr-Obs auf grounded,
            # übernimmt die Uhr: er ist „unterwegs" (der Uhr-Zweig unten kippt auf
            # flying — CONF_PLAN, keine erfundene Position; aircraft_live liefert
            # die echte Atlantik-Position sobald die Kachel die Maschine sieht).
            # Ein echter Abflug-Boden-Beweis (dep-Board Boarding/Gate/Delay) oder
            # ein annullierter Flug (oben schon gefangen) behält den Pin.
            _proof = _dep_ground_proof_kind(o, bucket_of)
            if _proof == 'delay':
                # ZEITBEGRENZTER Beweis (Julien LH423 2026-07-16): BOS postete
                # „Delayed 75 Minutes" und danach NIE „Departed". Ein dep-
                # gemeldeter Delay hält den Pin nur bis zur ANGEKÜNDIGTEN
                # Abflugzeit + Karenz — stünde die Maschine danach noch, meldete
                # das Board einen GRÖSSEREN Delay. Eingefrorener Text ⇒ sie fliegt.
                #
                # DOPPELZÄHL-FIX (Museum 2026-07-16): der angekündigte Abflug ist
                # `max(eff_dep, sched_dep + ann)` — NICHT `eff_dep + ann`. Ist der
                # Delay bereits in eff_dep materialisiert (dep_delay_min gesetzt →
                # eff_dep = sched + 75), zählte `eff_dep + ann` die 75 min DOPPELT
                # (Flip 75 min zu spät). Kommt der Delay dagegen NUR aus dem Text
                # (eff_dep = bare sched), liefert `sched + ann` denselben Zeitpunkt.
                # max() deckt beide Fälle byte-gleich ab.
                _ann = _announced_dep_delay_min(o) or 0
                _announced_dep = max(eff_dep, leg['dep'] + _dt.timedelta(minutes=_ann))
                if now >= _announced_dep + _dt.timedelta(
                        minutes=_DEP_LIFTOFF_GRACE_MIN):
                    _proof = None
            if (_proof is None
                    and now >= eff_dep + _dt.timedelta(
                        minutes=_DEP_LIFTOFF_GRACE_MIN)):
                # Live-Beweis darf trotzdem in BEIDE Richtungen kippen: sichtbar
                # am Boden nahe dep → wartet doch; nahe arr → schon gelandet.
                lv = _live(leg)
                if lv and lv.get('on_ground') and lv.get('near_dep'):
                    picked = ('waiting', idx, CONF_OBSERVED)
                    break
                if lv and lv.get('on_ground') and lv.get('near_arr'):
                    leg['flown'] = True
                    last_flown_observed = True
                    continue
                if lv and not lv.get('on_ground'):
                    picked = ('flying', idx, CONF_OBSERVED)
                    break
                # Nach Plan-Ankunft+Puffer: Leg gilt als geflogen (nächsten prüfen)
                # — sonst klebte „unterwegs" ewig auf einem Outstation-Leg ohne
                # Ankunfts-Board (gleiche Deckel-Logik wie der flying/departed-Zweig).
                if now >= eff_arr + _dt.timedelta(minutes=_STALE_GROUNDED_MIN):
                    leg['flown'] = True
                    last_flown_observed = False
                    continue
                picked = ('flying', idx, CONF_PLAN)
                break
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
            # ÜBERFÄLLIGKEITS-DECKEL (LH1126-Scraper-Müll, Museum 2026-07-16): ein
            # 'Boarding'/grounded-Board-Status auf einem längst vergangenen Leg
            # (Plan-Ankunft > _STALE_GROUNDED_LANDED_MIN vorbei) ist Scraper-Müll —
            # kein Ewig-Pin auf pre_flight. Der Owner-Grundsatz „Board schlägt Uhr"
            # gilt für HEUTIGE Legs; ist die Plan-Ankunft um Stunden überschritten,
            # ist die Maschine physisch längst gelandet → als geflogen behandeln,
            # ohne aircraft_live-Beweis (der bei alten Legs eh nicht mehr da ist).
            # Klein genug (6 h), dass ein echt am Gate stehender heutiger Flug mit
            # aktivem Boarding-Board (Basti/Sebastian) nie fälschlich „landet".
            if now >= eff_arr + _dt.timedelta(minutes=_STALE_GROUNDED_LANDED_MIN):
                leg['flown'] = True
                # Scraper-Müll-Grounded: wir haben KEIN echtes Landungs-Signal
                # (das Board log ja falsch) — die einzige belastbare Wahrheit ist
                # „das Leg ist physisch durch". Anders als der airborne-Stale-Pfad
                # (Flug ist weitergezogen → Layover) präsentieren wir das letzte
                # bekannte Leg als frisch gelandet: STATE_LANDED unabhängig von der
                # Recency (der Board-Müll erlaubt keine bessere Aussage).
                leg['muell_landed'] = True
                last_flown_observed = False
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
        # FRISCHE-GATE (Jennifer JFK→FRA 2026-07-21): „nachweislich" verlangt
        # einen FRISCHEN Fix. Vorher pinnte auch ein 7,5 h alter Ozean-Fix
        # (letzter Kontakt vorm Funkloch, on_ground=false) den State ewig auf
        # „fliegt", obwohl die Soll-Ankunft längst vorbei war — der Store
        # liefert Last-Known-Positionen mit ECHTEM altem seen_ts absichtlich
        # weiter. Nach window_end gilt: Position älter als 45 min ⇒ kein
        # Beweis ⇒ Landung annehmen (Plan-Wahrheit).
        lv = _live(leg)
        if lv:
            # Unbekanntes Alter (kein ts / unparsebar) zählt als frisch —
            # der Prod-Store liefert IMMER seen_ts; das hält den Vertrag
            # „airborne schlägt die Uhr" für ts-lose Test-/Alt-Quellen.
            _fresh = True
            _ts = lv.get('ts')
            if _ts is not None:
                _ep = None
                try:
                    _ep = float(_ts)
                except (TypeError, ValueError):
                    try:
                        _d = _dt.datetime.fromisoformat(
                            str(_ts).replace('Z', '+00:00'))
                        if _d.tzinfo is None:
                            _d = _d.replace(tzinfo=_dt.timezone.utc)
                        _ep = _d.timestamp()
                    except Exception:
                        _ep = None
                if _ep is not None:
                    _fresh = (now.timestamp() - _ep) <= 45 * 60
            if not lv.get('on_ground') and _fresh:
                picked = ('flying', idx, CONF_OBSERVED)
                break
            if lv.get('near_dep') and _fresh:
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
        # Scraper-Müll-Grounded-Deckel (LH1126): kein echtes Landungs-Signal, das
        # Leg wird als frisch gelandet gezeigt (nicht Layover) — s.o. muell_landed.
        recent = (now - eff_arr) <= _dt.timedelta(minutes=_LANDED_RECENT_MIN) \
            or bool(leg.get('muell_landed'))
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
        # Ankunftszeit im Text = die EFFEKTIVE Ankunft wie der Radar sie zeigt:
        # absolute Warehouse-esti schlägt sched+delay (Owner 2026-07-13 „Live-
        # Karte 8:40, Radar paar Min später"). Fehlt beides → Plan-Ankunft.
        eff_arr, _ = _eff_arr(leg, o)
        if eff_arr is None:
            eff_arr = leg['arr']
        route = f"{leg['dep_ap']} → {leg['arr_ap'] or '?'}"
        sub = route
        if not leg['arr_synth']:
            t = hhmm(eff_arr, leg['arr_ap'] or leg['dep_ap'])
            if t:
                sub = f'{route} · Ankunft {t}'
        return _result(STATE_FLYING, leg=leg, idx=idx,
                       position=_position(leg), title='Fliegt gerade',
                       subtitle=sub, confidence=conf)

    if kind == 'taxiing':
        # TAXI_OUT: off-block, rollt zur Startbahn — ehrlich, kein „Fliegt gerade",
        # keine Live-Position (state=pre_flight → keine Live-Flieger-Karte).
        route = f"{leg['dep_ap']} → {leg['arr_ap'] or '?'}"
        return _result(STATE_PRE_FLIGHT, leg=leg, idx=idx,
                       title='Startet gerade',
                       subtitle=f'{route} · Rollt zum Start', confidence=conf)

    if kind == 'cancelled':
        here = leg['dep_ap']
        return _result(STATE_PRE_FLIGHT, leg=leg, idx=idx,
                       title=f"{leg['flight'] or 'Flug'} annulliert",
                       subtitle=f'In {city(here)}', confidence=CONF_OBSERVED)

    # kind == 'waiting': vor Leg idx — physisch am Abflughafen dieses Legs.
    o = leg.get('obs') or {}
    # eff_dep = die EFFEKTIVE Abflugzeit wie der Radar sie zeigt: absolute
    # Warehouse-esti schlägt sched+dep_delay (EINE Wahrheit, Owner 2026-07-13).
    # dep_eff_min ist der abgeleitete Delay (aus dem absoluten esti ODER dem
    # expliziten dep_delay_min) — kein Rückfall auf max(0,…), damit ein echter
    # Delay auch aus einem reinen esti sichtbar wird.
    eff_dep, dep_eff_min = _eff_dep(leg, o)
    if eff_dep < leg['dep']:
        eff_dep = leg['dep']          # nie VOR den Plan schieben (nur später)
    t = hhmm(eff_dep, leg['dep_ap'])
    # Delay ist BEKANNT nur, wenn er den Abflug wirklich nach hinten schiebt
    # (est_dep > sched_dep). Ein 0-/Negativ-Delay ist „pünktlich", kein Grund
    # für den „Verspätet"-Text (Owner 2026-07-13, Basti-Fall).
    delay_known = dep_eff_min is not None and dep_eff_min > 0
    # WORDING (Owner 2026-07-14): „Wartet auf …" klang, als stünde der Abflug
    # unmittelbar bevor — „Nächster Flug · …" ist neutraler für einen Flug, der
    # erst Stunden später geht. Wire-Contract: friends_today_golden.json +
    # test_crew_live_state/-contract + iOS-Fixtures wurden synchron nachgezogen.
    wait_txt = (f"Nächster Flug · {leg['flight']} · {t}" if leg['flight'] and t
                else (f'Nächster Flug · {t}' if t else 'Nächster Flug'))
    # PRE-FLIGHT-TIMELINE (Owner 2026-07-12): feingranulare Phase aus reinen
    # Zeitvergleichen (now vs. berechnete Marken) + Boarding-Beobachtung.
    # delay_known kappt/ersetzt die hängende „Flugvorbereitung" durch den
    # ehrlichen „Verspätet"-Status (Owner 2026-07-13, Basti-Fall).
    pre = _resolve_pre_phase(leg, now, eff_dep, pre_ctx, hb,
                             first_leg=(idx == 0), delay_known=delay_known)
    if idx == 0:
        route = f"{leg['dep_ap']} → {leg['arr_ap'] or '?'}"
        # Fertiger Text in der Subtitle (Owner-Spez): alte Builds zeigen die
        # angereicherte Prosa, neue lesen zusätzlich pre_phase(_label).
        # „Verspätet" trägt zusätzlich die NEUE (verspätete) Abflugzeit im Text
        # („FRA → LHR · Verspätet 08:20") — der Owner-Wunsch „Status verspätet
        # und neuer Abflug" (die verspätete Zeit steht auch im Titel via t).
        if pre == PRE_DELAYED and t:
            sub = f'{route} · Verspätet {t}'
        elif pre:
            sub = f'{route} · {PRE_PHASE_LABEL[pre]}'
        else:
            sub = route
        return _result(STATE_PRE_FLIGHT, leg=leg, idx=idx, title=wait_txt,
                       subtitle=sub, confidence=conf, pre_phase=pre)
    # Zwischen zwei Legs: gelandet am Abflughafen des kommenden Legs.
    # pre ist hier höchstens prep/boarding/delayed (Turnaround am Flugzeug);
    # wait_txt trägt bereits die (ggf. verspätete) Abflugzeit (eff_dep).
    return _result(STATE_LANDED, leg=leg, idx=idx,
                   title=f"Gelandet in {city(leg['dep_ap'])}",
                   subtitle=wait_txt, confidence=conf, pre_phase=pre)


# ── Über-Mitternacht-Spillover (Jennifer-Fall, Owner 2026-07-12) ─────────────

def yesterday_leg_reaches_into_today(sectors, now,
                                     extra_min=_ARR_BUFFER_MIN + _LANDED_RECENT_MIN):
    """PURES Vorab-Gate: erreicht ein GESTRIGER Leg (dep gestern, arr am
    Folgetag — Über-Nacht-Rückflug wie SIN→FRA dep 23:40 LT) den heutigen
    Betriebstag? friends-today resolved den crew_state sonst NUR aus dem
    heutigen Roster-Tag → nach Berliner Mitternacht war die noch FLIEGENDE
    Crew plötzlich „Basis Frankfurt"/falscher Ort (Jennifer 2026-07-12/13:
    dep 12.07 15:40Z SIN→FRA, arr 13.07 — ab 00:00 Berlin zeigte der Feed
    das falsche Leg statt „Fliegt gerade SIN → FRA").

    True, wenn irgendein Leg der (gestrigen) Sektoren bereits abgeflogen ist
    (dep ≤ now) und sein Fenster inkl. Verspätungs-Puffer + „frisch
    gelandet"-Fenster noch bis `now` reicht — nur dann lohnt der (teurere)
    volle Resolver-Lauf über die gestrigen Sektoren. Wirft nie."""
    try:
        now = _parse_iso(now)
        if now is None:
            return False
        for leg in _norm_legs(sectors):
            if leg['dep'] <= now < leg['arr'] + _dt.timedelta(minutes=extra_min):
                return True
        return False
    except Exception:
        return False


# ── Über-Mitternacht-CARRY (Julien/Tibor 2026-07-16) ─────────────────────────
# Grenzen des CARRY-Fensters. Ein gestriger Leg wird in den heutigen Resolver-Lauf
# GEREICHT (nicht als separater Sieger-Vergleich wie spillover_wins), solange er
# physikalisch noch „aktiv" sein kann:
#   • arr in der ZUKUNFT (Nacht-Rückflug noch unterwegs), ODER
#   • arr < _CARRY_ARR_PAST_MIN vorbei (frisch gelandet — konsistent mit den
#     bestehenden Post-Landing-Fenstern _LANDED_RECENT_MIN/_STALE_GROUNDED_MIN).
# UND der Abflug muss schon durch sein (dep ≤ now) — ein Leg, dessen Abflug erst
# in der Zukunft liegt, gehört NICHT in den Rückblick (das ist das MORGEN-
# crew_state_next-Fenster). Alles auf UTC-Instants (nie Datums-Strings).
_CARRY_ARR_PAST_MIN = 3 * 60      # ≈3 h Post-Landing-Gnade (Owner: X≈3)


def carry_over_legs(prev_sectors, now, arr_past_min=_CARRY_ARR_PAST_MIN):
    """PUR: die noch aktiven Legs der VORTAGS-Sektoren als Sektor-Dicts, die in
    den HEUTIGEN Resolver-Lauf gereicht werden können (Julien 2026-07-16: LH423
    BOS→FRA dep 15.07 22:15Z / arr 16.07 06:00Z keyt auf dem 15. — nach Berliner
    Mitternacht ist „heute" der 16., der Resolver fand am 16. kein Leg und fiel
    auf `home`/„Basis Frankfurt" zurück, obwohl die Crew nachweislich noch flog).
    Statt den heutigen Zustand extern zu ÜBERSTIMMEN (spillover_wins) reichen wir
    das gestrige Leg VOR den heutigen Sektoren in denselben Resolver — die
    zeitbasierte Leg-Auswahl + Engine + Wrong-Day-Guards laufen dann ganz normal
    darüber (enroute/landed/pre_flight je nach Obs/Zeit-Physik).

    Übernommen wird jeder Leg mit dep ≤ now (schon abgeflogen) UND arr in der
    Zukunft ODER < arr_past_min vorbei. Alle Vergleiche auf UTC-Instants der
    ISO-Werte (_norm_legs → aware-UTC), nie auf Datums-Strings. Ein synthetisch
    ersetztes arr (Red-Eye ohne end_iso) wird konservativ mitgezählt. Gibt die
    ORIGINAL-Sektor-Dicts zurück (inkl. tail/reg fürs current_leg), damit der
    Resolver dieselben Felder sieht wie bei den heutigen Sektoren. Wirft nie."""
    try:
        nw = _parse_iso(now)
        if nw is None or not prev_sectors:
            return []
        floor = nw - _dt.timedelta(minutes=max(0, arr_past_min))
        out = []
        for s in prev_sectors:
            if not isinstance(s, dict):
                continue
            legs = _norm_legs([s])
            if not legs:
                continue
            leg = legs[0]
            if leg['dep'] <= nw and leg['arr'] >= floor:
                out.append(s)
        return out
    except Exception:
        return []


def _plan_only_future_pre_flight(today_state, now):
    """PUR: ist der heutige Zustand ein REINER Plan-pre_flight, dessen Abflug
    noch in der Zukunft liegt? Das ist KEIN aktiver Beweis-Zustand — nur die
    Plan-Uhr des heutigen Roster-Tags (CONF_PLAN, kein Board-/Live-Signal).
    Nacht-Turnaround (Regressions-Sweep 2026-07-12 #6): dep gestern 22:00 →
    arr heute 00:30, Rückleg heute 01:15 — zwischen Berliner Mitternacht und
    Landung resolved der heutige Tag pre_flight fürs Rückleg („Wartet auf
    LHxxx · 01:15" + checkin_open), während die Crew laut gestrigen Sektoren
    nachweislich noch FLIEGT. Wirft nie."""
    try:
        if not isinstance(today_state, dict):
            return False
        if today_state.get('state') != STATE_PRE_FLIGHT:
            return False
        if today_state.get('confidence') != CONF_PLAN:
            return False
        dep = _parse_iso((today_state.get('current_leg') or {}).get('dep_iso'))
        now = _parse_iso(now)
        return bool(dep is not None and now is not None and dep > now)
    except Exception:
        return False


def today_blocks_spillover(today_state, now=None):
    """PUR: blockiert der HEUTIGE Zustand den Über-Nacht-Rückblick komplett?
    Aktive Zustände (pre_flight/flying/landed) blockieren — AUSSER der
    pre_flight ist reine Plan-Uhr mit Abflug in der Zukunft (s.
    _plan_only_future_pre_flight): dann darf der Rückblick LAUFEN und
    spillover_wins entscheidet. EINE geteilte Prüfung für das app.py-Vorab-
    Gate UND spillover_wins, damit Gate und Gewinner-Regel nie divergieren
    (Regressions-Sweep 2026-07-12 #6)."""
    t = ((today_state or {}).get('state') if isinstance(today_state, dict)
         else today_state)
    if t not in (STATE_PRE_FLIGHT, STATE_FLYING, STATE_LANDED):
        return False
    return not _plan_only_future_pre_flight(today_state, now)


def spillover_wins(today_state, yesterday_state, now=None,
                   extra_min=_ARR_BUFFER_MIN + _LANDED_RECENT_MIN):
    """PUR: darf der GESTRIGE Über-Nacht-Zustand den heutigen ersetzen?
    Grundregel: nur wenn heute nichts AKTIVES läuft (kein pre_flight/flying/
    landed des heutigen Tages) UND gestern nachweislich noch geflogen/frisch
    gelandet wird. So gewinnt nie ein staler Gestern-Layover über einen
    echten Heute-Zustand.

    ZWEI Verfeinerungen (Regressions-Sweep 2026-07-12 #6, `now` optional —
    ohne now exakt das alte Verhalten):
      A) Nacht-Turnaround: ein heutiger PLAN-ONLY-pre_flight mit Abflug in
         der ZUKUNFT ist kein Beweis-Zustand — ein gestriges FLYING gewinnt
         (nur flying: nach der Landung ist „Wartet auf …" des Rücklegs der
         bessere Text, gestriges landed übernimmt dann nicht).
      B) Verspäteter Über-Nacht-Abflug: gestriges pre_flight mit
         BEOBACHTETEM Pin (Board grounded/delay/cancelled → CONF_OBSERVED)
         gewinnt über einen leg-losen Heute-Tag („Basis Frankfurt"), solange
         das gestrige Leg-Fenster (Plan-dep ≤ now < arr + Puffer) noch bis
         heute reicht — die Crew wartet real am Outstation-Gate."""
    t = ((today_state or {}).get('state') if isinstance(today_state, dict)
         else today_state)
    y = ((yesterday_state or {}).get('state') if isinstance(yesterday_state, dict)
         else yesterday_state)
    if t in (STATE_PRE_FLIGHT, STATE_FLYING, STATE_LANDED):
        if not _plan_only_future_pre_flight(today_state, now):
            return False
        return y == STATE_FLYING                       # Verfeinerung A
    if y in (STATE_FLYING, STATE_LANDED):
        return True
    # Verfeinerung B: gestern beobachtet am Boden gepinnt (verspäteter/
    # annullierter Über-Nacht-Abflug), heute leglos → gestern gewinnt,
    # solange das Leg-Fenster noch bis heute reicht.
    if y == STATE_PRE_FLIGHT and isinstance(yesterday_state, dict) \
            and yesterday_state.get('confidence') == CONF_OBSERVED:
        try:
            nw = _parse_iso(now)
            leg = yesterday_state.get('current_leg') or {}
            dep = _parse_iso(leg.get('dep_iso'))
            if nw is None or dep is None or dep > nw:
                return False
            end = (_parse_iso(leg.get('est_arr_iso'))
                   or _parse_iso(leg.get('arr_iso'))
                   or dep + _dt.timedelta(hours=3))     # wie _norm_legs-Synth
            return nw < end + _dt.timedelta(minutes=extra_min)
        except Exception:
            return False
    return False


# ── duty-Ableitung für Leg-lose Tage (B2-Nachfix, Regressions-Sweep #7) ──────

def duty_from_roster_day(klass=None, marker=None):
    """PUR: duty ('standby'|'vacation'|'visa'|'free'|None) aus klass/marker des
    Roster-Tages ableiten — Standby > Urlaub > Visum > Frei (B2 Tibor 2026-07-12).

    GETEILT zwischen den beiden Resolver-Consumers (app._crew_state_for_day
    für friends-today UND family_watch._load_crew_status_for_family): der
    B2-Fix war zunächst NUR in friends-today verdrahtet — Family zeigte für
    DIESELBE Person am selben Tag weiter „Basis Frankfurt", während der
    Crew-Feed „Heute frei"/„Im Urlaub" sagte (Regressions-Sweep 2026-07-12
    #7, exakt die Bug-Klasse, die B2 fixen sollte). EINE Funktion, damit die
    Ableitung nicht erneut divergiert.

    marker deckt auch reine iCal-Summaries ab ('OFF DAY …', 'SBY …',
    '… URLAUB …') — family_watch liest user_ical_briefings und hat KEIN
    klass-Feld. Wirft nie.

    'visa' (2026-07-17): ein Visum-/Visa-Termin ist ein administrativer NICHT-
    Dienst-Tag — keine Anfahrt zur Homebase, kein Pickup/Commute. Vorher fehlte
    der Marker → der Tag fiel auf None und wurde wie ein (Office-)Dienst-Tag
    behandelt (Smart-Pickup ausgelöst). Jetzt eigene Nicht-Commute-Kategorie."""
    marker_up = str(marker or '').upper()
    klass_up = str(klass or '').strip().upper()
    if 'SBY' in marker_up:
        return 'standby'
    if klass_up in ('URLAUB', 'VAC', 'VACATION') or 'URLAUB' in marker_up:
        return 'vacation'
    # Visum/Visa: Behörden-/Botschaftstermin, KEIN Dienst und KEIN Airport-Weg.
    # Robust gegen Case/Substring wie die übrigen Marker ('VISUM'/'VISA' im
    # Summary oder als klass). Vor FREI/OFF geprüft, damit ein „Visum" nie als
    # freier Tag mit „Heute frei"-Text durchrutscht.
    if klass_up in ('VISUM', 'VISA') or 'VISUM' in marker_up or 'VISA' in marker_up:
        return 'visa'
    if klass_up in ('FREI', 'OFF', 'X', 'REST') or 'OFF DAY' in marker_up:
        return 'free'
    return None


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
            # live=False (Owner 2026-07-13, gemessen): der Feed-Fan-out darf
            # NIE eine Flughafen-Tafel LIVE scrapen (_lhr_board ~6s, FRA ~3s pro
            # friends-today-Aufruf) — die Board-Obs kommen aus dem Warehouse
            # (Harvester hält FRA/LHR/… frisch). Nur so bleibt friends-today
            # schnell; Paid war via free_only eh schon aus.
            return resolver(flight_no, date=datum, dep_iata=dep_iata,
                            arr_iata=(arr_iata or None),
                            free_only=True, live=False)
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
    (dest == arr des Legs), on_ground-Nähe zu dep/arr via Referenz-DB. Wirft nie.
    Signatur (flight_no, dep_iata, arr_iata, reg=None): der optionale Roster-Tail
    (reg) speist den Reg-Match des Stores (Julien/Tibor 2026-07-16), der die
    Maschine auch über die bindestrich-lose Store-Reg findet, wenn der Harvester
    den Korridor noch nicht per Flugnummer erfasst hat. Alt-Callsites/Tests dürfen
    3-argig aufrufen (der Resolver fängt den TypeError)."""
    def _lookup(flight_no, dep_iata, arr_iata, reg=None):
        try:
            from blueprints.aerox_data_blueprint import (_aircraft_live_pos,
                                                         _free_crew_live_pos,
                                                         _iata_latlon)
        except Exception:
            return None
        try:
            # REG mitgeben (Julien/Tibor 2026-07-16): _aircraft_live_pos matcht
            # Flugnummer ZUERST (fängt Aircraft-Swaps), fällt aber auf die Reg
            # zurück, wenn der Welt-Harvester den Korridor per Flugnummer noch
            # nicht erfasst hat. Der Store normalisiert 'D-ABYN' → 'DABYN' intern
            # (re.sub[^A-Z0-9]) und findet so die bindestrich-lose Store-Row.
            pos, _rt, live_reg, _ty = _aircraft_live_pos(reg=reg, flight=flight_no,
                                                         dep=arr_iata)
            reg = live_reg or reg
            if not pos:
                # Der Welt-Harvester ist Round-robin und kann genau diesen
                # Korridor kurz verpasst haben. Ein gezielter, kurz memoiserter
                # GRATIS-gRPC-Fill sucht den Leg über Flug+Route; bei Ausfall
                # liefert er höchstens den echten, zeitgestempelten LKG-Fix.
                pos, _rt, _free_reg, _ty = _free_crew_live_pos(
                    flight_no, dep_iata, arr_iata)
                reg = _free_reg or reg
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
