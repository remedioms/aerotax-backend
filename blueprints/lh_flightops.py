"""Lufthansa FlightOps Crew API — MOCK-Client (Engine B, Gerüst, 2026-07-22).

Die SANKTIONIERTE Roster-Quelle (Duty Events/Rotation/Crew-Lists/Check-In/
Flight-Leg-Details/Landing-Reports/Crew-Hotel) für LH-/LCAG-Crews — ersetzt
mittelfristig die Calendar-Shares. Dieses Modul ist die verifizierbare
CLIENT-Hälfte (OAuth-Token + Drossel + Budget + Config, Muster wie
`lh_open_api`). Bewusst OHNE Response-Parser: die echte Mock-Response-Shape
ist noch nicht gesehen (Key-Status „waiting", Secret versteckt) — Parser →
Roster-Modell kommen erst gegen echte Mock-Daten (kein Raten).

Zugang (MOCK Plan, 2/sec · 5.000/Tag): OAuth2. Für den Mock sehr wahrscheinlich
client_credentials (wie die Open API); falls der Mock doch den vollen
Authorization-Code-Flow verlangt, wird `_token` entsprechend erweitert, sobald
die Konsolen-Details (Token-/Authorize-URL) bekannt sind. Alles env-gesteuert:
  LH_FLIGHTOPS_KEY / LH_FLIGHTOPS_SECRET   (client credentials)
  LH_FLIGHTOPS_TOKEN_URL                    (default = Standard-OAuth)
  LH_FLIGHTOPS_BASE                         (Mock-Base-URL — aus der Konsole)
Voll no-op ohne Key/Secret → Commit/Deploy immer sicher.
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
lh_flightops_bp = Blueprint('lh_flightops_bp', __name__)

_KEY = (os.environ.get('LH_FLIGHTOPS_KEY') or '').strip()
_SECRET = (os.environ.get('LH_FLIGHTOPS_SECRET') or '').strip()
_TOKEN_URL = (os.environ.get('LH_FLIGHTOPS_TOKEN_URL')
              or 'https://api.lufthansa.com/v1/oauth/token').strip()
# Mock-Base-URL kommt aus der Developer-Konsole — bis dahin leer (= no-op der
# Endpoint-Calls, Token-Teil funktioniert trotzdem zum Verifizieren).
_BASE = (os.environ.get('LH_FLIGHTOPS_BASE') or '').strip().rstrip('/')

# ── Token-Cache ──────────────────────────────────────────────────────────────
_tok_lock = threading.Lock()
_tok_val = None
_tok_exp = 0.0

# ── Drossel 2/sec + Tagesbudget 5.000 (Sicherheitsmarge 4.800) ───────────────
_rate_lock = threading.Lock()
_last_call = 0.0
_MIN_INTERVAL = 0.5            # 2 Calls/sec
_DAY_BUDGET = 4800
_day_window = 0               # epoch // 86400
_day_count = 0
_budget_warned_day = -1


def flightops_configured():
    """True nur mit Key+Secret (sonst voll no-op)."""
    return bool(_KEY and _SECRET)


def flightops_ready():
    """True wenn zusätzlich die Mock-Base-URL gesetzt ist (Endpoints callbar)."""
    return bool(flightops_configured() and _BASE)


def _token():
    """Client-Credentials-Access-Token, gecacht bis kurz vor Ablauf. None bei
    Fehler (z. B. Key noch „waiting" → invalid_client)."""
    global _tok_val, _tok_exp
    if not flightops_configured():
        return None
    now = time.time()
    with _tok_lock:
        if _tok_val and now < _tok_exp:
            return _tok_val
    body = urllib.parse.urlencode({
        'client_id': _KEY, 'client_secret': _SECRET,
        'grant_type': 'client_credentials'}).encode()
    req = urllib.request.Request(
        _TOKEN_URL, data=body,
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
    except urllib.error.HTTPError as e:
        log.warning('[lh_flightops] token HTTP %s (Key evtl. noch waiting/Secret falsch)', e.code)
        return None
    except Exception as e:
        log.warning('[lh_flightops] token fail: %s', type(e).__name__)
        return None


def _budget_ok():
    """2/sec-Drossel + Tagesbudget. False (geloggt) wenn Tagesbudget erschöpft."""
    global _last_call, _day_window, _day_count, _budget_warned_day
    now = time.time()
    day = int(now // 86400)
    with _rate_lock:
        if day != _day_window:
            _day_window = day
            _day_count = 0
        if _day_count >= _DAY_BUDGET:
            if _budget_warned_day != day:
                log.warning('[lh_flightops] Tagesbudget %d erreicht — pausiere bis morgen', _DAY_BUDGET)
                _budget_warned_day = day
            return False
        wait = _MIN_INTERVAL - (now - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.time()
        _day_count += 1
        return True


def flightops_get(path, params=None):
    """Authentifizierter GET gegen die Mock-Base-URL → dict/list oder None.
    `path` z. B. '/flight-operations/dutyEvents/...'. Wirft nie. No-op (None)
    solange Base-URL/Creds fehlen — genau das, was ein sicheres Gerüst tut."""
    if not flightops_ready():
        return None
    tok = _token()
    if not tok:
        return None
    if not _budget_ok():
        return None
    url = _BASE + path
    if params:
        url += ('&' if '?' in url else '?') + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url, headers={'Authorization': 'Bearer ' + tok,
                      'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        if e.code not in (404,):
            log.warning('[lh_flightops] GET %s -> HTTP %s', path.split('?')[0], e.code)
        return None
    except Exception as e:
        log.warning('[lh_flightops] GET %s -> %s', path.split('?')[0], type(e).__name__)
        return None


@lh_flightops_bp.route('/api/lh/flightops/ping', methods=['GET'])
def flightops_ping():
    """Diagnose (kein Secret, keine Daten): zeigt Konfig-/Token-Status, damit
    man nach dem Secret-Setzen + Freischaltung die Anbindung prüfen kann."""
    tok = _token()
    return jsonify({
        'configured': flightops_configured(),
        'ready': flightops_ready(),          # + Base-URL gesetzt
        'base_set': bool(_BASE),
        'token_ok': bool(tok),               # True erst wenn Key aktiv + Secret stimmt
        'note': ('Key-Status „waiting" bzw. Secret fehlt → token_ok=false erwartet, '
                 'bis LH freischaltet.'),
    })
