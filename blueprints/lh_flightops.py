"""Lufthansa FlightOps Crew API (Engine B) — sanktionierte Roster-QUELLE.

Ersetzt die Calendar-Share-Abrufe durch den offiziellen, per-Crew autorisierten
Weg: Authorization Code + PKCE (S256) gegen den LH-Crew-OAuth2-Server, dann
Duty Events aus dem Mock (jeder darf sich im MOCK anmelden; PROD nur echte
Crew). Duty Events → synthetisches ICS → bestehende Roster-Pipeline
(`import_calendar_feed`), also reuse von Merge/Briefings/Sektoren/Reconcile —
genau das Muster des CrewAccess-/Discover-PDF-Imports.

Sicherheit: das `client_secret` steckt NUR serverseitig (Basic-Auth am
Token-Endpoint) — NIE in der App. Der Code-Austausch läuft daher im Backend.
PKCE-Verifier wird serverseitig pro `state` gehalten. Per-Crew-Tokens (access+
refresh) liegen im Profil-Mirror (durable) + Disk.

Alles env-gesteuert, voll no-op ohne Creds → Commit/Deploy immer sicher:
  LH_FLIGHTOPS_KEY / LH_FLIGHTOPS_SECRET        client credentials (Basic)
  LH_FLIGHTOPS_AUTHORIZE_URL / _TOKEN_URL       OAuth-Server (Defaults = Doku)
  LH_FLIGHTOPS_BASE                             Mock-API-Base (Doku-Default)
  LH_FLIGHTOPS_SCOPE                            Crew-Scope (MOCK-Prefix)
  LH_FLIGHTOPS_REDIRECT_URI                     registrierte Callback-URL
"""
import os
import re
import time
import json
import base64
import hashlib
import secrets
import threading
import urllib.request
import urllib.parse
import urllib.error
import logging

from flask import Blueprint, jsonify, request, redirect

log = logging.getLogger('aerotax')
lh_flightops_bp = Blueprint('lh_flightops_bp', __name__)

# DREI UMGEBUNGEN (Owner-Doku 2026-07-22) — alles env-gesteuert, also nur ein
# Config-Flip, KEIN Umbau:
#   1) MOCK (Default): statische Testdaten. base .../crew_services/mock,
#      scope https://mock.cms.fra.dlh.de/publicCrewApiDev, Login = Google
#      Authenticator (jeder), OAuth oauth-test.lufthansa.com.
#   2) TEST/Sandbox: ECHTE anonymisierte Testdaten. base OHNE /mock
#      (.../crew_services), scope https://cms.fra.dlh.de/publicCrewApiDev,
#      braucht gültigen RSA-Token (echte Crew), OAuth oauth-test.
#   3) PROD: base https://api.lufthansa.com/v1/flight_operations/crew_services,
#      scope https://cms.fra.dlh.de/publicCrewApi, OAuth oauth.lufthansa.com.
_KEY = (os.environ.get('LH_FLIGHTOPS_KEY') or '').strip()
_SECRET = (os.environ.get('LH_FLIGHTOPS_SECRET') or '').strip()
_AUTHORIZE_URL = (os.environ.get('LH_FLIGHTOPS_AUTHORIZE_URL')
                  or 'https://oauth-test.lufthansa.com/lhcrew/oauth/authorize').strip()
_TOKEN_URL = (os.environ.get('LH_FLIGHTOPS_TOKEN_URL')
              or 'https://oauth-test.lufthansa.com/lhcrew/oauth/token').strip()
_BASE = (os.environ.get('LH_FLIGHTOPS_BASE')
         or 'https://api-sandbox.lufthansa.com/v1/flight_operations/crew_services/mock').strip().rstrip('/')
# MOCK-Scope: LIVE VERIFIZIERT 2026-07-22 — der Consent/Token liefert
# `publicCrewApiDev` (Authorize akzeptiert auch `publicCrewApi`, mappt aber auf
# Dev). Env-überschreibbar (für PROD ohne den mock-Prefix).
_SCOPE = (os.environ.get('LH_FLIGHTOPS_SCOPE')
          or 'https://mock.cms.fra.dlh.de/publicCrewApiDev').strip()
# Muss GENAU der im Portal registrierten Callback-URL entsprechen (Custom-Scheme
# für die iOS-ASWebAuthenticationSession).
_REDIRECT_URI = (os.environ.get('LH_FLIGHTOPS_REDIRECT_URI')
                 or 'aerox://lhcrew/callback').strip()


def flightops_configured():
    """True nur mit Key+Secret (sonst voll no-op)."""
    return bool(_KEY and _SECRET)


