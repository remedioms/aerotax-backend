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


# ── State→Verifier-Store (kurzlebig, in-memory; Flow dauert Minuten) ─────────
_flow_lock = threading.Lock()
_flow_store = {}   # state -> (expires_at, {verifier, user_token})
_FLOW_TTL = 900


def _flow_put(state, verifier, user_token):
    now = time.time()
    with _flow_lock:
        _flow_store[state] = (now + _FLOW_TTL, {'verifier': verifier, 'user_token': user_token})
        # aufräumen
        for k in [k for k, v in _flow_store.items() if v[0] < now]:
            _flow_store.pop(k, None)


def _flow_take(state):
    now = time.time()
    with _flow_lock:
        hit = _flow_store.pop(state, None)
    if not hit or hit[0] < now:
        return None
    return hit[1]


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


# Das einzige Fenster, für das der MOCK Daten hat (dokumentiertes Beispiel).
_MOCK_WINDOW = ('2016-10-01', '2016-10-31')


# ── Duty Events → synthetisches ICS (reuse der Roster-Pipeline) ─────────────
_FLIGHT_CATS = {'flight', 'flight_other'}


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
            cat = (ev.get('eventCategory') or '').lower()
            etype = (ev.get('eventType') or '').lower()
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
            elif cat in ('abs', 'lic', 'duty', 'ground_event') or etype in ('briefing', 'ground_event'):
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
