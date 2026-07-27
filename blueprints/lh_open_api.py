"""Lufthansa Open API — Flight-Facts-Enrichment (Engine A, 2026-07-21).

Autoritative, KOSTENLOSE Flug-Fakten aus erster Hand der operierenden Airline
für die Lufthansa-Group-Carrier, die AeroX-Crews fliegen (LH/LX/OS/SN/EW/4Y/
EN/CL/WK). Liefert Soll-/Ist-Zeiten (lokal+UTC → exakter Offset), Gate,
Terminal, Delay, Status, Flugzeugtyp UND Registration.

Architektur (Owner „Master-Plan", free-first):
- Client-Credentials-Token (auto-refresh, thread-safe).
- Pro-(Flug,Datum,Route)-Cache — geteilt über ALLE Crew + Familien auf dem Flug
  (ein Discover-FRA-MBA = EIN Call für alle). Kurze TTL für heute (Ist-Zeiten
  frisch), lange für Vergangenheit/Zukunft (Plan ändert sich kaum).
- 5/sec-Throttle + Stunden-Budget-Wächter (Public-Plan 1.000/h) — Überschuss
  wird NICHT still verschluckt, sondern geloggt und übersprungen (Fallback auf
  die bestehenden Board/FR24-Tiers).
- Voll no-op wenn nicht konfiguriert (Env fehlt) → Deploy/Commit ist immer
  sicher; erst das Setzen der Env-Vars aktiviert die Anreicherung.

Secret NUR aus Env (LH_OPEN_API_KEY/SECRET, Fallback LH_KEY/SECRET) — nie im
Code/Log. Rückgabe von `lh_flight_facts` ist byte-shape-kompatibel mit
`_obs_rows_to_facts` (aerox_data_blueprint), damit der Merge trivial bleibt.
"""
import os
import re
import time
import json
import threading
import urllib.request
import urllib.parse
import urllib.error
import logging

from flask import Blueprint, jsonify

log = logging.getLogger('aerotax')
lh_open_bp = Blueprint('lh_open_bp', __name__)

_BASE = 'https://api.lufthansa.com/v1'
_KEY = (os.environ.get('LH_OPEN_API_KEY') or os.environ.get('LH_KEY') or '').strip()
_SECRET = (os.environ.get('LH_OPEN_API_SECRET') or os.environ.get('LH_SECRET') or '').strip()

# Lufthansa-Group OPERIERENDE Carrier (die AeroX-Crews fliegen). Verifiziert
# 2026-07-21, dass die Open API LH/LX/4Y liefert; OS/SN/EW/EN/CL/WK sind
# dieselbe Group. AZ (ITA Airways, Group seit 2025) verifiziert 2026-07-22:
# flightstatus liefert volle AZ-Daten inkl. Actuals (AZ1157) und der
# Notification-Broker streamt AZ-Events — Owner-Entscheid „ITA ist auch LH
# Group". Bewusst KEINE Nicht-Group-Carrier — schont das 1.000/h-Budget
# (Roster-Sektoren der Crew sind immer Group-Flüge). VL (Lufthansa City,
# AeroX-Synthetik-Prefix) ist KEIN eigener API-Carrier → ausgelassen.
_LH_GROUP = {'LH', 'LX', 'OS', 'SN', 'EW', '4Y', 'EN', 'CL', 'WK', 'AZ'}

# ── Token-Cache ──────────────────────────────────────────────────────────────
_tok_lock = threading.Lock()
_tok_val = None
_tok_exp = 0.0