# ── PKCE ─────────────────────────────────────────────────────────────────────
def _pkce_pair():
    """(verifier, challenge) — RFC7636 S256, URL-safe ohne Padding."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(40)).rstrip(b'=').decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b'=').decode()
    return verifier, challenge


# ── State→Verifier-Store (kurzlebig, Flow dauert Minuten) ────────────────────
# WICHTIG (Multi-Worker): gunicorn fährt mehrere Worker — `start` und `exchange`
# landen oft auf VERSCHIEDENEN Workern. Ein reiner In-Memory-Dict wäre dann leer
# → `state_invalid_or_expired`. Deshalb DISK-backed (alle Worker teilen das
# Container-FS `_USER_HISTORY_DIR`), mit In-Memory-Fastpath. State ist
# kurzlebig + single-use (nach exchange gelöscht).
_flow_lock = threading.Lock()
_flow_store = {}   # state -> (expires_at, {verifier, user_token})  (Fastpath)
_FLOW_TTL = 900


def _flow_dir():
    try:
        import app as _app
        return _app._USER_HISTORY_DIR
    except Exception:
        return '/tmp'


def _flow_path(state):
    safe = re.sub(r'[^A-Za-z0-9_-]', '', state or '')[:80]
    return os.path.join(_flow_dir(), f'foflow_{safe}.json') if safe else None


def _flow_put(state, verifier, user_token):
    exp = time.time() + _FLOW_TTL
    rec = {'verifier': verifier, 'user_token': user_token, 'exp': exp}
    with _flow_lock:
        _flow_store[state] = (exp, rec)
    try:
        p = _flow_path(state)
        if p:
            with open(p, 'w') as f:
                json.dump(rec, f)
    except Exception as e:
        log.warning('[lh_flightops] flow_put disk: %s', type(e).__name__)


def _flow_take(state):
    now = time.time()
    # Fastpath: derselbe Worker
    with _flow_lock:
        hit = _flow_store.pop(state, None)
    if hit and hit[0] >= now:
        _flow_rm(state)
        return hit[1]
    # Cross-Worker: von Disk lesen (single-use → löschen)
    try:
        p = _flow_path(state)
        if p and os.path.exists(p):
            with open(p) as f:
                rec = json.load(f)
            try:
                os.remove(p)
            except OSError:
                pass
            if rec.get('exp', 0) >= now:
                return {'verifier': rec.get('verifier'), 'user_token': rec.get('user_token')}
    except Exception as e:
        log.warning('[lh_flightops] flow_take disk: %s', type(e).__name__)
    return None


def _flow_rm(state):
    try:
        p = _flow_path(state)
        if p and os.path.exists(p):
            os.remove(p)
    except Exception:
        pass


# ── Per-Crew-Token-Store (durable Profil-Mirror + Disk) ─────────────────────
def _tokens_load(user_token):
    """FlightOps-Tokens {access, refresh, expires_at, scope} für einen AeroX-User."""
    try:
        import app as _app
        prof = ((_app._profile_load(user_token) or {}).get('profile') or {})
        t = prof.get('flightops_tokens')
        return dict(t) if isinstance(t, dict) else {}
    except Exception:
        return {}


def _tokens_save(user_token, tokens):
    try:
        import app as _app
        pf = _app._profile_load(user_token) or {}
        prof = (pf.get('profile') or {})
        prof['flightops_tokens'] = tokens
        _app._profile_save(user_token, prof)
        return True
    except Exception as e:
        log.warning('[lh_flightops] token_save_fail: %s', type(e).__name__)
        return False


def _basic_header():
    raw = f'{_KEY}:{_SECRET}'.encode()
    return 'Basic ' + base64.b64encode(raw).decode()


def _exchange_code(code, verifier):
    """authorization_code → Token-Dict oder None. Client-Secret via Basic-Header."""
    body = urllib.parse.urlencode({
        'grant_type': 'authorization_code', 'code': code,
        'redirect_uri': _REDIRECT_URI, 'client_id': _KEY,
        'code_verifier': verifier}).encode()
    return _token_request(body)


def _refresh(refresh_token):
    body = urllib.parse.urlencode({
        'grant_type': 'refresh_token', 'refresh_token': refresh_token}).encode()
    return _token_request(body)


def _token_request(body):
    req = urllib.request.Request(
        _TOKEN_URL, data=body,
        headers={'Authorization': _basic_header(),
                 'Content-Type': 'application/x-www-form-urlencoded',
                 'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read().decode('utf-8'))
        if not d.get('access_token'):
            return None
        return {
            'access': d['access_token'],
            'refresh': d.get('refresh_token'),
            'scope': d.get('scope'),
            'expires_at': time.time() + max(60, int(d.get('expires_in') or 3600) - 60),
        }
    except urllib.error.HTTPError as e:
        log.warning('[lh_flightops] token HTTP %s', e.code)
        return None
    except Exception as e:
        log.warning('[lh_flightops] token %s', type(e).__name__)
        return None


def _valid_access(user_token):
    """Gültigen Access-Token holen (Auto-Refresh). None wenn nicht verbunden."""
    t = _tokens_load(user_token)
    if not t.get('access'):
        return None
    if time.time() < (t.get('expires_at') or 0):
        return t['access']
    if t.get('refresh'):
        nt = _refresh(t['refresh'])
        if nt:
            nt['refresh'] = nt.get('refresh') or t.get('refresh')
            _tokens_save(user_token, nt)
            return nt['access']
    return None


def flightops_connected(user_token):
    return bool(_tokens_load(user_token).get('access'))


# ── Mock-API-Call ────────────────────────────────────────────────────────────
def _api_get(user_token, path, params=None):
    access = _valid_access(user_token)
    if not access:
        return None
    url = _BASE + path
    if params:
        url += ('&' if '?' in url else '?') + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url, headers={'Authorization': 'Bearer ' + access,
                      'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        log.warning('[lh_flightops] api %s -> HTTP %s', path, e.code)
        return None
    except Exception as e:
        log.warning('[lh_flightops] api %s -> %s', path, type(e).__name__)
        return None


def _date_z(d):
    """'YYYY-MM-DD' → 'YYYY-MM-DDZ' (das von der API erwartete Format,
    live verifiziert 2026-07-22). Schon vorhandenes Z bleibt."""
    d = (d or '').strip()
    return d if d.endswith('Z') else (d[:10] + 'Z' if len(d) >= 10 else d)


def duty_events(user_token, from_date, to_date):
    """COMMON_DUTY_EVENTS für ein Zeitfenster → Response-Dict oder None.
    Datumsformat YYYY-MM-DDZ. HINWEIS: die MOCK-Umgebung liefert NUR für das
    dokumentierte Beispiel-Fenster (2016-10-01Z..2016-10-31Z) Daten; echte
    Fenster gehen erst gegen PROD."""
    resp = _api_get(user_token, '/COMMON_DUTY_EVENTS',
                    {'fromDate': _date_z(from_date), 'toDate': _date_z(to_date)})
    # Gateway-/Backend-Fehler kommen als {processingErrors:[…]} MIT 200/4xx/5xx —
    # nie als Duty-Events missdeuten.
    if isinstance(resp, dict) and resp.get('processingErrors'):
        try:
            e = (resp['processingErrors'] or [{}])[0]
            log.warning('[lh_flightops] duty_events upstream %s: %s',
                        e.get('code'), (e.get('type') or '')[:60])
        except Exception:
            pass
        return None
    return resp


def is_mock():
    """True wenn die aktuelle Base die MOCK-Sandbox ist (nur Beispiel-Daten)."""
    return '/mock' in _BASE.lower() or 'sandbox' in _BASE.lower()


# ── Alle Crew-Services (Resource-Pfade aus der Doku, 2026-07-22) ─────────────
# Duty Events = Roster (oben). Die weiteren Services füttern bestehende
# AeroX-Features: Landing Report → Flugbuch-Landungen, Flight Leg Details →
# Flug-Fakten, Crew List → Crew-Feed, Crew Hotel → Hotel-Verzeichnis, Rotation.
def crew_list(user_token, flight, date, dep, arr, access_code):
    """COMMON_CREWLIST — wer fliegt mit (crewMembers[])."""
    return _api_get(user_token, '/COMMON_CREWLIST', {
        'flightDesignator': (flight or '').upper().replace(' ', ''),
        'flightDate': _date_z(date), 'departureAirport': (dep or '').upper(),
        'arrivalAirport': (arr or '').upper(), 'accessCode': access_code or ''})


def crew_rotation(user_token, *rotation_numbers):
    """COMMON_CREW_ROTATION — Rotations-Details (rotations[].shifts[].legs[])."""
    params = {}
    for i, rn in enumerate([r for r in rotation_numbers if r][:6]):
        params['RN' if i == 0 else f'RN_{i + 1}'] = str(rn)
    if not params:
        return None
    return _api_get(user_token, '/COMMON_CREW_ROTATION', params)


def landing_report(user_token, flight, date, dep):
    """COMMON_LANDING_REPORT — u. a. `landingPerformed` (Bool) für dieses Leg."""
    return _api_get(user_token, '/COMMON_LANDING_REPORT', {
        'flightDesignator': (flight or '').upper().replace(' ', ''),
        'flightDate': _date_z(date), 'departureAirport': (dep or '').upper()})


def flight_leg_details(user_token, flight, date=None, dep=None, arr=None):
    """COMMON_FLIGHT_LEG_DETAILS — Reg/Muster/Gate/Blockzeit autoritativ."""
    params = {'flightDesignator': (flight or '').upper().replace(' ', '')}
    if date:
        params['flightDate'] = _date_z(date)
    if dep:
        params['departureAirport'] = dep.upper()
    if arr:
        params['arrivalAirport'] = arr.upper()
    return _api_get(user_token, '/COMMON_FLIGHT_LEG_DETAILS', params)


def crew_hotel(user_token, station, provider=None):
    """COMMON_CREW_HOTEL_INFO — Layover-Hotel-Infos für eine Station."""
    params = {'station': (station or '').upper()}
    if provider:
        params['provider'] = provider
    return _api_get(user_token, '/COMMON_CREW_HOTEL_INFO', params)


def _truthy(v):
    """LH liefert Booleans teils als STRING ('true'/'false' — live 2026-07-22)."""
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() == 'true'
    return None


def landing_report_facts(user_token, flight, date, dep):
    """Landing Report → normalisierte Fakten (gegen ECHTE Mock-Shape 2026-07-22):
    {landed: bool|None, tail, dep_iso, arr_iso, block_min}. OUT/IN = Block
    (aircraft.out/in), off/on = Flugzeit. None-Werte weggelassen. Pure-nah."""
    r = landing_report(user_token, flight, date, dep)
    if not isinstance(r, dict) or r.get('processingErrors'):
        return {}
    ev = (r.get('events') or {}).get('aircraft') or {}
    out = _valid_iso(ev.get('out'))
    _in = _valid_iso(ev.get('in'))
    facts = {'landed': _truthy(r.get('landingPerformed'))}
    tail = _norm_reg(r.get('tailsign'))
    if tail:
        facts['tail'] = tail
    if out:
        facts['dep_iso'] = out
    if _in:
        facts['arr_iso'] = _in
    bm = _block_min_iso(out, _in)
    if bm is not None:
        facts['block_min'] = bm
    return facts


def _valid_iso(v):
    return v if (isinstance(v, str) and 'T' in v) else None


def _norm_reg(reg):
    """'DAISQ' → 'D-AISQ' (heuristisch, verbreitete Präfixe)."""
    r = (reg or '').upper().replace('-', '').strip()
    if not r:
        return None
    for p in ('D', 'HB', 'OE', 'OO', '9H', 'I', 'G', 'F', 'EI', 'LX'):
        if r.startswith(p) and len(r) > len(p):
            return p + '-' + r[len(p):]
    return r


def _block_min_iso(a, b):
    try:
        from datetime import datetime as _dt
        d = _dt.fromisoformat((a or '').replace('Z', '+00:00'))
        e = _dt.fromisoformat((b or '').replace('Z', '+00:00'))
        m = int(round((e - d).total_seconds() / 60.0))
        return m if 0 < m < 20 * 60 else None
    except Exception:
        return None


def landing_performed(user_token, flight, date, dep):
    """True/False/None — hat der eingeloggte Crew das Leg gelandet?"""
    return landing_report_facts(user_token, flight, date, dep).get('landed')


def parse_crew_list(resp):
    """COMMON_CREWLIST-Response → normalisierte Liste (echte Shape 2026-07-22).
    [{position, name, pk}] — für „Wer fliegt mit". Pure/testbar."""
    if not isinstance(resp, dict):
        return []
    out = []
    for m in (resp.get('crewMembers') or []):
        if not isinstance(m, dict):
            continue
        first = (m.get('firstName') or '').strip().title()
        last = (m.get('lastName') or '').strip().title()
        name = ' '.join(x for x in (first, last) if x)
        out.append({'position': (m.get('crewPosition') or '').strip(),
                    'name': name or None, 'pk': m.get('pkNumber'),
                    'duty': m.get('dutyCode')})
    return out


