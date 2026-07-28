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

# ── LH-Bürodienst-Hauscodes (Owner 2026-07-26: „B4 = Office") ───────────────
# Im CRS-Handbuch stehen diese Codes NICHT (dort ist Bürodienst DS_F/DS_M/DS_S)
# — sie sind hausintern.
#
# MARKER-VERTRAG mit iOS (`Models/RosterEventClassifier.swift`) — beide Seiten
# MÜSSEN identisch klassifizieren. Owner-Entscheid 26.07.2026 (LH-Kabinencrew):
#   · Bürodienst = Token `B` + GENAU EINE Ziffer 1–9, also B1…B9,
#   · als EIGENSTÄNDIGES Token nach Split des „·"-Segments an Nicht-Alpha-
#     numerik — `B45` und `B455` zünden NICHT, kein Präfix-Match,
#   · ein Segment mit einem solchen Token ist BODEN-DIENST-BEWEIS → der Tag ist
#     NIE frei,
#   · Klassifikation = Office: Dienst ohne Sektoren, erhöht keinen Freitage-
#     Zähler und erzeugt keine Blockstunden.
#
# EINE AUSNAHME, aus dem CRS-Handbuch belegt — darf NICHT eingesammelt werden
# (deshalb `[1-9]`, nicht `\d`):
#   · nacktes `B` = Betriebsunfall → Abwesenheit (2,9 LSW/Tag, reduziert den
#     Freitage-Anspruch). Kein Bürodienst.
#
# `B1` IST Bürodienst (Owner-Entscheid 27.07.2026). Das CRS-Handbuch führt B1
# als Teilzeit-VERTRAGSART („Mini Flex", 26,29 %) — der Prod-Bestand ist aber
# eindeutiger: 74 von 84 B1-Tagen tragen LHs EIGENES Label „Office Day (B1)".
# B1 ist offenbar beides; im Tages-Kontext gilt Bürodienst.
LH_OFFICE_DAY_CODE_RE = re.compile(r'^B[1-9]$')

# Ein Segment in seine alphanumerischen Tokens zerlegen. IDENTISCH in
# app.py:_summary_has_ground_duty — divergierende Trenner hiessen divergierende
# Klassifikation.
_TOKEN_SPLIT = r'[^A-Z0-9ÄÖÜ]+'


def is_office_day_code(token):
    """True für die Bürodienst-Hauscodes B1…B9 (ganzes Token, GROSS)."""
    return bool(LH_OFFICE_DAY_CODE_RE.match((token or '').strip().upper()))


def segment_has_office_code(segment_upper):
    """True wenn ein '·'-Segment ein eigenständiges B1…B9-Token trägt."""
    return any(is_office_day_code(t)
               for t in re.split(_TOKEN_SPLIT, segment_upper or ''))

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
def _tokens_load(user_token, fresh=False):
    """FlightOps-Tokens {access, refresh, expires_at, scope} für einen AeroX-User.

    `fresh=True` für die absichtlichen Zweit-Lesungen des Refreshers: dort
    ist der ganze Zweck, den Stand zu sehen, den ein ANDERER Prozess gerade
    geschrieben hat (Token-Rotation). Ein Request-Memo darf da nicht
    dazwischen — sonst stirbt der Grant-Race-Schutz still und der Verlierer
    schreibt seinen verbrannten Refresh über den frischen des Gewinners."""
    try:
        import app as _app
        prof = ((_app._profile_load(user_token, fresh=fresh) or {})
                .get('profile') or {})
        t = prof.get('flightops_tokens')
        t = dict(t) if isinstance(t, dict) else {}
        # Rotations-Notfallspeicher: hängt der Mirror noch am konsumierten
        # RT (Save nach Rotation war fehlgeschlagen), ist die Prozess-Kopie
        # die Wahrheit — sonst würde der stale Mirror-RT beim nächsten
        # Refresh die Familie per Reuse-Detection verbrennen.
        pend = _rotated_pending_take(user_token, t)
        return pend if pend is not None else t
    except Exception:
        return {}


def _tokens_mirror_raw(user_token):
    """Durabler Mirror-Stand OHNE Pending-Overlay — für Supersede-Checks
    (»hängt der Mirror noch am konsumierten RT oder hat inzwischen ein
    Re-Login/anderer Stand gewonnen?«). Immer fresh, nie Memo."""
    try:
        import app as _app
        prof = ((_app._profile_load(user_token, fresh=True) or {})
                .get('profile') or {})
        t = prof.get('flightops_tokens')
        return dict(t) if isinstance(t, dict) else {}
    except Exception:
        return {}


def _tokens_disk_mirror(user_token, tokens):
    """Best-effort-Disk-Kopie NACH bestätigtem SB-Write (der atomare Merge
    berührt die Disk nicht). Nur Lese-Cache für degradierte SB-Phasen —
    rotiert wird bei SB-Ausfall ohnehin nie (fail-closed)."""
    try:
        import app as _app
        full = dict(_app._profile_load_from_disk(user_token) or {})
        prof = dict(full.get('profile') or {})
        prof['flightops_tokens'] = tokens
        full.update({'token': user_token, 'profile': prof})
        p = _app._user_profile_path(user_token)
        if p:
            _app._atomic_write_json(p, full)
    except Exception:
        pass


def _tokens_save(user_token, tokens):
    """Token-Stand DB-BESTÄTIGT persistieren. True NUR bei bestätigtem Write.

    ASYMMETRIE-VERTRAG (Grant-Burn #4, 26.07.2026): ein verpasster oder
    vertagter Refresh kostet Minuten Datenfrische — ein Refresh-Token-Reuse
    kostet die ganze Token-Familie und zwingt den User zum Neu-Login. Deshalb
    zählt hier ausschließlich ein von Supabase bestätigter Write als Erfolg;
    die Disk ist reiner Lese-Cache (der 26.07.-Massen-Burn entstand u.a.,
    weil ein Disk-only-»Erfolg« als Persistenz durchging und der Rückgabewert
    ignoriert wurde).

    Primärpfad: atomarer Top-Level-Merge NUR des Keys `flightops_tokens`
    (RPC profile_metadata_merge) — nie der ganze Profil-Blob. Gegenstück:
    app._profile_save_to_supabase strippt den Key aus JEDEM generischen
    Profil-Save (Writer-Flag), damit Crew-Cache/Location/pk-Writes nie eine
    stale Token-Kopie über eine frische Rotation mergen können.
    Ohne SB (Dev/Tests) gilt die Disk via _profile_save als Wahrheit."""
    try:
        import app as _app
        writer = getattr(_app, '_FLIGHTOPS_TOKEN_WRITER', None)
        if writer is not None:
            writer.active = True
        try:
            if getattr(_app, 'SB_AVAILABLE', False):
                if _app._profile_metadata_merge_sb(
                        user_token, {'flightops_tokens': tokens}):
                    _tokens_disk_mirror(user_token, tokens)
                    return True
                # Merge traf keine Row (Erstverbindung/Row fehlt) oder war
                # transient: voller Profil-Save, danach Readback als
                # Bestätigung — Rotationen laufen praktisch immer über den
                # Merge, dieser Pfad ist der seltene Anlage-Fall.
                pf = _app._profile_load(user_token) or {}
                prof = dict(pf.get('profile') or {})
                prof['flightops_tokens'] = tokens
                if not _app._profile_save(user_token, prof):
                    return False
                cur = _tokens_mirror_raw(user_token)
                return ((cur.get('refresh') or '')
                        == (tokens.get('refresh') or ''))
            # Dev/Tests ohne SB: single-process, Disk ist die Wahrheit.
            pf = _app._profile_load(user_token) or {}
            prof = dict(pf.get('profile') or {})
            prof['flightops_tokens'] = tokens
            return bool(_app._profile_save(user_token, prof))
        finally:
            if writer is not None:
                writer.active = False
    except Exception as e:
        log.warning('[lh_flightops] token_save_fail: %s', type(e).__name__)
        return False


# ── Rotations-Notfallspeicher (Grant-Burn #4, Massen-Burn 26.07.2026) ────────
# LH rotiert den Refresh-Token bei JEDEM Refresh und revoziert bei Reuse die
# ganze Familie. Schlägt der Profil-Save NACH einer erfolgreichen Rotation fehl
# (SB-Latenz/-Ausfall — genau das passierte fleet-weit am 26.07. unter dem
# Supabase-Client-Registry-Leak), ist der neue RT das EINZIGE lebende Exemplar
# der Familie: der Mirror zeigt weiter den KONSUMIERTEN RT, und der nächste
# Refresh verbrennt den Grant deterministisch. Der Save nach Rotation ist darum
# der wertvollste Write des Systems: hart retryen, und wenn ALLES scheitert,
# den Stand im Prozess parken — _tokens_load serviert ihn weiter und versucht
# bei jeder Gelegenheit erneut zu persistieren.
_rotated_pending = {}            # user_token → {'tokens', 'consumed_rt', 'ts'}
_rotated_pending_lock = threading.Lock()
_ROTATED_SAVE_RETRIES = 4
_ROTATED_SAVE_BACKOFF_S = 0.5
_ROTATED_PENDING_MAX_AGE_S = 24 * 3600


def _rotated_pending_take(user_token, mirror_tokens):
    """Geparkten Rotations-Stand gegen den Mirror abgleichen. Gibt die
    Prozess-Wahrheit zurück (dict) wenn der Mirror stale ist (zeigt noch den
    konsumierten RT bzw. ist leer), sonst None; verwirft die Kopie, sobald
    ein FREMDER (neuerer) Stand im Mirror steht. Versucht en passant erneut
    zu persistieren."""
    with _rotated_pending_lock:
        pend = _rotated_pending.get(user_token)
    if not pend:
        return None
    if time.time() - (pend.get('ts') or 0) > _ROTATED_PENDING_MAX_AGE_S:
        with _rotated_pending_lock:
            _rotated_pending.pop(user_token, None)
        return None
    mirror_rt = (mirror_tokens or {}).get('refresh') or ''
    ours = (pend.get('tokens') or {}).get('refresh') or ''
    if mirror_rt and mirror_rt not in (pend.get('consumed_rt') or '', ours):
        # Jemand anders hat inzwischen erfolgreich rotiert UND persistiert —
        # dessen Stand ist neuer, unsere Kopie ist Geschichte.
        with _rotated_pending_lock:
            _rotated_pending.pop(user_token, None)
        return None
    # Mirror hängt noch am konsumierten RT → unsere Kopie ist die Wahrheit.
    if _tokens_save(user_token, pend['tokens']):
        log.info('[lh_flightops] rotation-nachsave gelungen token=%s',
                 (user_token or '')[:8])
        with _rotated_pending_lock:
            _rotated_pending.pop(user_token, None)
    return dict(pend['tokens'])


def _save_rotated_cas(user_token, consumed_rt, tokens):
    """Atomarer Compare-and-Swap-Save nach Rotation (RPC flightops_save_rotated,
    Migration 20260727): schreibt server-seitig NUR, wenn der durable Stand
    noch am konsumierten RT hängt → 'saved'. Hängt er an einem FREMDEN
    (Re-Login währenddessen / anderer Prozess) → 'superseded' — dann nichts
    überschreiben. None = RPC nicht verfügbar (Migration fehlt / SB-Hiccup):
    Caller nutzt den Merge-Fallback mit Supersede-Readback."""
    try:
        import app as _app
        ok, data = _app._social_rpc_call('flightops_save_rotated', {
            'p_token': user_token, 'p_consumed': consumed_rt or '',
            'p_tokens': tokens})
        if not ok:
            return None
        if isinstance(data, list) and data:
            data = (next(iter(data[0].values()), None)
                    if isinstance(data[0], dict) else data[0])
        return data if data in ('saved', 'superseded', 'no_row') else None
    except Exception:
        return None


def _tokens_save_rotated(user_token, consumed_rt, tokens):
    """Persist NACH erfolgreicher LH-Rotation — der wertvollste Write des
    Systems, darf nie still scheitern. Primär CAS-RPC (atomar, DB-bestätigt,
    überschreibt nie einen fremden neueren Stand), Fallback bestätigter Merge
    mit Supersede-Readback; Retry mit Backoff; scheitert alles, wird der
    Stand geparkt (_rotated_pending) statt verloren zu gehen. True = Stand
    durabel gesichert ODER bewusst verworfen (fremder neuerer Stand);
    False = nur noch im Prozess geparkt."""
    delay = _ROTATED_SAVE_BACKOFF_S
    for attempt in range(_ROTATED_SAVE_RETRIES):
        cas = _save_rotated_cas(user_token, consumed_rt, tokens)
        if cas == 'saved':
            if attempt:
                log.warning('[lh_flightops] rotation-save nach %d Versuchen '
                            'gelungen token=%s', attempt + 1,
                            (user_token or '')[:8])
            _tokens_disk_mirror(user_token, tokens)
            with _rotated_pending_lock:
                _rotated_pending.pop(user_token, None)
            return True
        if cas == 'superseded':
            log.warning('[lh_flightops] rotation-save superseded (Re-Login/'
                        'fremder neuerer Stand) — Kopie verworfen token=%s',
                        (user_token or '')[:8])
            with _rotated_pending_lock:
                _rotated_pending.pop(user_token, None)
            return True
        if cas in (None, 'no_row'):
            # RPC (noch) nicht da: Supersede von Hand prüfen, dann
            # bestätigter Merge-Save (schafft im no_row-Fall auch die Row).
            cur_rt = (_tokens_mirror_raw(user_token) or {}).get('refresh') or ''
            if cur_rt and cur_rt not in (consumed_rt or '',
                                         tokens.get('refresh') or ''):
                log.warning('[lh_flightops] rotation-save superseded (readback)'
                            ' — Kopie verworfen token=%s', (user_token or '')[:8])
                with _rotated_pending_lock:
                    _rotated_pending.pop(user_token, None)
                return True
            if _tokens_save(user_token, tokens):
                if attempt:
                    log.warning('[lh_flightops] rotation-save nach %d Versuchen'
                                ' gelungen token=%s', attempt + 1,
                                (user_token or '')[:8])
                with _rotated_pending_lock:
                    _rotated_pending.pop(user_token, None)
                return True
        time.sleep(delay)
        delay = min(delay * 2, 4.0)
    with _rotated_pending_lock:
        _rotated_pending[user_token] = {
            'tokens': dict(tokens), 'consumed_rt': consumed_rt or '',
            'ts': time.time()}
    log.error('[lh_flightops] ROTATION-SAVE FEHLGESCHLAGEN token=%s rt8=%s — '
              'neuer Refresh-Token nur noch im Prozess (wird bei jedem '
              'Load nachpersistiert; Refresher rotiert diesen Grant NICHT '
              'weiter, bis der Nachsave bestätigt ist)', (user_token or '')[:8],
              _rt8(tokens.get('refresh')))
    return False


def _basic_header():
    raw = f'{_KEY}:{_SECRET}'.encode()
    return 'Basic ' + base64.b64encode(raw).decode()


def _exchange_code(code, verifier):
    """authorization_code → Token-Dict oder None. Client-Secret via Basic-Header.
    WICHTIG: _token_request liefert seit der Refresh-Härtung (tok, err) — hier
    NUR das Token-Dict weitergeben (Live-500 am 23.07.: das Tupel wanderte bis
    in _tokens_save/tok.get und crashte den Exchange NACH erfolgreichem LH-Login)."""
    body = urllib.parse.urlencode({
        'grant_type': 'authorization_code', 'code': code,
        'redirect_uri': _REDIRECT_URI, 'client_id': _KEY,
        'code_verifier': verifier}).encode()
    tok, _err = _token_request(body)
    return tok


# ── STRUKTURELLER REUSE-SCHUTZ: genau EIN Refresher im ganzen System ─────────
# Architektur-Umbau 2026-07-27 nach Grant-Burn #4 (254/571 Grants tot, weil
# unter SB-Degradierung ein blinder Guard-Fallback trotzdem refreshte und ein
# unbestätigter Save den konsumierten RT stehen ließ). Vorher durfte JEDER
# Prozess refreshen (3 Web-Worker × Threads, Poll-Cron, On-Demand-Import) und
# vier Generationen von Guards (Lock, Grace-Reload, Claim-RPC, Nonce-Soft-
# Guard) verkleinerten nur die Racefläche. Jetzt ist sie WEG:
#   · Rotieren darf ausschließlich der Refresher-Loop (Poll-Container,
#     LH_FLIGHTOPS_REFRESHER=1, flock → ein Thread im ganzen System).
#   · _refresh() — die EINZIGE Stelle, die grant_type=refresh_token sendet —
#     verweigert jeden Aufruf außerhalb dieses Threads (Choke-Point-Gate).
#   · Alle anderen Pfade LESEN nur Access-Tokens; abgelaufen ⇒ Vormerkung +
#     Cache/»kommt gleich«, nie selbst rotieren.
_REFRESHER_THREAD_ID = [None]    # threading.get_ident() des Rotations-Threads


def _refresher_may_rotate():
    return (_REFRESHER_THREAD_ID[0] is not None
            and _REFRESHER_THREAD_ID[0] == threading.get_ident())


def _refresh(refresh_token):
    """→ (Token-Dict|None, err|None). Doku (Token_Endpoint): für
    grant_type=refresh_token sind client_id/redirect_uri/code/code_verifier
    explizit NICHT nötig — nur Basic-Header + diese zwei Body-Params.

    CHOKE-POINT-GATE: nur der Refresher-Thread darf hier durch. Jeder andere
    Aufrufer (Web-Worker, Cron-Import, künftiger neuer Code-Pfad) bekommt
    eine laute Verweigerung statt eines LH-Calls — ein RT-Reuse ist damit
    strukturell unmöglich, egal wie degradiert die Infrastruktur ist."""
    if not _refresher_may_rotate():
        log.error('[lh_flightops] REFRESH VERWEIGERT — Aufruf außerhalb des '
                  'Refresher-Threads (struktureller Reuse-Schutz) rt8=%s',
                  _rt8(refresh_token))
        return None, {'http': None, 'oauth': None, 'fatal': False,
                      'refused': True}
    body = urllib.parse.urlencode({
        'grant_type': 'refresh_token', 'refresh_token': refresh_token}).encode()
    # MIT ZÄHLEN (2026-07-26): der Refresh ist ein echter HTTP-Call gegen LH
    # und lief im `refresh-all`-Cron bis zu 1× pro User und Lauf — er war im
    # Zähler unsichtbar und hätte das FlightOps-Volumen fast verdoppelt.
    _flightops_budget_inc('/oauth_refresh')
    return _token_request(body)


# OAuth-Fehler, bei denen der Grant DEFINITIV tot ist — nur dann Re-Login
# verlangen. Doku (Token_Endpoint) nennt invalid_grant/invalid_client;
# `invalid_token` LIVE beobachtet (2026-07-23, 401 beim Refresh mit stalen
# Sandbox-Tokens nach dem Prod-Key-Wechsel) — ebenfalls toter Grant. Alles
# andere (service_unavailable=Wartung, 403 Rate-Limit, 5xx, Netz) ist
# transient: Tokens BEHALTEN, später erneut.
_FATAL_OAUTH_ERRORS = ('invalid_grant', 'invalid_client', 'invalid_token')


