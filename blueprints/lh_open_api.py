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
# dieselbe Group. Bewusst KEINE Nicht-Group-Carrier — schont das 1.000/h-Budget
# (Roster-Sektoren der Crew sind immer Group-Flüge). VL (Lufthansa City,
# AeroX-Synthetik-Prefix) ist KEIN eigener API-Carrier → ausgelassen.
_LH_GROUP = {'LH', 'LX', 'OS', 'SN', 'EW', '4Y', 'EN', 'CL', 'WK'}

# ── Token-Cache ──────────────────────────────────────────────────────────────
_tok_lock = threading.Lock()
_tok_val = None
_tok_exp = 0.0

# ── 5/sec-Throttle + Stunden-Budget (Public-Plan: 1.000/h, wir kappen bei 900) ─
_rate_lock = threading.Lock()
_last_call_ts = 0.0
_MIN_INTERVAL = 0.22            # ≈ 4,5 Calls/sec, unter dem 5/sec-Limit
_HOUR_BUDGET = 900             # Sicherheitsmarge unter 1.000/h
_hour_window = 0               # aktuelle Stunde (epoch // 3600)
_hour_count = 0
_budget_warned_hour = -1

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


def _token():
    """Client-Credentials-Access-Token, gecacht bis kurz vor Ablauf. None bei
    Fehler (Aufrufer fällt dann still auf die anderen Tiers zurück)."""
    global _tok_val, _tok_exp
    if not lh_open_configured():
        return None
    now = time.time()
    with _tok_lock:
        if _tok_val and now < _tok_exp:
            return _tok_val
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
        return None


def _budget_ok():
    """5/sec-Throttle + Stunden-Budget. False (mit Log) wenn das Stunden-Budget
    erschöpft ist — kein stiller Cap."""
    global _last_call_ts, _hour_window, _hour_count, _budget_warned_hour
    now = time.time()
    hour = int(now // 3600)
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
        # sanftes 5/sec-Spacing
        wait = _MIN_INTERVAL - (now - _last_call_ts)
        if wait > 0:
            time.sleep(wait)
        _last_call_ts = time.time()
        _hour_count += 1
        return True


def _get(path):
    """Authentifizierter GET → dict oder None. Wirft nie."""
    tok = _token()
    if not tok:
        return None
    if not _budget_ok():
        return None
    req = urllib.request.Request(
        _BASE + path,
        headers={'Authorization': 'Bearer ' + tok,
                 'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        if e.code not in (404,):     # 404 = Flug an dem Tag nicht geflogen (normal)
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


def lh_flight_facts(flight_no, date, dep_iata=None, arr_iata=None, force=False):
    """Autoritative LH-Group-Flug-Fakten (Shape wie _obs_rows_to_facts) oder {}.
    Gecacht pro (flight,date,dep,arr). No-op wenn nicht konfiguriert / kein
    LH-Group-Flug. Wirft nie. force=True überspringt den Memo-READ (schreibt
    ihn aber neu) — für den MQTT-Push-Pfad, der per Definition frischer als
    die TTL ist (Broker sagt „hat sich GERADE geändert")."""
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

    data = _get(f'/operations/flightstatus/{urllib.parse.quote(fn)}/{d}')
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
                chosen = lg
                break
        if chosen is None and legs and not (dep or arr):
            chosen = legs[0]
        if chosen is not None:
            facts = _leg_to_facts(chosen)
    except Exception as e:
        log.warning('[lh_open] parse %s/%s: %s', fn, d, type(e).__name__)
        facts = {}

    # TTL: heute kurz (Gate-Wechsel/Ist-Zeiten frisch — ein Gate kann sich
    # ~40 min vor Abflug ändern), sonst lang (Plan/Historie stabil).
    today = time.strftime('%Y-%m-%d', time.gmtime())
    ttl = 120 if d == today else 6 * 3600
    with _facts_lock:
        _facts_memo[key] = (now + ttl, dict(facts))
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
        'facts': lh_flight_facts(flight, date, dep, arr),
    })