def parse_crew_hotel(resp):
    """COMMON_CREW_HOTEL_INFO → [{airline, hotel, phone, transfer, transfer_phone}]
    (echte Shape 2026-07-22). Pure/testbar."""
    if not isinstance(resp, dict):
        return []
    out = []
    for h in (resp.get('hotelInformation') or []):
        if not isinstance(h, dict):
            continue
        hc = h.get('hotelContact') or {}
        tc = h.get('hotelTransferContact') or {}
        out.append({
            'airline': h.get('forAirline'),
            'hotel': hc.get('company'), 'phone': hc.get('phone') or None,
            'transfer': tc.get('company') or None,
            'transfer_phone': tc.get('phone') or None,
            'station': resp.get('station'),
        })
    return out


def check_in_times(user_token, flight, date, dep, arr,
                   duty_type='OD', crew_category='COC', **extra):
    """COMMON_CHECK_IN_TIMES — Briefing-/Check-in-Zeiten je FLUG (→ Pickup/
    Report). Doku-bestätigte Parameter (Owner 2026-07-22): flightDesignator,
    flightDate, departureAirport, arrivalAirport, dutyType (OD/DH),
    crewCategory (COC=Cockpit / CAB=Cabin). Das war die 409-Ursache (vorher
    fälschlich Datumsfenster)."""
    params = {
        'flightDesignator': (flight or '').upper().replace(' ', ''),
        'flightDate': _date_z(date), 'departureAirport': (dep or '').upper(),
        'arrivalAirport': (arr or '').upper(),
        'dutyType': duty_type, 'crewCategory': crew_category, **extra}
    return _api_get(user_token, '/COMMON_CHECK_IN_TIMES', params)