# ── 5/sec-Throttle + Stunden-Budget ──────────────────────────────────────────
# ACHTUNG (live gelernt 2026-07-22, „Developer Over Rate" bis auf den OAuth-
# Endpoint): die 1.000/h-Quota gilt PRO KEY, dieser Zähler aber PRO PROZESS —
# und produktiv laufen 3 Backend-Gunicorn-Worker + 1 Poll-Worker (+ Daemon,
# der selbst kaum ruft). 900/h pro Prozess konnte also ~4× überziehen und
# legte den MQTT-Daemon (kein Token mehr) lahm. 220 × 4 Prozesse ≈ 880/h
# Aggregat-Obergrenze mit Marge.
_rate_lock = threading.Lock()
_last_call_ts = 0.0
# QPS-LEHRE (Owner 2026-07-24 „warum immer über dem Limit?"): auch das
# 5/sec-Limit gilt PRO KEY, dieser Throttle aber PRO PROZESS. 0.22s pro
# Prozess erlaubte mit 4 rufenden Prozessen (3 Gunicorn-Worker + Poll)
# ~18 Calls/sec Aggregat-Bursts → „Developer Over Rate"-403-Salven, obwohl
# die Stunden-Quote längst passte (61 Calls/h und trotzdem 40× 403).
# 0.9s ≈ 1,1/sec pro Prozess → ≤ ~4,4/sec Aggregat mit Marge.
_MIN_INTERVAL = 0.9
_HOUR_BUDGET = 220             # PRO PROZESS (s.o.) — Aggregat < 1.000/h
# 403-Cool-down: meldet LH „Over Rate", kurz komplett aussetzen (Fallback-
# Tiers übernehmen), statt weitere Budget-Slots in die 403-Wand zu feuern.
_rate_penalty_until = 0.0
_RATE_PENALTY_SEC = 60.0
_hour_window = 0               # aktuelle Stunde (epoch // 3600)
_hour_count = 0
_budget_warned_hour = -1

# ── Prozess-ÜBERGREIFENDER Stunden-Gate ─────────────────────────────────────
# WARUM (Owner 2026-07-27, drei Stunden über 1.000/h): `_HOUR_BUDGET` oben
# zählt pro Prozess. 3 Backend-Worker + 1 Poll-Worker × 220 = 880/h Decke, und
# weil die Prozesse voneinander nichts wissen, hilft ein Senken des Werts nur
# um den Preis, dass ein einzelner Prozess unnötig früh dichtmacht.
# Gemeinsame Wahrheit ist der Zähler `lhopen:<YYYYMMDDHH>` in ax_api_budget.
# Der Flusher schreibt ihn ohnehin alle 30 s und bekommt vom atomaren RPC den
# neuen GESAMTSTAND zurück — den übernehmen wir hier, ohne einen einzigen
# zusätzlichen Netz-Call.
# 850 statt 1.000: zwischen zwei Flushes liegen bis zu 30 s, in denen alle
# Prozesse zusammen (~4,4 Calls/s, s. _MIN_INTERVAL) noch überziehen können.
_GLOBAL_HOUR_BUDGET = 850
_global_lock = threading.Lock()
_global_hour = 0               # Stunde, auf die sich _global_count bezieht
_global_count = 0              # Gesamtstand ALLER Prozesse beim letzten Flush
_global_local_since = 0        # was DIESER Prozess seither noch drauflegte
_global_warned_hour = -1


def note_global_budget(hour_key, total):
    """Vom Flusher aufgerufen, sobald der atomare RPC den echten Gesamtstand
    für `lhopen:<hour>` geliefert hat. Setzt den lokalen Seit-Flush-Zähler
    zurück — ab hier ist `total` die Wahrheit. Wirft nie."""
    global _global_hour, _global_count, _global_local_since
    try:
        h = int(hour_key)
        with _global_lock:
            if h < _global_hour:
                return                     # veralteter Flush, ignorieren
            if h > _global_hour:
                _global_hour = h
                _global_local_since = 0
            _global_count = max(0, int(total))
            _global_local_since = 0
    except Exception:
        pass