def _token_request(body):
    """POST an den Token-Endpoint → (Token-Dict|None, err|None).
    err = {'http': int|None, 'oauth': str|None, 'fatal': bool}."""
    req = urllib.request.Request(
        _TOKEN_URL, data=body,
        headers={'Authorization': _basic_header(),
                 'Content-Type': 'application/x-www-form-urlencoded',
                 'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read().decode('utf-8'))
        if not d.get('access_token'):
            return None, {'http': 200, 'oauth': d.get('error'), 'fatal': False}
        return {
            'access': d['access_token'],
            'refresh': d.get('refresh_token'),
            'scope': d.get('scope'),
            'expires_at': time.time() + max(60, int(d.get('expires_in') or 3600) - 60),
        }, None
    except urllib.error.HTTPError as e:
        oauth = None
        try:
            oauth = (json.loads(e.read().decode('utf-8', 'ignore')) or {}).get('error')
        except Exception:
            pass
        fatal = (e.code in (400, 401)) and (oauth in _FATAL_OAUTH_ERRORS)
        log.warning('[lh_flightops] token HTTP %s oauth=%s fatal=%s',
                    e.code, oauth, fatal)
        return None, {'http': e.code, 'oauth': oauth, 'fatal': fatal}
    except Exception as e:
        log.warning('[lh_flightops] token %s', type(e).__name__)
        return None, {'http': None, 'oauth': None, 'fatal': False}


def _notify_relogin(user_token):
    """KEIN Push mehr bei totem Grant (Owner 2026-07-25: „ich will nicht
    einmal eine Push — die App wirkt sonst unzuverlässig"). Der Relogin-Weg
    ist die „Neu verbinden"-Marken-Karte im Dienstplan-Screen (+ Mehr);
    needs_relogin-Flag + Status-Endpoint tragen den Zustand. Funktion bleibt
    als Hook (Tests prüfen weiterhin, dass sie im Race-/Transient-Fall NICHT
    gerufen wird)."""
    log.info('[lh_flightops] grant tot, relogin nötig (kein Push, Owner): %s',
             user_token[:8])


# Pro-User-Refresh-Lock (in-process): der Refresher-Loop ist single-threaded,
# das Lock ist seit dem Umbau 2026-07-27 reine Tiefenverteidigung (falls je
# ein zweiter Aufrufer _refresher_refresh_grant erreicht, serialisiert es —
# und das Choke-Point-Gate in _refresh verweigert ihm den LH-Call ohnehin).
_user_refresh_locks = {}
_user_refresh_locks_guard = threading.Lock()


def _user_refresh_lock(user_token):
    with _user_refresh_locks_guard:
        lk = _user_refresh_locks.get(user_token)
        if lk is None:
            lk = threading.Lock()
            _user_refresh_locks[user_token] = lk
        return lk


# Vormerkliste »Access-Token abgelaufen« — Web-Worker/Cron-Import melden hier
# nur AN (in-memory, gedeckelt); GEHÖRT wird sie ausschließlich vom Refresher-
# Loop im selben Prozess (Poll-Container). In den Web-Containern ist sie ein
# bewusster No-Op-Briefkasten: dort refresht NIEMAND, auch nicht auf Wunsch.
_refresh_wanted = set()
_refresh_wanted_lock = threading.Lock()
_REFRESH_WANTED_CAP = 2000


def _refresh_wanted_add(user_token):
    try:
        with _refresh_wanted_lock:
            if len(_refresh_wanted) < _REFRESH_WANTED_CAP:
                _refresh_wanted.add(user_token)
    except Exception:
        pass


def _refresh_wanted_drain():
    with _refresh_wanted_lock:
        s = set(_refresh_wanted)
        _refresh_wanted.clear()
    return s


# ── DEMAND-SET der LAZY ROTATION (Quota-Diät 2026-07-28) ─────────────────────
# Seit der Lazy-Rotation ist »der AT läuft gleich ab« ALLEIN kein Grund mehr zu
# rotieren (siehe _refresher_due). Rotiert wird, wenn jemand den Grant WIRKLICH
# braucht — genau das steht hier drin. Befüllt von (a) flightops_import, wenn
# ein User-Request auf einen abgelaufenen AT läuft (direkt im selben Prozess +
# best-effort Cross-Container-Poke, s. _rotate_poke_remote), (b) der
# Demand-Vorlauf-Phase in _refresh_all_work.
#
# THREAD-SAFETY: `set.add`/`set.discard`/`in` sind unter dem GIL atomar; ein
# zusätzliches Lock brächte nichts, weil hier nie ein zusammengesetzter
# Read-Modify-Write nötig ist (anders als bei _refresh_wanted, das komplett
# geleert wird). Der Deckel schützt gegen einen entarteten Poker (kaputter
# Client, Angreifer mit Poll-Secret): bei Überlauf wird der Poke IGNORIERT —
# nicht geleert, sonst würde ein Flood die echten Demands verdrängen. Der
# Refresher räumt jeden rotierten Token wieder heraus, ein voller Deckel ist
# also selbstheilend.
_refresher_demand = set()
_REFRESHER_DEMAND_CAP = 500


def _refresher_demand_add(user_token):
    """Grant als »wird jetzt gebraucht« vormerken. Wirft nie."""
    try:
        if user_token and len(_refresher_demand) < _REFRESHER_DEMAND_CAP:
            _refresher_demand.add(user_token)
            return True
    except Exception:
        pass
    return False


# Der Refresher lebt NUR im Poll-Container — ein Demand aus dem Web-Container
# (dort landen die User-Requests) muss also über das interne Netz. Default-URL
# ist der Compose-Servicename; per Env überschreibbar.
_POLL_INTERNAL_URL = (os.environ.get('AEROX_POLL_INTERNAL_URL')
                      or 'http://aerotax-poll:8081').rstrip('/')
_ROTATE_POKE_TIMEOUT_S = 2.0


def _rotate_poke_remote(user_token):
    """Best-effort Cross-Container-Poke an den Refresher (POST rotate-poke).
    STRENG optional: schlägt er fehl (kein Poll-Container, DNS, Timeout,
    Secret falsch), bleibt alles wie vorher — der User bekommt weiter seine
    503-Antwort und der nächste Retry bzw. der Keepalive heilt. Wirft nie und
    blockiert den Request maximal _ROTATE_POKE_TIMEOUT_S."""
    try:
        body = json.dumps({'token': user_token}).encode()
        headers = {'Content-Type': 'application/json'}
        secret = (os.environ.get('ADSB_POLL_SECRET') or '').strip()
        if secret:
            headers['X-Poll-Secret'] = secret
        req = urllib.request.Request(
            _POLL_INTERNAL_URL + '/api/internal/flightops/rotate-poke',
            data=body, headers=headers)
        with urllib.request.urlopen(req, timeout=_ROTATE_POKE_TIMEOUT_S) as r:
            r.read()
        return True
    except Exception as e:
        log.info('[lh_flightops] rotate-poke best-effort fehlgeschlagen: %s',
                 type(e).__name__)
        return False


def _access_state(user_token):
    """(state, access) — state ∈ 'ok' | 'pending' | 'disconnected'.

    LESEPFAD OHNE JEDEN REFRESH (Single-Refresher-Architektur, 2026-07-27):
    Web-Worker, MQTT-Fanout und Cron-Import bekommen hier nur, was durabel
    da ist. Abgelaufener Access-Token ⇒ 'pending' + Vormerkung für den
    Refresher — der Caller degradiert weich (Last-Good-Cache bzw. »Daten
    kommen gleich«), er rotiert NIEMALS selbst.

    ASYMMETRIE (der Grund für alles hier): ein verpasster Refresh kostet
    Minuten Datenfrische — ein Refresh-Token-Reuse kostet die ganze
    LH-Token-Familie und den User (Zwangs-Relogin). »Kein LH-Call« ist in
    jedem Zweifelsfall die richtige Antwort."""
    t = _tokens_load(user_token)
    if not t.get('access') or t.get('needs_relogin'):
        return 'disconnected', None
    if time.time() < (t.get('expires_at') or 0):
        return 'ok', t['access']
    _refresh_wanted_add(user_token)
    return 'pending', None


def _valid_access(user_token):
    """Gültigen Access-Token LESEN. None wenn nicht verbunden ODER der AT
    gerade abgelaufen ist (dann übernimmt der zentrale Refresher; Details
    und Zustands-Differenzierung: _access_state). Refresht selbst NIE."""
    return _access_state(user_token)[1]


# Wartezeit vor dem endgültigen Toter-Grant-Flag (Fatal-Pfad) — deckt den
# Deploy-Übergang ab, in dem Alt-Code-Container theoretisch noch selbst
# rotieren; danach reine Vorsicht. Tests patchen auf 0.
_FATAL_GRACE_SEC = 2.0

# TTL des server-seitigen Claim-Guards (p_ttl des RPC flightops_claim_refresh).
_REFRESH_GUARD_SEC = 15.0

# Refresher-Vorlauf: Grants werden rotiert, sobald der AT in weniger als
# dieser Spanne abläuft — Web-Worker sehen dadurch praktisch nie 'pending'.
_REFRESH_AHEAD_S = 15 * 60


def _refresher_refresh_grant(user_token):
    """EINE Grant-Rotation — ausschließlich vom Refresher-Thread aufgerufen
    (das Gate sitzt zusätzlich in _refresh selbst). Gibt einen Status-String
    für die Lauf-Statistik zurück.

    FAIL-CLOSED OHNE AUSNAHME (Leitplanke aus Grant-Burn #4): kein gewonnenes
    Claim, Claim-Infrastruktur nicht erreichbar oder Save nicht DB-bestätigt
    ⇒ KEIN LH-Call bzw. keine weitere Rotation. Ein verpasster Refresh kostet
    Minuten Datenfrische; ein RT-Reuse kostet die Token-Familie und den User —
    diese Asymmetrie entscheidet jeden Zweifelsfall hier gegen den LH-Call."""
    with _user_refresh_lock(user_token):
        with _rotated_pending_lock:
            parked = user_token in _rotated_pending
        if parked:
            # Unpersistierte Rotation im Prozess: NIE weiterrotieren (eine
            # Kette unpersistierter RTs stirbt mit dem Prozess = Familientod).
            # Nur den Nachsave versuchen — _rotated_pending_take tut das en
            # passant beim Load.
            _tokens_load(user_token, fresh=True)
            with _rotated_pending_lock:
                still = user_token in _rotated_pending
            return 'save_pending' if still else 'save_healed'
        t = _tokens_load(user_token, fresh=True)
        if t.get('needs_relogin'):
            return 'dead_flagged'
        rt = t.get('refresh')
        if not rt:
            return 'no_refresh'
        if t.get('access') and ((t.get('expires_at') or 0) - time.time()) > _REFRESH_AHEAD_S:
            return 'fresh'
        claimed = _claim_refresh_sb(user_token, rt)
        if claimed is None:
            # Claim-Infra down/degradiert ⇒ FAIL-CLOSED: kein LH-Call. Genau
            # hier refreshte der 26.07.-Soft-Guard-Fallback »trotzdem« und
            # verbrannte 254 Familien. Wenn SB kein Claim bestätigen kann,
            # könnte es auch den Rotations-Save nicht bestätigen — der
            # Refresh wird vertagt, der nächste Tick versucht es erneut.
            log.warning('[fo-refresher] claim nicht verfügbar -> Refresh '
                        'vertagt (fail-closed) token=%s', (user_token or '')[:8])
            return 'skipped_claim_unavailable'
        if claimed is False:
            # Fremder Claim auf diesem RT: praktisch nur im Deploy-Übergang
            # möglich, solange irgendwo noch Alt-Code läuft, der selbst
            # rotiert. Dessen Ergebnis wird beim nächsten Tick einfach
            # gelesen — hier passiert bewusst NICHTS.
            log.info('[fo-refresher] fremdes claim (Alt-Code/Übergang) '
                     'token=%s', (user_token or '')[:8])
            return 'skipped_claim_foreign'
        nt, err = _refresh(rt)
        if nt:
            # Rotiert der Server den Refresh-Token, den NEUEN persistieren;
            # sonst den bewährten behalten (Rotation ist LH-seitig
            # undokumentiert). Der neue RT gilt erst als »aktiv«, wenn der
            # Save DB-bestätigt ist — sonst bleibt er geparkt und dieser
            # Grant wird bis zum bestätigten Nachsave nicht mehr angefasst.
            nt['refresh'] = nt.get('refresh') or rt
            return ('rotated' if _tokens_save_rotated(user_token, rt, nt)
                    else 'rotated_parked')
        if err and err.get('refused'):
            return 'refused'
        if err and err.get('fatal'):
            # GRACE-RELOAD (Deploy-Übergang): hat ein Alt-Code-Container
            # parallel rotiert, ist unser fatal nur der Race-Verlierer —
            # Grant lebt, nichts flaggen.
            time.sleep(_FATAL_GRACE_SEC)
            cur = _tokens_mirror_raw(user_token)
            if (cur.get('refresh') or '') != rt:
                return 'raced'
            # Wirklich toter Grant: auf dem FRISCHEN Stand flaggen (nie einen
            # alten Snapshot zurückschreiben). FORENSIK-Log (Massen-Burn
            # 26.07.: die Burns waren in den Logs unauffindbar): rt8 +
            # Minuten seit dem letzten erfolgreichen Save — damit ist die
            # Mechanik aus den Logs beweisbar statt aus SB-Timestamps
            # rekonstruiert.
            _last_ok_min = (time.time() - ((cur.get('expires_at') or 0) - 3540)) / 60.0
            log.error('[lh_flightops] GRANT-BURN token=%s rt8=%s http=%s '
                      'oauth=%s last_ok_vor_min=%.0f',
                      user_token[:8], _rt8(rt),
                      err.get('http'), err.get('oauth'), _last_ok_min)
            cur['needs_relogin'] = True
            cur['relogin_at'] = time.time()
            cur.pop('access', None)
            _tokens_save(user_token, cur)
            _notify_relogin(user_token)
            return 'dead'
        return 'transient'


def _rt8(rt):
    """Kurzer, log-sicherer Fingerabdruck eines Refresh-Tokens."""
    return hashlib.sha256((rt or '').encode()).hexdigest()[:8]


def _claim_refresh_sb(user_token, rt):
    """Atomares Claim via RPC flightops_claim_refresh. True = Zuschlag
    gewonnen (Guard server-seitig gesetzt), False = ein anderer Prozess hält
    das Claim auf DIESEM RT (nur im Deploy-Übergang mit Alt-Code relevant),
    None = RPC nicht verfügbar ⇒ der Refresher vertagt FAIL-CLOSED (seit dem
    Umbau 2026-07-27 gibt es KEINEN Soft-Guard-Fallback mehr — der blinde
    Fallback war der Massen-Burn #4)."""
    if not rt:
        return None
    try:
        import app as _app
        ok, data = _app._social_rpc_call('flightops_claim_refresh', {
            'p_token': user_token, 'p_refresh': rt, 'p_rt8': _rt8(rt),
            'p_ttl': _REFRESH_GUARD_SEC})
        if not ok:
            # LAST-HÄRTUNG (25.07. abends, 33 Grants verbrannt): unter Cron-/
            # Abend-Last kann der RPC-Call transient scheitern — der stille
            # Soft-Guard-Fallback öffnet dann genau das Reuse-Race, das der
            # Claim schließen soll. EINMAL kurz retryen und den Fallback
            # SICHTBAR loggen (vorher: lautlos → im Log unauffindbar).
            time.sleep(0.5)
            ok, data = _app._social_rpc_call('flightops_claim_refresh', {
                'p_token': user_token, 'p_refresh': rt, 'p_rt8': _rt8(rt),
                'p_ttl': _REFRESH_GUARD_SEC})
        if not ok:
            log.warning('[lh_flightops] claim_rpc_unavailable -> Refresh wird '
                        'vertagt (fail-closed) token=%s', user_token[:8])
            return None
        if isinstance(data, list) and data:
            data = (next(iter(data[0].values()), None)
                    if isinstance(data[0], dict) else data[0])
        if isinstance(data, bool):
            return data
        return None
    except Exception as e:
        log.warning('[lh_flightops] claim_rpc_fail: %s', type(e).__name__)
        return None


def flightops_connected(user_token):
    t = _tokens_load(user_token)
    return bool(t.get('access') and not t.get('needs_relogin'))


# ── Mock-API-Call ────────────────────────────────────────────────────────────
def _flightops_budget_inc(path):
    """LH-FlightOps-Call im PROZESS-ÜBERGREIFENDEN Stundenzähler buchen.
    (Sichtbarkeit statt Schätzen — Owner 2026-07-26: „erst messen".) Der
    FlightOps-Key ist ein EIGENER LH-Key, darum eigener Schlüssel-Präfix
    `lhfo:`; zweiter Schlüssel je Service für die Verbraucher-Aufschlüsselung.
    Wirft nie und darf den API-Pfad niemals blockieren.

    ZUSÄTZLICH seit 2026-07-28 (Quota-Diät): derselbe Call wird in einem
    TAGES-Zähler `lhfoD:<YYYYMMDD>` gebucht. Der FlightOps-Key hat neben dem
    Stundenlimit ein Tageskontingent (6.000 lt. Owner) — ohne Tages-Sicht ist
    ein Dauerlauf knapp unter der Stundengrenze rechnerisch bei 16.800/Tag und
    reißt das Tageslimit lange vor der Stunde. budget_inc hängt die STUNDE
    automatisch an, deshalb hier der Key-genaue Zwilling budget_inc_key."""
    try:
        from blueprints.lh_open_api import budget_inc, budget_inc_key
        svc = re.sub(r'[^A-Za-z_]', '', (path or '').lstrip('/'))[:40] or 'unknown'
        budget_inc('lhfo', svc)
        budget_inc_key('lhfoD:' + time.strftime('%Y%m%d', time.gmtime()))
    except Exception:
        pass


# ── KEY-BUDGET-GATE (Quota-403-Vorfall 27.07. ~21 UTC) ──────────────────────
# Der FlightOps-Key hat 1.000 Calls/h; die Stunden 16/18/20 UTC lagen bei
# 1.052–1.183 → LH antwortete pauschal 403 — und zwar AUCH auf die frischen
# Roster-Abrufe direkt nach „Neu verbinden": ausgerechnet die Re-Login-
# Heilung der needs_relogin-User schlug fehl („Es hat nicht geklappt"),
# während Hintergrund-Syncs das Kontingent weiter leerten. Anders als der
# Open-API-Key hatte lhfo KEIN eigenes Budget-Gate.
# Regel: Hintergrund (refresh-all) stoppt bei 700/h und lässt damit Headroom
# für interaktive Flows (Connect-Erstimport, manuelles „Jetzt aktualisieren"),
# die bis 950/h dürfen; darüber ist Schluss BEVOR LH selbst 403t (die 403s
# zählen sonst weiter aufs Kontingent und verlängern die Sperre). Der
# oauth_refresh des Ein-Refreshers läuft UNGEGATET (Grant-Hygiene schlägt
# Roster-Frische; ~170/h passen in den Headroom). Zähler-Memo ist 60 s
# (_rot_budget_memo) — Überschwinger ≤ ~85 Calls (0,7-s-Takt) sind in den
# Puffern (950+85+Refresher < 1000 knapp; 700er-Grenze völlig entspannt).
_LHFO_HOUR_BACKGROUND_CEILING = 700
_LHFO_HOUR_INTERACTIVE_CEILING = 950

# ── TAGES-DECKEL (Quota-Diät 2026-07-28) ────────────────────────────────────
# Der Key hat zusätzlich ein TAGESkontingent von 6.000 Calls (Owner). Das
# Stunden-Gate allein schützt davor NICHT: 700/h Hintergrund sind 16.800/Tag.
# Gleiche Zwei-Stufen-Logik wie stündlich — Hintergrund stoppt früher und
# lässt den Rest als Headroom für interaktive Flows (Connect-Erstimport,
# „Jetzt aktualisieren", Re-Login-Heilung). Auch hier gilt: VORHER stoppen,
# denn die 403s des Gateways zählen selbst aufs Kontingent und verlängern
# die Sperre nur.
_LHFO_DAY_BACKGROUND_CEILING = 5200
_LHFO_DAY_INTERACTIVE_CEILING = 5800

# Tagesstand-Memo (analog _rot_budget_memo): _budget_key_used geht auf
# Supabase, der Tagesstand ändert sich träge — 120 s reichen für einen
# Deckel, der erst ab ~87% des Kontingents greift.
_lhfo_day_memo = [0.0, 0]        # (ts, used)
_LHFO_DAY_MEMO_S = 120.0


def _lhfo_day_used():
    """Aktueller lhfo-TAGESstand (memoisiert) oder 0, wenn nicht ermittelbar."""
    now = time.time()
    if (now - _lhfo_day_memo[0]) < _LHFO_DAY_MEMO_S:
        return _lhfo_day_memo[1]
    try:
        from blueprints.aerox_data_blueprint import _budget_key_used
        used = int(_budget_key_used(
            'lhfoD:' + time.strftime('%Y%m%d', time.gmtime())) or 0)
    except Exception:
        used = 0
    _lhfo_day_memo[0], _lhfo_day_memo[1] = now, used
    return used


def _api_get(user_token, path, params=None, interactive=False):
    access = _valid_access(user_token)
    if not access:
        return None
    _used = _rot_hour_used()
    _ceiling = (_LHFO_HOUR_INTERACTIVE_CEILING if interactive
                else _LHFO_HOUR_BACKGROUND_CEILING)
    if _used >= _ceiling:
        log.warning('[lh_flightops] lhfo-Stundenbudget %s >= %s — %s-Call %s '
                    'übersprungen', _used, _ceiling,
                    'interaktiver' if interactive else 'Hintergrund', path)
        return None
    _dused = _lhfo_day_used()
    _dceiling = (_LHFO_DAY_INTERACTIVE_CEILING if interactive
                 else _LHFO_DAY_BACKGROUND_CEILING)
    if _dused >= _dceiling:
        log.warning('[lh_flightops] lhfo-Tagesbudget %s >= %s — %s-Call %s '
                    'übersprungen', _dused, _dceiling,
                    'interaktiver' if interactive else 'Hintergrund', path)
        return None
    _flightops_budget_inc(path)
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


def _as_list(v):
    """LH-Known-Issue (developer.lufthansa.com/docs, Known_Issues): JSON-Arrays
    mit GENAU EINEM Element werden als Skalar gerendert ({'names': 'alice'} bzw.
    ein nacktes Objekt statt [Objekt]). Jeder Listen-Zugriff auf LH-Responses
    muss deshalb hierdurch — sonst bricht der Parser exakt an dem Tag, an dem
    ein User nur EINEN rosterDay/crewMember/Hotel-Eintrag hat."""
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def duty_events(user_token, from_date, to_date, interactive=False):
    """COMMON_DUTY_EVENTS für ein Zeitfenster → Response-Dict oder None.
    Datumsformat YYYY-MM-DDZ. HINWEIS: die MOCK-Umgebung liefert NUR für das
    dokumentierte Beispiel-Fenster (2016-10-01Z..2016-10-31Z) Daten; echte
    Fenster gehen erst gegen PROD. `interactive=True` = nutzerausgelöster
    Abruf (Connect-Erstimport, manuelles Aktualisieren) → höhere
    Budget-Grenze im Key-Gate (siehe _api_get)."""
    resp = _api_get(user_token, '/COMMON_DUTY_EVENTS',
                    {'fromDate': _date_z(from_date), 'toDate': _date_z(to_date)},
                    interactive=interactive)
    # Gateway-/Backend-Fehler kommen als {processingErrors:[…]} MIT 200/4xx/5xx —
    # nie als Duty-Events missdeuten.
    if isinstance(resp, dict) and resp.get('processingErrors'):
        try:
            e = (_as_list(resp['processingErrors']) or [{}])[0]
            if isinstance(e, dict) and isinstance(e.get('processingError'), dict):
                e = e['processingError']   # dokumentierte Wrapper-Shape
            log.warning('[lh_flightops] duty_events upstream %s: %s',
                        e.get('code'), (e.get('type') or '')[:60])
        except Exception:
            pass
        return None
    return resp


def is_mock():
    """True nur wenn die Base die MOCK-Umgebung ist (Pfad-Segment /mock, nur
    statische Beispiel-Daten). TEST/Sandbox (api-sandbox OHNE /mock) hat ECHTE
    anonymisierte Daten → NICHT als Mock behandeln (sonst würde das
    2016-Beispielfenster statt des echten Roster-Fensters genutzt)."""
    return '/mock' in _BASE.lower()


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
    for m in _as_list(resp.get('crewMembers')):
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
    for h in _as_list(resp.get('hotelInformation')):
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
    fälschlich Datumsfenster).

    NICHT die Hotel-Pickup-Zeit (live gemessen 2026-07-26, Owner-Frage
    „Pickup-Zeiten verschwunden seit FlightOps-Login"): `crewBusDeparture` ist
    der APRON-Bus vom Briefing zum Flieger, NICHT der Hotelbus. Belege aus zwei
    echten Responses —
      MUC (Homebase)  briefingBegin 08:30Z → security 08:57Z →
                      crewBusDeparture 09:02Z → boarding 09:40Z → STD 10:20Z
      BOM (Layover)   briefingBegin 18:35Z → security 19:05Z →
                      crewBusDeparture 19:12Z → paxOnBoard 19:37Z → STD 20:15Z
    Der Hotel-Pickup liegt real ~2:10–2:30 h VOR Abflug (myTime-Bestand:
    'Layover [PEK] … 10:55 LT Pickup PEK', 'Layover [EWR] … 18:40 LT Pickup
    EWR'), also klar VOR briefingBegin. Kein Feld DIESER Response trägt ihn.

    NACHTRAG 2026-07-27 (Owner-Fund, in PROD belegt): die Pickup-Zeit gibt es
    doch — nur in einem ANDEREN Service. `COMMON_CREW_ROTATION` liefert
    `legs[].pickupTime` (UTC) + `legs[].pickupTimeLT` + `legs[].hotelName`.
    Siehe parse_rotation_pickups/pickup_rotation_ids. Dieser Service hier bleibt
    trotzdem der falsche Ort dafür — `crewBusDeparture` ist und bleibt der
    Apron-Bus."""
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


# ── HOTEL-PICKUP aus COMMON_CREW_ROTATION ───────────────────────────────────
# Owner-Fund 2026-07-26, live in PROD belegt: die Hotel-Pickup-Zeit, die seit
# dem Umstieg auf den direkten FlightOps-Login fehlte, steckt NICHT in
# COMMON_CHECK_IN_TIMES (dort ist `crewBusDeparture` der APRON-Bus, siehe
# check_in_times-Docstring), sondern in COMMON_CREW_ROTATION als
# `rotations[].shifts[].legs[].pickupTime` (UTC) — plus `pickupTimeLT`
# (Ortszeit, fertig) in der aktuellen PROD-Shape.
#
# Zwei echte Messungen (Owner):
#   RN 169929  LH443 DTW→FRA  Abflug 26.07. 20:00Z  Briefing 19:00Z
#              pickupTime 18:00Z  pickupTimeLT 14:00 (DTW)   → 2:00 h vor Abflug
#   RN 171012  LH743 KIX→MUC  Abflug 29.07. 00:30Z  Briefing 28.07. 23:30Z
#              pickupTime 28.07. 21:50Z  pickupTimeLT 06:50 (KIX) → 2:40 h
# Das trifft die myTime-Semantik exakt (dort real: 'Layover [PEK] … 10:55 LT
# Pickup PEK'): VOR dem Briefing, ~2:00–2:40 h vor Abflug.
#
# SEMANTIK, die den Bau bestimmt (alles am echten Bestand geprüft):
#  * gesetzt NUR am Layover-Rückflug; am Homebase-Abflug immer None.
#  * erst ~1 Tag vorher gefüllt — für Umläufe in 6–10 Tagen stand überall None,
#    und LH trägt spät nach und löscht später wieder (Florian). Deshalb enger
#    Horizont + kurze Cache-TTL, und ein späteres Verschwinden muss den Marker
#    wieder verschwinden lassen (kein Sticky-Cache).
#  * das `hotel`-Flag ist UNBRAUCHBAR (False trotz gesetztem hotelName) — nur
#    den Namen auswerten, nie das Flag.
#  * `rotationId` liegt bereits in `eventAttributes` der Duty-Events-Response,
#    die der Import ohnehin holt → kein Extra-Call zum Auffinden.
#  * EIN Rotations-Call liefert alle Shifts und Legs → 1 Call pro UMLAUF.
#
# GOTCHA in der LH-Shape: das Abflugdatum-Feld heißt `depatureDate` (LH-Typo,
# so in der echten Response und in tests/fixtures/flightops_COMMON_CREW_ROTATION
# .json). Beide Schreibweisen werden gelesen.
_ROT_DEP_KEYS = ('depatureDate', 'departureDate')


def _rot_hhmm_lt(v):
    """`pickupTimeLT` → 'HH:MM' oder None. LH liefert das Feld je nach Service-
    Version als ISO-Ortszeit ('2026-07-29T06:50:00') ODER als nackte Uhrzeit
    ('06:50' / '0650'). Nur plausible Zeiten (h<24, m<60) — nie raten."""
    s = str(v or '').strip()
    if not s:
        return None
    if 'T' in s:
        s = s.split('T', 1)[1]
    m = re.match(r'^(\d{1,2}):(\d{2})', s) or re.match(r'^(\d{2})(\d{2})$', s)
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    return f'{hh:02d}:{mm:02d}' if 0 <= hh <= 23 and 0 <= mm <= 59 else None


def parse_rotation_pickups(resp):
    """COMMON_CREW_ROTATION-Response → {leg_key: {...}}. Pure/testbar.

    leg_key = (flightDesignator, departureAirport, arrivalAirport, 'YYYYMMDD'
    des Abflugs in UTC). Zusätzlich wird derselbe Eintrag unter dem
    flugnummernlosen Schlüssel ('', dep, arr, day8) abgelegt — die Flugnummer
    aus `eventDetails` der Duty-Events kann Deadhead-Präfixe/Leerzeichen
    tragen, die Stations-/Datums-Kombination ist der robustere Anker.

    ABER (adversarialer Review 2026-07-27, an echten Roster-Formen bewiesen):
    der flugnummernlose Schlüssel ist NUR eindeutig, wenn die Route an diesem
    Tag GENAU EINMAL vorkommt. Bei MUC-FRA-MUC-FRA (alltäglicher LH-Umlauf)
    kollidiert er, und der Pickup des einen Legs landete am anderen — sichtbar
    als „PU 15:00" an einem Tag, der um 07:00 Ortszeit beginnt, während das Leg
    MIT echtem Pickup leer blieb. Mehrfach belegte Routen-Schlüssel werden
    deshalb wieder ENTFERNT: dann gilt nur der Treffer mit Flugnummer.

    Wert: {'pickup_utc': 'YYYY-MM-DDTHH:MM:SSZ', 'pickup_lt': 'HH:MM'|None,
           'hotel': str|None, 'station': 'XXX'}
    Legs OHNE pickupTime erscheinen NICHT — kein Eintrag heißt kein Event."""
    out = {}
    if not isinstance(resp, dict):
        return out
    _route_seen = {}          # ('', dep, arr, day8) → Zahl der Legs der Route
    # ── hotelName ↔ pickupTime hängen an VERSCHIEDENEN Legs ──────────────────
    # Live gemessen 2026-07-27 (10 PROD-Rotationen, 24 Legs): 8 Legs trugen
    # pickupTime, 8 trugen hotelName — und es war NIE dasselbe Leg.
    #   FRA→BOS  hotelName 'Hyatt Regency Boston'   pickupTime None
    #   BOS→MUC  hotelName None                     pickupTime gesetzt
    # Logisch: der Name hängt am HINFLUG (arrivalAirport = Hotel-Station), der
    # Pickup am RÜCKFLUG (departureAirport = Hotel-Station). Ein Parser, der nur
    # Legs MIT pickupTime ansieht, verliert deshalb JEDEN Hotelnamen (erste
    # Fassung tat genau das und lieferte durchgehend hotel=None). Also: erst
    # eine Station→Name-Karte über ALLE Legs bauen, dann an den Pickup hängen.
    # Echte Namen, keine Codes ('Radisson Blu Hamburg', 'Altis Grand Hotel',
    # 'Mercure Warszawa Grand') — die Repo-Fixture mit 'H9941671' ist das
    # Doku-Beispiel, nicht die PROD-Shape.
    _hotel_at = {}
    for rot in _as_list(resp.get('rotations')):
        if not isinstance(rot, dict):
            continue
        for sh in _as_list(rot.get('shifts')):
            if not isinstance(sh, dict):
                continue
            for lg in _as_list(sh.get('legs')):
                if not isinstance(lg, dict):
                    continue
                hn = str(lg.get('hotelName') or '').strip()
                stn = str(lg.get('arrivalAirport') or '').upper().strip()
                if hn and len(stn) == 3:
                    _hotel_at.setdefault(stn, hn)
    for rot in _as_list(resp.get('rotations')):
        if not isinstance(rot, dict):
            continue
        for sh in _as_list(rot.get('shifts')):
            if not isinstance(sh, dict):
                continue
            for lg in _as_list(sh.get('legs')):
                if not isinstance(lg, dict):
                    continue
                dep = str(lg.get('departureAirport') or '').upper().strip()
                arr = str(lg.get('arrivalAirport') or '').upper().strip()
                if len(dep) != 3 or len(arr) != 3:
                    continue
                dd = ''
                for k in _ROT_DEP_KEYS:
                    if str(lg.get(k) or '').strip():
                        dd = str(lg[k]).strip()
                        break
                if len(dd) < 10:
                    continue
                day8 = dd[:10].replace('-', '')
                rkey = ('', dep, arr, day8)
                # JEDES Leg der Route zählt für die Eindeutigkeit — auch die
                # ohne pickupTime, sonst bliebe ein Duplikat unentdeckt.
                _route_seen[rkey] = _route_seen.get(rkey, 0) + 1
                pu = str(lg.get('pickupTime') or '').strip()
                if not pu:
                    continue
                flt = re.sub(r'\s', '',
                             str(lg.get('flightDesignator') or '').upper())
                # Hotelname der ABFLUG-Station (= Hotel-Station des Rückflugs);
                # notfalls der am Leg selbst. `hotel`/`airportRoom` bewusst
                # ignoriert — das Flag ist False trotz gesetztem Namen.
                hn = (_hotel_at.get(dep)
                      or str(lg.get('hotelName') or '').strip() or None)
                val = {'pickup_utc': pu, 'pickup_lt': _rot_hhmm_lt(lg.get('pickupTimeLT')),
                       'hotel': hn, 'station': dep}
                out[(flt, dep, arr, day8)] = val
                out.setdefault(rkey, val)
    for rkey, n in _route_seen.items():
        if n > 1:
            out.pop(rkey, None)
    return out


def _pickup_for_leg(pickups, flt, frm, to, st):
    """Pickup-Eintrag für EIN Duty-Flug-Event oder None. `st` ist das bereits
    ICS-formatierte DTSTART ('YYYYMMDDTHHMMSSZ'), sein Datum ist der Anker.
    Reihenfolge: exakter Treffer mit Flugnummer → Treffer nur über Route+Datum
    (die Flugnummer aus `eventDetails` kann Deadhead-Präfixe tragen) → ±1 Tag,
    aber NUR mit Flugnummer (Flug + Route + Nachbartag ist eindeutig; ohne
    Flugnummer würde ein täglicher Umlauf auf dieselbe Route falsch matchen)."""
    if not isinstance(pickups, dict) or not pickups:
        return None
    st, frm, to = str(st or ''), str(frm or ''), str(to or '')
    if len(st) < 8 or len(frm) != 3 or len(to) != 3:
        return None
    day8 = st[:8]
    for key in ((flt, frm, to, day8), ('', frm, to, day8)):
        hit = pickups.get(key)
        if hit:
            return hit
    if not flt:
        return None
    from datetime import datetime as _d, timedelta as _td
    try:
        base = _d.strptime(day8, '%Y%m%d')
    except Exception:
        return None
    for delta in (-1, 1):
        hit = pickups.get((flt, frm, to,
                           (base + _td(days=delta)).strftime('%Y%m%d')))
        if hit:
            return hit
    return None


# Vorlauf-Fenster wie iOS RosterLabels.maxLeadWindowMinutes und
# crew_live_state._PRE_LEAD_MAX_MIN. Bewusst DERSELBE Wert: was der Konsument
# still verwirft, darf gar nicht erst in den Roster geschrieben werden.
_PICKUP_LEAD_MAX_MIN = 6 * 60


def _pickup_lead_ok(pickup_utc, dep_utc):
    """True, wenn der Pickup 0…6 h VOR dem Plan-Abflug liegt. Beides sind echte
    UTC-Instanzen, der Vergleich ist deshalb DST- und zeitzonenfrei — anders als
    die HH:MM-Rückrechnung im Konsumenten. Bei Parse-Fehler False (fail-closed:
    lieber kein Marker als ein falscher)."""
    try:
        from datetime import datetime as _d, timezone as _tz
        a = _d.fromisoformat(str(pickup_utc or '').replace('Z', '+00:00'))
        b = _d.fromisoformat(str(dep_utc or '').replace('Z', '+00:00'))
        if a.tzinfo is None:
            a = a.replace(tzinfo=_tz.utc)
        if b.tzinfo is None:
            b = b.replace(tzinfo=_tz.utc)
    except Exception:
        return False
    lead_min = (b - a).total_seconds() / 60.0
    return 0 <= lead_min <= _PICKUP_LEAD_MAX_MIN


def _berlin_day(iso_utc):
    """UTC-ISO → 'YYYY-MM-DD' im EUROPE/BERLIN-Kalender, sonst None. Genau der
    Bucket, den der Feed-Import für ein zeitbehaftetes VEVENT bildet (der
    Stations-Lokal-Rebucket in app.py läuft nur für SWISS/ITA, nicht für LH)."""
    s = str(iso_utc or '').strip()
    if not s:
        return None
    try:
        from datetime import datetime as _d, timezone as _tz
        from zoneinfo import ZoneInfo
        dt = _d.fromisoformat(s.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_tz.utc)
        return dt.astimezone(ZoneInfo('Europe/Berlin')).strftime('%Y-%m-%d')
    except Exception:
        return None


# Horizont für Rotations-Calls. BEGRÜNDUNG mit gemessenen Zahlen (26.07.2026,
# echter lhfo-Zähler via /api/ax/lh-quota, Stunde 2026072622 = ein voller
# refresh-all-Lauf):
#   FlightOps-Key-Kontingent          1.000 Calls/h  (+ 5/s)
#   gemessen in der refresh-all-Stunde  478 Calls    (262 COMMON_DUTY_EVENTS
#                                                    + 216 oauth_refresh)
#   → freier Kopfraum                   522 Calls/h
#   verbundene Grants                   227 (455 Profile, 228 needs_relogin)
# Ein Rotations-Call fällt nur für einen User mit Layover-Rückflug IM Horizont
# an, und ALLE seine Umläufe gehen in EINEN Call (bis 6 RNs pro Request) →
# höchstens 1 Call pro User pro Lauf. P2 hat bei 36 h 25–35 Calls/Lauf gemessen.
# 30 h statt 36 h, weil der Wert erst ~24 h vorher überhaupt gefüllt wird: 30 h
# deckt die Füllzeit + 6 h Reserve, und weil refresh-all alle 2 h läuft, sieht
# jeder Umlauf den Wert danach noch ~15-mal — ein größerer Horizont kauft keinen
# Nutzen, nur Calls. Erwartete Last also ~20–30 Calls/h = ~5 % des freien
# Kopfraums, ~3 % des Kontingents. Die echte Zahl steht nach dem Deploy als
# lhfo:<stunde>:common_crew_rotation im Zähler.
_ROT_PICKUP_HORIZON_H = 30
# Rückblick: ein Rückflug, der GERADE abgeht, soll seinen Marker behalten
# (die Kachel „Dienst heute" liest ihn bis zum Abflug).
_ROT_PICKUP_BACK_H = 3
# COMMON_CREW_ROTATION nimmt bis zu 6 Rotationsnummern pro Request
# (crew_rotation baut RN, RN_2 … RN_6). Deshalb ist die Obergrenze pro Import
# genau 6: mehr Umläufe kosten NICHTS extra, solange sie in einen Call passen,
# und mehr als 6 Layover-Rückflüge in 30 h sind operativ unmöglich (die Kappe
# schützt gegen kaputte Payloads, nicht gegen echte Roster).
_ROT_RN_PER_CALL = 6
_ROT_MAX_PER_IMPORT = _ROT_RN_PER_CALL
# NOTBREMSE: oberhalb dieses lhfo-Stundenstands werden Rotations-Calls
# komplett übersprungen. Der Roster (COMMON_DUTY_EVENTS) ist das Kernprodukt
# und darf NIE an einem Pickup-Nice-to-have verhungern. Gemessener Normalstand
# ist 478/h, die Kappe greift also nur bei echter Anomalie.
_ROT_LHFO_HOUR_CEILING = 800


def pickup_rotation_ids(resp, now=None, horizon_h=_ROT_PICKUP_HORIZON_H):
    """Duty-Events-Response → Liste der rotationIds, für die ein Pickup-Wert
    plausibel VORLIEGEN kann und GEBRAUCHT wird. Pure/testbar.

    Ein Leg qualifiziert, wenn ALLE vier Bedingungen gelten:
      1. Flug-Event mit startTime im Fenster [now − 3 h, now + horizon_h].
      2. Es startet an einer Station, an der laut Roster eine HOTEL-Nacht
         liegt (±2 Tage) — nur dort gibt es einen Hotel-Pickup. Am
         Homebase-Abflug ist pickupTime laut LH immer None, ein Call dorthin
         wäre garantiert verschwendet.
      3. Das Event trägt eine rotationId in eventAttributes.
      4. Ergebnis dedupliziert und auf _ROT_MAX_PER_IMPORT gekappt.
    Reihenfolge: früheste Abflüge zuerst (die brauchen den Wert am dringendsten)."""
    if not isinstance(resp, dict):
        return []
    from datetime import datetime as _d, timedelta as _td, timezone as _tz
    now = now or _d.now(_tz.utc)
    lo = (now - _td(hours=_ROT_PICKUP_BACK_H)).strftime('%Y-%m-%dT%H:%M:%SZ')
    hi = (now + _td(hours=max(1, int(horizon_h)))).strftime('%Y-%m-%dT%H:%M:%SZ')

    hotel_days = []          # (day8, station)
    legs = []                # (start_iso, station, day8, rotation_id)
    for d in _as_list(resp.get('rosterDays')):
        if not isinstance(d, dict):
            continue
        day8 = (d.get('day') or '')[:10].replace('-', '')
        for ev in _as_list(d.get('events')):
            if not isinstance(ev, dict):
                continue
            cat = re.sub(r'[_\s]', '', (ev.get('eventCategory') or '').lower())
            et = re.sub(r'[_\s]', '', (ev.get('eventType') or '').lower())
            frm = (ev.get('startLocation') or '').upper().strip()
            to = (ev.get('endLocation') or '').upper().strip()
            if et == 'hotel' or cat == 'hotel':
                stn = to or frm
                if len(day8) == 8 and len(stn) == 3:
                    hotel_days.append((day8, stn))
                continue
            if not (et == 'flight' or cat in _FLIGHT_CATS):
                continue
            s = (ev.get('startTime') or '').strip()
            if not (s and len(frm) == 3 and lo <= s <= hi):
                continue
            # _as_list wegen der LH-Known-Issue (Ein-Element-Arrays kommen als
            # Skalar) — hier defensiv in BEIDE Richtungen: käme
            # eventAttributes je als [{…}], wäre `isinstance(ea, dict)` False
            # und das Feature ginge komplett und lautlos dunkel.
            rn = None
            for ea in _as_list(ev.get('eventAttributes')):
                if isinstance(ea, dict) and ea.get('rotationId') not in (None, ''):
                    rn = ea['rotationId']
                    break
            if rn is None:
                continue
            legs.append((s, frm, s[:10].replace('-', ''), str(rn)))

    def _near_hotel(stn, day8):
        """Hotel-Nacht derselben Station binnen ±2 Kalendertagen? Immer über
        echte Datumsarithmetik — ein numerischer Vergleich der day8-Strings
        bricht über Monats-/Jahresgrenzen (20260801 vs 20260731)."""
        for hd, hs in hotel_days:
            if hs != stn:
                continue
            try:
                a = _d.strptime(hd, '%Y%m%d')
                b = _d.strptime(day8, '%Y%m%d')
            except Exception:
                continue
            if abs((a - b).days) <= 2:
                return True
        return False

    out = []
    for s, stn, day8, rn in sorted(legs):
        if rn in out or not _near_hotel(stn, day8):
            continue
        out.append(rn)
        if len(out) >= _ROT_MAX_PER_IMPORT:
            break
    return out


# Rotations-Cache (prozess-lokal). TTL absichtlich KURZ: LH trägt pickupTime
# spät nach UND löscht sie später wieder — ein langer Cache würde einen
# gelöschten Pickup als Geist weiterleben lassen. 30 min ist kürzer als der
# 2-h-refresh-all-Takt, jeder Cron-Lauf holt also frisch (das ist die Zahl, die
# im Zähler auftaucht), während wiederholte On-Demand-Syncs desselben Users
# innerhalb der halben Stunde gratis sind.
_ROT_CACHE_TTL_S = 1800.0
# Cache-Key ist (user_token, rotation_id), NICHT die rotationId allein
# (adversarialer Review 2026-07-27): die COMMON_CREW_ROTATION-Response ist
# ROLLENSPEZIFISCH — die Repo-Fixture zeigt `briefingBeginCoc: null` neben
# `briefingBeginCab: …` und rein kabinenseitige `CAB_*`-attributes. Cockpit und
# Kabine haben auf Langstrecke regelmäßig verschiedene Hotels und damit
# verschiedene Pickup-Zeiten; ein Cache nur über die rotationId hätte den
# Kollegen der anderen Rolle deren Werte serviert.
_rot_cache = {}                  # (user_token, rotation_id) → (ts, pickups)
_rot_cache_lock = threading.Lock()
# Stundenstand-Memo: _budget_key_used geht auf Supabase. Einmal pro Minute
# genügt für eine Notbremse und hält den Roster-Hot-Path netzfrei (sonst ~227
# zusätzliche SELECTs pro refresh-all-Lauf).
_rot_budget_memo = [0.0, 0]      # (ts, used)
_ROT_BUDGET_MEMO_S = 60.0


def _rot_hour_used():
    """Aktueller lhfo-Stundenstand (memoisiert) oder 0, wenn nicht ermittelbar."""
    now = time.time()
    if (now - _rot_budget_memo[0]) < _ROT_BUDGET_MEMO_S:
        return _rot_budget_memo[1]
    try:
        from blueprints.aerox_data_blueprint import _budget_key_used
        used = int(_budget_key_used(
            'lhfo:' + time.strftime('%Y%m%d%H', time.gmtime())) or 0)
    except Exception:
        used = 0
    _rot_budget_memo[0], _rot_budget_memo[1] = now, used
    return used


def rotation_pickups_for(user_token, rotation_ids):
    """rotationIds → gemergtes Pickup-Dict (siehe parse_rotation_pickups).
    Cache pro (Token, rotationId) mit kurzer TTL.

    KOSTEN: COMMON_CREW_ROTATION nimmt bis zu SECHS rotationIds pro Request
    (`RN`, `RN_2` … `RN_6`, siehe crew_rotation). Alle Cache-Misses eines
    Imports gehen deshalb in EINEN Call — nicht einen pro Umlauf. Das war die
    wichtigste Korrektur des adversarialen Reviews: die Variante mit einem Call
    pro rotationId riss bei parallelen Imports (ein Daemon-Thread pro Token)
    gemessen 24 Calls in ein 1-s-Fenster gegen das 5/s-Limit des Keys — und
    brauchte dafür noch ein sleep, das den Deploy-Drain verlängert. Ein Call
    pro User braucht keine Bremse.

    Wirft NIE — ohne Pickup-Daten muss der Roster-Import normal durchlaufen."""
    out = {}
    # Dedupe unter Erhalt der Reihenfolge — EIN Eintrag pro Umlauf, auch wenn
    # mehrere Legs desselben Umlaufs im Horizont liegen.
    ids, _seen = [], set()
    try:
        _src = list(rotation_ids or [])
    except TypeError:
        return out
    for r in _src:
        s = str(r or '').strip()
        if s and s not in _seen:
            _seen.add(s)
            ids.append(s)
    if not ids:
        return out
    now = time.time()
    misses = []
    with _rot_cache_lock:
        for rn in ids:
            hit = _rot_cache.get((user_token, rn))
            if hit and (now - hit[0]) < _ROT_CACHE_TTL_S:
                out.update(hit[1] or {})
            else:
                misses.append(rn)
    if not misses:
        return out
    # NOTBREMSE: steht der FlightOps-Key diese Stunde schon hoch, wird der
    # Pickup geopfert — nie der Roster.
    used = _rot_hour_used()
    if used >= _ROT_LHFO_HOUR_CEILING:
        log.warning('[lh_flightops] pickup: lhfo-Stundenstand %s >= %s -> '
                    'Rotations-Calls uebersprungen', used, _ROT_LHFO_HOUR_CEILING)
        return out
    batch = misses[:_ROT_RN_PER_CALL]
    try:
        # Gezählt wird automatisch in _api_get als
        # lhfo:<YYYYMMDDHH>:common_crew_rotation (eigenes Aufrufer-Label, im
        # Report /api/ax/lh-quota sichtbar) — EIN Zähl-Ereignis pro Batch.
        raw = crew_rotation(user_token, *batch)
    except Exception as e:
        log.warning('[lh_flightops] rotation %s: %s', batch, type(e).__name__)
        return out
    if not isinstance(raw, dict):
        # KEIN Negativ-Cache bei Transportfehler (adversarialer Review):
        # _api_get gibt bei HTTP 403/500/Timeout `None` zurück, es WIRFT nicht.
        # Ein solches None als „dieser Umlauf hat keinen Pickup" zu cachen hätte
        # den Marker bei einem einzigen LH-Schluckauf für 30 min gelöscht und
        # beim Wiederauftauchen eine erfundene „Dienstplan-Änderung" gepusht.
        # Nur eine echte, geparste Antwort darf den Cache füllen.
        log.warning('[lh_flightops] rotation %s: keine Antwort (kein '
                    'Negativ-Cache)', batch)
        return out
    got = parse_rotation_pickups(raw)
    # Ein geparstes, aber pickupfreies Ergebnis WIRD gecacht — sonst fragt jeder
    # Sync denselben Umlauf erneut ab. Die kurze TTL sorgt dafür, dass ein spät
    # nachgetragener Wert trotzdem ankommt.
    with _rot_cache_lock:
        for rn in batch:
            _rot_cache[(user_token, rn)] = (time.time(), got)
        if len(_rot_cache) > 4000:
            for k in sorted(_rot_cache, key=lambda k: _rot_cache[k][0])[:2000]:
                _rot_cache.pop(k, None)
    out.update(got or {})
    return out


# ── ZWEITE PICKUP-QUELLE: der myTime-/Kalender-Link ──────────────────────────
# Owner 2026-07-27 („die Pickup-Zeit wird nicht aus dem iCal-Kalender-Link
# geholt, wenn sie aus der primären Quelle fehlt"). Die Lücke ist belegt, nicht
# geraten — seit dem FlightOps-Login ist COMMON_CREW_ROTATION.pickupTime die
# EINZIGE Pickup-Quelle, und alle drei Wege zum Kalender-Link sind zu:
#   • Server-Refresh: app.py `_maybe_refresh_calendar_feed` steigt bei
#     `_flightops_active(token)` sofort aus (Quellen-Priorität).
#   • Geräte-Abruf: iOS `RosterFeedDeviceSync.runIfDue` steigt beim
#     `aerox.flightops.connected`-Flag sofort aus.
#   • Bestand: `_ics_events_to_briefings` (app.py) baut JEDEN vom Import
#     angefassten Tag frisch auf (REPLACE-NOT-ACCUMULATE) — ein früher aus dem
#     myTime-iCal gelesener Pickup-Marker überlebt den FlightOps-Import nicht.
# Ergebnis: liefert LH keinen `pickupTime` (Feld leer, Umlauf außerhalb
# `_ROT_PICKUP_HORIZON_H`, Quota-Notbremse), gibt es GAR KEINEN Pickup — obwohl
# er im myTime-Kalender steht.
#
# Diese Ebene hängt genau dann die Pickup-VEVENTs des Kalender-Links in das aus
# FlightOps erzeugte ICS, wenn die Primärquelle für den Tag nichts geliefert hat
# („primary wins"). Danach ist die Kette unverändert: der Feed-Import bucketed
# den Marker wie jeden myTime-Pickup, `crew_live_state.parse_pickup_hhmm` und
# iOS `RosterLabels.pickupTimeFromSummary` lesen ihn ohne eine Zeile Neu-Code.
_ICS_LEG_LOCATION_RE = re.compile(r'^[A-Z]{3}\s*[-–]\s*[A-Z]{3}$')


def _ics_stamp(iso_utc):
    """'YYYY-MM-DDTHH:MM:SSZ' → 'YYYYMMDDTHHMMSSZ' (ICS-Form), sonst None."""
    m = re.match(r'^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})',
                 str(iso_utc or '').strip())
    if not m:
        return None
    y, mo, d, hh, mi, ss = m.groups()
    return f'{y}{mo}{d}T{hh}{mi}{ss}Z'


def ical_pickup_candidates(ical_text):
    """Kalender-ICS-Text → [{'utc': 'YYYY-MM-DDTHH:MM:SSZ', 'summary': str}]
    für jedes Pickup-VEVENT. Pure/testbar, wirft nie.

    Der Summary bleibt WÖRTLICH („10:55 LT Pickup PEK" / „Pickup 1430") — er
    trägt die ORTSZEIT, und genau die liest der Konsument. Ganztags-Events
    (kein `start_iso`) und Summaries ohne plausible Zeit fallen raus; geraten
    wird nie."""
    out = []
    try:
        import app as _app
        from blueprints.crew_live_state import parse_pickup_hhmm
        events = _app._parse_ics_to_events(ical_text or '')
    except Exception as e:
        log.warning('[lh_flightops] ical-pickup parse: %s', type(e).__name__)
        return out
    for ev in (events or []):
        if not isinstance(ev, dict):
            continue
        summ = re.sub(r'[\r\n\t]+', ' ', str(ev.get('summary') or '')).strip()
        iso = str(ev.get('start_iso') or '').strip()
        if not summ or not _ics_stamp(iso):
            continue
        try:
            if not parse_pickup_hhmm(summ):
                continue
        except Exception:
            continue
        out.append({'utc': iso, 'summary': summ[:120]})
    return out


def merge_ical_pickups(ics, candidates):
    """Pickup-VEVENTs aus dem Kalender-Link in ein FlightOps-ICS einhängen —
    NUR für Roster-Tage, die die Primärquelle leer gelassen hat. Pure/testbar,
    wirft nie; ohne verwertbaren Kandidaten kommt das ICS unverändert zurück.

    Riegel — bewusst DIESELBEN wie im Primärpfad, denn ein Marker aus einer
    Zweitquelle darf nicht schwächer geprüft sein als einer aus der ersten:
      • ANKER-PFLICHT: es muss ein Flug-VEVENT geben, dessen Abflug 0…6 h NACH
        dem Pickup liegt (`_PICKUP_LEAD_MAX_MIN`). Ohne Anker kein Marker —
        sonst wandert der Pickup eines stornierten/verschobenen Umlaufs aus dem
        noch nicht nachgezogenen Kalender in den Roster.
      • PRIMARY WINS: trägt der Roster-Tag (Berlin-Bucket des Abflugs) bereits
        einen Pickup aus COMMON_CREW_ROTATION, passiert nichts.
      • MITTERNACHTS-WRAP wie im Primärpfad: liegt der Pickup in einem anderen
        Berlin-Tag als der Abflug, wird DTSTART auf den Abflug gezogen (die
        Wahrheit steckt im Summary, `pickup_utc_for_leg` rekonstruiert sie).
      • Höchstens EIN Pickup je Roster-Tag; bei mehreren Kandidaten gewinnt der
        SPÄTESTE plausible — dieselbe Regel wie in `pickup_utc_for_leg`."""
    if not ics or not candidates:
        return ics
    try:
        import app as _app
        from datetime import datetime as _d, timezone as _tz
        from blueprints.crew_live_state import parse_pickup_hhmm

        def _inst(iso):
            try:
                v = _d.fromisoformat(str(iso or '').replace('Z', '+00:00'))
                return v if v.tzinfo else v.replace(tzinfo=_tz.utc)
            except Exception:
                return None

        have_days = set()      # Berlin-Tage, die schon einen Pickup tragen
        deps = []              # (instant, iso) aller Flug-VEVENTs
        for ev in (_app._parse_ics_to_events(ics) or []):
            if not isinstance(ev, dict):
                continue
            iso = str(ev.get('start_iso') or '').strip()
            inst = _inst(iso)
            if inst is None:
                continue
            summ = str(ev.get('summary') or '')
            if parse_pickup_hhmm(summ):
                d = _berlin_day(iso)
                if d:
                    have_days.add(d)
                continue
            loc = str(ev.get('location') or '').strip().upper()
            if _ICS_LEG_LOCATION_RE.match(loc):
                deps.append((inst, iso))
        if not deps:
            return ics
        deps.sort()
        chosen = {}            # Berlin-Tag → (pickup_instant, iso, summary, dep_iso)
        for cand in candidates:
            if not isinstance(cand, dict):
                continue
            p_iso = str(cand.get('utc') or '')
            p_inst = _inst(p_iso)
            summ = re.sub(r'[\r\n\t]+', ' ', str(cand.get('summary') or '')).strip()
            if p_inst is None or not summ:
                continue
            anchor = None
            for d_inst, d_iso in deps:
                lead = (d_inst - p_inst).total_seconds() / 60.0
                if 0 <= lead <= _PICKUP_LEAD_MAX_MIN:
                    anchor = d_iso
                    break
            if anchor is None:
                continue
            day = _berlin_day(anchor)
            if not day or day in have_days:
                continue
            prev = chosen.get(day)
            if prev is None or p_inst > prev[0]:
                chosen[day] = (p_inst, p_iso, summ[:120], anchor)
        if not chosen:
            return ics
        add = []
        for i, day in enumerate(sorted(chosen)):
            _p_inst, p_iso, summ, dep_iso = chosen[day]
            stamp = _ics_stamp(p_iso if _berlin_day(p_iso) == day else dep_iso)
            if not stamp:
                continue
            add += ['BEGIN:VEVENT', f'UID:pu-ical-{i}@aerox-flightops',
                    f'DTSTART:{stamp}', f'DTEND:{stamp}',
                    f'SUMMARY:{summ}', 'END:VEVENT']
        if not add:
            return ics
        log.info('[lh_flightops] pickup-fallback: %d Marker aus dem '
                 'Kalender-Link ergaenzt', len(add) // 6)
        # VOR dem ersten VEVENT einhängen — der Tages-Summary wird in
        # Event-Reihenfolge zusammengesetzt, und der Hotel-Pickup ist der
        # FRÜHESTE Termin des Tages (vor Briefing und Abflug). Damit liest der
        # Marker sich exakt wie im myTime-Feed und wie im Primärpfad, der sein
        # Pickup-VEVENT ebenfalls vor das Flug-VEVENT setzt.
        marker = 'BEGIN:VEVENT'
        head, sep, tail = ics.partition(marker)
        if not sep:
            return ics
        return head + '\r\n'.join(add) + '\r\n' + marker + tail
    except Exception as e:
        log.warning('[lh_flightops] pickup-fallback: %s', type(e).__name__)
        return ics


# Der Kalender-Link wird für die Fallback-Quelle HÖCHSTENS alle 3 h geholt und
# das Ergebnis prozess-lokal gecacht — der Import läuft (refresh-all, Foreground-
# Sync, manueller Tap) deutlich häufiger, und ein myTime-Share ist genau das,
# was wir NICHT im Takt hämmern wollen. Scheitert der Abruf, bleibt der letzte
# gute Stand stehen (Grace) statt den Pickup wegfallen zu lassen.
_PICKUP_ICAL_TTL_S = 3 * 3600
_pickup_ical_cache = {}          # token → (ts, candidates)
_pickup_ical_lock = threading.Lock()


def pickup_ical_url(user_token, body_url=None):
    """URL des Kalender-Links, der als ZWEITE Pickup-Quelle dient — oder ''.

    `body_url` (aus dem Import-Body, die App kennt den Link lokal) gewinnt und
    wird persistiert: der Direkt-ICS-Import in app.py hat die gespeicherte
    `calendar_feed.url` bei Bestandsusern bereits geleert, `pickup_ical_url`
    ist der Slot, der einen Direkt-Import überlebt. Wirft nie."""
    try:
        import app as _app
        cand = _app._normalize_feed_scheme(
            _app._sanitize_feed_url(body_url or ''))
        pf = _app._profile_load(user_token) or {}
        prof = dict(pf.get('profile') or {})
        feed = prof.get('calendar_feed')
        feed = dict(feed) if isinstance(feed, dict) else {}
        if cand.startswith('https://'):
            if feed.get('pickup_ical_url') != cand:
                feed['pickup_ical_url'] = cand
                prof['calendar_feed'] = feed
                _app._profile_save(user_token, prof)
            return cand
        for k in ('pickup_ical_url', 'url'):
            v = str(feed.get(k) or '').strip()
            if v.startswith('https://'):
                return v
    except Exception as e:
        log.warning('[lh_flightops] pickup_ical_url: %s', type(e).__name__)
    return ''


def pickup_ical_candidates_for(user_token, url):
    """Pickup-Kandidaten des Kalender-Links (gecacht, gedrosselt). Wirft nie.
    Respektiert den Server-iCal-Kill-Switch (`AEROX_SERVER_ICAL_REFRESH=0`) —
    steht der auf 0, holt der Server GAR KEINE myTime-Links mehr."""
    if not url:
        return []
    try:
        import app as _app
        if not _app._server_ical_refresh_enabled():
            return []
        now = time.time()
        with _pickup_ical_lock:
            hit = _pickup_ical_cache.get(user_token)
            if hit and now - hit[0] < _PICKUP_ICAL_TTL_S:
                return hit[1]
        text, ferr = _app._fetch_calendar_feed_text(url)
        if ferr or not text:
            log.warning('[lh_flightops] pickup-fallback fetch: %s', ferr)
            return (hit[1] if hit else [])
        got = ical_pickup_candidates(text)
        with _pickup_ical_lock:
            _pickup_ical_cache[user_token] = (now, got)
            if len(_pickup_ical_cache) > 2000:
                for k in sorted(_pickup_ical_cache,
                                key=lambda k: _pickup_ical_cache[k][0])[:1000]:
                    _pickup_ical_cache.pop(k, None)
        return got
    except Exception as e:
        log.warning('[lh_flightops] pickup-fallback: %s', type(e).__name__)
        return []


def apply_ical_pickup_fallback(user_token, ics, body_url=None):
    """Kette: primär COMMON_CREW_ROTATION (steckt schon im `ics`), sekundär der
    Kalender-Link. Gibt das (ggf. ergänzte) ICS zurück; wirft nie."""
    try:
        url = pickup_ical_url(user_token, body_url)
        if not url:
            return ics
        return merge_ical_pickups(
            ics, pickup_ical_candidates_for(user_token, url))
    except Exception as e:
        log.warning('[lh_flightops] pickup-fallback wiring: %s',
                    type(e).__name__)
        return ics


def duty_events_to_ics(resp, pickups=None):
    """FlightOps-Duty-Events → ICS-String (oder None). Pure/testbar.
    Flight-Events → VEVENT im LH-Summary-Format ('LH400: FRA-JFK'), Off/Vac/
    Standby/Hotel → Marker-/Layover-Events. Zeiten kommen als UTC-ISO. NICHTS
    wird erfunden; unbekannte Kategorien reisen als Roh-Summary mit.

    `pickups` (optional) = Ergebnis von parse_rotation_pickups/
    rotation_pickups_for. Ist es leer oder trägt es für ein Leg keinen Wert,
    entsteht KEIN Pickup-Event — geraten wird nie."""
    if not isinstance(resp, dict):
        return None
    days = _as_list(resp.get('rosterDays'))
    if not days:
        return None
    lines = ['BEGIN:VCALENDAR', 'VERSION:2.0',
             'PRODID:-//AeroX LH FlightOps//DE']
    n = 0

    # ── Flug-Zeitachse → ECHTE Layover-Spanne (Tibor „Tag 2/2 in Athen",
    # 2026-07-26) ────────────────────────────────────────────────────────────
    # FlightOps liefert Hotel-Events OHNE Zeiten und EINES PRO NACHT. Der alte
    # Weg (Datums-Event Tag..Tag+2 je Hotel-Event) hatte drei bewiesene Fehler:
    #   1. N war IMMER 2 — ein 3-Nächte-Layover las live „(Tag 2/2) · (Tag 1/2)"
    #      am selben Tag (KIX 25.–29.07., Prod-Payload) statt „Tag 2/5".
    #   2. Der erfundene Tag+1 nach der letzten Hotel-Nacht trug „(Tag 2/2)",
    #      auch wenn die Crew längst weg war.
    #   3. Ein Datums-Event hat KEINE start_iso/end_iso → in
    #      _ics_events_to_briefings (app.py) warf `datetime.fromisoformat('')`
    #      immer → die 6-h-Mindestbodenzeit-Regel (Turnaround ≠ Layover) lief
    #      für FlightOps NIE, und `ical_layover_ort` landete nur auf Tag 1.
    # Die echte Spanne IST ableitbar — aber NUR mit drei Sicherungen, die ein
    # adversarialer Review am 26.07. erzwungen hat (jede davon hat eine naive
    # Fassung an echten Roster-Formen zerlegt):
    #   (a) ANKUNFT ZUERST, dann Abflug. Verankert man den Abflug am Hotel-Tag
    #       („erster Abflug ab Station ab 00:00Z"), gewinnt bei FRA-MUC-FRA
    #       (morgens) + FRA-MUC (abends) + Hotel MUC der MORGEN-Rückflug → eine
    #       1-h-„Layover"-Spanne, die die 6-h-Regel verwirft: die echte Nacht
    #       verschwindet komplett.
    #   (b) Der Abflug muss das ERSTE Leg NACH der Ankunft sein UND ab dieser
    #       Station starten. Sonst läuft die Suche über Wochen weiter, wenn die
    #       Crew anders heimkommt (Bahn/Bus/Leg ohne endTime) — gesehen: ein
    #       21-Tage-„Layover", der 18 freie Tage als Layover stempelte.
    #   (c) Mehrere Hotel-Nächte werden als LAUF zusammengefasst und ergeben EIN
    #       Event; die Spanne wird gegen die Lauflänge plausibilisiert
    #       (max. Nächte+2 Kalendertage).
    # Ist die Spanne nicht bestimmbar, fällt der ganze Lauf auf EIN Datums-Event
    # erster Tag…letzter Tag+2 zurück — also das alte Verhalten, aber pro LAUF
    # statt pro Nacht (das Stapeln „(Tag 2/2) · (Tag 1/2)" ist damit auch dort
    # weg) und mit erhaltenem Layover-Morgen-Marker (Tim/KRK 25.07.).
    _ISO_Z = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$')
    _legs = []
    _hotel_seq = []          # (day8, station) in Roster-Reihenfolge
    for _d0 in days:
        if not isinstance(_d0, dict):
            continue
        _day0 = (_d0.get('day') or '')[:10].replace('-', '')
        for _e0 in _as_list(_d0.get('events')):
            if not isinstance(_e0, dict):
                continue
            _c0 = re.sub(r'[_\s]', '', (_e0.get('eventCategory') or '').lower())
            _t0 = re.sub(r'[_\s]', '', (_e0.get('eventType') or '').lower())
            _f0 = (_e0.get('startLocation') or '').upper().strip()
            _o0 = (_e0.get('endLocation') or '').upper().strip()
            if _t0 == 'hotel' or _c0 == 'hotel':
                _st0 = (_o0 or _f0)
                if len(_day0) == 8 and len(_st0) == 3:
                    _hotel_seq.append((_day0, _st0))
                continue
            if not (_t0 == 'flight' or _c0 in _FLIGHT_CATS):
                continue
            _s0 = (_e0.get('startTime') or '').strip()
            _x0 = (_e0.get('endTime') or '').strip()
            # STRIKTE Form: nur 'YYYY-MM-DDTHH:MM:SSZ'. Nur dann ist der
            # lexikografische Vergleich unten beweisbar korrekt; alles andere
            # (Offset statt Z, Millisekunden) würde falsch sortieren, also
            # lieber sauber degradieren als still falsch rechnen.
            if not (_ISO_Z.match(_s0) and len(_f0) == 3):
                continue
            # endTime/endLocation sind für die ABFLUG-Seite nicht nötig — ein
            # Leg ohne endTime darf den Lauf trotzdem beenden.
            if not (_ISO_Z.match(_x0) and len(_o0) == 3):
                _x0, _o0 = '', ''
            _legs.append((_s0, _x0, _f0, _o0))
    _legs.sort(key=lambda x: x[0])

    def _shift(day8, delta):
        from datetime import datetime as _d, timedelta as _td
        try:
            return (_d.strptime(day8, '%Y%m%d') + _td(days=delta)).strftime('%Y%m%d')
        except Exception:
            return day8

    def _iso_day(day8, suffix='T00:00:00Z'):
        return f'{day8[:4]}-{day8[4:6]}-{day8[6:]}{suffix}'

    # Läufe aufeinanderfolgender Hotel-Nächte derselben Station bilden.
    _runs = {}               # (day8, station) → (first_day8, last_day8)
    _seen_hd = set()
    _ordered = [hd for hd in _hotel_seq
                if not (hd in _seen_hd or _seen_hd.add(hd))]
    _i = 0
    while _i < len(_ordered):
        _d1, _stn = _ordered[_i]
        _j = _i
        while (_j + 1 < len(_ordered) and _ordered[_j + 1][1] == _stn
               and _ordered[_j + 1][0] == _shift(_ordered[_j][0], 1)):
            _j += 1
        for _k in range(_i, _j + 1):
            _runs[_ordered[_k]] = (_d1, _ordered[_j][0])
        _i = _j + 1

    def _night_span(station, day8):
        """Spanne EINER Hotel-Nacht: (ankunft_iso, abflug_iso) oder (None, None)."""
        # (a) ANKUNFT ZUERST: das späteste Leg, das an der Station landet und
        # noch an diesem Hotel-Tag endet (Rückblick 3 Tage für Langstrecke /
        # rosterDay-Drift). Verankert man stattdessen den ABFLUG am Tag, gewinnt
        # bei FRA-MUC-FRA (morgens) + FRA-MUC (abends) + Hotel MUC der Morgen-
        # Rückflug → 1-h-„Layover", und die echte Nacht verschwindet.
        lo = _iso_day(_shift(day8, -3))
        hi = _iso_day(_shift(day8, 1))
        arr_iso = None
        for _s, _x, _f, _o in _legs:
            if _o == station and _x and lo <= _x < hi:
                arr_iso = _x                     # sortiert → am Ende der späteste
        if not arr_iso:
            return None, None
        # (b) ABFLUG: das ERSTE Leg NACH der Ankunft — und es MUSS an dieser
        # Station starten. Startet es woanders, ist die Crew längst anders
        # heimgekommen (Bahn/Bus/Leg ohne Zeiten); dann lieber nichts ableiten
        # als über Wochen weiterzusuchen (gesehen: 21-Tage-Phantom-Layover).
        for _s, _x, _f, _o in _legs:
            if _s > arr_iso:
                return (arr_iso, _s) if _f == station else (None, None)
        return None, None

    def _run_span(station, first_day, last_day):
        """Spanne des GANZEN Hotel-Laufs = früheste Ankunft … spätester Abflug
        seiner Nächte. Ein Turnaround AUS dem Layover-Ort heraus (JFK-YYZ-JFK
        mitten im Aufenthalt) zerlegt den Aufenthalt sonst in zwei Hälften und
        die erste Nacht ginge verloren."""
        arrs, deps = [], []
        d8 = first_day
        for _ in range(64):
            a, b = _night_span(station, d8)
            if a and b:
                arrs.append(a)
                deps.append(b)
            if d8 == last_day:
                break
            d8 = _shift(d8, 1)
        if not arrs:
            return None, None
        arr_iso, dep_iso = min(arrs), max(deps)
        if dep_iso <= arr_iso:
            return None, None
        # (c) Plausibilität gegen die Lauflänge — harte Obergrenze gegen jede
        # noch unbekannte Roster-Form: Nächte + 3 Kalendertage.
        try:
            from datetime import datetime as _d
            nights = ((_d.strptime(last_day, '%Y%m%d')
                       - _d.strptime(first_day, '%Y%m%d')).days + 1)
            span = (_d.strptime(dep_iso[:10], '%Y-%m-%d')
                    - _d.strptime(arr_iso[:10], '%Y-%m-%d')).days + 1
            if span > nights + 3:
                return None, None
        except Exception:
            return None, None
        return arr_iso, dep_iso

    _hotel_emitted = set()

    def _dt(v):
        # 'YYYY-MM-DDTHH:MM:SSZ' → 'YYYYMMDDTHHMMSSZ'
        try:
            s = (v or '').strip().replace('-', '').replace(':', '')
            return s if s.endswith('Z') else (s + 'Z' if 'T' in s else None)
        except Exception:
            return None

    for d in days:
        if not isinstance(d, dict):
            continue
        for ev in _as_list(d.get('events')):
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
                # myTime-PARITAET (Tim/KRK 2026-07-25): Feed-Import und iOS kennen
                # die myTime-VEVENT-Form — 'DH LH 1623: KRK-MUC' + LOCATION
                # 'KRK - MUC'. Ohne LOCATION bekam der Tag kein Routing
                # (flownSectors=0) und iOS stufte einen 4-Leg-Diensttag mit
                # Layover-Nacht als reinen Ruhetag ein → der Feed sprang am
                # Layover-Morgen auf den MORGIGEN Umlauf. Deadhead-Flag steht in
                # eventDetails ('DH LH1623') und darf nicht verworfen werden.
                flt_disp = _re.sub(r'^([A-Z]{2}|\d[A-Z])(?=\d)', r'\g<1> ', flt)
                is_dh = det.upper().strip().startswith('DH ')
                summary = ((f'DH {flt_disp}' if is_dh else flt_disp) + f': {frm}-{to}'
                           if flt else f'{frm}-{to}')
                # ── HOTEL-PICKUP vor dem Layover-Rückflug ────────────────────
                # VOR dem Flug-VEVENT emittiert, damit der Tages-Summary die
                # myTime-Reihenfolge behält ('… 16:45 LT Pickup MEX · LH 499:
                # MEX-FRA'). Quelle ist ausschließlich pickupTime aus
                # COMMON_CREW_ROTATION; ohne Wert entsteht KEIN Event.
                _pu = _pickup_for_leg(pickups, flt, frm, to, st)
                if _pu and not _pickup_lead_ok(_pu.get('pickup_utc'),
                                               ev.get('startTime')):
                    # PLAUSIBILITÄT vor allem anderen (adversarialer Review
                    # 2026-07-27): ohne diesen Riegel wanderte ein Pickup NACH
                    # dem Abflug, ein 14-h-Vorlauf oder ein 3 Tage alter Wert
                    # aus einem noch gecachten Umlauf in den Roster — sichtbar
                    # für den User, während die Pre-Flight-Timeline ihn
                    # (zurecht) still verwarf. Fenster wie iOS/crew_live_state:
                    # 0…6 h vor dem Plan-Abflug.
                    _pu = None
                if _pu:
                    # ORTSZEIT: bewusst PRIMÄR aus `pickupTime` + Stations-TZ
                    # gerechnet, `pickupTimeLT` nur als Fallback. Ursprünglich
                    # war es umgekehrt (LT ist ja „fertig"), aber der Review hat
                    # gezeigt: ein LT, das nicht zum UTC-Wert passt (LH rendert
                    # es in Base-TZ o. ä.), zeigt dem User eine falsche Zeit UND
                    # killt die Pre-Flight-Timeline still (6-h-Fenster). Der
                    # UTC-Wert ist die Größe, gegen die wir oben plausibilisiert
                    # haben — die angezeigte Zeit muss zu IHM passen.
                    _hh = None
                    try:
                        import app as _app
                        _hh = _app._ics_local_hhmm_at(_pu.get('pickup_utc'), frm)
                    except Exception:
                        _hh = None
                    _lt = _pu.get('pickup_lt')
                    if _hh and _lt and _hh != _lt:
                        log.warning('[lh_flightops] pickupTimeLT %s != aus '
                                    'pickupTime gerechnet %s (%s %s) -> UTC '
                                    'gewinnt', _lt, _hh, flt, frm)
                    if not _hh:
                        # Stations-TZ unbekannt → fertige Ortszeit von LH.
                        _hh = _lt
                    _pst = _dt(_pu.get('pickup_utc'))
                    if _hh and _pst:
                        # MITTERNACHTS-WRAP (bewiesen an RN 171012): der
                        # Tages-Bucket des Feed-Imports ist das EUROPE/BERLIN-
                        # Datum von DTSTART. Pickup 28.07. 21:50Z (Berlin
                        # 23:50 am 28.) und Abflug 29.07. 00:30Z (Berlin 02:30
                        # am 29.) fallen damit auf VERSCHIEDENE Roster-Tage —
                        # der Marker landete auf dem Layover-Tag statt auf dem
                        # Rückflug-Tag, und _rc_pickup_hhmm liest ihn pro TAG.
                        # Deshalb: liegt der echte Pickup-Zeitpunkt in einem
                        # anderen Berlin-Tag als der Abflug, wird DTSTART auf
                        # den Abflug-Zeitpunkt gezogen. Die WAHRHEIT bleibt
                        # erhalten, weil der Summary die Ortszeit trägt und
                        # crew_live_state.pickup_utc_for_leg den echten UTC-
                        # Zeitpunkt aus (HH:MM + Stations-TZ + Abflug)
                        # rekonstruiert — inklusive Tagesabzug beim Wrap.
                        if _berlin_day(_pu.get('pickup_utc')) != _berlin_day(
                                ev.get('startTime')):
                            _pst = st
                        lines += ['BEGIN:VEVENT', f'UID:pu-{uid}',
                                  f'DTSTART:{_pst}', f'DTEND:{_pst}',
                                  f'SUMMARY:{_hh} LT Pickup {frm}',
                                  'END:VEVENT']
                lines += ['BEGIN:VEVENT', f'UID:{uid}',
                          f'DTSTART:{st}', f'DTEND:{en}',
                          f'SUMMARY:{summary}',
                          f'LOCATION:{frm} - {to}', 'END:VEVENT']
                continue
            # BRIEFING (Miguel/Thomas 2026-07-24 „falsche Briefing-Zeiten seit
            # dem Update"): FlightOps liefert Briefings MIT startTime, aber
            # systematisch OHNE endTime → der alte `st and en`-Zweig griff nie
            # und das Event fiel in den GANZTAGS-Zweig — die echte Report-Zeit
            # ging verloren, `ical_start` des Tages wurde der ABFLUG und die
            # App riet Abflug−60 (Langstrecke real ~110 min Vorlauf). Jetzt:
            # zeitbehaftetes Event im kanonischen LH-Marker-Format
            # „HH:MM LT Briefing FRA" (Station-ORTSZEIT) — exakt die Form, die
            # _corrected_briefing_start_iso (app.py) und iOS
            # briefingTimeFromSummary bereits lesen; kein weiterer Code nötig.
            if etype == 'briefing' and st:
                summary = f'Briefing {frm}'.strip() if len(frm) == 3 else 'Briefing'
                try:
                    import app as _app   # lazy wie import_calendar_feed (kein Import-Zirkel)
                    hhmm = _app._ics_local_hhmm_at(
                        ev.get('startTime'), frm if len(frm) == 3 else None)
                    if hhmm and len(frm) == 3:
                        summary = f'{hhmm} LT Briefing {frm}'
                except Exception:
                    pass
                lines += ['BEGIN:VEVENT', f'UID:{uid}',
                          f'DTSTART:{st}', f'DTEND:{en or st}',
                          f'SUMMARY:{summary}', 'END:VEVENT']
                continue
            # Nicht-Flug: Marker/Standby/Hotel/Layover
            summary = None
            loc_line = None
            if cat in ('off', 'offduty'):
                # myTime-Paritaet: 'Off Day (FREE)' / 'Off Day (ORTSTAG)' statt
                # nacktem Code — iOS-Klassifikation kennt beide, aber die App
                # zeigt den Marker 1:1 (live kommt cat='OFFDUTY', det='FREE').
                summary = f'Off Day ({det})' if det else 'Off Day'
            elif cat in ('vac',):
                summary = 'Urlaub'
            elif cat in ('absence',):
                # Live-Shape (Remo 2026-07-25): Urlaub kommt als
                # eventCategory=ABSENCE, eventDetails='U1' — der nackte Code
                # fiel durch und iOS klassifizierte den Urlaubstag als Dienst.
                # myTime-Prosa 'Absence (U1)' → iOS mappt ABSENCE auf Urlaub.
                summary = f'Absence ({det})' if det else 'Absence'
            elif cat in ('res', 'frs'):
                # RESERVE ist NICHT Standby (LH-Crew-Feedback 2026-07-27:
                # „Reserve wird als Standby angezeigt inkl. 60-min-Karenzzeit").
                # MTV Nr. 2a, § 4, 6. Abschnitt: (1) Standby = binnen 60 Min
                # nach Abruf am Reporting Point; (2) Reserve = 12 Std Karenzzeit
                # zwischen Abruf und Dienstantritt, Reservezeiten 06–22 Uhr LT.
                # Bis hierher minteten wir für eventCategory RES (= Reservedienst,
                # CRS-Handbuch MPG.4.9.1.1) hart das Wort „Standby" — damit war
                # die Dienstart im ICS unwiederbringlich verloren und iOS zeigte
                # jedem Reserve-Tag die Standby-Karten (60 Min Meldezeit).
                # LHs EIGENER Hauscode in eventDetails entscheidet (gleiche Lehre
                # wie B1/„Office Day"): ein SB-Code bleibt Standby, alles andere
                # ist Reserve. Der Code reist in Klammern mit (myTime-Prosa-Form
                # wie „Absence (U1)"/„Office Day (B4)"), damit iOS/Kalender ihn
                # weiter 1:1 zeigen können.
                _du = det.upper().replace('_', '').replace('-', '')
                _is_sb = _du.startswith('SB') or _du.startswith('STBY')
                _word = 'Standby' if _is_sb else 'Reserve'
                summary = f'{_word} ({det})' if det else (
                    f'{_word} {frm}' if len(frm) == 3 else _word)
            elif etype == 'hotel' or cat == 'hotel':
                # myTime-Paritaet (Tim/KRK 2026-07-25): 'Layover [BRE]' + die
                # IATA als LOCATION — NUR mit LOCATION setzt der Feed-Import
                # `ical_layover_ort` (Nightstop/Hotel-Karten/isHomebaseNight).
                # Live trägt das Hotel-Event die Stadt in startLocation,
                # endLocation ist null.
                _hiata = (to or frm)
                summary = f'Layover [{_hiata}]' if len(_hiata) == 3 else 'Layover'
                if len(_hiata) == 3:
                    loc_line = f'LOCATION:{_hiata}'
            elif cat in ('sim',):
                summary = 'Simulator'
            elif cat == 'groundduty':
                # BÜRODIENST (Owner 2026-07-26 „B4 löst einen freien Tag aus"):
                # FlightOps schickt Office-Tage als eventCategory=GROUNDDUTY mit
                # dem NACKTEN Hauscode in eventDetails ('B4', live verifiziert:
                # GROUNDDUTY/B4/06:30–15:00Z MUC). Der fiel bisher in den Roh-
                # Zweig und reiste als „B4" mit — ohne jede Dienst-Evidenz.
                # myTime schreibt für DENSELBEN Tag „Office Day (B4)"; genau
                # diese Prosa minten wir jetzt (myTime-Paritaet), damit die
                # bestehende Klassifikation (_summary_has_ground_duty → 'OFFICE',
                # iOS RosterEventClassifier → .office) ohne Sonderweg greift.
                # Nur wenn das Detail EXAKT einer der Codes ist — ein „MED B4
                # MUC" darf nicht als Bürotag umetikettiert werden (der Code
                # bleibt im Roh-Summary und wird trotzdem als Boden-Dienst
                # erkannt). Andere GROUNDDUTY-Details (EMCRM, TK, MED, D4,
                # WBT_GR …) reisen unverändert roh weiter — kein Bürodienst.
                summary = (f'Office Day ({det})'
                           if is_office_day_code(det)
                           else (det or cat.upper() or 'Duty'))
            elif cat in ('abs', 'lic', 'duty') or etype in ('briefing', 'groundevent'):
                summary = det or cat.upper() or 'Duty'
            else:
                summary = det or cat.upper() or 'Event'
            if loc_line is None and len(frm) == 3:
                # Boden-/Marker-Events tragen ihre Station (myTime: Off Day @MUC).
                loc_line = f'LOCATION:{frm}'
            day = (d.get('day') or '')[:10].replace('-', '')
            is_hotel = (etype == 'hotel' or cat == 'hotel')
            _run = _runs.get((day, (to or frm))) if is_hotel else None
            if _run:
                # EIN Event pro Hotel-LAUF (nicht pro Nacht) — sonst stapeln
                # sich „(Tag 2/2) · (Tag 1/2)"-Segmente auf einem Tag.
                _first, _last = _run
                if ((to or frm), _first, _last) in _hotel_emitted:
                    continue
                _hotel_emitted.add(((to or frm), _first, _last))
                _h_arr, _h_dep = _run_span((to or frm), _first, _last)
                if _h_arr and _h_dep:
                    # Echte Spanne Ankunft…Weiterflug → zeitbehaftetes VEVENT
                    # (wie im myTime-Feed). Erst damit greift die 6-h-Regel und
                    # „(Tag i/N)" zählt die echten Nächte.
                    lines += ['BEGIN:VEVENT', f'UID:{uid}',
                              f'DTSTART:{_dt(_h_arr)}', f'DTEND:{_dt(_h_dep)}',
                              f'SUMMARY:{summary}'] \
                        + ([loc_line] if loc_line else []) + ['END:VEVENT']
                else:
                    # Nicht ableitbar (Layover am Fensterrand, Heimreise ohne
                    # Flug-Leg, Stations-Code passt zu keinem Leg): altes
                    # Datums-Verhalten für den GANZEN Lauf — erster Tag bis
                    # letzter Tag+2 (DTEND exklusiv), damit der Layover-Morgen
                    # seinen Marker behält.
                    lines += ['BEGIN:VEVENT', f'UID:{uid}',
                              f'DTSTART;VALUE=DATE:{_first}',
                              f'DTEND;VALUE=DATE:{_shift(_last, 2)}',
                              f'SUMMARY:{summary}'] \
                        + ([loc_line] if loc_line else []) + ['END:VEVENT']
            elif ev.get('wholeDay') and day:
                nd = _next_day(day)
                lines += ['BEGIN:VEVENT', f'UID:{uid}',
                          f'DTSTART;VALUE=DATE:{day}', f'DTEND;VALUE=DATE:{nd}',
                          f'SUMMARY:{summary}'] \
                    + ([loc_line] if loc_line else []) + ['END:VEVENT']
            elif st and not is_hotel:
                # Zeitbehaftete Events OHNE endTime (FlightOps lässt endTime
                # öfter null) behalten ihre echte Startzeit — vorher fielen
                # sie in den Ganztags-Zweig und verloren die Uhrzeit.
                lines += ['BEGIN:VEVENT', f'UID:{uid}',
                          f'DTSTART:{st}', f'DTEND:{en or st}',
                          f'SUMMARY:{summary}'] \
                    + ([loc_line] if loc_line else []) + ['END:VEVENT']
            elif day:
                # Zeitloses Nicht-Hotel-Event → EIN Datums-Tag. (Hotels laufen
                # oben über den Lauf-Zweig und kommen hier nie an.)
                nd = _next_day(day)
                lines += ['BEGIN:VEVENT', f'UID:{uid}',
                          f'DTSTART;VALUE=DATE:{day}', f'DTEND;VALUE=DATE:{nd}',
                          f'SUMMARY:{summary}'] \
                    + ([loc_line] if loc_line else []) + ['END:VEVENT']
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


# ── Duty-Events-_links → Service-Referenzen (accessCode!) ────────────────────
# Doku (Duty_Events): jedes Flug-Event trägt `_links` mit fertigen Referenzen
# auf crewList / checkInTimes / landingReport / flightInfo — INKLUSIVE des
# `accessCode`, ohne den COMMON_CREWLIST/SIMULATOR_CREWLIST gar nicht aufrufbar
# sind (403 invalid access code). Wir extrahieren die Query-Params jeder
# Referenz und cachen sie pro User auf Disk (ephemeral ist ok: jeder Import
# erneuert sie; bei Miss wird das Tages-Fenster live nachgeladen).
def extract_duty_links(resp):
    """COMMON_DUTY_EVENTS-Response → [{'service': 'crewlist', 'params': {…}}].
    Pure/testbar. Service-Namen normalisiert (lower, nur Buchstaben):
    crewlist / checkintimes / landingreport / flightinfo / simulatorcrewlist."""
    out = []
    for d in _as_list((resp or {}).get('rosterDays') if isinstance(resp, dict) else None):
        if not isinstance(d, dict):
            continue
        for ev in _as_list(d.get('events')):
            links = ev.get('_links') if isinstance(ev, dict) else None
            if not isinstance(links, dict):
                continue
            for name, ref in links.items():
                href = ref.get('href') if isinstance(ref, dict) else (
                    ref if isinstance(ref, str) else None)
                if not href:
                    continue
                try:
                    q = dict(urllib.parse.parse_qsl(
                        urllib.parse.urlsplit(href).query))
                except Exception:
                    continue
                if not q:
                    continue
                out.append({'service': re.sub(r'[^a-z]', '', (name or '').lower()),
                            'params': q})
    return out


def _links_path(user_token):
    safe = re.sub(r'[^A-Za-z0-9_-]', '', user_token or '')[:64]
    return os.path.join(_flow_dir(), f'folinks_{safe}.json') if safe else None


def _links_save(user_token, links):
    try:
        p = _links_path(user_token)
        if p and isinstance(links, list):
            with open(p, 'w') as f:
                json.dump({'ts': time.time(), 'links': links}, f)
    except Exception as e:
        log.warning('[lh_flightops] links_save: %s', type(e).__name__)


def _links_load(user_token):
    try:
        p = _links_path(user_token)
        if p and os.path.exists(p):
            with open(p) as f:
                d = json.load(f)
            return d.get('links') or []
    except Exception:
        pass
    return []


def _links_find(links, service, flight, date, dep=None, arr=None):
    """Beste Link-Params für (service, flight, date[, dep, arr]) oder None."""
    f = (flight or '').upper().replace(' ', '')
    dt_ = (date or '')[:10]
    best = None
    for l in links or []:
        if not isinstance(l, dict) or l.get('service') != service:
            continue
        p = l.get('params') or {}
        if (p.get('flightDesignator') or '').upper() != f:
            continue
        if dt_ and not (p.get('flightDate') or '').startswith(dt_):
            continue
        if dep and (p.get('departureAirport') or '').upper() != dep.upper():
            continue
        if arr and p.get('arrivalAirport') and \
                (p.get('arrivalAirport') or '').upper() != arr.upper():
            continue
        best = p
        break
    return best


def _resolve_link_params(user_token, service, flight, date, dep=None, arr=None):
    """Link-Params aus dem Cache; bei Miss das Tages-Fenster live nachladen
    (1 Duty-Events-Call) und Cache erneuern. None wenn der Flug nicht im
    eigenen Roster ist (dann gibt es auch keinen accessCode)."""
    p = _links_find(_links_load(user_token), service, flight, date, dep, arr)
    if p:
        return p
    if not date:
        return None
    resp = duty_events(user_token, date, date)
    if not isinstance(resp, dict):
        return None
    fresh = extract_duty_links(resp)
    if fresh:
        merged = [l for l in _links_load(user_token)
                  if not any(l == g for g in fresh)] + fresh
        _links_save(user_token, merged[-800:])
    return _links_find(fresh, service, flight, date, dep, arr)


def _store_own_pk(user_token, pk):
    """LH-Personalnummer im Profil-Mirror ablegen (metadata.lh_pk_number).
    Idempotent, wirft nie."""
    if not pk:
        return
    try:
        import app as _app
        pf = _app._profile_load(user_token) or {}
        prof = (pf.get('profile') or {})
        if prof.get('lh_pk_number') == pk:
            return
        prof['lh_pk_number'] = pk
        _app._profile_save(user_token, prof)
    except Exception as e:
        log.warning('[lh_flightops] store_pk: %s', type(e).__name__)


def _match_aerox_profiles(members):
    """Crew-Listen-Mitglieder → AeroX-PUBLIC-Profile (best-effort, wirft nie).
    Primär EXAKT über die LH-Personalnummer (metadata.lh_pk_number — beim
    Duty-Events-Import jedes verbundenen Users gespeichert), Fallback exakter
    Name (case-insensitiv, NUR eindeutige Treffer) + Lufthansa-Airline.
    Response-Felder = exakt die Public-Shape von /api/user/search (token/name/
    airline/homebase/position/avatar_url) — nie email/apple_sub/internal;
    Family-Accounts nie. Rückgabe: {pk-oder-name-Key: public_profile}."""
    try:
        import app as _app
        if not getattr(_app, 'SB_AVAILABLE', False) or not members:
            return {}
        sel = 'token,name,airline,homebase,position,metadata'
        by_pk = {}
        pks = [str(m.get('pk') or '').strip() for m in members]
        pks = [p for p in pks if p]
        if pks:
            r = (_app.sb.table('user_profiles').select(sel)
                 .in_('metadata->>lh_pk_number', pks).limit(60).execute())
            for row in (r.data or []):
                md = row.get('metadata') or {}
                by_pk[str(md.get('lh_pk_number') or '').strip()] = row
        by_name = {}
        need = [m for m in members
                if str(m.get('pk') or '').strip() not in by_pk
                and (m.get('name') or '').strip()]
        for m in need[:12]:
            try:
                r = (_app.sb.table('user_profiles').select(sel)
                     .ilike('name', m['name'].strip()).limit(3).execute())
                cand = [row for row in (r.data or [])
                        if 'lufthansa' in str(row.get('airline') or '').lower()]
                if len(cand) == 1:
                    by_name[m['name'].strip().lower()] = cand[0]
            except Exception:
                continue

        # UNSCHARFER FALLBACK (Owner 2026-07-26, „sieht man wirklich wer auf
        # AeroX ist?" — Live-Check: 1/76 gematcht). LH-Crew-Listen führen
        # ABGEKÜRZTE Namen („Markus K."), AeroX-Profile den vollen Namen
        # („Markus Krause") → der exakte ilike-Match oben traf fast nie.
        # Regel (Owner-Entscheid „nur eindeutige Treffer"): letztes Token =
        # Nachname-Initial (1 Buchstabe, ggf. mit Punkt) → Vorname + Initial +
        # Airline Lufthansa; NUR übernehmen, wenn GENAU EIN Profil passt (kein
        # Falsch-Treffer-Risiko bei zwei „Markus K." an derselben Base).
        def _abbrev_parts(name):
            toks = [t for t in str(name or '').split() if t]
            if len(toks) < 2:
                return None
            last = toks[-1].rstrip('.')
            if len(last) != 1 or not last.isalpha():
                return None          # kein abgekürzter Nachname → nicht fuzzy
            return toks[0], last.upper()   # (Vorname, Initial)

        fuzzy_need = [m for m in need
                      if m['name'].strip().lower() not in by_name]
        for m in fuzzy_need[:12]:
            parts = _abbrev_parts(m.get('name'))
            if not parts:
                continue
            first, initial = parts
            if len(first) < 2:
                continue             # zu kurzer Vorname → zu unspezifisch
            try:
                # Vorname als Präfix (deckt Zweit-Vornamen im Profil mit ab).
                r = (_app.sb.table('user_profiles').select(sel)
                     .ilike('name', first + ' %').limit(8).execute())
            except Exception:
                continue
            cand = []
            for row in (r.data or []):
                if 'lufthansa' not in str(row.get('airline') or '').lower():
                    continue
                ntoks = [t for t in str(row.get('name') or '').split() if t]
                if len(ntoks) < 2:
                    continue
                # Vorname exakt + Nachname beginnt mit dem Initial.
                if ntoks[0].lower() != first.lower():
                    continue
                if not ntoks[-1][:1].upper() == initial:
                    continue
                cand.append(row)
            # Eindeutigkeit über den Token (dieselbe Person kann mehrfach
            # zurückkommen ist hier ausgeschlossen, aber sicher ist sicher).
            uniq_tokens = {row.get('token') for row in cand if row.get('token')}
            if len(uniq_tokens) == 1:
                by_name[m['name'].strip().lower()] = cand[0]

        def _pub(row):
            md = row.get('metadata') or {}
            if str(md.get('account_type') or '').strip().lower() == 'family':
                return None
            if not row.get('token'):
                return None
            return {'token': row.get('token'), 'name': row.get('name'),
                    'airline': row.get('airline'),
                    'homebase': row.get('homebase'),
                    'position': row.get('position'),
                    'avatar_url': md.get('avatar_url')}

        out = {}
        for m in members:
            row = (by_pk.get(str(m.get('pk') or '').strip())
                   or by_name.get(str(m.get('name') or '').strip().lower()))
            p = _pub(row) if row else None
            if p:
                out[str(m.get('pk') or m.get('name') or '')] = p
        return out
    except Exception as e:
        log.warning('[lh_flightops] profile_match: %s', type(e).__name__)
        return {}


def parse_check_in_times(resp):
    """COMMON_CHECK_IN_TIMES → kompakte Zeiten-Map (nur dokumentierte Felder,
    ISO-Werte unverändert durchgereicht). Pure/testbar."""
    if not isinstance(resp, dict) or resp.get('processingErrors'):
        return {}
    keys = ('briefingRoom', 'briefingBegin', 'cocJoining', 'briefingEnd',
            'crewAtSecurityCheck', 'crewBusDeparture', 'readinessNotification',
            'boardingBegin', 'paxOnBoard')
    return {k: resp.get(k) for k in keys if resp.get(k) is not None}


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
    if not _tokens_save(flow['user_token'], tok):
        # Save NICHT bestätigt ⇒ ehrlich scheitern: ein »verbunden« ohne
        # durablen RT wäre eine Familie, die beim ersten Refresh stirbt.
        # Der User loggt sich schlicht erneut ein (neuer Grant, kein Schaden).
        log.error('[lh_flightops] exchange-save unbestätigt token=%s',
                  (flow.get('user_token') or '')[:8])
        return jsonify({'ok': False, 'error': 'store_failed'}), 503
    return jsonify({'ok': True, 'connected': True, 'scope': tok.get('scope')})


@lh_flightops_bp.route('/lh/oauth/callback', methods=['GET'])
def flightops_oauth_callback_relay():
    """HTTPS-OAuth-Callback für PROD. Das LH-Prod-Portal akzeptiert KEINE Custom-
    Scheme-Redirect-URIs (nur https://) — also registrieren wir hier eine HTTPS-
    Callback-URL (`https://api.aerosteuer.de/lh/oauth/callback`, = LH_FLIGHTOPS_
    REDIRECT_URI). LH leitet nach dem Consent HIERHER mit ?code&state (oder ?error).
    Diese Route macht KEINEN Token-Austausch — sie bounced NUR zurück ins App-
    Scheme `aerox://lhcrew/callback?...`, das die ASWebAuthenticationSession der App
    abfängt; die App tauscht den Code dann über /api/lh/flightops/oauth/exchange
    (state-gebunden, verifier serverseitig). So bleibt die iOS-App unverändert.
    Bounce via JS/Meta-Refresh statt `Location: aerox://…` (robust gegen Proxies/
    CDN, die einen non-http Location-Header verwerfen)."""
    args = {}
    for k in ('code', 'state', 'error', 'error_description'):
        v = request.args.get(k)
        if v:
            args[k] = v
    target = 'aerox://lhcrew/callback'
    if args:
        target += '?' + urllib.parse.urlencode(args)
    tesc = target.replace('&', '&amp;').replace('"', '%22')
    tjs = target.replace('\\', '\\\\').replace('"', '\\"')
    page = ('<!doctype html><html><head><meta charset=utf-8>'
            f'<meta http-equiv="refresh" content="0;url={tesc}">'
            f'<script>location.replace("{tjs}")</script></head>'
            '<body style="font-family:-apple-system,BlinkMacSystemFont,sans-serif;'
            'background:#0b1020;color:#d6e6ff;padding:24px;line-height:1.4">'
            'Zurück zur AeroX-App…</body></html>')
    return page, 200, {'Content-Type': 'text/html; charset=utf-8'}


@lh_flightops_bp.route('/api/lh/flightops/status/<token>', methods=['GET'])
def flightops_status(token):
    """Ist dieser User mit FlightOps verbunden?"""
    t = _tokens_load(token)
    return jsonify({'ok': True,
                    'connected': bool(t.get('access')
                                      and not t.get('needs_relogin')),
                    'needs_relogin': bool(t.get('needs_relogin')),
                    'scope': t.get('scope'),
                    'configured': flightops_configured()})


@lh_flightops_bp.route('/api/lh/flightops/import/<token>', methods=['POST'])
def flightops_import(token):
    """Schritt 3: Duty Events holen → ICS → bestehende Roster-Pipeline.
    Body optional {from_date, to_date} (YYYY-MM-DD); Default −7…+45 Tage."""
    if not flightops_configured():
        return jsonify({'ok': False, 'error': 'not_configured'}), 503
    _st, _acc = _access_state(token)
    if _st == 'pending':
        # Grant lebt, nur der Access-Token ist gerade abgelaufen: der
        # zentrale Refresher rotiert in Kürze. Transiente Antwort — der
        # Status-Endpoint bleibt connected, iOS zeigt KEINE Relogin-Karte.
        #
        # DEMAND-POKE (Lazy Rotation, 2026-07-28): seit der Quota-Diät rotiert
        # der Refresher nicht mehr auf Verdacht, sondern auf Bedarf — und
        # DAS hier ist der Bedarf. Lokal eintragen (falls dieser Prozess der
        # Poll-Container ist) UND best-effort über das interne Netz poken
        # (Regelfall: der Request läuft im Web-Container, wo kein Refresher
        # lebt). Antwort-Shape bleibt unverändert — der iOS-Retry-Weg ist der,
        # der die frischen Daten holt.
        _refresher_demand_add(token)
        _rotate_poke_remote(token)
        return jsonify({'ok': False, 'error': 'token_refresh_pending'}), 503
    if _st != 'ok':
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
    # Interaktiv = alles, was NICHT der refresh-all-Hintergrundlauf ist
    # (der markiert sich via body.background) — Connect-Erstimport und
    # manuelles „Jetzt aktualisieren" bekommen die höhere Budget-Grenze,
    # damit die Re-Login-Heilung nie an Hintergrund-Syncs verhungert.
    resp = duty_events(token, fd, td,
                       interactive=not bool(body.get('background')))
    if resp is None:
        return jsonify({'ok': False, 'error': 'duty_events_failed'}), 502
    # Service-Referenzen (accessCode für Crew-Liste/Check-in!) mitnehmen —
    # kostenlos, sie stecken schon in dieser Response.
    links = extract_duty_links(resp)
    if links:
        _links_save(token, links)
    # Eigene LH-Personalnummer persistieren (Owner 2026-07-23 „Crews mit AeroX-
    # Profilen verbinden"): die pkNumber aus der Duty-Events-Response ist DER
    # exakte Schlüssel, über den andere verbundene User diesen Account in ihrer
    # Crew-Liste wiederfinden (COMMON_CREWLIST liefert pkNumber pro Mitglied).
    _store_own_pk(token, (resp.get('pkNumber') or '').strip()
                  if isinstance(resp, dict) else '')
    # ── HOTEL-PICKUP (Owner 2026-07-26: „Pickup-Zeiten verschwunden seit dem
    # direkten FlightOps-Login") ────────────────────────────────────────────
    # pickupTime lebt in COMMON_CREW_ROTATION, die rotationId liegt schon in
    # dieser Duty-Events-Response. Nur Umläufe mit Layover-Rückflug im engen
    # Horizont (_ROT_PICKUP_HORIZON_H, Begründung dort mit Zähler-Zahlen),
    # dedupliziert pro rotationId, gecacht, mit Stunden-Notbremse. Schlägt
    # etwas fehl, läuft der Roster-Import unverändert weiter — der Pickup ist
    # eine Zugabe, nie eine Vorbedingung.
    _pickups = None
    try:
        _rns = pickup_rotation_ids(resp)
        if _rns:
            _pickups = rotation_pickups_for(token, _rns)
    except Exception as e:
        log.warning('[lh_flightops] pickup lookup: %s', type(e).__name__)
        _pickups = None
    ics = duty_events_to_ics(resp, pickups=_pickups)
    if not ics:
        return jsonify({'ok': True, 'events_count': 0, 'source': 'flightops',
                        'detail': 'no_events'}), 200
    # FALLBACK-EBENE (Owner 2026-07-27): fehlt der Pickup in der Primärquelle,
    # holt ihn der gespeicherte Kalender-Link (myTime) nach — pro Tag, nur wo
    # oben nichts stand. Siehe apply_ical_pickup_fallback. Nie eine Vorbedingung.
    ics = apply_ical_pickup_fallback(token, ics, body.get('pickup_ical_url'))
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
    _st, _acc = _access_state(token)
    if _st == 'pending':
        return jsonify({'ok': False, 'error': 'token_refresh_pending'}), 503
    if _st != 'ok':
        return jsonify({'ok': False, 'error': 'not_connected'}), 401
    body = request.get_json(silent=True) or {}
    service = (body.get('service') or '').strip().upper()
    if service not in FLIGHTOPS_SERVICES:
        return jsonify({'ok': False, 'error': 'unknown_service',
                        'services': list(FLIGHTOPS_SERVICES)}), 400
    params = body.get('params') if isinstance(body.get('params'), dict) else {}
    return jsonify({'ok': True, 'service': service,
                    'response': service_get(token, service, params)})


# ── Crew-Listen Last-Good-Cache (Owner 2026-07-24: „Crew soll gecached
# bleiben, damit bei totem Grant nichts Leeres dasteht") ─────────────────────
# Durabel im Profil-Mirror (wie flightops_tokens — überlebt Redeploys, alle
# Container sehen denselben Stand). LRU-gekappt auf die letzten Legs, damit
# das Profil nicht wächst; ein Crew-Eintrag ist klein (Name/PK/Kategorie).
_CREW_CACHE_MAX = 8


def _crew_cache_get(token, flight, date):
    try:
        import app as _app
        prof = ((_app._profile_load(token) or {}).get('profile') or {})
        for e in (prof.get('flightops_crew_cache') or []):
            if (str(e.get('flight') or '') == str(flight or '')
                    and str(e.get('date') or '')[:10] == str(date or '')[:10]):
                return e
    except Exception:
        pass
    return None


def _crew_cache_put(token, flight, date, crew):
    try:
        import app as _app
        pf = _app._profile_load(token) or {}
        prof = (pf.get('profile') or {})
        lst = [e for e in (prof.get('flightops_crew_cache') or [])
               if not (str(e.get('flight') or '') == str(flight or '')
                       and str(e.get('date') or '')[:10] == str(date or '')[:10])]
        lst.append({'flight': flight, 'date': str(date or '')[:10],
                    'crew': crew, 'cached_at': time.time()})
        prof['flightops_crew_cache'] = lst[-_CREW_CACHE_MAX:]
        _app._profile_save(token, prof)
    except Exception as e:
        log.warning('[lh_flightops] crew_cache_put: %s', type(e).__name__)


@lh_flightops_bp.route('/api/lh/flightops/crewlist/<token>', methods=['POST'])
def flightops_crewlist(token):
    """„Wer fliegt mit" für ein Leg (COMMON_CREWLIST → normalisiert). Body
    {flight, date, dep, arr, access?}. Ohne `access` wird der accessCode aus
    den Duty-Events-_links aufgelöst (Cache → Live-Nachladen des Tages) — die
    App muss ihn also NICHT kennen. Parser gegen echte Shape verifiziert.

    LAST-GOOD-CACHE (Owner 2026-07-24): jede erfolgreiche Liste wird pro Leg
    im Profil-Mirror persistiert. Ist der Grant tot (needs_relogin), der
    accessCode nicht auflösbar oder LH down, kommt die LETZTE Liste mit
    `cached:true` statt eines Fehlers — die Crew-Fläche ist nie leer, ein
    Relogin fällt im Feature nicht als Loch auf."""
    b = request.get_json(silent=True) or {}
    flight, date = b.get('flight'), b.get('date')
    dep, arr = b.get('dep'), b.get('arr')

    def _cached():
        e = _crew_cache_get(token, flight, date)
        if e and e.get('crew'):
            return jsonify({'ok': True, 'crew': e['crew'], 'cached': True,
                            'cached_at': e.get('cached_at')})
        return None

    if not _valid_access(token):
        return _cached() or (jsonify({'ok': False, 'error': 'not_connected'}), 401)
    access = (b.get('access') or '').strip()
    if not access:
        p = _resolve_link_params(token, 'crewlist', flight, date, dep, arr) or {}
        access = p.get('accessCode') or ''
        dep = dep or p.get('departureAirport')
        arr = arr or p.get('arrivalAirport')
    if not access:
        # Kein Link = Flug nicht im eigenen Roster → LH würde eh 401/403 geben.
        return _cached() or (jsonify({'ok': False, 'error': 'no_access_code'}), 404)
    resp = crew_list(token, flight, date, dep, arr, access)
    if not isinstance(resp, dict) or resp.get('processingErrors'):
        return _cached() or (jsonify({'ok': False, 'error': 'crewlist_unavailable'}), 502)
    crew = parse_crew_list(resp)
    # AeroX-Profil-Verknüpfung (Owner 2026-07-23): wer aus der Crew ist selbst
    # auf AeroX? → Avatar/Profil direkt aus der Liste öffnen.
    matches = _match_aerox_profiles(crew)
    for m in crew:
        p = matches.get(str(m.get('pk') or m.get('name') or ''))
        if p:
            m['aerox'] = p
    if crew:
        _crew_cache_put(token, flight, date, crew)
    return jsonify({'ok': True, 'crew': crew})


@lh_flightops_bp.route('/api/lh/flightops/checkin/<token>', methods=['POST'])
def flightops_checkin(token):
    """Briefing-/Check-in-Zeiten für ein Leg (COMMON_CHECK_IN_TIMES →
    normalisiert). Body {flight, date, dep, arr, duty_type?, crew_category?}.
    Bevorzugt die fertigen Link-Params aus den Duty-Events (die tragen schon
    dutyType/crewCategory korrekt); sonst Doku-Defaults OD/COC. ±6-Tage-
    Fenster lt. Doku — außerhalb 404."""
    _st, _acc = _access_state(token)
    if _st == 'pending':
        return jsonify({'ok': False, 'error': 'token_refresh_pending'}), 503
    if _st != 'ok':
        return jsonify({'ok': False, 'error': 'not_connected'}), 401
    b = request.get_json(silent=True) or {}
    flight, date = b.get('flight'), b.get('date')
    p = _resolve_link_params(token, 'checkintimes', flight, date,
                             b.get('dep'), b.get('arr'))
    if p:
        resp = service_get(token, 'COMMON_CHECK_IN_TIMES', p)
    else:
        resp = check_in_times(token, flight, date, b.get('dep'), b.get('arr'),
                              duty_type=(b.get('duty_type') or 'OD'),
                              crew_category=(b.get('crew_category') or 'COC'))
    times = parse_check_in_times(resp)
    if not times:
        return jsonify({'ok': False, 'error': 'checkin_unavailable'}), 502
    return jsonify({'ok': True, 'times': times})


@lh_flightops_bp.route('/api/lh/flightops/hotel/<token>', methods=['POST'])
def flightops_hotel(token):
    """Layover-Hotel für eine Station (COMMON_CREW_HOTEL_INFO → normalisiert).
    Body {station, provider?}. Parser gegen echte Shape verifiziert."""
    _st, _acc = _access_state(token)
    if _st == 'pending':
        return jsonify({'ok': False, 'error': 'token_refresh_pending'}), 503
    if _st != 'ok':
        return jsonify({'ok': False, 'error': 'not_connected'}), 401
    b = request.get_json(silent=True) or {}
    resp = crew_hotel(token, b.get('station'), b.get('provider'))
    return jsonify({'ok': True, 'hotels': parse_crew_hotel(resp),
                    'station': (b.get('station') or '').upper()})


# ── Periodischer Voll-Refresh (Cron → Poll-Service :8081) ────────────────────
# Der on-demand-Refresh in app.py (_maybe_refresh_flightops) feuert nur wenn
# der User (oder ein Freund) get_briefings auslöst — App zu = kein Refresh =
# (a) Roster-Änderungen erreichen die Push-Kette nicht und (b) der Refresh-
# Token wird nie benutzt und kann LH-seitig idle sterben (Lebensdauer ist
# UNDOKUMENTIERT). Dieser Endpoint refresht ALLE verbundenen User im
# Hintergrund-Thread: Token-Grant warmhalten + import→diff→push-Kette auch
# bei geschlossener App. Auth wie poll-boards (X-Poll-Secret / localhost).
_refresh_all_lock = threading.Lock()
_refresh_all_state = {'running': False, 'last': None, 'drain': False}
_refresh_all_thread = [None]     # [Thread|None] — für den Exit-Drain

# EXIT-DRAIN (Massen-Burn 26./27.07.2026): der Deploy-Drain in
# deploy-hetzner.sh schützt NUR vor Deploys. gunicorn recycelt den
# Poll-Worker aber auch per --max-requests (~alle 1–2 h bei aktueller
# poll-tick-Rate) — und ein refresh-all-Lauf dauert mit 500+ Usern
# inzwischen 60–90 min: Recycle mitten im Lauf killte den Daemon-Thread
# hart, ggf. zwischen LH-Rotation und Persist (= Familien-Tod), und ließ
# den Rest der Liste unrefresht. atexit läuft beim GRACEFUL Worker-Exit
# (max-requests wie SIGTERM) VOR dem Abräumen der Daemon-Threads: Drain
# setzen + auf das Ende des aktuellen Grants warten (deutlich unter
# gunicorns --graceful-timeout 60).
_EXIT_DRAIN_JOIN_SEC = 45


def _refresh_all_exit_drain():
    """Wirft nie (läuft in atexit; Tests ersetzen threading.Thread durch
    synchrone Fakes ohne is_alive — duck-typed prüfen)."""
    try:
        th = _refresh_all_thread[0]
        if not (th and getattr(th, 'is_alive', lambda: False)()):
            return
        with _refresh_all_lock:
            _refresh_all_state['drain'] = True
        log.info('[flightops-refresh-all] worker-exit -> drain, warte auf '
                 'laufenden Grant (max %ds)', _EXIT_DRAIN_JOIN_SEC)
        th.join(_EXIT_DRAIN_JOIN_SEC)
        if th.is_alive():
            log.error('[flightops-refresh-all] exit-drain TIMEOUT — Thread '
                      'lebt noch, möglicher Mid-Rotation-Kill')
    except Exception:
        pass


import atexit as _atexit
_atexit.register(_refresh_all_exit_drain)


def _internal_secret_ok():
    """Gleiche Auth wie /api/internal/poll-boards (vgl. lh_mqtt._secret_ok)."""
    import hmac as _hmac
    secret = os.environ.get('ADSB_POLL_SECRET', '').strip()
    if secret:
        provided = (request.headers.get('X-Poll-Secret') or '').strip()
        return bool(provided) and _hmac.compare_digest(provided, secret)
    return (request.remote_addr or '') in ('127.0.0.1', '::1')


def _connected_tokens(limit=2000):
    """Alle AeroX-User-Tokens mit gespeicherten FlightOps-Tokens. Quelle:
    Supabase user_profiles.metadata->flightops_tokens (SB ist prod-primary).
    PostgREST kappt bei 1000 → paginiert lesen. Leer ohne SB (Dev)."""
    try:
        import app as _app
        if not getattr(_app, 'SB_AVAILABLE', False):
            return []
        out, page, size = [], 0, 500
        while len(out) < limit:
            r = (_app.sb.table('user_profiles').select('token')
                 .filter('metadata->flightops_tokens', 'not.is', 'null')
                 .range(page * size, page * size + size - 1).execute())
            rows = r.data or []
            out += [row.get('token') for row in rows if row.get('token')]
            if len(rows) < size:
                break
            page += 1
        return out
    except Exception as e:
        log.warning('[lh_flightops] connected_tokens: %s', type(e).__name__)
        return []


# ── ADAPTIVE SYNC-KADENZ (Quota-Diät 2026-07-28) ────────────────────────────
# Der Host-Cron ruft refresh-all weiter alle 2 h (unverändert) — aber nicht
# mehr jeder Lauf synct jeden User. Wer in den nächsten 48 h Dienst hat, wird
# eng getaktet (≈alle 4 h), alle anderen locker (≈alle 12 h). Ein Roster ohne
# anstehenden Dienst ändert sich selten und der User schaut auch nicht drauf;
# ein Roster mit Dienst morgen ist das Kernprodukt.
_FO_SYNC_NEAR_S = 3.5 * 3600
_FO_SYNC_FAR_S = 11.5 * 3600
_FO_DUTY_NEAR_S = 48 * 3600
# Letzter Sync je Token — bewusst PROZESS-LOKAL (der refresh-all-Lauf lebt
# ohnehin nur im Poll-Container). Nach einem Neustart/Deploy ist die Map leer
# ⇒ jeder Token ist einmal fällig. Das ist gewollt: nach einem Deploy einmal
# alle synchronisieren ist billig (1 Lauf) und stellt Frische her.
_fo_last_sync = {}
_FO_LAST_SYNC_CAP = 8000
# Demand-Vorlauf: wie lange ein Lauf auf die Rotation abgelaufener Grants
# wartet, bevor er sie überspringt (Refresher-Tick ist 60 s).
_FO_DEMAND_WAIT_S = 120
_FO_DEMAND_WAIT_STEP_S = 5


def _fo_day_has_duty(ev):
    """Trägt dieser Briefing-Tag Flug-/Dienst-Evidenz? Nutzt AUSSCHLIESSLICH
    Felder, die die Briefing-Row wirklich führt (ical_sectors, ical_klass,
    ical_summary) — nichts Erfundenes. Wirft nie."""
    if not isinstance(ev, dict):
        return False
    secs = ev.get('ical_sectors')
    if isinstance(secs, list) and secs:
        return True                       # echte Legs = Dienst, fertig
    klass = str(ev.get('ical_klass') or '').strip().lower()
    if klass in ('hotel_layover', 'standby'):
        return True                       # unterwegs bzw. Bereitschaft
    try:
        from blueprints.crew_live_state import duty_from_roster_day
        d = duty_from_roster_day(ev.get('ical_klass'), ev.get('ical_summary'))
    except Exception:
        d = None
    # 'free'/'vacation'/'visa' sind explizit KEIN Dienst; None heißt „nicht
    # erkannt" und gilt hier als kein Nachweis (die Kadenz fällt dann auf die
    # lockere Regel zurück — sie verschiebt nur, sie verliert nichts).
    return d in ('standby', 'reserve')


def _fo_duty_within(token, now=None, horizon_s=_FO_DUTY_NEAR_S):
    """Hat dieser User innerhalb des Horizonts (Default 48 h) Dienst?
    Kleinstes Briefing-Datum ≥ heute mit Dienst-Evidenz; verglichen wird
    dessen Tagesbeginn (UTC) — der Dienst kann also frühestens dann
    beginnen. Kein Treffer / keine Daten ⇒ False („kein Dienst bekannt").
    Wirft nie."""
    now = now or time.time()
    try:
        import app as _app
        briefs = _app._ical_briefings_load(token) or {}
    except Exception:
        return False
    from datetime import datetime as _d, timezone as _tz
    today = _d.fromtimestamp(now, _tz.utc).strftime('%Y-%m-%d')
    best = None
    for datum, ev in (briefs.items() if isinstance(briefs, dict) else []):
        ds = str(datum)[:10]
        if len(ds) != 10 or ds < today:
            continue
        if best is not None and ds >= best:
            continue
        if _fo_day_has_duty(ev):
            best = ds
    if not best:
        return False
    try:
        start = _d.strptime(best, '%Y-%m-%d').replace(
            tzinfo=_tz.utc).timestamp()
    except Exception:
        return False
    return (start - now) < horizon_s


def _fo_should_sync(token, now=None):
    """(bool, grund) — synct DIESER refresh-all-Lauf diesen Token?
    Erst-Kontakt immer; sonst 3,5 h bei Dienst in Sicht, 11,5 h sonst.
    Die Briefings werden nur im Graubereich dazwischen gelesen (spart den
    Supabase-Read für die klaren Fälle)."""
    now = now or time.time()
    last = _fo_last_sync.get(token)
    if last is None:
        return True, 'first'
    age = now - last
    if age >= _FO_SYNC_FAR_S:
        return True, 'far_due'
    if age < _FO_SYNC_NEAR_S:
        return False, 'too_soon'
    if _fo_duty_within(token, now):
        return True, 'duty_near'
    return False, 'no_duty_near'


def _fo_mark_synced(token, now=None):
    """Sync-Versuch stempeln. Bewusst beim VERSUCH, nicht erst beim Erfolg:
    der LH-Call ist raus und hat Kontingent gekostet — ein 502 darf nicht dazu
    führen, dass derselbe Token in jedem 2-h-Lauf erneut dagegenläuft."""
    try:
        _fo_last_sync[token] = now or time.time()
        if len(_fo_last_sync) > _FO_LAST_SYNC_CAP:
            for k in sorted(_fo_last_sync,
                            key=lambda k: _fo_last_sync[k])[:_FO_LAST_SYNC_CAP // 2]:
                _fo_last_sync.pop(k, None)
    except Exception:
        pass


def _fo_demand_prephase(tokens):
    """Demand-Vorlauf vor der Import-Schleife: alle Tokens, deren Access-Token
    abgelaufen ist, beim Refresher anmelden und ihm kurz Zeit geben.

    WARUM: seit der Lazy Rotation hält der Refresher ATs nicht mehr auf Vorrat
    frisch — ohne diesen Vorlauf würde der 2-h-Lauf reihenweise 'pending' sehen
    und die Roster gar nicht erst holen. Der Refresher tickt alle 60 s im
    SELBEN Container, 120 s Wartezeit decken also einen vollen Tick plus
    Rotationsdauer ab. Drain-aware (Deploy killt uns sonst mitten drin).
    Returns Anzahl der noch immer abgelaufenen Grants."""
    pend = []
    for tok in tokens:
        try:
            if _access_state(tok)[0] == 'pending':
                _refresher_demand_add(tok)
                # Best-effort auch übers Netz — falls refresh-all je aus einem
                # Container ohne Refresher angestoßen wird.
                _rotate_poke_remote(tok)
                pend.append(tok)
        except Exception:
            pass
    if not pend:
        return 0
    log.info('[flightops-refresh-all] demand-vorlauf: %d abgelaufene Grants '
             'angemeldet, warte max %ds', len(pend), _FO_DEMAND_WAIT_S)
    waited = 0
    while waited < _FO_DEMAND_WAIT_S and pend:
        if _refresh_all_state.get('drain'):
            break
        time.sleep(_FO_DEMAND_WAIT_STEP_S)
        waited += _FO_DEMAND_WAIT_STEP_S
        # Zustand nur alle 15 s nachlesen — jeder Check ist ein Profil-Read
        # pro Token, ein 5-s-Takt wäre reine Supabase-Last.
        if waited % 15:
            continue
        try:
            pend = [t for t in pend if _access_state(t)[0] == 'pending']
        except Exception:
            break
    if pend:
        log.info('[flightops-refresh-all] demand-vorlauf: %d Grants weiter '
                 'pending -> werden übersprungen', len(pend))
    return len(pend)


def _refresh_all_work(tokens):
    ok = fail = skipped = deferred = 0
    try:
        import app as _app
        # KADENZ-PLANUNG vor allem anderen: was dieser Lauf ohnehin nicht
        # synct, braucht auch keinen Demand-Vorlauf und keine Rotation.
        _now0 = time.time()
        plan = []
        for _tok in tokens:
            _do, _why = _fo_should_sync(_tok, _now0)
            if _do:
                plan.append(_tok)
            else:
                deferred += 1
        if plan and not _refresh_all_state.get('drain'):
            _fo_demand_prephase(plan)
        for tok in plan:
            # DEPLOY-DRAIN (Grant-Burn #3, 2026-07-26): dieser Daemon-Thread
            # wurde beim Container-Recreate HART gekillt — traf der Kill das
            # Fenster zwischen LH-Rotation und _tokens_save, war der neue
            # Refresh-Token weg und der naechste Versuch verbrannte per
            # Reuse-Detection die ganze Familie (29/126 Grants, Cluster exakt
            # an den Deploy-Zeitpunkten). deploy-hetzner.sh setzt vor dem
            # Recreate das drain-Flag und wartet bis running=False — hier
            # deshalb VOR jedem Grant pruefen und sauber abbrechen (der
            # aktuelle Grant persistiert fertig, kein neuer LH-Call startet).
            if _refresh_all_state.get('drain'):
                log.info('[flightops-refresh-all] drain angefordert — '
                         'Abbruch nach %d/%d Grants', ok + fail + skipped,
                         len(plan))
                break
            try:
                _st, _acc = _access_state(tok)
                if _st != 'ok':
                    # needs_relogin / Tokens weg / AT abgelaufen (dann ist der
                    # Grant beim Refresher vorgemerkt — dieser Lauf importiert
                    # nur, refresht seit dem Umbau 2026-07-27 NIE selbst).
                    skipped += 1
                    continue
                # background-Flag → niedrigere Budget-Grenze im Key-Gate
                # (interaktive Connects/Refreshes behalten Headroom).
                with _app.app.test_request_context(json={'background': 1}):
                    rv = flightops_import(tok)
                status = rv[1] if isinstance(rv, tuple) else 200
                if status == 200:
                    ok += 1
                    # NUR Erfolg stempelt den Sync (Review-Fund 2026-07-28):
                    # ein LH-Schluckauf darf den User nicht 3,5–11,5 h ohne
                    # Retry lassen — Fehlläufe bleiben fällig für den
                    # nächsten 2-h-Cron-Lauf.
                    _fo_mark_synced(tok)
                else:
                    fail += 1
            except Exception as e:
                fail += 1
                log.warning('[flightops-refresh-all] tok=%s %s',
                            (tok or '')[:8], type(e).__name__)
            # Service-QPS-Schonung (Sandbox zeigte 2/sec pro Service → 403
            # „Developer Over Qps"; Prod-Plan 20/sec, trotzdem sanft bleiben).
            time.sleep(0.7)
    finally:
        with _refresh_all_lock:
            _refresh_all_state['running'] = False
            _refresh_all_state['last'] = {
                'ts': time.time(), 'users': len(tokens),
                'ok': ok, 'fail': fail, 'skipped': skipped,
                'deferred': deferred}
        log.info('[flightops-refresh-all] done users=%d ok=%d fail=%d '
                 'skipped=%d deferred=%d',
                 len(tokens), ok, fail, skipped, deferred)


@lh_flightops_bp.route('/api/internal/flightops/refresh-drain', methods=['POST'])
def flightops_refresh_drain():
    """Deploy-Vorbereitung: laufenden refresh-all-Lauf UND den Refresher-Loop
    sauber auslaufen lassen (kein neuer LH-Call startet, der aktuelle Grant
    persistiert fertig). Der Deploy pollt bis running=False. Idempotent —
    der Container wird danach ohnehin neu erstellt (frischer Prozess hebt
    das Drain wieder auf)."""
    if not _internal_secret_ok():
        return jsonify({'ok': False, 'error': 'forbidden'}), 403
    with _refresh_all_lock:
        _refresh_all_state['drain'] = True
        running = bool(_refresh_all_state['running'])
    _refresher_state['drain'] = True
    running = running or bool(_refresher_state.get('busy'))
    return jsonify({'ok': True, 'running': running,
                    'refresher_active': bool(_refresher_state.get('active'))})


@lh_flightops_bp.route('/api/internal/flightops/rotate-poke', methods=['POST'])
def flightops_rotate_poke():
    """Cross-Container-Demand für die Lazy Rotation: der Web-Container meldet
    hier »dieser Grant wird JETZT gebraucht« an den Poll-Container, in dem der
    einzige Refresher lebt. Setzt NUR ein Flag — rotiert selbst NICHTS (das
    Choke-Point-Gate in _refresh bleibt unangetastet). Auth wie poll-boards."""
    if not _internal_secret_ok():
        return jsonify({'ok': False, 'error': 'forbidden'}), 403
    tok = ((request.get_json(silent=True) or {}).get('token') or '').strip()
    if not tok:
        return jsonify({'ok': False, 'error': 'no_token'}), 400
    queued = _refresher_demand_add(tok)
    return jsonify({'ok': True, 'queued': bool(queued)})


@lh_flightops_bp.route('/api/internal/flightops/refresh-all', methods=['POST'])
def flightops_refresh_all():
    if not _internal_secret_ok():
        return jsonify({'ok': False, 'error': 'forbidden'}), 403
    if not flightops_configured():
        return jsonify({'ok': True, 'skipped': 'not_configured'})
    with _refresh_all_lock:
        if _refresh_all_state['running']:
            return jsonify({'ok': True, 'already_running': True,
                            'last': _refresh_all_state['last']})
        _refresh_all_state['running'] = True
        _refresh_all_state['drain'] = False
    tokens = _connected_tokens()
    if not tokens:
        with _refresh_all_lock:
            _refresh_all_state['running'] = False
        return jsonify({'ok': True, 'users': 0,
                        'last': _refresh_all_state['last']})
    th = threading.Thread(target=_refresh_all_work, args=(tokens,),
                          daemon=True)
    _refresh_all_thread[0] = th
    th.start()
    return jsonify({'ok': True, 'started': True, 'users': len(tokens),
                    'last': _refresh_all_state['last']})


# ═════════════════════════════════════════════════════════════════════════════
# DER REFRESHER — der EINZIGE Prozessteil im ganzen System, der je einen
# LH-Refresh-Token benutzt (Architektur-Umbau 2026-07-27, siehe Banner über
# _refresh). Läuft als ein Daemon-Thread im Poll-Container
# (LH_FLIGHTOPS_REFRESHER=1 in der Compose, sonst startet nichts); ein flock
# auf dem Container-FS garantiert Einzigkeit auch über den kurzen
# gunicorn-Worker-Recycle-Overlap hinweg. Der Loop hält alle Access-Tokens
# proaktiv frisch (Rotation _REFRESH_AHEAD_S vor Ablauf, mit Jitter und
# QPS-Abstand) — Web-Worker sehen dadurch praktisch nie einen abgelaufenen AT.
# ═════════════════════════════════════════════════════════════════════════════
_REFRESHER_TICK_S = 60           # Scan-Takt (drain-aware, 1-s-Granularität)
_REFRESHER_GRANT_GAP_S = 1.0     # Mindestabstand zwischen zwei Rotationen
_REFRESHER_LOCKFILE = '/tmp/lh_flightops_refresher.lock'
_refresher_state = {'active': False, 'drain': False, 'busy': False,
                    'last_tick': 0.0, 'last': None, 'active_since': 0.0}
_refresher_thread = [None]
_refresher_lock_fh = [None]      # offenes flock-Handle (hält den Lock am Leben)


def _refresher_enabled():
    return (os.environ.get('LH_FLIGHTOPS_REFRESHER') or '').strip() == '1'


def _refresher_scan():
    """(token, tokens)-Paare aller verbundenen Grants, paginiert aus SB
    (PostgREST kappt bei 1000). Eine Query pro Tick — der Loop braucht nur
    expires_at/needs_relogin/refresh-Präsenz zur Fällig-Entscheidung; die
    eigentliche Rotation lädt ihren Stand ohnehin fresh."""
    out = []
    try:
        import app as _app
        if not getattr(_app, 'SB_AVAILABLE', False):
            return out
        page, size = 0, 500
        while True:
            r = (_app.sb.table('user_profiles')
                 .select('token,metadata->flightops_tokens')
                 .filter('metadata->flightops_tokens', 'not.is', 'null')
                 .range(page * size, page * size + size - 1).execute())
            rows = r.data or []
            for row in rows:
                t = row.get('flightops_tokens')
                if row.get('token') and isinstance(t, dict):
                    out.append((row['token'], t))
            if len(rows) < size:
                break
            page += 1
    except Exception as e:
        log.warning('[fo-refresher] scan: %s', type(e).__name__)
    return out


# Keepalive-Abstand der Lazy-Rotation: JEDER gesunde Grant rotiert mindestens
# einmal in diesem Fenster, auch ohne jeden Bedarf — der LH-Refresh-Token darf
# nicht idle sterben (Lebensdauer LH-seitig UNDOKUMENTIERT, deshalb bewusst
# konservativ deutlich unter 24 h).
_REFRESHER_KEEPALIVE_S = 20 * 3600
# Angenommene AT-Lebensdauer für die Rückrechnung des letzten Rotations-
# Zeitpunkts (s. _refresher_due). LH liefert expires_in=3600, _token_request
# speichert expires_at = now + (expires_in − 60).
_REFRESHER_AT_LIFETIME_S = 3600
# Rotations-Status, nach denen der Demand-Eintrag STEHEN bleibt (der Versuch
# ist vertagt, nicht erledigt). Alle anderen quittiert der Refresher.
_REFRESHER_DEMAND_RETRY_STATES = frozenset((
    'transient', 'skipped_claim_unavailable', 'skipped_claim_foreign',
    'refused', 'error', 'save_pending'))


def _refresher_due(scan, now=None, demand=None):
    """Fällige Grants, am knappsten ablaufende zuerst. needs_relogin und
    Grants ohne RT fallen raus; geparkte behandelt _refresher_refresh_grant
    selbst (nur Nachsave, keine Rotation).

    LAZY ROTATION (Quota-Diät 2026-07-28) — der große Kostenblock: ATs leben
    ~1 h, der Vorlauf ist 15 min, also war JEDER Grant bisher rund 32×/Tag
    fällig (gemessen 5.381 oauth_refresh/Tag) — Dauerfrische für Roster, die
    zwölfmal am Tag angefasst werden. »Läuft bald ab« ist deshalb nur noch die
    NOTWENDIGE Bedingung; zusätzlich muss EINES gelten:

      a) DEMAND — jemand braucht den Grant JETZT (User-Import auf abgelaufenem
         AT bzw. Demand-Vorlauf des Sync-Laufs, s. _refresher_demand).
      b) KEEPALIVE — die letzte Rotation ist länger als _REFRESHER_KEEPALIVE_S
         her. Damit rotiert jeder gesunde Grant garantiert ~1×/20 h und der RT
         kann nicht idle ablaufen.

    EHRLICHE ABLEITUNG von `last_rotated`: es gibt KEINEN rotated_at-Stempel im
    Token-Dict. Da der Refresher der einzige Schreiber von expires_at ist und
    _token_request expires_at = now + (expires_in−60) setzt, gilt
    last_rotated ≈ expires_at − _REFRESHER_AT_LIFETIME_S. Der Fehler ist die
    60-s-Sicherheitsmarge (die Schätzung liegt ~1 min ZU FRÜH ⇒ minimal
    eifriger, nie zu spät). Fehlt expires_at ganz (0), ist der Grant nach
    dieser Rechnung uralt ⇒ keepalive-fällig — die sichere Richtung."""
    now = now or time.time()
    demand = _refresher_demand if demand is None else demand
    due = []
    for tok, t in scan:
        if t.get('needs_relogin') or not t.get('refresh'):
            continue
        exp = t.get('expires_at') or 0
        if exp - now >= _REFRESH_AHEAD_S:
            continue
        last_rotated = exp - _REFRESHER_AT_LIFETIME_S
        if not (tok in demand
                or (now - last_rotated) > _REFRESHER_KEEPALIVE_S):
            continue
        due.append((exp, tok))
    due.sort()
    return [tok for _exp, tok in due]


def _refresher_tick():
    # Die Vormerkliste ist echte Nachfrage: sie füllt sich nur, wenn ein
    # Consumer IN DIESEM Prozess auf einen abgelaufenen AT gelaufen ist.
    # Deshalb zählt sie (wie bisher für die Reihenfolge) jetzt auch als
    # Demand-Quelle für die Fällig-Entscheidung.
    wanted = _refresh_wanted_drain()
    due = _refresher_due(_refresher_scan(),
                         demand=set(_refresher_demand) | wanted)
    # Vorgemerkte (ein Worker sah einen abgelaufenen AT) zuerst — aber nur,
    # wenn der durable Stand die Fälligkeit bestätigt; sonst war die
    # Vormerkung stale und wird verworfen.
    ordered = ([t for t in due if t in wanted]
               + [t for t in due if t not in wanted])
    stats = {}
    for tok in ordered:
        if _refresher_state['drain']:
            break
        _refresher_state['busy'] = True
        try:
            st = _refresher_refresh_grant(tok)
        except Exception as e:
            st = 'error'
            log.warning('[fo-refresher] grant tok=%s %s',
                        (tok or '')[:8], type(e).__name__)
        finally:
            _refresher_state['busy'] = False
        stats[st] = stats.get(st, 0) + 1
        # Demand quittieren — außer bei den Status, die exakt „nochmal
        # versuchen" bedeuten (Claim-Infra weg, LH transient): dort bleibt der
        # Bedarf stehen, sonst müsste der User erneut anklopfen.
        if st not in _REFRESHER_DEMAND_RETRY_STATES:
            _refresher_demand.discard(tok)
        # QPS-Schonung + Jitter: Rotationen entzerren sich selbst, statt
        # stündliche Refresh-Wellen zu bilden.
        time.sleep(_REFRESHER_GRANT_GAP_S + secrets.randbelow(600) / 1000.0)
    _refresher_state['last'] = {'ts': time.time(), 'due': len(ordered),
                                'stats': stats}
    if ordered:
        log.info('[fo-refresher] tick due=%d %s', len(ordered), stats)


def _refresher_main():
    """Loop-Rumpf: flock erwerben (Einzigkeit im Container), sich als DER
    Rotations-Thread registrieren (Choke-Point-Gate in _refresh), dann
    ticken bis zum Drain (Deploy/Worker-Exit)."""
    import fcntl
    fh = None
    while fh is None:
        if _refresher_state['drain']:
            return
        f = None
        try:
            f = open(_REFRESHER_LOCKFILE, 'a+')
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fh = f
        except OSError:
            # Alt-Worker hält den Loop noch (Recycle-Overlap) — warten.
            try:
                if f:
                    f.close()
            except Exception:
                pass
            time.sleep(15)
    _refresher_lock_fh[0] = fh
    _REFRESHER_THREAD_ID[0] = threading.get_ident()
    _refresher_state['active'] = True
    _refresher_state['active_since'] = time.time()
    log.info('[fo-refresher] aktiv pid=%s — einziger RT-Rotierer des Systems',
             os.getpid())
    try:
        while not _refresher_state['drain']:
            try:
                _refresher_tick()
            except Exception as e:
                log.warning('[fo-refresher] tick: %s', type(e).__name__)
            _refresher_state['last_tick'] = time.time()
            for _i in range(_REFRESHER_TICK_S):
                if _refresher_state['drain']:
                    break
                time.sleep(1)
    finally:
        _refresher_state['active'] = False
        log.info('[fo-refresher] beendet (drain/exit)')


def _maybe_start_refresher():
    """Startet den Loop GENAU DANN, wenn dieser Container die Refresher-Rolle
    trägt (Compose-Env) und FlightOps konfiguriert ist. In Web-/MQTT-
    Containern und in Tests passiert hier nichts — strukturell, nicht per
    Konvention."""
    if not (_refresher_enabled() and flightops_configured()):
        return None
    if _refresher_thread[0] is not None:
        return _refresher_thread[0]
    th = threading.Thread(target=_refresher_main, daemon=True,
                          name='fo-refresher')
    _refresher_thread[0] = th
    th.start()
    return th


def _refresher_exit_drain():
    """atexit-Zwilling von _refresh_all_exit_drain für den Refresher-Loop:
    Worker-Recycle (gunicorn --max-requests) und SIGTERM warten auf das Ende
    der LAUFENDEN Rotation, statt sie zwischen LH-Rotation und Persist zu
    killen. Wirft nie."""
    try:
        th = _refresher_thread[0]
        if not (th and getattr(th, 'is_alive', lambda: False)()):
            return
        _refresher_state['drain'] = True
        log.info('[fo-refresher] worker-exit -> drain, warte auf laufende '
                 'Rotation (max %ds)', _EXIT_DRAIN_JOIN_SEC)
        th.join(_EXIT_DRAIN_JOIN_SEC)
        if th.is_alive():
            log.error('[fo-refresher] exit-drain TIMEOUT — möglicher '
                      'Mid-Rotation-Kill')
    except Exception:
        pass


_atexit.register(_refresher_exit_drain)


# ── WÄCHTER (Leitplanke 5): needs_relogin-Anstieg + Refresher-Herzschlag ─────
# Host-Cron (stündlich, :07) → dieser Endpoint auf :8081. Alarmiert per
# Resend-Mail wenn (a) needs_relogin um mehr als N in ~1h steigt (Burn-Muster)
# oder (b) der Refresher-Loop fehlt/steht, obwohl er konfiguriert ist —
# ohne ihn rotiert NIEMAND mehr (fail-closed heißt: still veraltende ATs,
# kein Burn; genau deshalb muss das Fehlen laut sein).
_RELOGIN_WATCH_STATE = '/tmp/fo_relogin_watch.json'
_RELOGIN_ALERT_DEFAULT_N = 5
_RELOGIN_ALERT_COOLDOWN_S = 3 * 3600


def _relogin_count():
    """Zahl der Grants mit needs_relogin=true (SB, count-only). None bei
    Fehler/ohne SB — der Wächter meldet dann bewusst nichts Falsches."""
    try:
        import app as _app
        if not getattr(_app, 'SB_AVAILABLE', False):
            return None
        r = (_app.sb.table('user_profiles').select('token', count='exact')
             .filter('metadata->flightops_tokens->>needs_relogin', 'eq', 'true')
             .limit(1).execute())
        return int(r.count or 0)
    except Exception as e:
        log.warning('[fo-watch] count: %s', type(e).__name__)
        return None


def _fo_watch_alert_mail(reasons, count, delta):
    """Alarm-Mail via Resend (Pattern _mk_send_alert_email). GOTCHA aus dem
    Signup-Notify-Memory: Resend/CF blockt den Python-Default-User-Agent
    (403/1010) — expliziter UA-Header ist Pflicht. Failures nur loggen."""
    api_key = (os.environ.get('RESEND_API_KEY') or '').strip()
    to_email = (os.environ.get('SUPPORT_NOTIFY_EMAIL')
                or 'miguel.schumann@icloud.com').strip()
    if not api_key:
        log.warning('[fo-watch] RESEND_API_KEY fehlt — Alarm nur im Log: %s',
                    '; '.join(reasons))
        return False
    try:
        import html as _html
        items = ''.join(f'<li>{_html.escape(r)}</li>' for r in reasons)
        payload = json.dumps({
            'from': 'AeroX FlightOps <support@aerosteuer.de>',
            'to': [to_email],
            'subject': f'[AeroX FLIGHTOPS-WACHE] {"; ".join(reasons)[:140]}',
            'html': (f"<h2 style='font-family:sans-serif'>LH-FlightOps-Wächter</h2>"
                     f"<ul style='font-family:sans-serif'>{items}</ul>"
                     f"<p style='font-family:sans-serif'>needs_relogin gesamt: "
                     f"<b>{count}</b> (Δ letzte Stunde: {delta})<br>"
                     f"Forensik: <code>docker logs aerotax-poll | grep "
                     f"'GRANT-BURN\\|fo-refresher'</code></p>"),
        }).encode()
        req = urllib.request.Request(
            'https://api.resend.com/emails', data=payload,
            headers={'Authorization': f'Bearer {api_key}',
                     'Content-Type': 'application/json',
                     'User-Agent': 'AeroX-Backend/1.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            ok = 200 <= resp.status < 300
        log.info('[fo-watch] alert-mail %s', 'sent' if ok else 'FAILED')
        return ok
    except Exception as e:
        log.warning('[fo-watch] mail: %s', type(e).__name__)
        return False


@lh_flightops_bp.route('/api/internal/flightops/relogin-watch',
                       methods=['POST', 'GET'])
def flightops_relogin_watch():
    """Stündlicher Wächter-Check (Host-Cron auf :8081, Auth wie refresh-all).
    Vergleicht needs_relogin mit dem letzten Stand (/tmp-State überlebt
    Worker-Recycles im Container) und prüft den Refresher-Herzschlag."""
    if not _internal_secret_ok():
        return jsonify({'ok': False, 'error': 'forbidden'}), 403
    now = time.time()
    try:
        alert_n = int(os.environ.get('LH_FLIGHTOPS_RELOGIN_ALERT_N')
                      or _RELOGIN_ALERT_DEFAULT_N)
    except ValueError:
        alert_n = _RELOGIN_ALERT_DEFAULT_N
    cnt = _relogin_count()
    prev = {}
    try:
        with open(_RELOGIN_WATCH_STATE) as f:
            prev = json.load(f) or {}
    except Exception:
        prev = {}
    delta = None
    if (cnt is not None and isinstance(prev.get('count'), int)
            and now - (prev.get('ts') or 0) <= 2 * 3600):
        delta = cnt - prev['count']
    reasons = []
    if delta is not None and delta >= alert_n:
        reasons.append(f'needs_relogin +{delta} in ~1h (jetzt {cnt}) — '
                       f'Burn-Muster?')
    if _refresher_enabled():
        if not _refresher_state.get('active'):
            reasons.append('Refresher-Loop NICHT aktiv (konfiguriert, aber '
                           'kein Thread/Lock) — niemand rotiert')
        elif not _refresher_state.get('last_tick'):
            # BOOT-KARENZ (Fehlalarm 27.07. 21:07: Wächter-Cron traf 38 s nach
            # dem pushprefs-Containerstart — last_tick war noch 0.0 und
            # „now − 0 > 15 min" meldete „steht", obwohl der Loop gerade erst
            # startete; Pass lief danach 7/7 sauber durch). Ohne JEMALS einen
            # Tick gab es nichts zu vergleichen: erst meckern, wenn der Loop
            # seit >10 min aktiv ist und immer noch nie getickt hat
            # (Takt ist 60 s — 10 min sind >Erst-Pass, kein echtes Loch).
            if now - (_refresher_state.get('active_since') or now) > 10 * 60:
                reasons.append('Refresher-Loop hat seit Boot NIE getickt '
                               '(>10min aktiv ohne ersten Pass)')
        elif now - (_refresher_state.get('last_tick') or 0) > 15 * 60:
            reasons.append('Refresher-Loop steht (>15min kein Tick)')
    elif flightops_configured():
        reasons.append('LH_FLIGHTOPS_REFRESHER nicht gesetzt — in dieser '
                       'Architektur rotiert dann NIEMAND (ATs laufen ab)')
    alerted = False
    if reasons and now - (prev.get('alerted_at') or 0) > _RELOGIN_ALERT_COOLDOWN_S:
        alerted = _fo_watch_alert_mail(reasons, cnt, delta)
    try:
        with open(_RELOGIN_WATCH_STATE, 'w') as f:
            json.dump({'ts': now, 'count': cnt,
                       'alerted_at': now if alerted else (prev.get('alerted_at') or 0)},
                      f)
    except Exception:
        pass
    return jsonify({'ok': True, 'needs_relogin': cnt, 'delta_1h': delta,
                    'reasons': reasons, 'alerted': alerted,
                    'refresher': {
                        'expected': _refresher_enabled(),
                        'active': bool(_refresher_state.get('active')),
                        'last_tick': _refresher_state.get('last_tick'),
                        'last': _refresher_state.get('last')}})


# Rollen-gesteuerter Autostart (No-Op ohne LH_FLIGHTOPS_REFRESHER=1 bzw. ohne
# Creds — Tests und Web-Container starten hier nie einen Thread).
_maybe_start_refresher()