def airport_weather(user_token, station, **extra):
    """COMMON_AIRPORT_WEATHER — Flughafenwetter (METAR/TAF-nah)."""
    params = {'station': (station or '').upper(), **extra}
    return _api_get(user_token, '/COMMON_AIRPORT_WEATHER', params)


def simulator_crewlist(user_token, **params):
    """COMMON_SIMULATOR_CREWLIST — Sim-Session-Crew."""
    return _api_get(user_token, '/COMMON_SIMULATOR_CREWLIST', params)


def service_get(user_token, service, params=None):
    """Generischer Service-Call (für Diagnose/Verdrahtung). `service` ist der
    COMMON_*-Name. Nur echte Services zulassen."""
    s = (service or '').strip().upper()
    if not s.startswith('COMMON_') or not re.fullmatch(r'COMMON_[A-Z_]+', s):
        return None
    return _api_get(user_token, '/' + s, params if isinstance(params, dict) else {})


# Das einzige Fenster, für das der MOCK Daten hat (dokumentiertes Beispiel).
_MOCK_WINDOW = ('2016-10-01', '2016-10-31')


# ── Duty Events → synthetisches ICS (reuse der Roster-Pipeline) ─────────────
# nach Normalisierung (Unterstriche raus): 'flight_other' → 'flightother'
_FLIGHT_CATS = {'flight', 'flightother'}