def _global_budget_check(now):
    """False, wenn der GANZE Key sein Stundenkontingent ausgeschöpft hat.
    Schätzung = letzter bekannter Gesamtstand + was dieser Prozess seit dem
    Flush selbst verbraucht hat. Prüft NUR — gebucht wird in
    `_global_budget_commit`, erst wenn der Call auch wirklich rausgeht.
    Fail-OPEN: ohne Supabase-Stand (frischer Prozess, RPC-Fallback) bleibt der
    Prozess-Gate `_HOUR_BUDGET` die Bremse — lieber die alte Decke als
    LH-Enrichment komplett aus."""
    global _global_hour, _global_count, _global_local_since, _global_warned_hour
    hour = int(time.strftime('%Y%m%d%H', time.gmtime(now)))
    with _global_lock:
        if hour != _global_hour:
            _global_hour = hour
            _global_count = 0
            _global_local_since = 0
            return True
        est = _global_count + _global_local_since
        if est >= _GLOBAL_HOUR_BUDGET:
            if _global_warned_hour != hour:
                log.warning('[lh_open] GLOBALES Stunden-Budget %d erreicht '
                            '(geschätzt %d über alle Prozesse) — Fallback-Tiers',
                            _GLOBAL_HOUR_BUDGET, est)
                _global_warned_hour = hour
            return False
        return True


def _global_budget_commit():
    """Einen tatsächlich abgesetzten Call auf den globalen Schätzer buchen."""
    global _global_local_since
    with _global_lock:
        _global_local_since += 1

# ── Fakten-Cache ─────────────────────────────────────────────────────────────
_facts_lock = threading.Lock()
_facts_memo = {}               # key -> (expires_at, facts_dict)
_FACTS_MAX = 4000


def lh_open_configured():
    """True nur wenn Key+Secret via Env gesetzt sind (sonst voll no-op)."""
    return bool(_KEY and _SECRET)


def is_lh_group(flight_no):
    """True wenn die Flugnummer ein LH-Group-Operating-Carrier ist (2-stelliger
    IATA-Prefix, inkl. ziffern-führend wie 4Y). Robuster Prefix-Parse."""
    fn = (flight_no or '').replace(' ', '').upper().strip()
    if len(fn) < 3:
        return False
    # 2-Zeichen-Carrier + Ziffern: 'LH400', '4Y136', 'EW8'.
    pfx = fn[:2]
    return pfx in _LH_GROUP and fn[2:3].isdigit()


_tok_fail_until = 0.0