def duty_events_to_ics(resp):
    """FlightOps-Duty-Events → ICS-String (oder None). Pure/testbar.
    Flight-Events → VEVENT im LH-Summary-Format ('LH400: FRA-JFK'), Off/Vac/
    Standby/Hotel → Marker-/Layover-Events. Zeiten kommen als UTC-ISO. NICHTS
    wird erfunden; unbekannte Kategorien reisen als Roh-Summary mit."""
    if not isinstance(resp, dict):
        return None
    days = resp.get('rosterDays')
    if not isinstance(days, list):
        return None
    lines = ['BEGIN:VCALENDAR', 'VERSION:2.0',
             'PRODID:-//AeroX LH FlightOps//DE']
    n = 0

    def _dt(v):
        # 'YYYY-MM-DDTHH:MM:SSZ' → 'YYYYMMDDTHHMMSSZ'
        try:
            s = (v or '').strip().replace('-', '').replace(':', '')
            return s if s.endswith('Z') else (s + 'Z' if 'T' in s else None)
        except Exception:
            return None

    for d in days:
        for ev in (d.get('events') or []):
            if not isinstance(ev, dict):
                continue
            # Robust gegen Doku-Diskrepanz: eventType kommt GROSS + ohne
            # Unterstrich ('FLIGHT','GROUNDEVENT','BRIEFING','HOTEL'), mein
            # früherer Code prüfte 'ground_event'. Normalisieren: lower +
            # Unterstriche/Whitespace raus → 'groundevent'. (Owner/Claude-Web-
            # Hinweis 2026-07-22, final gegen Live-JSON prüfen.)
            cat = re.sub(r'[_\s]', '', (ev.get('eventCategory') or '').lower())
            etype = re.sub(r'[_\s]', '', (ev.get('eventType') or '').lower())
            frm = (ev.get('startLocation') or '').upper().strip()
            to = (ev.get('endLocation') or '').upper().strip()
            det = (ev.get('eventDetails') or '').strip()
            st = _dt(ev.get('startTime'))
            en = _dt(ev.get('endTime'))
            uid = f'fo-{n}@aerox-flightops'
            n += 1
            is_flight = (etype == 'flight' or cat in _FLIGHT_CATS)
            if is_flight and len(frm) == 3 and len(to) == 3 and st and en:
                # Flugnummer aus eventDetails (z. B. 'LH400' / 'LH 400 …').
                import re as _re
                m = _re.search(r'\b([A-Z]{2}|\d[A-Z])\s?\d{1,4}[A-Z]?\b', det.upper())
                flt = (m.group(0).replace(' ', '') if m else '').strip()
                summary = (f'{flt}: {frm}-{to}' if flt else f'{frm}-{to}')
                lines += ['BEGIN:VEVENT', f'UID:{uid}',
                          f'DTSTART:{st}', f'DTEND:{en}',
                          f'SUMMARY:{summary}', 'END:VEVENT']
                continue
            # Nicht-Flug: Marker/Standby/Hotel/Layover
            summary = None
            if cat in ('off',):
                summary = 'Off Day'
            elif cat in ('vac',):
                summary = 'Urlaub'
            elif cat in ('res', 'frs'):
                summary = f'Standby {frm}' if len(frm) == 3 else 'Standby'
            elif etype == 'hotel' or cat == 'hotel':
                summary = f'Layover {to or frm}'
            elif cat in ('sim',):
                summary = 'Simulator'
            elif cat in ('abs', 'lic', 'duty') or etype in ('briefing', 'groundevent'):
                summary = det or cat.upper() or 'Duty'
            else:
                summary = det or cat.upper() or 'Event'
            day = (d.get('day') or '')[:10].replace('-', '')
            if ev.get('wholeDay') and day:
                nd = _next_day(day)
                lines += ['BEGIN:VEVENT', f'UID:{uid}',
                          f'DTSTART;VALUE=DATE:{day}', f'DTEND;VALUE=DATE:{nd}',
                          f'SUMMARY:{summary}', 'END:VEVENT']
            elif st and en:
                lines += ['BEGIN:VEVENT', f'UID:{uid}',
                          f'DTSTART:{st}', f'DTEND:{en}',
                          f'SUMMARY:{summary}', 'END:VEVENT']
            elif day:
                nd = _next_day(day)
                lines += ['BEGIN:VEVENT', f'UID:{uid}',
                          f'DTSTART;VALUE=DATE:{day}', f'DTEND;VALUE=DATE:{nd}',
                          f'SUMMARY:{summary}', 'END:VEVENT']
    lines.append('END:VCALENDAR')
    if n == 0:
        return None
    return '\r\n'.join(lines)