def _token():
    """Client-Credentials-Access-Token, gecacht bis kurz vor Ablauf. None bei
    Fehler (Aufrufer fällt dann still auf die anderen Tiers zurück).
    NEGATIV-CACHE 90s (Ausfall 22.07. abends: OAuth-403 → JEDER LH-Aufruf
    machte einen eigenen blockierenden Token-Roundtrip, 67 Fails/5 min)."""
    global _tok_val, _tok_exp, _tok_fail_until
    if not lh_open_configured():
        return None
    now = time.time()
    with _tok_lock:
        if _tok_val and now < _tok_exp:
            return _tok_val
        if now < _tok_fail_until:
            return None
    body = urllib.parse.urlencode({
        'client_id': _KEY, 'client_secret': _SECRET,
        'grant_type': 'client_credentials'}).encode()
    req = urllib.request.Request(
        _BASE + '/oauth/token', data=body,
        headers={'Content-Type': 'application/x-www-form-urlencoded',
                 'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read().decode('utf-8'))
        tok = d.get('access_token')
        ttl = int(d.get('expires_in') or 3600)
        if not tok:
            return None
        with _tok_lock:
            _tok_val = tok
            _tok_exp = now + max(60, ttl - 60)
        return tok
    except Exception as e:
        log.warning('[lh_open] token fail: %s', type(e).__name__)
        with _tok_lock:
            _tok_fail_until = time.time() + 90
        return None


def _budget_ok():
    """5/sec-Throttle + Stunden-Budget. False (mit Log) wenn das Stunden-Budget
    erschöpft ist — kein stiller Cap. INCIDENT-LEHRE 2026-07-22 (Backend
    unresponsive, sogar /api/health 30s): der Spacing-Sleep lief UNTERM
    _rate_lock — jeder wartende Worker-Thread hing zusätzlich im Lock des
    Vordermanns. Jetzt: Slot unterm Lock reservieren, schlafen DANACH."""
    global _last_call_ts, _hour_window, _hour_count, _budget_warned_hour
    now = time.time()
    if now < _rate_penalty_until:
        return False               # Over-Rate-Cool-down aktiv → Fallback-Tiers
    if not _global_budget_check(now):
        return False               # ganzer Key ausgeschöpft (alle Prozesse)
    hour = int(now // 3600)
    wait = 0.0
    with _rate_lock:
        if hour != _hour_window:
            _hour_window = hour
            _hour_count = 0
        if _hour_count >= _HOUR_BUDGET:
            if _budget_warned_hour != hour:
                log.warning('[lh_open] Stunden-Budget %d erreicht — überspringe '
                            'LH-Enrichment bis zur nächsten Stunde (Fallback aktiv)',
                            _HOUR_BUDGET)
                _budget_warned_hour = hour
            return False
        wait = _MIN_INTERVAL - (now - _last_call_ts)
        _last_call_ts = now + max(0.0, wait)   # Slot reservieren
        _hour_count += 1
    _global_budget_commit()        # erst buchen, wenn der Slot wirklich steht
    if wait > 0:
        time.sleep(wait)
    return True


# ── Prozess-übergreifender LH-Zähler (Owner 2026-07-26: „erst messen") ──────
# WARUM: `_HOUR_BUDGET`/`_hour_count` oben zählen PRO PROZESS — produktiv laufen
# 3 Backend-Worker (:8080) + 1 Poll-Worker (:8081) + der Pfad des MQTT-Daemons,
# und ein datei-basierter Zähler ginge nicht (die Container teilen KEIN Volume).
# Die LH-Quota gilt aber PRO KEY. Gemeinsame Wahrheit = ax_api_budget.
#
# SCHLÜSSEL-LAYOUT — Stunde VOR dem Aufrufer:
#     lhopen:<YYYYMMDDHH>            lhopen:<YYYYMMDDHH>:<caller>
# Damit ist die Stunden-Abfrage ein PRÄFIX (`lhopen:2026072621%`) und kann den
# Primärschlüssel-Index nutzen; mit dem Aufrufer vorn wäre nur ein
# `%:<stunde>`-Suffix-Match möglich → Seq-Scan über eine ewig wachsende Tabelle.
_budget_buf_lock = threading.Lock()
_budget_buf = {}
_BUDGET_FLUSH_S = 30.0
_budget_thread = None


def _budget_writes_allowed():
    """Kein Zähler-Schreiben aus der Test-Suite — sonst verfälscht ein lokaler
    pytest-Lauf (mit echten Supabase-Creds in der Env) genau die Zahlen, die
    dieser Zähler beweisen soll."""
    return not os.environ.get('PYTEST_CURRENT_TEST')


def budget_flush():
    """Gepufferte Zähler nach Supabase schreiben. Wirft nie.

    NUR aus dem eigenen Flusher-Thread aufrufen (oder aus Tests). Der
    Supabase-Client hat 120 s postgrest-Timeout und der Retry-Transport kann
    das auf ein Vielfaches strecken — im Request-/MQTT-/Cron-Thread wäre das
    ein Aufhänger in einem MESS-Pfad. Fehlgeschlagene Einheiten wandern zurück
    in den Puffer, gehen also nicht schon beim ersten Netzruckler verloren."""
    with _budget_buf_lock:
        if not _budget_buf:
            return 0
        pending = dict(_budget_buf)
        _budget_buf.clear()
    done = 0
    try:
        from blueprints.aerox_data_blueprint import _budget_key_inc
        for k, u in list(pending.items()):
            total = _budget_key_inc(k, u)
            # Der bare Stunden-Key (ohne :<caller>) ist die gemeinsame Wahrheit
            # für den prozess-übergreifenden Gate — der RPC liefert hier den
            # Stand ALLER Prozesse zurück, sonst weiß niemand voneinander.
            if total is not None and k.startswith('lhopen:'):
                parts = k.split(':')
                if len(parts) == 2:
                    note_global_budget(parts[1], total)
            pending.pop(k, None)
            done += 1
    except Exception:
        pass
    if pending:                       # Rest zurücklegen statt verwerfen
        with _budget_buf_lock:
            for k, u in pending.items():
                _budget_buf[k] = _budget_buf.get(k, 0) + u
    return done


def _budget_thread_start():
    """Einen (1) Daemon-Thread pro Prozess, der den Puffer wegschreibt.
    Bewusst EIN langlebiger Thread: `supabase_threadlocal` hält pro Thread
    einen eigenen httpx-Client und schließt ihn beim Thread-Ende NICHT — ein
    Flush aus kurzlebigen `lh-warm`-Threads würde also Sockets lecken."""
    global _budget_thread
    if _budget_thread is not None or not _budget_writes_allowed():
        return

    def _run():
        while True:
            time.sleep(_BUDGET_FLUSH_S)
            try:
                budget_flush()
            except Exception:
                pass

    _budget_thread = threading.Thread(target=_run, daemon=True,
                                      name='lh-budget-flush')
    _budget_thread.start()


def budget_inc(key_prefix, caller=None, units=1):
    """Einen LH-Call buchen — reine In-Memory-Addition, KEIN Netz.
    Der Flusher-Thread schreibt höchstens alle 30 s. Wirft nie."""
    try:
        h = time.strftime('%Y%m%d%H', time.gmtime())
        lbl = re.sub(r'[^a-z0-9_]', '', str(caller or 'unknown').lower())[:32]
        with _budget_buf_lock:
            for k in (f'{key_prefix}:{h}', f'{key_prefix}:{h}:{lbl or "unknown"}'):
                _budget_buf[k] = _budget_buf.get(k, 0) + max(1, int(units))
        _budget_thread_start()
    except Exception:
        pass


# ── „leer" ≠ „unbekannt" ────────────────────────────────────────────────────
# Ein leeres Ergebnis hat DREI sehr verschiedene Ursachen: (a) LH sagt sauber
# „diesen Flug gibt es an dem Tag nicht" (HTTP 404), (b) der Call wurde vom
# eigenen Throttle abgewiesen, (c) LH war kaputt (403/503/Timeout). Nur (a) ist
# eine ANTWORT und darf negativ gecacht werden; (b) und (c) sind „wir wissen es
# nicht" und würden als Negativ-Cache einen Ausfall verlängern.
# MESSUNG 2026-07-27: in der Stichprobe waren alle Reg-Fehlschläge HTTP-503 —
# die landeten bisher 30 min lang als „hat keine Reg" im Memo.
# Thread-local, weil Gunicorn hier mit --threads 8 fährt.
_call_state = threading.local()


def last_call_answered():
    """True, wenn der letzte LH-GET dieses Threads eine ECHTE Antwort bekam
    (HTTP 200 oder 404). False bei Throttle-Abweisung, 403/5xx, Netzfehler —
    dann ist ein leeres Ergebnis kein Fakt, sondern eine Lücke."""
    return bool(getattr(_call_state, 'answered', False))


def _get(path, caller=None):
    """Authentifizierter GET → dict oder None. Wirft nie."""
    global _rate_penalty_until
    _call_state.answered = False
    tok = _token()
    if not tok:
        return None
    if not _budget_ok():
        # ABGEWIESEN mitzählen (sonst ist der Zähler blind für genau die Frage
        # des Owners): `_budget_ok` deckelt bei _HOUR_BUDGET pro Prozess, ein
        # reiner Gesendet-Zähler könnte also NIE über 220×Prozesse steigen.
        # Erst „gewollt = gesendet + abgewiesen" zeigt den echten Bedarf.
        budget_inc('lhopen_denied',
                   'rate_penalty' if time.time() < _rate_penalty_until
                   else 'hour_budget')
        return None
    budget_inc('lhopen', caller)
    req = urllib.request.Request(
        _BASE + path,
        headers={'Authorization': 'Bearer ' + tok,
                 'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read().decode('utf-8'))
        _call_state.answered = True
        return d
    except urllib.error.HTTPError as e:
        if e.code == 403:
            # „Developer Over Rate": kurzer Voll-Stopp statt 403-Salve —
            # jeder weitere Call verbrennt nur Budget gegen dieselbe Wand.
            _rate_penalty_until = time.time() + _RATE_PENALTY_SEC
        if e.code == 404:            # 404 = Flug an dem Tag nicht geflogen (normal)
            _call_state.answered = True
        else:
            log.warning('[lh_open] GET %s -> HTTP %s', path.split('?')[0], e.code)
        return None
    except Exception as e:
        log.warning('[lh_open] GET %s -> %s', path.split('?')[0], type(e).__name__)
        return None


def _offset_iso(local_str, utc_str):
    """('2026-07-21T10:55', '2026-07-21T08:55Z') → '2026-07-21T10:55:00+02:00'.
    Offset exakt aus lokal−UTC (auf Minuten). Fällt auf naives Lokal-ISO zurück,
    wenn UTC fehlt (Consumer datieren es dann stations-lokal)."""
    if not local_str:
        return None
    loc = local_str.strip()
    if len(loc) == 16:               # 'YYYY-MM-DDTHH:MM'
        loc_full = loc + ':00'
    else:
        loc_full = loc
    if not utc_str:
        return loc_full
    try:
        from datetime import datetime as _dt
        lu = _dt.strptime(loc[:16], '%Y-%m-%dT%H:%M')
        uu = _dt.strptime(utc_str.strip().rstrip('Z')[:16], '%Y-%m-%dT%H:%M')
        delta_min = round((lu - uu).total_seconds() / 60.0)
        sign = '+' if delta_min >= 0 else '-'
        delta_min = abs(delta_min)
        return f'{loc_full}{sign}{delta_min // 60:02d}:{delta_min % 60:02d}'
    except Exception:
        return loc_full


def _side_times(side):
    """LH-Departure/Arrival-Block → (sched_iso, est_iso). Ist bevorzugt Actual,
    sonst Estimated. Alles Station-Offset-ISO."""
    def _pick(kind):
        b = side.get(kind) or {}
        return b.get('DateTime')
    sched = _offset_iso(_pick('ScheduledTimeLocal'),
                        (side.get('ScheduledTimeUTC') or {}).get('DateTime'))
    # Ist: Actual gewinnt, sonst Estimated.
    a_loc = _pick('ActualTimeLocal') or _pick('EstimatedTimeLocal')
    a_utc = ((side.get('ActualTimeUTC') or {}).get('DateTime')
             or (side.get('EstimatedTimeUTC') or {}).get('DateTime'))
    est = _offset_iso(a_loc, a_utc) if a_loc else None
    return sched, est


def _delay_min(sched_iso, est_iso):
    """Delay in Minuten aus zwei Offset-ISO-Strings (oder None)."""
    if not sched_iso or not est_iso:
        return None
    try:
        from datetime import datetime as _dt
        s = _dt.fromisoformat(sched_iso)
        e = _dt.fromisoformat(est_iso)
        return int(round((e - s).total_seconds() / 60.0))
    except Exception:
        return None


def _leg_to_facts(leg):
    """Ein FlightStatus-Leg → Fakten-Dict (Shape wie _obs_rows_to_facts)."""
    dep = leg.get('Departure') or {}
    arr = leg.get('Arrival') or {}
    facts = {}
    sd, ed = _side_times(dep)
    sa, ea = _side_times(arr)
    if sd:
        facts['sched_dep'] = sd
    if ed:
        facts['est_dep'] = ed
    if sa:
        facts['sched_arr'] = sa
    if ea:
        facts['est_arr'] = ea
    dm = _delay_min(sd, ed)
    if dm is not None:
        facts['dep_delay_min'] = dm
    am = _delay_min(sa, ea)
    if am is not None:
        facts['arr_delay_min'] = am
    # Gate/Terminal
    dt = dep.get('Terminal') or {}
    if dt.get('Name'):
        facts['terminal'] = str(dt['Name'])
    if dt.get('Gate'):
        facts['gate'] = str(dt['Gate'])
    at = arr.get('Terminal') or {}
    if at.get('Name'):
        facts['arr_terminal'] = str(at['Name'])
    if at.get('Gate'):
        facts['arr_gate'] = str(at['Gate'])
    # Equipment: Typ + Registration (autoritativ, frisch)
    eq = leg.get('Equipment') or {}
    if eq.get('AircraftCode'):
        facts['type'] = str(eq['AircraftCode'])
    if eq.get('AircraftRegistration'):
        # LH gibt Regs ohne Bindestrich ('DAIKP') — normalisieren auf 'D-AIKP'
        facts['reg'] = _norm_reg(str(eq['AircraftRegistration']))
    # Status / Cancelled
    fs = leg.get('FlightStatus') or {}
    code = (fs.get('Code') or '').upper()
    definition = fs.get('Definition')
    if code in ('CD', 'CX', 'DX'):
        facts['cancelled'] = True
    if definition and definition.lower() != 'no status':
        facts['dep_status'] = definition
    # Route
    if dep.get('AirportCode'):
        facts['dep_iata'] = dep['AirportCode']
    if arr.get('AirportCode'):
        facts['arr_iata'] = arr['AirportCode']
    return facts


def _norm_reg(reg):
    """'DAIKP' → 'D-AIKP' (heuristisch, verbreitete Präfixe). Unbekanntes bleibt
    unverändert. Nur kosmetisch — der Wert wird als tail durchgereicht."""
    r = (reg or '').upper().replace('-', '').strip()
    if not r:
        return reg
    for p in ('D', 'HB', 'OE', 'OO', '9H', 'I', 'G', 'F', 'EI', 'LX'):
        if r.startswith(p) and len(r) > len(p):
            return p + '-' + r[len(p):]
    return reg


# ── Hintergrund-Warmup (Incident-Fix 2026-07-22) ────────────────────────────
# Fan-out-Consumer (cached_only=True) blockieren NIE auf LH-HTTP: Memo-Miss
# → {} sofort zurück + EIN dedupliziertes Warmup im Daemon-Thread; der
# nächste Read innerhalb der TTL trifft dann den Memo.
_warm_lock = threading.Lock()
_warm_inflight = set()
_WARM_MAX_INFLIGHT = 8


def _warm_async(fn, d, dep, arr, caller=None):
    key = (fn, d, dep, arr)
    with _warm_lock:
        if key in _warm_inflight or len(_warm_inflight) >= _WARM_MAX_INFLIGHT:
            return
        _warm_inflight.add(key)

    def _run():
        try:
            lh_flight_facts(fn, d, dep, arr,
                            caller=f'warm_{caller}' if caller else 'warm')
        except Exception:
            pass
        finally:
            with _warm_lock:
                _warm_inflight.discard(key)

    threading.Thread(target=_run, daemon=True, name='lh-warm').start()


def lh_flight_facts(flight_no, date, dep_iata=None, arr_iata=None, force=False,
                    cached_only=False, caller=None):
    """Autoritative LH-Group-Flug-Fakten (Shape wie _obs_rows_to_facts) oder {}.
    Gecacht pro (flight,date,dep,arr). No-op wenn nicht konfiguriert / kein
    LH-Group-Flug. Wirft nie. force=True überspringt den Memo-READ (schreibt
    ihn aber neu) — für den MQTT-Push-Pfad, der per Definition frischer als
    die TTL ist (Broker sagt „hat sich GERADE geändert"). cached_only=True:
    NIE blockieren — Memo-Hit liefert, Miss triggert nur ein Hintergrund-
    Warmup und gibt {} (Pflicht für Fan-out-Pfade wie _flight_obs_merged;
    Incident 2026-07-22: blockierende LH-Calls + Throttle-Sleeps im Request-
    Pfad legten alle Gunicorn-Worker lahm)."""
    fn = (flight_no or '').replace(' ', '').upper().strip()
    d = ((date or '').strip()[:10])
    if not fn or not d or not lh_open_configured() or not is_lh_group(fn):
        return {}
    dep = (dep_iata or '').upper().strip() or None
    arr = (arr_iata or '').upper().strip() or None
    key = (fn, d, dep, arr)
    now = time.time()
    if not force:
        with _facts_lock:
            hit = _facts_memo.get(key)
            if hit and now < hit[0]:
                return dict(hit[1])
    if cached_only:
        _warm_async(fn, d, dep, arr, caller)
        return {}

    data = _get(f'/operations/flightstatus/{urllib.parse.quote(fn)}/{d}',
                caller=caller)
    facts = {}
    try:
        legs = (((data or {}).get('FlightStatusResource') or {})
                .get('Flights') or {}).get('Flight')
        if isinstance(legs, dict):
            legs = [legs]
        legs = legs or []
        chosen = None
        for lg in legs:
            lf = (lg.get('Departure') or {}).get('AirportCode')
            lt = (lg.get('Arrival') or {}).get('AirportCode')
            if dep and arr:
                if lf == dep and lt == arr:
                    chosen = lg
                    break
            elif dep:
                if lf == dep:
                    chosen = lg
                    break
            else:
                # MEHR-LEG-GUARD (Tail-Audit 22.07.): ohne dep/arr bei einem
                # Multi-Sektor-Flug (4Y136 FRA-MBA-JRO) wäre „erstes Leg"
                # geraten — falsche Reg/Zeiten für Flugnummer-only-Caller.
                # Ein-Leg-Flüge (der Normalfall) bleiben unverändert.
                chosen = lg if len(legs) == 1 else None
                break
        if chosen is not None:
            facts = _leg_to_facts(chosen)
    except Exception as e:
        log.warning('[lh_open] parse %s/%s: %s', fn, d, type(e).__name__)
        facts = {}

    # TTL: heute kurz (Gate-Wechsel/Ist-Zeiten frisch — ein Gate kann sich
    # ~40 min vor Abflug ändern), sonst lang (Plan/Historie stabil).
    today = time.strftime('%Y-%m-%d', time.gmtime())
    ttl = 120 if d == today else 6 * 3600
    # CACHE-FRAGMENTIERUNG (2026-07-26): der MQTT-Push-Pfad ruft ohne dep/arr
    # (Key (fn,date,None,None)), die Roster-/Detail-Pfade IMMER mit
    # (fn,date,dep,arr). Derselbe Flug belegte dadurch zwei Einträge und der
    # frisch bezahlte Push-Call wärmte den Key nicht, den die App danach liest.
    # Der Alias-Write kostet nichts und macht jeden Call doppelt nutzbar.
    _alias = None
    if (dep is None or arr is None) and isinstance(facts, dict):
        _fd = (facts.get('dep_iata') or '').upper().strip() or None
        _fa = (facts.get('arr_iata') or '').upper().strip() or None
        if _fd and _fa and (_fd, _fa) != (dep, arr):
            _alias = (fn, d, _fd, _fa)
    with _facts_lock:
        _facts_memo[key] = (now + ttl, dict(facts))
        if _alias is not None:
            _facts_memo[_alias] = (now + ttl, dict(facts))
        if len(_facts_memo) > _FACTS_MAX:
            items = sorted(_facts_memo.items(), key=lambda kv: kv[1][0])
            for k, _v in items[:len(items) // 4 or 1]:
                _facts_memo.pop(k, None)
    return facts


@lh_open_bp.route('/api/lh/flight/<flight>/<date>', methods=['GET'])
def lh_flight_debug(flight, date):
    """Diagnose: die autoritativen LH-Fakten für einen Flug (kein Secret, keine
    PII). Zeigt configured/is_group-Status, damit man die Verdrahtung prüfen
    kann. Optional ?dep=FRA&arr=MBA für die Leg-Wahl."""
    from flask import request
    dep = request.args.get('dep')
    arr = request.args.get('arr')
    return jsonify({
        'configured': lh_open_configured(),
        'is_lh_group': is_lh_group(flight),
        'flight': flight, 'date': date,
        'facts': lh_flight_facts(flight, date, dep, arr, caller='debug'),
    })