def _next_day(yyyymmdd):
    from datetime import datetime as _dt, timedelta as _td
    try:
        return (_dt.strptime(yyyymmdd, '%Y%m%d') + _td(days=1)).strftime('%Y%m%d')
    except Exception:
        return yyyymmdd


# ── Endpoints ────────────────────────────────────────────────────────────────
@lh_flightops_bp.route('/api/lh/flightops/oauth/start', methods=['GET'])
def flightops_oauth_start():
    """Schritt 1: Authorize-URL bauen (PKCE-Challenge + state serverseitig).
    Query `token` = AeroX-User-Token (an den der Crew-Login gebunden wird)."""
    if not flightops_configured():
        return jsonify({'ok': False, 'error': 'not_configured'}), 503
    user_token = (request.args.get('token') or '').strip()
    if not user_token:
        return jsonify({'ok': False, 'error': 'token_required'}), 400
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(24)
    _flow_put(state, verifier, user_token)
    q = urllib.parse.urlencode({
        'response_type': 'code', 'client_id': _KEY,
        'redirect_uri': _REDIRECT_URI, 'scope': _SCOPE, 'state': state,
        'code_challenge': challenge, 'code_challenge_method': 'S256'})
    return jsonify({'ok': True, 'authorize_url': f'{_AUTHORIZE_URL}?{q}',
                    'state': state, 'redirect_uri': _REDIRECT_URI})


@lh_flightops_bp.route('/api/lh/flightops/oauth/exchange', methods=['POST'])
def flightops_oauth_exchange():
    """Schritt 2: Code (den die App per Custom-Scheme empfangen hat) gegen Token
    tauschen (serverseitig, Secret sicher) und per-Crew speichern.
    Body: {code, state}. Der User wird über den state-gebundenen Flow aufgelöst."""
    if not flightops_configured():
        return jsonify({'ok': False, 'error': 'not_configured'}), 503
    body = request.get_json(silent=True) or {}
    code = (body.get('code') or '').strip()
    state = (body.get('state') or '').strip()
    if not code or not state:
        return jsonify({'ok': False, 'error': 'code_state_required'}), 400
    flow = _flow_take(state)
    if not flow:
        return jsonify({'ok': False, 'error': 'state_invalid_or_expired'}), 400
    tok = _exchange_code(code, flow['verifier'])
    if not tok:
        return jsonify({'ok': False, 'error': 'exchange_failed'}), 502
    _tokens_save(flow['user_token'], tok)
    return jsonify({'ok': True, 'connected': True, 'scope': tok.get('scope')})


@lh_flightops_bp.route('/api/lh/flightops/status/<token>', methods=['GET'])
def flightops_status(token):
    """Ist dieser User mit FlightOps verbunden?"""
    t = _tokens_load(token)
    return jsonify({'ok': True, 'connected': bool(t.get('access')),
                    'scope': t.get('scope'),
                    'configured': flightops_configured()})


@lh_flightops_bp.route('/api/lh/flightops/import/<token>', methods=['POST'])
def flightops_import(token):
    """Schritt 3: Duty Events holen → ICS → bestehende Roster-Pipeline.
    Body optional {from_date, to_date} (YYYY-MM-DD); Default −7…+45 Tage."""
    if not flightops_configured():
        return jsonify({'ok': False, 'error': 'not_configured'}), 503
    if not _valid_access(token):
        return jsonify({'ok': False, 'error': 'not_connected'}), 401
    body = request.get_json(silent=True) or {}
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    today = _dt.now(_tz.utc)
    # MOCK liefert nur das Beispiel-Fenster → dort default darauf, sonst echtes
    # Fenster −7…+45 Tage. Body kann beides überschreiben.
    if is_mock():
        fd = body.get('from_date') or _MOCK_WINDOW[0]
        td = body.get('to_date') or _MOCK_WINDOW[1]
    else:
        fd = body.get('from_date') or (today - _td(days=7)).strftime('%Y-%m-%d')
        td = body.get('to_date') or (today + _td(days=45)).strftime('%Y-%m-%d')
    resp = duty_events(token, fd, td)
    if resp is None:
        return jsonify({'ok': False, 'error': 'duty_events_failed'}), 502
    ics = duty_events_to_ics(resp)
    if not ics:
        return jsonify({'ok': True, 'events_count': 0, 'source': 'flightops',
                        'detail': 'no_events'}), 200
    try:
        import app as _app
        with _app.app.test_request_context(json={'ics_text': ics}):
            rv = _app.import_calendar_feed(token)
        resp_obj, status = (rv if isinstance(rv, tuple) else (rv, 200))
        payload = resp_obj.get_json() or {}
    except Exception as e:
        log.warning('[lh_flightops] import pipeline fail: %s', type(e).__name__)
        return jsonify({'ok': False, 'error': 'pipeline_failed'}), 500
    if status == 200 and payload.get('ok'):
        payload['source'] = 'flightops'
    return jsonify(payload), status


@lh_flightops_bp.route('/api/lh/flightops/ping', methods=['GET'])
def flightops_ping():
    """Diagnose (kein Secret): Konfig-Status + effektive URLs/Scope/Redirect."""
    return jsonify({
        'configured': flightops_configured(),
        'authorize_url': _AUTHORIZE_URL,
        'token_url': _TOKEN_URL,
        'base': _BASE,
        'scope': _SCOPE,
        'redirect_uri': _REDIRECT_URI,
    })


# Alle 9 Crew-Services (Konsole 2026-07-22) — der raw-Endpoint kann jeden davon
# für den EIGENEN Token abfragen (Verdrahtung/Diagnose, sobald Mock/PROD live).
FLIGHTOPS_SERVICES = (
    'COMMON_DUTY_EVENTS', 'COMMON_CREWLIST', 'COMMON_CREW_ROTATION',
    'COMMON_CHECK_IN_TIMES', 'COMMON_FLIGHT_LEG_DETAILS', 'COMMON_LANDING_REPORT',
    'COMMON_CREW_HOTEL_INFO', 'COMMON_AIRPORT_WEATHER', 'COMMON_SIMULATOR_CREWLIST',
)


# ── TEST-Umgebungs-Verifikation (self-contained Browser-Flow) ────────────────
# EINE URL: /testflow → echter Crew-Login (TEST, anonymisierte echte Daten) →
# /land tauscht Code→Token, zieht Duty Events und rendert sie. Custom-Scheme-
# Redirect (aerox://) scheitert in Safari; HTTPS-Redirect wird akzeptiert.
_TESTFLOW_REDIRECT = 'https://api.aerosteuer.de/api/lh/flightops/land'
# Umgebungs-Presets für den Verifikations-Flow (state-Präfix wählt die Env).
_TESTFLOW_ENVS = {
    'test': {'authorize': 'https://oauth-test.lufthansa.com/lhcrew/oauth/authorize',
             'token': 'https://oauth-test.lufthansa.com/lhcrew/oauth/token',
             'scope': 'https://cms.fra.dlh.de/publicCrewApiDev',
             'base': 'https://api-sandbox.lufthansa.com/v1/flight_operations/crew_services'},
    'prod': {'authorize': 'https://oauth.lufthansa.com/lhcrew/oauth/authorize',
             'token': 'https://oauth.lufthansa.com/lhcrew/oauth/token',
             'scope': 'https://cms.fra.dlh.de/publicCrewApi',
             'base': 'https://api.lufthansa.com/v1/flight_operations/crew_services'},
}


@lh_flightops_bp.route('/api/lh/flightops/testflow', methods=['GET'])
def flightops_testflow():
    """Startet den echten Crew-Login zur Verifikation. `?env=prod` = offizielle
    PROD-Endpoints, sonst TEST/Sandbox. Nach Login → /land."""
    if not flightops_configured():
        return 'not configured', 503
    env = 'prod' if (request.args.get('env') or '').lower() == 'prod' else 'test'
    cfg = _TESTFLOW_ENVS[env]
    verifier, challenge = _pkce_pair()
    state = f'tf{env}_' + secrets.token_urlsafe(14)
    _flow_put(state, verifier, 'TESTFLOW')
    q = urllib.parse.urlencode({
        'response_type': 'code', 'client_id': _KEY,
        'redirect_uri': _TESTFLOW_REDIRECT, 'scope': cfg['scope'],
        'state': state, 'code_challenge': challenge,
        'code_challenge_method': 'S256'})
    return redirect(f"{cfg['authorize']}?{q}")


@lh_flightops_bp.route('/api/lh/flightops/land', methods=['GET'])
def flightops_land():
    """Landeseite: Code→Token→Duty Events (TEST), rendert das JSON."""
    import html as _html
    def _page(title, body, status=200):
        return (f'<html><head><meta charset=utf-8><title>{title}</title></head>'
                '<body style="font-family:ui-monospace,monospace;background:#0b1020;'
                'color:#d6e6ff;padding:18px;line-height:1.4">' + body + '</body></html>',
                status, {'Content-Type': 'text/html; charset=utf-8'})
    code = request.args.get('code')
    state = request.args.get('state', '')
    if not code:
        return _page('FlightOps', f'<h2>Kein Code</h2><p>{_html.escape(request.args.get("error") or "")}</p>', 400)
    flow = _flow_take(state)
    if not flow:
        return _page('FlightOps', '<h2>Session abgelaufen</h2><p>Bitte /api/lh/flightops/testflow neu öffnen.</p>', 400)
    # Env aus dem state-Präfix (tfprod_ / tftest_) → richtiger Token-Endpoint + Base
    env = 'prod' if state.startswith('tfprod_') else 'test'
    cfg = _TESTFLOW_ENVS[env]
    body = urllib.parse.urlencode({
        'grant_type': 'authorization_code', 'code': code,
        'redirect_uri': _TESTFLOW_REDIRECT, 'client_id': _KEY,
        'code_verifier': flow['verifier']}).encode()
    req_t = urllib.request.Request(cfg['token'], data=body,
        headers={'Authorization': _basic_header(),
                 'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(req_t, timeout=15) as r:
            _tj = json.loads(r.read().decode('utf-8'))
        tok = {'access': _tj.get('access_token'), 'scope': _tj.get('scope')} if _tj.get('access_token') else None
    except urllib.error.HTTPError as e:
        return _page('FlightOps', f'<h2>Token-Austausch fehlgeschlagen ({env})</h2><pre>{_html.escape(e.read().decode("utf-8","ignore")[:400])}</pre>', 502)
    except Exception as ex:
        return _page('FlightOps', f'<h2>Token-Fehler: {type(ex).__name__}</h2>', 502)
    if not tok:
        return _page('FlightOps', '<h2>Kein Token erhalten</h2>', 502)
    from datetime import datetime as _dt, timedelta as _td
    today = _dt.utcnow()
    fd = (today - _td(days=20)).strftime('%Y-%m-%d') + 'Z'
    td = (today + _td(days=40)).strftime('%Y-%m-%d') + 'Z'
    url = cfg['base'] + '/COMMON_DUTY_EVENTS?' + urllib.parse.urlencode({'fromDate': fd, 'toDate': td})
    req = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + tok['access'], 'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            data = r.read().decode('utf-8')
    except urllib.error.HTTPError as e:
        data = e.read().decode('utf-8', 'ignore')
    except Exception as ex:
        data = '{"error":"%s"}' % type(ex).__name__
    try:  # serverseitig sichern, damit Miguel den Parser direkt verifizieren kann
        with open('/tmp/fo_testdata.json', 'w') as f:
            f.write(data)
    except Exception:
        pass
    return _page('FlightOps TEST',
                 '<h2>✅ Duty Events (TEST-Umgebung)</h2>'
                 '<p>Alles hat geklappt — der Text unten ist dein echter Roster. '
                 'Du musst nichts kopieren, ich hab ihn serverseitig.</p>'
                 '<pre style="white-space:pre-wrap;word-break:break-word;background:#111a30;padding:12px;border-radius:8px">'
                 + _html.escape(data[:20000]) + '</pre>')


@lh_flightops_bp.route('/api/lh/flightops/raw/<token>', methods=['POST'])
def flightops_raw(token):
    """Verdrahtung/Diagnose: roher Service-Call für den EIGENEN Token (POST,
    auth-gated). Body {service: 'COMMON_…', params: {…}}. Zeigt die echte
    Response-Shape, sobald Mock/PROD antwortet — dann werden die Feature-Parser
    (Crew-List/Hotel/Landing…) final verdrahtet."""
    if not flightops_configured():
        return jsonify({'ok': False, 'error': 'not_configured'}), 503
    if not _valid_access(token):
        return jsonify({'ok': False, 'error': 'not_connected'}), 401
    body = request.get_json(silent=True) or {}
    service = (body.get('service') or '').strip().upper()
    if service not in FLIGHTOPS_SERVICES:
        return jsonify({'ok': False, 'error': 'unknown_service',
                        'services': list(FLIGHTOPS_SERVICES)}), 400
    params = body.get('params') if isinstance(body.get('params'), dict) else {}
    return jsonify({'ok': True, 'service': service,
                    'response': service_get(token, service, params)})


@lh_flightops_bp.route('/api/lh/flightops/crewlist/<token>', methods=['POST'])
def flightops_crewlist(token):
    """„Wer fliegt mit" für ein Leg (COMMON_CREWLIST → normalisiert). Body
    {flight, date, dep, arr, access}. Parser gegen echte Shape verifiziert."""
    if not _valid_access(token):
        return jsonify({'ok': False, 'error': 'not_connected'}), 401
    b = request.get_json(silent=True) or {}
    resp = crew_list(token, b.get('flight'), b.get('date'), b.get('dep'),
                     b.get('arr'), b.get('access'))
    return jsonify({'ok': True, 'crew': parse_crew_list(resp)})


@lh_flightops_bp.route('/api/lh/flightops/hotel/<token>', methods=['POST'])
def flightops_hotel(token):
    """Layover-Hotel für eine Station (COMMON_CREW_HOTEL_INFO → normalisiert).
    Body {station, provider?}. Parser gegen echte Shape verifiziert."""
    if not _valid_access(token):
        return jsonify({'ok': False, 'error': 'not_connected'}), 401
    b = request.get_json(silent=True) or {}
    resp = crew_hotel(token, b.get('station'), b.get('provider'))
    return jsonify({'ok': True, 'hotels': parse_crew_hotel(resp),
                    'station': (b.get('station') or '').upper()})
