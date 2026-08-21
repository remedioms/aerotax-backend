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
import datetime as _dt

from flask import Blueprint, jsonify, request, redirect

from roster_markers import is_cancelled_standby_marker

log = logging.getLogger('aerotax')
lh_flightops_bp = Blueprint('lh_flightops_bp', __name__)


def _roster_v2_lh_enabled():
    allowed = {
        value.strip().upper()
        for value in os.environ.get('AEROX_ROSTER_V2_AIRLINES', '').split(',')
        if value.strip()
    }
    return bool(allowed & {'*', 'LH', 'LUFTHANSA'})


def _roster_v2_shadow_enabled():
    return os.environ.get('AEROX_ROSTER_V2_SHADOW', '').strip().lower() in (
        '1', 'true', 'yes', 'on')

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


def _flow_take(state, expected_user_token=None):
    """Consume a PKCE flow once, optionally bound to its authenticated owner.

    The `/api/me` exchange must not let a bearer exchange another user's
    browser callback.  A mismatched owner deliberately leaves the state intact
    so an unrelated request cannot burn a valid in-progress login.
    """
    now = time.time()
    # Fastpath: derselbe Worker
    with _flow_lock:
        hit = _flow_store.get(state)
        if hit and hit[0] < now:
            _flow_store.pop(state, None)
            hit = None
        if (hit and expected_user_token is not None and
                not secrets.compare_digest(str(hit[1].get('user_token') or ''),
                                           str(expected_user_token))):
            return None
        if hit:
            _flow_store.pop(state, None)
    if hit:
        _flow_rm(state)
        return hit[1]
    # Cross-Worker: von Disk lesen (single-use → löschen)
    try:
        p = _flow_path(state)
        if p and os.path.exists(p):
            with open(p) as f:
                rec = json.load(f)
            if rec.get('exp', 0) < now:
                try:
                    os.remove(p)
                except OSError:
                    pass
                return None
            if (rec.get('exp', 0) >= now and
                    (expected_user_token is None or
                     secrets.compare_digest(str(rec.get('user_token') or ''),
                                            str(expected_user_token)))):
                try:
                    os.remove(p)
                except OSError:
                    pass
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

# ── DEPLOY-FESTER PARK (Vorfall 31.07.–01.08.2026: 275 Familien tot) ─────────
# Der Prozess-Park oben stirbt mit dem Worker — und der Poll-Worker stirbt
# ÖFTER als gedacht (Deploy-Recreate UND gunicorn-Worker-Recycle). Ein
# geparkter neuer RT ist das einzige lebende Exemplar seiner Familie; geht er
# mit dem Prozess, ist der Grant deterministisch verbrannt (LH: 400
# invalid_request auf den konsumierten RT, für immer). Deshalb wird jeder Park
# ZUSÄTZLICH auf ein Host-Volume gespiegelt (compose: /opt/aerox/fo-state →
# AEROX_FO_STATE_DIR) und beim Refresher-Start zurückgeladen. Ohne Volume
# (Dev/Tests) degradiert alles lautlos zum reinen Prozess-Park von vorher.
_FO_STATE_DIR = (os.environ.get('AEROX_FO_STATE_DIR') or '/var/aerox-fo-state')


def _parked_disk_path(user_token):
    """Dateiname über Hash — kein Roh-Token im Verzeichnis-Listing (das
    AT-Token IST das Credential). None, wenn kein State-Dir nutzbar ist."""
    try:
        if not os.path.isdir(_FO_STATE_DIR):
            return None
        h = hashlib.sha256((user_token or '').encode()).hexdigest()[:16]
        return os.path.join(_FO_STATE_DIR, f'parked-{h}.json')
    except Exception:
        return None


def _parked_disk_write(user_token, pend):
    """Park-Stand atomar aufs Volume (tmp + rename). Best-effort, wirft nie —
    der Prozess-Park bleibt die primäre Wahrheit."""
    p = _parked_disk_path(user_token)
    if not p:
        return
    try:
        payload = dict(pend)
        payload['user_token'] = user_token
        tmp = p + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(payload, f)
        os.replace(tmp, p)
    except Exception as e:
        log.warning('[lh_flightops] parked-disk write: %s', type(e).__name__)


def _parked_disk_rm(user_token):
    p = _parked_disk_path(user_token)
    if not p:
        return
    try:
        if os.path.exists(p):
            os.remove(p)
    except Exception:
        pass


def _rotated_pending_drop(user_token):
    """Park-Eintrag in BEIDEN Speichern räumen (Prozess + Volume) — die eine
    Stelle für jede Heilung/Verwerfung, damit Disk und RAM nie divergieren."""
    with _rotated_pending_lock:
        _rotated_pending.pop(user_token, None)
    _parked_disk_rm(user_token)


def _parked_disk_restore():
    """Beim Refresher-Start: überlebende Park-Stände vom Volume zurück in den
    Prozess-Park (nur wenn dort nichts Neueres liegt). Zu alte Dateien
    (> _ROTATED_PENDING_MAX_AGE_S) werden gelöscht — deren Familie ist ohnehin
    entschieden. Gibt die Zahl der restaurierten Einträge zurück, wirft nie."""
    n = 0
    try:
        if not os.path.isdir(_FO_STATE_DIR):
            return 0
        now = time.time()
        for fn in os.listdir(_FO_STATE_DIR):
            if not (fn.startswith('parked-') and fn.endswith('.json')):
                continue
            p = os.path.join(_FO_STATE_DIR, fn)
            try:
                with open(p) as f:
                    rec = json.load(f) or {}
                tok = rec.pop('user_token', None)
                if (not tok or not rec.get('tokens')
                        or now - (rec.get('ts') or 0) > _ROTATED_PENDING_MAX_AGE_S):
                    os.remove(p)
                    continue
                with _rotated_pending_lock:
                    if tok not in _rotated_pending:
                        _rotated_pending[tok] = rec
                        n += 1
            except Exception:
                continue
        if n:
            log.warning('[lh_flightops] %d geparkte Rotation(en) vom Volume '
                        'restauriert — Nachsave läuft über _tokens_load', n)
    except Exception as e:
        log.warning('[lh_flightops] parked-disk restore: %s', type(e).__name__)
    return n


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
        _rotated_pending_drop(user_token)
        return None
    mirror_rt = (mirror_tokens or {}).get('refresh') or ''
    ours = (pend.get('tokens') or {}).get('refresh') or ''
    if mirror_rt and mirror_rt not in (pend.get('consumed_rt') or '', ours):
        # Jemand anders hat inzwischen erfolgreich rotiert UND persistiert —
        # dessen Stand ist neuer, unsere Kopie ist Geschichte.
        _rotated_pending_drop(user_token)
        return None
    # Mirror hängt noch am konsumierten RT → unsere Kopie ist die Wahrheit.
    if _tokens_save(user_token, pend['tokens']):
        log.info('[lh_flightops] rotation-nachsave gelungen token=%s',
                 (user_token or '')[:8])
        _rotated_pending_drop(user_token)
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
            _rotated_pending_drop(user_token)
            return True
        if cas == 'superseded':
            log.warning('[lh_flightops] rotation-save superseded (Re-Login/'
                        'fremder neuerer Stand) — Kopie verworfen token=%s',
                        (user_token or '')[:8])
            _rotated_pending_drop(user_token)
            return True
        if cas in (None, 'no_row'):
            # RPC (noch) nicht da: Supersede von Hand prüfen, dann
            # bestätigter Merge-Save (schafft im no_row-Fall auch die Row).
            cur_rt = (_tokens_mirror_raw(user_token) or {}).get('refresh') or ''
            if cur_rt and cur_rt not in (consumed_rt or '',
                                         tokens.get('refresh') or ''):
                log.warning('[lh_flightops] rotation-save superseded (readback)'
                            ' — Kopie verworfen token=%s', (user_token or '')[:8])
                _rotated_pending_drop(user_token)
                return True
            if _tokens_save(user_token, tokens):
                if attempt:
                    log.warning('[lh_flightops] rotation-save nach %d Versuchen'
                                ' gelungen token=%s', attempt + 1,
                                (user_token or '')[:8])
                _rotated_pending_drop(user_token)
                return True
        time.sleep(delay)
        delay = min(delay * 2, 4.0)
    pend = {'tokens': dict(tokens), 'consumed_rt': consumed_rt or '',
            'ts': time.time()}
    with _rotated_pending_lock:
        _rotated_pending[user_token] = pend
    # Deploy-fest: der Park überlebt Worker-Recycle UND Container-Recreate auf
    # dem Host-Volume — genau die zwei Tode, die am 31.07./01.08. Familien
    # gekostet haben.
    _parked_disk_write(user_token, pend)
    log.error('[lh_flightops] ROTATION-SAVE FEHLGESCHLAGEN token=%s rt8=%s — '
              'neuer Refresh-Token geparkt (Prozess + Volume; wird bei jedem '
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
    # api=False: Token-Endpoint ist oauth.lufthansa.com, NICHT das quotierte
    # Gateway — nicht mehr in die Gate-Zähler buchen (s. _flightops_budget_inc).
    _flightops_budget_inc('/oauth_refresh', api=False)
    return _token_request(body)


# OAuth-Fehler, bei denen der Grant DEFINITIV tot ist — nur dann Re-Login
# verlangen. Doku (Token_Endpoint) nennt invalid_grant/invalid_client;
# `invalid_token` LIVE beobachtet (2026-07-23, 401 beim Refresh mit stalen
# Sandbox-Tokens nach dem Prod-Key-Wechsel) — ebenfalls toter Grant. Alles
# andere (service_unavailable=Wartung, 403 Rate-Limit, 5xx, Netz) ist
# transient: Tokens BEHALTEN, später erneut.
_FATAL_OAUTH_ERRORS = ('invalid_grant', 'invalid_client', 'invalid_token')

# ── 400/invalid_request = LH-Dialekt für einen VERBRAUCHTEN Refresh-Token ────
# Gemessen 02.08.2026 (Vorfall 275 Grants): ein UNBEKANNTER RT bekommt
# 401/invalid_token (fatal, korrekt), ein bereits eingelöster/übersprungener
# RT dagegen 400/invalid_request — und der lief als »transient« in einen
# EWIGEN stündlichen Retry (441 Fehlversuche allein an einem Vormittag),
# während der User nie die »Neu verbinden«-Karte sah. Ein einzelner
# invalid_request bleibt transient (LH-Schluckauf ist real); erst das MUSTER
# — derselbe RT, mehrfach, über einen längeren Zeitraum — ist der Beweis für
# einen toten Grant. Owner-Regel dazu (02.08.): »wenn länger nichts mehr kommt
# von api … am besten sogar komplett pausiert« — genau das passiert über den
# needs_relogin-Weg (Refresher fasst den Grant nie wieder an, App zeigt still
# die Reconnect-Karte, kein Push).
_INVREQ_DEAD_N = 3               # Fehlschläge in Folge auf DEMSELBEN RT …
_INVREQ_DEAD_SPAN_S = 1800       # … über mindestens diese Zeitspanne


def _invreq_track(user_token, tokens, rt):
    """400/invalid_request-Fehlschlag DURABEL am Token-Stand zählen (im
    Token-Dict selbst — überlebt Worker-Recycle und Deploy, resettet
    automatisch bei jedem neuen RT, weil der Zähler an rt8 gebunden ist).
    True erst, wenn das Muster den toten Grant beweist. Wirft nie."""
    try:
        now = time.time()
        rec = tokens.get('invreq')
        rec = dict(rec) if isinstance(rec, dict) else {}
        if rec.get('rt8') != _rt8(rt):
            rec = {'rt8': _rt8(rt), 'first': now, 'n': 0}
        rec['n'] = int(rec.get('n') or 0) + 1
        rec['last'] = now
        if (rec['n'] >= _INVREQ_DEAD_N
                and now - float(rec.get('first') or now) >= _INVREQ_DEAD_SPAN_S):
            return True
        t2 = dict(tokens)
        t2['invreq'] = rec
        if not _tokens_save(user_token, t2):
            # Best-effort: ohne persistierten Zähler dauert die Eskalation nur
            # länger — sie kippt nie fälschlich auf »tot«.
            log.warning('[lh_flightops] invreq-zähler nicht persistiert '
                        'token=%s n=%d', (user_token or '')[:8], rec['n'])
    except Exception as e:
        log.warning('[lh_flightops] invreq-track: %s', type(e).__name__)
    return False


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
        # VERTEILUNGS-MESSUNG (2026-07-29): genau hier — und nur hier — geht ein
        # LH-Token-Call raus. Der Zähler beantwortet die Frage, die heute nicht
        # beantwortbar war: viele Nutzer je einmal zu oft ODER wenige sehr oft?
        _rot_day_note(user_token)
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
        dead_why = None
        if err and err.get('fatal'):
            dead_why = 'oauth_fatal'
        elif (err and err.get('http') == 400
                and (err.get('oauth') or '') == 'invalid_request'
                and _invreq_track(user_token, t, rt)):
            # LH kennt den RT, weist die Anfrage aber dauerhaft zurück —
            # das Muster des verbrauchten Tokens (Vorfall 02.08.2026).
            dead_why = 'invalid_request_persistent'
        if dead_why:
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
                      'oauth=%s grund=%s last_ok_vor_min=%.0f',
                      user_token[:8], _rt8(rt),
                      err.get('http'), err.get('oauth'), dead_why, _last_ok_min)
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
def _flightops_budget_inc(path, api=True):
    """LH-FlightOps-Call im PROZESS-ÜBERGREIFENDEN Stundenzähler buchen.
    (Sichtbarkeit statt Schätzen — Owner 2026-07-26: „erst messen".) Der
    FlightOps-Key ist ein EIGENER LH-Key, darum eigener Schlüssel-Präfix
    `lhfo:`; zweiter Schlüssel je Service für die Verbraucher-Aufschlüsselung.
    Wirft nie und darf den API-Pfad niemals blockieren.

    ZUSÄTZLICH seit 2026-07-28: derselbe Call wird in einem TAGES-Zähler
    `lhfoD:<YYYYMMDD>` gebucht. KORREKTUR 10.08.2026: das dabei angenommene
    „Tageskontingent von 6.000" gibt es nicht — LH nennt für den PROD-Key
    20.000/Stunde und 20/Sekunde, kein Tageslimit (Mail Alex). Der Tageszähler
    bleibt als NOTBREMSE gegen Endlosschleifen, die unter beiden echten Grenzen
    durchrutschen. budget_inc hängt die STUNDE automatisch an, deshalb hier der
    Key-genaue Zwilling budget_inc_key.

    `api=False` (seit 2026-07-28 abends, Owner „refresh token without using
    APIs?"): der TOKEN-Endpoint ist `oauth.lufthansa.com` — ein ANDERER Host
    als das quotierte Gateway `api.lufthansa.com`. oauth_refresh wurde bisher
    trotzdem in die Gate-Zähler gebucht und fraß so bis zu ~5k Calls/Tag vom
    SELBSTGEBAUTEN Budget, ohne (nach allem, was messbar ist) LH-Gateway-Quote
    zu kosten — genau das ließ interaktive Reconnects verhungern. Refreshes
    laufen jetzt in EIGENE Sichtbarkeits-Zähler (`lhfoR`/`lhfoRD:`), nicht in
    die Gates. VERSICHERUNG, falls LH Token-Calls doch aufs Tageslimit zählt:
    die Deckel sind zeitgleich um je ~200-300 gesenkt (s. Ceiling-Konstanten)."""
    try:
        from blueprints.lh_open_api import budget_inc, budget_inc_key
        svc = re.sub(r'[^A-Za-z_]', '', (path or '').lstrip('/'))[:40] or 'unknown'
        prefix = 'lhfo' if api else 'lhfoR'
        budget_inc(prefix, svc)
        budget_inc_key(('lhfoD:' if api else 'lhfoRD:')
                       + time.strftime('%Y%m%d', time.gmtime()))
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
# Seit 28.07. abends zählen oauth_refreshes NICHT mehr in diese Zähler
# (eigener Host, s. _flightops_budget_inc api=False) — die Deckel sind dafür
# um die frühere Refresher-Marge gesenkt: die Gates messen jetzt NUR echte
# Gateway-Calls, schützen aber dieselbe 1.000/h-Grenze inkl. Sicherheitsband,
# falls LH Token-Calls wider Erwarten doch mitzählt.
#
# ══ KORREKTUR 10.08.2026 — DIE ECHTEN LIMITS, VON LH SCHRIFTLICH ═══════════
# Alex (LH) per Mail: „Bei dem PROD Key hast Du eine Quota von 20.000 Calls
# pro Stunde und ein Rate Limit von 20 Calls pro Sekunde."
#
# Damit ist ALLES darüber Makulatur. Die 1.000/h stammten aus einer Annahme,
# die nie an der Quelle geprüft wurde; der 403-Vorfall vom 27.07. war
# höchstwahrscheinlich das SEKUNDEN-Rate-Limit (20/s) während eines Bursts,
# nicht ein Stunden-Kontingent. Ein Tages-Kontingent gibt es GAR NICHT.
#
# Was uns diese Fehlannahme gekostet hat, ist messbar: wir liefen auf 3 % der
# erlaubten Rate. Der Tagesdeckel riss dadurch an JEDEM Tag (05.–10.08. je
# ~5.500), und jedes Mal starben zuerst die Hintergrund-Features — zuletzt
# Briefing-Raum/Security/Crewbus/Boarding, die deshalb wochenlang bei ALLEN
# Nutzern leer blieben.
#
# ⚠️ DIE GRENZE, DIE WIRKLICH ZÄHLT, IST DIE SEKUNDE. 20 Calls/s lassen sich
# mit einem einzigen ungebremsten Loop reißen, während die Stunde noch fast
# leer ist. Der 0,7-s-Takt der Massen-Verbraucher (`_LB_SPACING_S`) hält
# 1,4 Calls/s — Faktor 14 Sicherheitsabstand. Wer hier künftig parallelisiert,
# muss diese Zahl anfassen, NICHT die Stundenwerte.
#
# LEHRE, teuer bezahlt: eine Limit-Zahl, die im eigenen Kommentar steht, ist
# keine Quelle. Beim Anbieter nachfragen kostet eine Mail.
_LHFO_HOUR_BACKGROUND_CEILING = 12000     # 60 % der echten 20.000/h
_LHFO_HOUR_INTERACTIVE_CEILING = 18000    # 90 % — Taps zuletzt sterben

# ── TAGES-DECKEL — jetzt NOTBREMSE, nicht mehr Kontingent-Abbild ─────────────
# Bis 10.08.2026 bildeten diese Zahlen ein „Tageskontingent von 6.000" ab, das
# es NIE GAB (s. Korrektur oben — LH kennt nur 20.000/Stunde und 20/Sekunde).
# Sie waren damit die schärfste Bremse im System und rissen an jedem Tag.
#
# Weg können sie trotzdem nicht: ein Tageszähler ist die einzige Stelle, die
# eine Endlosschleife bemerkt, die brav 1,4 Calls/s macht und damit weder das
# Sekunden- noch das Stunden-Gate auslöst. Er bleibt also — aber als
# NOTBREMSE, eine Größenordnung über dem realen Verbrauch (~5.500/Tag), nicht
# als tägliche Ration.
#
# 60.000/Tag = gut das Zehnfache des heutigen Verbrauchs und immer noch nur
# ein Achtel dessen, was 20.000/h theoretisch pro Tag hergäben. Wer diese
# Marke reißt, hat einen Bug, kein Wachstum.
# ══ DIE TAGESGRENZE IST REAL: ~10.000 — GEMESSEN, NICHT BEHAUPTET ═══════════
# (Vorfall 10.08. abends, dritte und letzte Revision dieser Kommentar-Kette.)
#
# Die Beweiskette:
#   · LHs EIGENE Portal-CSVs (10.08., Owner-Export) sind die Quelle — nicht
#     mehr unser Zähler, der auch geblockte VERSUCHE bucht (stand 10.155):
#       04.–09.08.: 5.378–6.104 erfolgreich, 0× „Over Limit".
#       10.08.: erfolgreich EINGEFROREN bei 7.389, danach 2.372× Over Limit.
#     ⇒ Quote zwischen 6.150 (05.08. lief durch) und 7.430 (Einfrierpunkt).
#       Planungsgrösse: ~7.400 durchgelassene Calls/Tag.
#   · Die Stunde stand beim Block bei 52 von 20.000, der Takt bei 1,4/s von
#     20 — beides scheidet aus. Reset am UTC-Mitternacht (Vorfall 27.07.).
#   · Treiber heute: COMMON_DUTY_EVENTS 7.460 statt üblich ~3.600 — exakt die
#     Verdopplung durch die (zurückgenommene) Kadenz-Verschärfung.
#
# WARUM DAS PORTAL SIE NICHT ZEIGT: die Key-Seite listet nur „Rate Limits"
# (20/s, 20.000/h). Mashery führt QUOTEN als getrennte Einstellung am Plan —
# und ERR_403_DEVELOPER_OVER_RATE ist genau der Quoten-Fehler, nicht der
# Sekunden-Drossel-Fehler (der hiesse OVER_QPS). Der Portal-Key IST der
# FlightOps-Key (verglichen, nicht geraten). Alex' Mail und der Screenshot
# waren also beide korrekt — nur unvollständig.
#
# DIE STAFFELUNG, kalibriert an der UNTEREN plausiblen Wand (7.389 beobachtet):
# Hintergrund stoppt bei 6.200, die Dienst-Marken bei 6.500, interaktive Flows
# (Erstimport der Neuen — gerade läuft eine ZRH-Welle) dürfen bis 7.000. Der
# Hintergrund stirbt zuerst, der neue Nutzer zuletzt — und alles bleibt unter
# dem Punkt, an dem LH ALLE sperrt.
_LHFO_DAY_BACKGROUND_CEILING = 6200
_LHFO_DAY_INTERACTIVE_CEILING = 7000

# ── DRITTE STUFE: PRIORISIERTER HINTERGRUND (Owner-Befund 09.08.2026) ────────
# MESSUNG, die diese Stufe ausgelöst hat: der Hintergrund-Deckel (5.000) wird
# an JEDEM Tag gerissen — 05.08.=5.633, 06.08.=5.494, 07.08.=5.576,
# 08.08.=5.465. Folge: `/COMMON_CHECK_IN_TIMES` (Briefing-Raum, Security,
# Crewbus, Boarding) ist ein reiner Hintergrund-Call und stirbt damit an den
# meisten Tagen, bevor das 6-h-Fenster eines Diensttags überhaupt aufgeht.
# Der Owner sah es an seinem eigenen Dienst (06.08., FRA→SIN): alle vier
# Marken leer, Grant ok, Flugnummer da, Fenster offen — abgewiesen wurde erst
# das Budget-Gate. Das Feature war gebaut, deployt und lief faktisch nie.
#
# WARUM NICHT `interactive=True`: der Call hängt an keinem Nutzer-Tap. Er
# würde den Headroom fressen, der Connect-Erstimport und „Jetzt
# aktualisieren" am Leben hält — genau die sind am 29.07. abends fleet-weit
# gestorben, als der Deckel riss. Deshalb eine eigene Stufe DAZWISCHEN.
#
# NACHTRAG 10.08.2026: Mit den ECHTEN Limits (20.000/h, kein Tageskontingent)
# ist die Knappheit weg, für die diese Stufe erfunden wurde. Sie bleibt
# trotzdem — die Rangfolge ist unabhängig von der Menge richtig: geht je etwas
# schief und ein Gate greift, sollen Nutzer-Taps zuletzt sterben, dann die
# Dienst-Marken, dann der Rest. Die Werte liegen jetzt einfach zwischen den
# neuen Stufen statt zwischen den alten.
_LHFO_HOUR_PRIORITY_CEILING = 15000
_LHFO_DAY_PRIORITY_CEILING = 6500

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


# ── GEMEINSAME SCHRITTWEITE VOR DEM VERSAND (10.08.2026) ────────────────────
# LHs Portal-CSVs zeigen 40–96× „Developer Over QPS" an JEDEM Tag — wir reissen
# die 20/s-Drossel regelmässig in kleinen Bursts. Der Grund ist nicht EIN
# schneller Loop (die Massen-Verbraucher schlafen brav 0,7 s), sondern dass
# MEHRERE Verbraucher parallel feuern, ohne voneinander zu wissen: refresh-all,
# Marken-Wärmer, Crew-Prefetch, interaktive Taps — und der NAS-Zweitorigin
# macht dasselbe nochmal.
#
# Deshalb EIN Takt-Gate für alle Sender DIESES Prozesses, direkt vor dem
# Request: 0,12 s Mindestabstand ≈ 8/s pro Origin. Zwei Origins zusammen
# bleiben damit unter 17/s — Luft zur 20er-Drossel, deren 403s sonst selbst
# auf die Tagesquote zahlen. Die Wartenden bilden eine Schlange und verlassen
# sie im Takt: der SLOT wird unter dem Lock vergeben, GEWARTET wird ohne Lock.
_API_PACE_LOCK = threading.Lock()
_api_pace_last = [0.0]
_API_PACE_MIN_S = 0.12


def _api_pace(now_fn=time.time, sleep_fn=time.sleep):
    """Blockiert, bis der nächste Sende-Slot frei ist. Injektierbare Uhren
    NUR für die Tests — Produktion ruft ohne Argumente."""
    # Der Lock schuetzt NUR die Slot-Buchung (2026-08-13). Vorher lag der
    # `sleep` IM Lock: ein Schlaefer blockierte damit jeden anderen Sender —
    # auch den, dessen Slot laengst frei gewesen waere. Bei n parallelen
    # Anfragen wartete jede auf die Summe aller Vorgaenger, statt nur auf den
    # eigenen Takt; interaktive Taps standen hinter dem Hintergrund-Schwarm.
    # Die Reihenfolge bleibt identisch — die Schlange wird jetzt nur ohne
    # gehaltenen Lock abgesessen.
    with _API_PACE_LOCK:
        slot = max(now_fn(), _api_pace_last[0] + _API_PACE_MIN_S)
        _api_pace_last[0] = slot
    wartezeit = slot - now_fn()
    if wartezeit > 0:
        sleep_fn(wartezeit)


def _api_get(user_token, path, params=None, interactive=False, status_out=None,
             priority=False):
    """LH-Call mit Budget-Gate. Return: Response-Dict oder None.

    `status_out` (optionales dict, additiv 2026-07-31): der Aufrufer bekommt
    darin die KLASSE des Ausgangs — sonst ist `None` mehrdeutig und ein
    HTTP-404 („die Ressource gibt es (noch) nicht") wäre von „LH war kaputt"
    nicht unterscheidbar. Das ist für den Landing Report entscheidend: 404 =
    Report noch nicht da (später nachladen), NIEMALS „nicht gelandet".
        kind: 'ok' | 'no_access' | 'hour_budget' | 'day_budget' | 'http' | 'error'
        code: HTTP-Status (nur bei kind='http')
    Bestandsaufrufer übergeben nichts und sehen keinerlei Verhaltensänderung."""
    def _note(kind, code=None):
        if isinstance(status_out, dict):
            status_out['kind'] = kind
            if code is not None:
                status_out['code'] = code

    _note('error')          # bis zum Beweis des Gegenteils
    access = _valid_access(user_token)
    if not access:
        _note('no_access')
        return None
    # Drei Stufen statt zwei: interaktiv > priorisiert > Hintergrund.
    # `interactive` gewinnt, falls ein Aufrufer beides setzt.
    _tier = ('interaktiver' if interactive
             else 'priorisierter' if priority else 'Hintergrund')
    _used = _rot_hour_used()
    _ceiling = (_LHFO_HOUR_INTERACTIVE_CEILING if interactive
                else _LHFO_HOUR_PRIORITY_CEILING if priority
                else _LHFO_HOUR_BACKGROUND_CEILING)
    if _used >= _ceiling:
        log.warning('[lh_flightops] lhfo-Stundenbudget %s >= %s — %s-Call %s '
                    'übersprungen', _used, _ceiling, _tier, path)
        _note('hour_budget')
        return None
    _dused = _lhfo_day_used()
    _dceiling = (_LHFO_DAY_INTERACTIVE_CEILING if interactive
                 else _LHFO_DAY_PRIORITY_CEILING if priority
                 else _LHFO_DAY_BACKGROUND_CEILING)
    if _dused >= _dceiling:
        log.warning('[lh_flightops] lhfo-Tagesbudget %s >= %s — %s-Call %s '
                    'übersprungen', _dused, _dceiling, _tier, path)
        _note('day_budget')
        return None
    # Takt NACH den Budget-Gates (abgewiesene Calls brauchen keinen Slot),
    # VOR der Buchung — gebucht wird nur, was wirklich gesendet wird.
    _api_pace()
    # GEBUCHT WIRD VOR DEM VERSAND, also VOR der Antwort: ein LH-403
    # („Developer Over QPS"/„Over Rate Limit") verbraucht damit unser EIGENES
    # Budget mit. Das ist bewusst so gelassen — sicher-aus-Versehen: wir zaehlen
    # jeden Call, den LH gezaehlt hat. Folge fuer die Auswertung: der effektive
    # ERFOLGREICHE Durchsatz liegt unter der Deckel-Zahl, die Deckel sind also
    # keine Ist-Messung des Nutzens (Kontext Quoten-Diaet 29.07./10.08.).
    _flightops_budget_inc(path)
    url = _BASE + path
    if params:
        url += ('&' if '?' in url else '?') + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url, headers={'Authorization': 'Bearer ' + access,
                      'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            out = json.loads(r.read().decode('utf-8'))
        _note('ok')
        return out
    except urllib.error.HTTPError as e:
        log.warning('[lh_flightops] api %s -> HTTP %s', path, e.code)
        _note('http', getattr(e, 'code', None))
        return None
    except Exception as e:
        log.warning('[lh_flightops] api %s -> %s', path, type(e).__name__)
        _note('error')
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
def crew_list(user_token, flight, date, dep, arr, access_code,
              interactive=False):
    """COMMON_CREWLIST — wer fliegt mit (crewMembers[]).

    `interactive=True` = der User hat gerade den Crew-Button gedrückt → höhere
    Budget-Grenze im Key-Gate (siehe _api_get). Hintergrund-Nutzer (Briefing)
    bleiben beim Default False."""
    return _api_get(user_token, '/COMMON_CREWLIST', {
        'flightDesignator': (flight or '').upper().replace(' ', ''),
        'flightDate': _date_z(date), 'departureAirport': (dep or '').upper(),
        'arrivalAirport': (arr or '').upper(), 'accessCode': access_code or ''},
        interactive=interactive)


def crew_rotation(user_token, *rotation_numbers):
    """COMMON_CREW_ROTATION — Rotations-Details (rotations[].shifts[].legs[])."""
    params = {}
    for i, rn in enumerate([r for r in rotation_numbers if r][:6]):
        params['RN' if i == 0 else f'RN_{i + 1}'] = str(rn)
    if not params:
        return None
    return _api_get(user_token, '/COMMON_CREW_ROTATION', params)


def landing_report(user_token, flight, date, dep, interactive=False,
                   status_out=None):
    """COMMON_LANDING_REPORT — OOOI-Zeiten + `landingPerformed` für ein Leg.

    `landingPerformed` ist PER-USER (die ANFRAGENDE Person hat die Landung
    durchgeführt) und kommt als STRING 'true'/'false' — siehe
    `landing_report_parse`. `status_out` (dict) nimmt die Abruf-Klasse auf
    (siehe `_api_get`), damit der Aufrufer 404 („Report noch nicht da") von
    einem echten Fehler unterscheiden kann."""
    return _api_get(user_token, '/COMMON_LANDING_REPORT', {
        'flightDesignator': (flight or '').upper().replace(' ', ''),
        'flightDate': _date_z(date), 'departureAirport': (dep or '').upper()},
        interactive=interactive, status_out=status_out)


def flight_leg_details(user_token, flight, date=None, dep=None, arr=None,
                       interactive=False, status_out=None):
    """COMMON_FLIGHT_LEG_DETAILS — Reg/Muster/Gate/Blockzeit autoritativ.

    `status_out` (additiv 2026-07-31, wie bei `landing_report`): der Aufrufer
    unterscheidet damit „LH kennt das Leg nicht" von „das Budget-Gate hat
    zugemacht". Der Plan-Backfill braucht das, um einen NICHT gesendeten Call
    nicht in seinem Tageszähler zu buchen. Bestandsaufrufer übergeben nichts
    und sehen keinerlei Verhaltensänderung."""
    params = {'flightDesignator': (flight or '').upper().replace(' ', '')}
    if date:
        params['flightDate'] = _date_z(date)
    if dep:
        params['departureAirport'] = dep.upper()
    if arr:
        params['arrivalAirport'] = arr.upper()
    return _api_get(user_token, '/COMMON_FLIGHT_LEG_DETAILS', params,
                    interactive=interactive, status_out=status_out)


def crew_hotel(user_token, station, provider=None, interactive=False):
    """COMMON_CREW_HOTEL_INFO — Layover-Hotel-Infos für eine Station.

    `interactive=True` = Nutzer-Tap (Hotel-Fläche) → reservierter Headroom im
    Key-Gate, s. _api_get. Default bleibt Hintergrund (Briefing/Verzeichnis)."""
    params = {'station': (station or '').upper()}
    if provider:
        params['provider'] = provider
    return _api_get(user_token, '/COMMON_CREW_HOTEL_INFO', params,
                    interactive=interactive)


def _truthy(v):
    """LH liefert Booleans teils als STRING ('true'/'false' — live 2026-07-22)."""
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() == 'true'
    return None


def landing_report_parse(resp):
    """COMMON_LANDING_REPORT-Response → normalisierte Fakten. PURE/testbar,
    verifiziert gegen die ECHTE PROD-Shape (10 Live-Calls 2026-07-31, Fixture
    `tests/fixtures/lh_landing_report_prod.json`, PII/pkNumber redigiert).

        {self_landed: bool|None,   # PER-USER, s.u.
         tail, arr,                # Flug-Fakten
         dep_iso, arr_iso,         # = aircraft.out / aircraft.in (BLOCK)
         off_iso, on_iso,          # = aircraft.off / aircraft.on (FLUG)
         block_min, air_min}

    `self_landed` (LH-Feld `landingPerformed`) heißt WÖRTLICH: „die ANFRAGENDE
    Person hat die Landung durchgeführt". Es ist damit
      · ein PER-USER-Fakt — dieselbe Response desselben Legs sagt für einen
        anderen Account etwas anderes; er darf deshalb NIE aus einem geteilten
        Cache stammen (s. `_lr_shared_put`), und
      · KEIN Flug-Status: 'false' heißt NICHT „der Flug ist nicht gelandet"
        (empirisch: Kabinen-Account ⇒ 9 gelandete Flüge, überall 'false'),
      · KEIN Ownership-Check: der Service ist nicht auf den eigenen Roster
        gescoped, fremde LH-Flüge liefern Daten.
    Der Wert kommt als STRING 'true'/'false' (→ `_truthy`).

    Block = out→in, **Flugzeit = off→on** (FCL.050-Kern). Beide Spannen werden
    aus absoluten ISO-Zeitstempeln gerechnet, der Mitternachts-Rollover ist
    damit inhärent abgedeckt (off 23:50Z → on 01:10Z Folgetag = 80 min).

    `lowVisibilityApproach` wird BEWUSST NICHT übernommen: LH führt das Feld
    als deprecated, PROD liefert konstant 'unknown' (die Doku schreibt es
    'unkown' — beide Schreibweisen laufen hier ins Leere, nicht in einen
    Fehler). None-Werte werden weggelassen."""
    if not isinstance(resp, dict) or resp.get('processingErrors'):
        return {}
    ev = (resp.get('events') or {}).get('aircraft') or {}
    out = _valid_iso(ev.get('out'))
    _in = _valid_iso(ev.get('in'))
    off = _valid_iso(ev.get('off'))
    on = _valid_iso(ev.get('on'))
    facts = {'self_landed': _truthy(resp.get('landingPerformed'))}
    tail = _norm_reg(resp.get('tailsign'))
    if tail:
        facts['tail'] = tail
    # Ankunftsflughafen: der Landing Report trägt ihn selbst — der Leg-Key ist
    # damit ohne Extra-Call vollständig (Leg-Key-Kollisions-Falle, Flugbuch).
    arr = (resp.get('destinationAirport') or '').strip().upper()
    if arr:
        facts['arr'] = arr
    if out:
        facts['dep_iso'] = out
    if _in:
        facts['arr_iso'] = _in
    if off:
        facts['off_iso'] = off
    if on:
        facts['on_iso'] = on
    bm = _block_min_iso(out, _in)
    if bm is not None:
        facts['block_min'] = bm
    am = _block_min_iso(off, on)
    if am is not None:
        facts['air_min'] = am
    return facts


def landing_report_facts(user_token, flight, date, dep):
    """Landing Report ABRUFEN und normalisieren (s. `landing_report_parse`)."""
    return landing_report_parse(landing_report(user_token, flight, date, dep))


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
    """True/False/None — hat DIE ANFRAGENDE Person dieses Leg gelandet?
    PER-USER-Fakt, kein Flug-Status (s. `landing_report_parse`)."""
    return landing_report_facts(user_token, flight, date, dep).get('self_landed')


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


def parse_simulator_crewlist(resp):
    """COMMON_SIMULATOR_CREWLIST → dieselbe normalisierte Form wie die
    Flug-Crewliste: [{position, name, pk, duty}] — damit die App-Oberfläche
    („Wer fliegt mit") unverändert bleibt und nur die Überschrift wechselt.

    ECHTE SHAPE (live geholt 2026-07-30 an einem SIM-Termin von Mark Elser,
    Forum-Anfrage „gibt es eine Möglichkeit die SIM Crewlisten abzufragen?"):
    die Antwort ist eine LISTE von Sessions — nicht ein Objekt wie bei
    COMMON_CREWLIST —, jede mit
        {entries: [{crewName, staffIdentifier, crewFunction,
                    simulatorFunction, simulatorActivity}],
         forDate, ftNumber, shift, simulator, errorReply, errorMessage}

    Unterschiede, die hier aufgelöst werden:
      · `crewName` ist EIN Feld (kein first/last wie bei COMMON_CREWLIST),
      · `staffIdentifier` ist die Personalnummer — also das, was dort
        `pkNumber` heißt. Genau darüber läuft die AeroX-Verknüpfung
        (Identitäten NUR über die PK, siehe Crew-Match-Regel).
      · `simulatorFunction` (z.B. PF/PM) ist SIM-spezifisch und wird an die
        Position gehängt, wenn sie etwas anderes sagt als `crewFunction`.

    Sessions mit `errorReply` werden übersprungen. Pure/testbar."""
    out, seen = [], set()
    for session in _as_list(resp):
        if not isinstance(session, dict) or session.get('errorReply'):
            continue
        for m in _as_list(session.get('entries')):
            if not isinstance(m, dict):
                continue
            name = (m.get('crewName') or '').strip()
            pk = (m.get('staffIdentifier') or '').strip() or None
            # Dieselbe Person kann in mehreren Sessions desselben Tages
            # stehen (Doppel-Slot) — einmal reicht.
            key = (pk or name.upper())
            if not key or key in seen:
                continue
            seen.add(key)
            pos = (m.get('crewFunction') or '').strip()
            sim_fn = (m.get('simulatorFunction') or '').strip()
            if sim_fn and sim_fn.upper() != pos.upper():
                pos = f'{pos} · {sim_fn}' if pos else sim_fn
            out.append({'position': pos, 'name': name.title() or None,
                        'pk': pk,
                        'duty': (m.get('simulatorActivity') or '').strip() or None})
    return out


def simulator_session_info(resp):
    """Session-Kopf der SIM-Antwort → {simulator, shift, ftNumber, date} für
    die Überschrift („SIM 3 · Frühschicht"). Nimmt die erste fehlerfreie
    Session. Nie Pflicht — fehlt ein Feld, bleibt es None."""
    for session in _as_list(resp):
        if not isinstance(session, dict) or session.get('errorReply'):
            continue
        return {'simulator': (session.get('simulator') or '').strip() or None,
                'shift': (session.get('shift') or '').strip() or None,
                'ftNumber': (session.get('ftNumber') or '').strip() or None,
                'date': (session.get('forDate') or '').strip() or None}
    return {}


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
                   duty_type='OD', crew_category='COC', interactive=False,
                   priority=False, status_out=None, **extra):
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
    # `interactive` ist ein EIGENER Keyword-Parameter (NICHT in **extra): sonst
    # landete er als Query-Param bei LH. Bedeutung wie überall — Nutzer-Tap
    # bekommt den reservierten Headroom im Key-Gate (s. _api_get).
    # `priority`/`status_out` stehen aus DEMSELBEN Grund explizit hier: über
    # **extra würden sie als Query-Parameter bei LH landen (409-Klasse).
    return _api_get(user_token, '/COMMON_CHECK_IN_TIMES', params,
                    interactive=interactive, priority=priority,
                    status_out=status_out)


def airport_weather(user_token, station, **extra):
    """COMMON_AIRPORT_WEATHER — Flughafenwetter (METAR/TAF-nah)."""
    params = {'station': (station or '').upper(), **extra}
    return _api_get(user_token, '/COMMON_AIRPORT_WEATHER', params)


def simulator_crewlist(user_token, interactive=False, **params):
    """COMMON_SIMULATOR_CREWLIST — Sim-Session-Crew. Parameter: `forDate`
    (YYYY-MM-DDZ) + `accessCode` aus den Duty-Events-_links; eine Flugnummer
    gibt es hier nicht.

    `interactive` ist ein EIGENER Keyword-Parameter (NICHT in **params) —
    sonst landet er als Query-Param bei LH. Genau dieselbe Falle steckt in
    check_in_times, dort mit derselben Begründung dokumentiert."""
    return _api_get(user_token, '/COMMON_SIMULATOR_CREWLIST', params,
                    interactive=interactive)


def service_get(user_token, service, params=None, interactive=False,
                priority=False, status_out=None):
    """Generischer Service-Call (für Diagnose/Verdrahtung). `service` ist der
    COMMON_*-Name. Nur echte Services zulassen.

    `interactive=True` nur setzen, wenn der Aufruf wirklich an einem Nutzer-Tap
    hängt (z.B. Check-in-Zeiten) — sonst frisst Hintergrundarbeit den
    reservierten Headroom (s. _api_get).

    `priority=True` ist die Stufe DAZWISCHEN: kein Nutzer-Tap, aber ein Call,
    der ohne eigenes Band an den meisten Tagen gar nicht mehr stattfindet
    (s. `_LHFO_DAY_PRIORITY_CEILING`). Nur mit eigenem Tagestopf benutzen."""
    s = (service or '').strip().upper()
    if not s.startswith('COMMON_') or not re.fullmatch(r'COMMON_[A-Z_]+', s):
        return None
    return _api_get(user_token, '/' + s,
                    params if isinstance(params, dict) else {},
                    interactive=interactive, priority=priority,
                    status_out=status_out)


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


def parse_rotation_legs(resp):
    """COMMON_CREW_ROTATION-Response → {leg_key: {…}} mit den GRATIS-Fakten,
    die schon in derselben Antwort liegen wie `pickupTime`. Pure/testbar,
    KOSTET KEINEN zusätzlichen LH-Call (Welle 0 „LH-Gratis-Ernte").

    Schlüssel-Schema IDENTISCH zu parse_rotation_pickups (inkl. Entfernen des
    flugnummernlosen Route-Schlüssels bei mehrfach geflogener Route) — beide
    werden über denselben `_pickup_for_leg`-Matcher gelesen.

    Wert (Felder FEHLEN, wenn LH nichts hergibt — nie null, nie geraten):
      'dh'        True  ← legs[].dutyCode == 'DH' (Deadhead, Crew sitzt als Pax)
      'ac_change' True  ← legs[].aircraftChanged is True. Das Flag sitzt am
                  VORHERIGEN Leg und heißt „nach DIESEM Leg wechselt das Gerät"
                  (daily_briefing.py-Header, an PROD-Payloads mehrfach belegt:
                  LH027 DAIRO→True, nächstes Leg LH332 DAIWJ). Gesetzt wird es
                  deshalb am Leg, das das Flag trägt — aber NUR, wenn in
                  derselben Schicht noch ein Leg folgt; am letzten Leg der
                  Schicht gäbe es kein „danach", auf das sich der Chip beziehen
                  könnte.
      'hotel'     str   ← legs[].hotelName, unverändert an dem Leg, an dem LH
                  ihn führt: dem HINFLUG zur Layover-Station. Das `hotel`-Flag
                  daneben ist unbrauchbar (False trotz gesetztem Namen) und
                  wird NIE ausgewertet."""
    out = {}
    if not isinstance(resp, dict):
        return out
    _route_seen = {}
    for rot in _as_list(resp.get('rotations')):
        if not isinstance(rot, dict):
            continue
        for sh in _as_list(rot.get('shifts')):
            if not isinstance(sh, dict):
                continue
            _legs = [lg for lg in _as_list(sh.get('legs')) if isinstance(lg, dict)]
            for _i, lg in enumerate(_legs):
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
                _route_seen[rkey] = _route_seen.get(rkey, 0) + 1
                val = {}
                if str(lg.get('dutyCode') or '').strip().upper() == 'DH':
                    val['dh'] = True
                if lg.get('aircraftChanged') is True and (_i + 1) < len(_legs):
                    val['ac_change'] = True
                hn = str(lg.get('hotelName') or '').strip()
                if hn:
                    val['hotel'] = hn
                if not val:
                    continue
                flt = re.sub(r'\s', '',
                             str(lg.get('flightDesignator') or '').upper())
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


def _rot_fact_for_leg(facts, flt, frm, to, st):
    """Rotations-Fakten (parse_rotation_legs) für EIN Duty-Flug-Event oder None.
    Bewusst DERSELBE Matcher wie beim Pickup — beide Dicts tragen dasselbe
    Schlüssel-Schema, und ein zweiter, leicht anderer Matcher wäre genau die
    Sorte Divergenz, die später still falsche Legs beschriftet."""
    return _pickup_for_leg(facts, flt, frm, to, st)


def _ics_text_value(v, cap=60):
    """Freitext → ICS-tauglicher Einzeiler ('' wenn nichts übrig bleibt).
    Zeilenumbrüche würden das VEVENT zerreißen; die Kappung hält die Zeile
    unter der RFC-5545-Faltgrenze, ohne dass wir falten müssen."""
    s = re.sub(r'[\r\n\t]+', ' ', str(v or ''))
    s = re.sub(r'\s{2,}', ' ', s).strip()
    return s[:cap].strip()


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
# KONDITIONALE VERLÄNGERUNG (Owner 2026-07-29). Miguels Fall hat die 30-h-Kante
# als zu knapp entlarvt: sein Rückflug LH455 SFO→FRA lag beim Import 30 h 43 min
# voraus — der Rotations-Call fiel aus, und weil im selben Lauf auch der
# myTime-Fetch scheiterte, verlor der Tag seinen bereits bekannten Marker.
# 36 h decken die Lücke, sollen aber NICHT pauschal Calls kosten (LH-Quota).
# Deshalb gilt der Fern-Bereich (30…36 h) nur für Legs, für die wir NOCH KEINEN
# Pickup kennen (`known_anchors` = Anker der Last-Good-Einträge). Wer den Wert
# schon hat, zahlt nichts; wer blind ist, bekommt seinen einen Versuch früher.
# Kosten bleiben ≤1 Call pro User pro Lauf (alle RNs gehen in EINEN Request).
_ROT_PICKUP_HORIZON_FAR_H = 36
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


def pickup_rotation_ids(resp, now=None, horizon_h=_ROT_PICKUP_HORIZON_H,
                        far_horizon_h=_ROT_PICKUP_HORIZON_FAR_H,
                        known_anchors=None):
    """Duty-Events-Response → Liste der rotationIds, für die ein Pickup-Wert
    plausibel VORLIEGEN kann und GEBRAUCHT wird. Pure/testbar.

    Ein Leg qualifiziert, wenn ALLE vier Bedingungen gelten:
      1. Flug-Event mit startTime im Fenster [now − 3 h, now + horizon_h].
         ERWEITERUNG: bis `far_horizon_h`, wenn für dieses Leg noch KEIN Pickup
         bekannt ist (`known_anchors`, s. _ROT_PICKUP_HORIZON_FAR_H).
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
    known = set(known_anchors or ())
    lo = (now - _td(hours=_ROT_PICKUP_BACK_H)).strftime('%Y-%m-%dT%H:%M:%SZ')
    near_h = max(1, int(horizon_h))
    hi = (now + _td(hours=near_h)).strftime('%Y-%m-%dT%H:%M:%SZ')
    hi_far = (now + _td(hours=max(near_h, int(far_horizon_h or 0)))
              ).strftime('%Y-%m-%dT%H:%M:%SZ')

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
            if not (s and len(frm) == 3 and lo <= s <= hi_far):
                continue
            # Fern-Bereich (zwischen hi und hi_far): nur, solange wir für genau
            # dieses Leg noch KEINEN Pickup kennen. Sonst wäre der Call ein
            # Quota-Geschenk an einen bereits beantworteten Tag.
            if s > hi and _anchor_key(s) in known:
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


def rotation_pickups_for(user_token, rotation_ids, facts_out=None):
    """rotationIds → gemergtes Pickup-Dict (siehe parse_rotation_pickups).
    Cache pro (Token, rotationId) mit kurzer TTL.

    `facts_out` (optional, dict) wird zusätzlich mit den GRATIS-Fakten derselben
    Antwort gefüllt (parse_rotation_legs: dh/ac_change/hotel). Bewusst als
    Out-Parameter statt als geänderter Rückgabewert — der Rückgabe-Shape ist
    Vertrag für die bestehenden Aufrufer/Tests, und die Fakten kosten KEINEN
    zusätzlichen Call: sie liegen in genau derselben Response wie pickupTime.

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
                # Alt-Einträge (2-Tupel) können nach einem Deploy noch im
                # Prozess-Cache liegen — dann gibt es für diesen Umlauf in
                # dieser halben Stunde eben keine Zusatz-Fakten. Kein Grund,
                # einen Call nachzuschieben.
                if isinstance(facts_out, dict) and len(hit) > 2:
                    facts_out.update(hit[2] or {})
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
    # GRATIS aus DERSELBEN Antwort (Welle 0): dh/ac_change/hotel pro Leg.
    try:
        got_facts = parse_rotation_legs(raw)
    except Exception as e:
        log.warning('[lh_flightops] rotation legs %s: %s', batch,
                    type(e).__name__)
        got_facts = {}
    # Ein geparstes, aber pickupfreies Ergebnis WIRD gecacht — sonst fragt jeder
    # Sync denselben Umlauf erneut ab. Die kurze TTL sorgt dafür, dass ein spät
    # nachgetragener Wert trotzdem ankommt.
    with _rot_cache_lock:
        for rn in batch:
            _rot_cache[(user_token, rn)] = (time.time(), got, got_facts)
        if len(_rot_cache) > 4000:
            for k in sorted(_rot_cache, key=lambda k: _rot_cache[k][0])[:2000]:
                _rot_cache.pop(k, None)
    out.update(got or {})
    if isinstance(facts_out, dict):
        facts_out.update(got_facts or {})
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


def _pickup_inst(iso):
    """UTC-ISO → aware datetime, sonst None. Wirft nie."""
    try:
        v = _dt.datetime.fromisoformat(str(iso or '').replace('Z', '+00:00'))
        return v if v.tzinfo else v.replace(tzinfo=_dt.timezone.utc)
    except Exception:
        return None


def _anchor_key(iso):
    """Anker-Identität eines Legs = sein Abflug-Instant auf die MINUTE genau
    ('YYYY-MM-DDTHH:MM' in UTC), sonst ''. Bewusst minutengenau und ohne
    Toleranz: der Last-Good-Pfad (s.u.) darf einen Pickup nur dann wieder
    einhängen, wenn der ankernde Leg UNVERÄNDERT ist — verschiebt LH den
    Abflug, verschiebt sich in aller Regel auch der Hotel-Pickup, und dann
    wäre ein wiederbelebter Alt-Wert geraten statt gemessen."""
    inst = _pickup_inst(iso)
    if inst is None:
        return ''
    return inst.astimezone(_dt.timezone.utc).strftime('%Y-%m-%dT%H:%M')


def _ics_pickup_scan(ics):
    """ICS → (pickups, deps) — die zwei Listen, die JEDER Pickup-Riegel braucht.
      pickups = [(instant, iso, summary)] jedes Pickup-VEVENTs
      deps    = sortiertes [(instant, iso)] jedes Flug-VEVENTs (LOCATION 'XXX - YYY')
    Pure/testbar, wirft nie."""
    pickups, deps = [], []
    try:
        import app as _app
        from blueprints.crew_live_state import parse_pickup_hhmm
        for ev in (_app._parse_ics_to_events(ics) or []):
            if not isinstance(ev, dict):
                continue
            iso = str(ev.get('start_iso') or '').strip()
            inst = _pickup_inst(iso)
            if inst is None:
                continue
            summ = re.sub(r'[\r\n\t]+', ' ',
                          str(ev.get('summary') or '')).strip()
            if parse_pickup_hhmm(summ):
                pickups.append((inst, iso, summ))
                continue
            loc = str(ev.get('location') or '').strip().upper()
            if _ICS_LEG_LOCATION_RE.match(loc):
                deps.append((inst, iso))
    except Exception as e:
        log.warning('[lh_flightops] ics-pickup-scan: %s', type(e).__name__)
    deps.sort()
    return pickups, deps


def merge_ical_pickups(ics, candidates, uid_prefix='pu-ical',
                       strict_anchor=False):
    """Pickup-VEVENTs aus einer Zweitquelle in ein FlightOps-ICS einhängen —
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
        SPÄTESTE plausible — dieselbe Regel wie in `pickup_utc_for_leg`.

    `strict_anchor=True` (Last-Good-Pfad): der Kandidat MUSS ein `anchor`-Feld
    tragen, und im neuen ICS muss GENAU dieser Abflug (minutengenau) noch
    stehen. Ohne das Feld oder bei verschobenem Abflug fällt der Kandidat raus.
    `uid_prefix` trennt die Ebenen im erzeugten VEVENT (Diagnose + keine
    doppelten UIDs, wenn zwei Ebenen nacheinander laufen)."""
    if not ics or not candidates:
        return ics
    try:
        pickups, deps = _ics_pickup_scan(ics)
        have_days = set()      # Berlin-Tage, die schon einen Pickup tragen
        for _p_inst, p_iso, _summ in pickups:
            d = _berlin_day(p_iso)
            if d:
                have_days.add(d)
        if not deps:
            return ics
        chosen = {}            # Berlin-Tag → (pickup_instant, iso, summary, dep_iso)
        for cand in candidates:
            if not isinstance(cand, dict):
                continue
            p_iso = str(cand.get('utc') or '')
            p_inst = _pickup_inst(p_iso)
            summ = re.sub(r'[\r\n\t]+', ' ', str(cand.get('summary') or '')).strip()
            if p_inst is None or not summ:
                continue
            want = _anchor_key(cand.get('anchor')) if strict_anchor else ''
            if strict_anchor and not want:
                continue
            anchor = None
            for d_inst, d_iso in deps:
                if want and _anchor_key(d_iso) != want:
                    continue
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
            add += ['BEGIN:VEVENT', f'UID:{uid_prefix}-{i}@aerox-flightops',
                    f'DTSTART:{stamp}', f'DTEND:{stamp}',
                    f'SUMMARY:{summ}', 'END:VEVENT']
        if not add:
            return ics
        log.info('[lh_flightops] pickup-fallback (%s): %d Marker ergaenzt',
                 uid_prefix, len(add) // 6)
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


# ── DRITTE EBENE: LAST-GOOD (Owner 2026-07-29 „der Import löscht einen bereits
# bekannten Pickup") ────────────────────────────────────────────────────────
# BEWEIS (Miguel, Layover SFO, Rückflug LH455 30.07. 21:40Z): der Marker
# „12:40 LT Pickup SFO" stand am 27.07. 22:14 im gespeicherten Roster und war
# nach dem Import am 29.07. 14:57Z WEG. Zwei Ursachen stapelten sich:
#   1. Die Primärquelle schwieg — der Abflug lag 30 h 43 min voraus, also knapp
#      außerhalb von `_ROT_PICKUP_HORIZON_H` (dagegen der konditionale
#      Fern-Horizont unten in `pickup_rotation_ids`).
#   2. Die Zweitquelle schwieg — der myTime-Fetch scheiterte, und die Grace lag
#      NUR im prozess-lokalen `_pickup_ical_cache`, der an dem Tag zweimal mit
#      dem Container neu entstand.
# Schweigen BEIDER Quellen ist ein Normalfall (Netz, Deploy, Quota-Notbremse) —
# und weil `_ics_events_to_briefings` jeden angefassten Tag frisch aufbaut,
# UPSERTet ein pickup-loser Import den Tag OHNE Marker. Genau das ist die
# Löschung.
#
# Diese Ebene macht die Löschung unmöglich, ohne je zu raten:
#   • Was ein Import tatsächlich ausgeliefert hat, wird pro (Token, Tag) im
#     PROFIL gemerkt (`calendar_feed.pickup_last_good`) — Supabase, also
#     deploy-fest, im Gegensatz zum Prozess-Cache.
#   • Liefert ein späterer Import für den Tag NICHTS, wird der gemerkte Marker
#     wieder eingehängt — aber nur, wenn der ANKERNDE Leg minutengenau
#     unverändert im neuen ICS steht (`strict_anchor`). Ist der Umlauf
#     gestrichen oder verschoben, fällt der Pickup von selbst weg.
#   • Liefert eine echte Quelle etwas, gewinnt sie (PRIMARY WINS in
#     `merge_ical_pickups`) und der neue Wert wird das neue Last-Good.
# Owner-Regel bleibt gewahrt: Pickup NUR aus echten Quellen, NIE geschätzt —
# ein Last-Good-Wert IST ein echter, früher gemessener Wert.
_PICKUP_LAST_GOOD_MAX = 60          # Einträge (Tage) pro User
_PICKUP_LAST_GOOD_MAX_AGE_D = 30    # Tage seit der letzten echten Beobachtung
_PICKUP_LAST_GOOD_KEEP_BACK_D = 2   # so viele Tage Vergangenheit bleiben stehen


def pickups_in_ics(ics):
    """ICS → [{'day','utc','summary','anchor'}] für jedes Pickup-VEVENT, das
    einen Anker-Leg hat. `day` ist der Berlin-Tag des ANKERS (= der Roster-Tag,
    auf dem der Marker landet), `anchor` der Abflug des ankernden Legs.
    Pure/testbar, wirft nie."""
    out = []
    try:
        pickups, deps = _ics_pickup_scan(ics)
        for p_inst, p_iso, summ in pickups:
            anchor = None
            for d_inst, d_iso in deps:
                lead = (d_inst - p_inst).total_seconds() / 60.0
                if 0 <= lead <= _PICKUP_LEAD_MAX_MIN:
                    anchor = d_iso
                    break
            if anchor is None:
                continue
            day = _berlin_day(anchor)
            if not day:
                continue
            out.append({'day': day, 'utc': p_iso, 'summary': summ[:120],
                        'anchor': anchor})
    except Exception as e:
        log.warning('[lh_flightops] pickups_in_ics: %s', type(e).__name__)
    return out


def _ics_covered_days(ics):
    """Berlin-Tage, die dieses ICS überhaupt anfasst. Nur für DIESE Tage darf
    ein Import den Last-Good-Stand ersetzen — ein Import mit engerem Fenster
    (`from_date`/`to_date` im Body) darf die Marker der übrigen Tage nicht
    stillschweigend wegwerfen."""
    days = set()
    try:
        import app as _app
        for ev in (_app._parse_ics_to_events(ics) or []):
            if not isinstance(ev, dict):
                continue
            d = _berlin_day(str(ev.get('start_iso') or '').strip())
            if d:
                days.add(d)
    except Exception:
        pass
    return days


def _pickup_last_good_feed(user_token, fresh=False):
    """(profile, calendar_feed) als KOPIEN — Schreiber müssen beide zurückhängen."""
    import app as _app
    pf = _app._profile_load(user_token, fresh=fresh) or {}
    prof = dict(pf.get('profile') or {})
    feed = prof.get('calendar_feed')
    feed = dict(feed) if isinstance(feed, dict) else {}
    return prof, feed


def pickup_last_good_load(user_token):
    """Zuletzt ausgelieferte Pickups aus dem Profil. Liste (evtl. leer), nie None,
    wirft nie. Einträge ohne vollständigen Anker sind wertlos und fallen raus."""
    try:
        _prof, feed = _pickup_last_good_feed(user_token)
        blob = feed.get('pickup_last_good')
        items = blob.get('items') if isinstance(blob, dict) else blob
        out = []
        for it in (items or []):
            if not isinstance(it, dict):
                continue
            if not (it.get('utc') and it.get('summary')
                    and it.get('anchor') and it.get('day')):
                continue
            out.append(dict(it))
        return out
    except Exception as e:
        log.warning('[lh_flightops] pickup last-good load: %s', type(e).__name__)
        return []


def pickup_last_good_anchors(user_token):
    """Anker-Keys (Abflug-Minuten), für die wir SCHON einen Pickup kennen.
    Genau die Legs, für die sich ein teurer Fern-Rotations-Call NICHT lohnt."""
    try:
        return {_anchor_key(it.get('anchor'))
                for it in pickup_last_good_load(user_token)
                if _anchor_key(it.get('anchor'))}
    except Exception:
        return set()


def _pickup_last_good_norm(items):
    """Vergleichsform (ohne `ts`) — nur inhaltliche Änderungen dürfen schreiben."""
    return sorted((str(it.get('day') or ''), str(it.get('utc') or ''),
                   str(it.get('summary') or ''), _anchor_key(it.get('anchor')))
                  for it in (items or []) if isinstance(it, dict))


def pickup_last_good_store(user_token, ics, previous=None, now=None):
    """Merkt sich, was DIESER Import für jeden Tag wirklich ausgeliefert hat.
    Gibt die neue Liste zurück; schreibt nur, wenn sie sich inhaltlich geändert
    hat (ein refresh-all-Lauf über 250 Profile soll keine 250 Profil-Writes
    kosten). Wirft nie."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    now_iso = now.strftime('%Y-%m-%dT%H:%M:%SZ')
    floor_day = (now - _dt.timedelta(days=_PICKUP_LAST_GOOD_KEEP_BACK_D)
                 ).strftime('%Y-%m-%d')
    prev_list = list(previous or [])
    prev_ts = {}
    for it in prev_list:
        if isinstance(it, dict) and it.get('ts'):
            prev_ts[(str(it.get('day') or ''),
                     _anchor_key(it.get('anchor')))] = str(it.get('ts'))
    covered = _ics_covered_days(ics)
    keep = []
    for it in pickups_in_ics(ics):
        if it['day'] < floor_day:
            continue
        it['ts'] = prev_ts.get((it['day'], _anchor_key(it['anchor'])), now_iso)
        keep.append(it)
    fresh_days = {it['day'] for it in keep}
    # Tage, die dieser Import GAR NICHT abgedeckt hat, bleiben unangetastet.
    for it in prev_list:
        if not isinstance(it, dict):
            continue
        day = str(it.get('day') or '')
        if not day or day < floor_day or day in fresh_days or day in covered:
            continue
        seen = _pickup_inst(it.get('ts'))
        if seen is not None and (now - seen).days > _PICKUP_LAST_GOOD_MAX_AGE_D:
            continue
        keep.append(dict(it))
    keep.sort(key=lambda x: (str(x.get('day') or ''), str(x.get('utc') or '')))
    keep = keep[-_PICKUP_LAST_GOOD_MAX:]
    if _pickup_last_good_norm(keep) == _pickup_last_good_norm(prev_list):
        return keep
    try:
        import app as _app
        prof, feed = _pickup_last_good_feed(user_token, fresh=True)
        feed['pickup_last_good'] = {'ts': now_iso, 'items': keep}
        prof['calendar_feed'] = feed
        _app._profile_save(user_token, prof)
    except Exception as e:
        log.warning('[lh_flightops] pickup last-good save: %s', type(e).__name__)
    return keep


def apply_pickup_last_good(user_token, ics, now=None):
    """NIE-LÖSCHEN-RIEGEL: hängt bekannte Pickups wieder ein, deren Anker-Leg
    unverändert ist, und schreibt den neuen Stand fort. Wirft nie.
    `now` nur für Tests (die Aufräum-Grenze hängt an der Wanduhr)."""
    if not ics:
        return ics
    try:
        stored = pickup_last_good_load(user_token)
        if stored:
            ics = merge_ical_pickups(ics, stored, uid_prefix='pu-lastgood',
                                     strict_anchor=True)
        pickup_last_good_store(user_token, ics, stored, now=now)
    except Exception as e:
        log.warning('[lh_flightops] pickup last-good wiring: %s',
                    type(e).__name__)
    return ics


def _tz_offset_min_east(iso_utc, iata):
    """Eigene Stations-TZ → Minuten ÖSTLICH von UTC zum Zeitpunkt `iso_utc`
    (FRA im Juli = +120). None, wenn die Station unbekannt ist — dieselbe
    Quelle wie `app._ics_local_hhmm_at` (airport_tz), damit das Audit genau die
    Tabelle prüft, die der Roster auch wirklich benutzt."""
    try:
        from datetime import datetime as _d
        from zoneinfo import ZoneInfo as _ZI
        from airport_tz import airport_tz as _atz
        tzname = _atz(iata) if iata else None
        if not tzname:
            return None
        dt = _d.fromisoformat(str(iso_utc or '').replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_ZI('UTC'))
        off = dt.astimezone(_ZI(tzname)).utcoffset()
        return None if off is None else int(off.total_seconds() // 60)
    except Exception:
        return None


def tz_audit(resp, max_lines=60):
    """TELEMETRIE, sonst NICHTS. Vergleicht `startTimeZoneOffset`/
    `endTimeZoneOffset` der Duty-Events mit der Verschiebung, die unsere eigene
    Stations-Tabelle (airport_tz) für denselben Zeitpunkt rechnet, und loggt
    Abweichungen mit MAX EINER Zeile pro Station und Import-Lauf.

    VORZEICHEN: LH liefert die JavaScript-Konvention (`getTimezoneOffset`) —
    Minuten, die man zur ORTSZEIT addiert, um UTC zu bekommen. FRA im Juli
    kommt deshalb als -120, obwohl die Station UTC+2 liegt. Verglichen und
    geloggt wird einheitlich in Minuten ÖSTLICH von UTC (also `-lh`), sonst
    liest sich jede Zeile wie ein Fehler.

    Ändert NICHTS am Import. Rückgabe (für Tests): Liste der Abweichungen als
    [{'station','own','lh','event'}]. Wirft nie."""
    out = []
    try:
        seen = set()
        for d in _as_list((resp or {}).get('rosterDays')
                          if isinstance(resp, dict) else None):
            if not isinstance(d, dict):
                continue
            for ev in _as_list(d.get('events')):
                if not isinstance(ev, dict):
                    continue
                label = str(ev.get('eventDetails')
                            or ev.get('eventType') or '').strip()[:40]
                for t_key, loc_key, off_key in (
                        ('startTime', 'startLocation', 'startTimeZoneOffset'),
                        ('endTime', 'endLocation', 'endTimeZoneOffset')):
                    stn = str(ev.get(loc_key) or '').upper().strip()
                    iso = str(ev.get(t_key) or '').strip()
                    raw = ev.get(off_key)
                    if len(stn) != 3 or not iso or stn in seen:
                        continue
                    try:
                        lh_east = -int(raw)
                    except (TypeError, ValueError):
                        continue
                    own_east = _tz_offset_min_east(iso, stn)
                    if own_east is None or own_east == lh_east:
                        continue
                    seen.add(stn)
                    out.append({'station': stn, 'own': own_east,
                                'lh': lh_east, 'event': label})
                    log.info('[tz-audit] station=%s own=%s lh=%s event=%s',
                             stn, own_east, lh_east, label)
                    if len(out) >= max_lines:
                        return out
    except Exception as e:
        log.warning('[lh_flightops] tz-audit: %s', type(e).__name__)
    return out


def duty_events_to_ics(resp, pickups=None, rot_legs=None, enrich=True):
    """FlightOps-Duty-Events → ICS-String (oder None). Pure/testbar.
    Flight-Events → VEVENT im LH-Summary-Format ('LH400: FRA-JFK'), Off/Vac/
    Standby/Hotel → Marker-/Layover-Events. Zeiten kommen als UTC-ISO. NICHTS
    wird erfunden; unbekannte Kategorien reisen als Roh-Summary mit.

    `pickups` (optional) = Ergebnis von parse_rotation_pickups/
    rotation_pickups_for. Ist es leer oder trägt es für ein Leg keinen Wert,
    entsteht KEIN Pickup-Event — geraten wird nie.

    `rot_legs` (optional) = Ergebnis von parse_rotation_legs (dieselbe, ohnehin
    geholte COMMON_CREW_ROTATION-Antwort). `enrich=False` schaltet ALLE Welle-0-
    Zusätze ab (Historien-Import).

    ── WELLE-0-ZUSÄTZE (X-Props, SUMMARY bleibt UNVERÄNDERT!) ────────────────
    Der SUMMARY jedes VEVENTs ist iOS-Regex-Kontrakt („HH:MM LT Pickup XXX",
    „DH LH 1623: KRK-MUC"). Neue Information reist deshalb AUSSCHLIESSLICH als
    X-Prop (und beim Pickup zusätzlich als DESCRIPTION-Zeile) mit:
      X-AEROX-DH:1        Deadhead-Leg
      X-AEROX-DOS:<n>     eventAttributes.dayOfShift (Rotationstag)
      X-AEROX-ACCHG:1     nach diesem Leg wechselt das Gerät
      X-AEROX-HOTEL:<name>  Layover-Hotel (Pickup-VEVENT + ankommendes Leg)
    Fehlt ein Wert, fehlt die ZEILE — nie ein Platzhalter."""
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

    def _day_of_shift(ev):
        """eventAttributes.dayOfShift → int oder None. Über `_as_list`, weil LH
        Ein-Element-Arrays als Skalar rendert (Known-Issue) — käme
        eventAttributes je als [{…}], ginge das Feld sonst lautlos dunkel
        (dieselbe Härtung wie beim rotationId-Lesen in pickup_rotation_ids)."""
        for ea in _as_list(ev.get('eventAttributes')):
            if not isinstance(ea, dict):
                continue
            v = ea.get('dayOfShift')
            if v in (None, ''):
                continue
            try:
                n = int(v)
            except (TypeError, ValueError):
                continue
            if 1 <= n <= 99:
                return n
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
            # DER TYP ENTSCHEIDET, NICHT DER TEXT (Owner-Bug 2026-07-31,
            # „FRA→SBA / Santa Barbara"): das echte Prod-Event 15.02.2026 war
            # eventCategory=STANDBY / eventType=GROUNDEVENT / eventDetails='SBA'
            # / FRA→FRA — dreifach als Nicht-Flug gekennzeichnet. Ein
            # GROUNDEVENT ist NIE ein Flug, egal was in eventDetails steht;
            # und startLocation == endLocation ist NIE eine Strecke. Ohne die
            # beiden Guards konnte ein künftiges Schema-Drift-Event (cat
            # 'flight', etype GROUNDEVENT o.ä.) als Leg gemintet werden.
            is_flight = ((etype == 'flight' or cat in _FLIGHT_CATS)
                         and etype != 'groundevent')
            if is_flight and len(frm) == 3 and len(to) == 3 and frm != to \
                    and st and en:
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
                        # HOTELNAME (Welle 0): parse_rotation_pickups trägt ihn
                        # längst mit, der ICS-Builder hat ihn bisher verworfen.
                        # SUMMARY bleibt byte-identisch (iOS-Regex-Kontrakt) —
                        # der Name reist als DESCRIPTION-Zeile + X-Prop.
                        _pu_hotel = (_ics_text_value(_pu.get('hotel'))
                                     if enrich else '')
                        lines += ['BEGIN:VEVENT', f'UID:pu-{uid}',
                                  f'DTSTART:{_pst}', f'DTEND:{_pst}',
                                  f'SUMMARY:{_hh} LT Pickup {frm}'] \
                            + ([f'DESCRIPTION:Hotel: {_pu_hotel}',
                                f'X-AEROX-HOTEL:{_pu_hotel}']
                               if _pu_hotel else []) \
                            + ['END:VEVENT']
                # ── WELLE-0-X-PROPS am Flug-VEVENT ──────────────────────────
                # Reihenfolge: DH → DOS → ACCHG → HOTEL. Jede Zeile fällt weg,
                # wenn LH den Wert nicht hergibt (keine Platzhalter).
                _x = []
                if enrich:
                    _rf = _rot_fact_for_leg(rot_legs, flt, frm, to, st) or {}
                    # DH-QUELLE: die Duty-Events selbst (`eventDetails` beginnt
                    # mit 'DH ', z.B. 'DH LH1623' — Tim/KRK 25.07., dieselbe
                    # Quelle, aus der schon der SUMMARY sein 'DH ' bezieht).
                    # Sie deckt ALLE Tage des Fensters ab. Der Rotations-
                    # `dutyCode == 'DH'` ist nur der Nachschlag für die wenigen
                    # Legs im Pickup-Fenster — er kann bestätigen, nie
                    # widersprechen.
                    if is_dh or _rf.get('dh') is True:
                        _x.append('X-AEROX-DH:1')
                    _dos = _day_of_shift(ev)
                    if _dos is not None:
                        _x.append(f'X-AEROX-DOS:{_dos}')
                    if _rf.get('ac_change') is True:
                        _x.append('X-AEROX-ACCHG:1')
                    # hotelName sitzt laut LH am HINFLUG-Leg zur Layover-
                    # Station — also genau an DIESEM ankommenden Leg.
                    _hn = _ics_text_value(_rf.get('hotel'))
                    if _hn:
                        _x.append(f'X-AEROX-HOTEL:{_hn}')
                lines += ['BEGIN:VEVENT', f'UID:{uid}',
                          f'DTSTART:{st}', f'DTEND:{en}',
                          f'SUMMARY:{summary}',
                          f'LOCATION:{frm} - {to}'] + _x + ['END:VEVENT']
                continue
            # UNVOLLSTÄNDIGER FLUG (Deep Review 2026-08-03): FlightOps kann ein
            # echtes FLIGHT-Event bereits mit Route/Flugnummer veröffentlichen,
            # während endTime (seltener startTime) noch null ist. Der alte Code
            # ließ es in den generischen Marker-Zweig fallen: LOCATION wurde nur
            # FRA statt FRA-JFK, also verschwand das Leg vollständig. Bekannte
            # Fakten bleiben jetzt erhalten; unbekannte Zeit wird NICHT erfunden.
            _is_incomplete_real_flight = (
                is_flight and len(frm) == 3 and len(to) == 3 and frm != to)
            if _is_incomplete_real_flight and _roster_v2_shadow_enabled():
                # Nur ein Zähler, keine Flug-/Crewdaten. Legacy bleibt Output.
                log.warning('roster_v2_shadow_incomplete_flight_candidate')
            if _is_incomplete_real_flight and _roster_v2_lh_enabled():
                import re as _re
                m = _re.search(r'\b([A-Z]{2}|\d[A-Z])\s?\d{1,4}[A-Z]?\b',
                               det.upper())
                flt = (m.group(0).replace(' ', '') if m else '').strip()
                flt_disp = _re.sub(r'^([A-Z]{2}|\d[A-Z])(?=\d)', r'\g<1> ', flt)
                is_dh = det.upper().strip().startswith('DH ')
                summary = ((f'DH {flt_disp}' if is_dh else flt_disp)
                           + f': {frm}-{to}' if flt else f'{frm}-{to}')
                incomplete = ('missing_end_time' if st and not en
                              else 'missing_start_time' if en and not st
                              else 'missing_times')
                event_lines = ['BEGIN:VEVENT', f'UID:{uid}']
                if st:
                    event_lines.append(f'DTSTART:{st}')
                    # DTEND bewusst weglassen: DTSTART==DTEND würde dem späteren
                    # Sektor eine falsche Null-Minuten-Ankunft als Wahrheit geben.
                else:
                    day = (d.get('day') or '')[:10].replace('-', '')
                    if not day:
                        continue
                    event_lines += [f'DTSTART;VALUE=DATE:{day}',
                                    f'DTEND;VALUE=DATE:{_shift(day, 1)}']
                event_lines += [f'SUMMARY:{summary}',
                                f'LOCATION:{frm} - {to}',
                                f'X-AEROX-INCOMPLETE:{incomplete}',
                                'END:VEVENT']
                lines += event_lines
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
            elif cat in ('res', 'frs', 'standby', 'sby'):
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
                # eventCategory=STANDBY (Prod 15.02.2026, eventDetails='SBA'):
                # LHs Kategorie IST die Dienstart — vorher fiel STANDBY in den
                # Roh-Zweig unten und der NACKTE Hauscode 'SBA' reiste als
                # SUMMARY. iOS las das freistehende 3-Letter-Token als IATA
                # (SBA = Santa Barbara) und erfand daraus ein Leg FRA→SBA.
                # Jetzt myTime-Prosa 'Standby (SBA)' — der Code bleibt 1:1
                # sichtbar, aber als Dienstart etikettiert, nie als Ort.
                # SCU ist die Stornierung eines Standbys, kein Standby-Typ.
                # Als Off-Day-Prosa greift die bestehende Frei-Klassifikation
                # in Feed, Kalender, Crew-State und Smart-Pickup automatisch.
                if is_cancelled_standby_marker(det):
                    summary = 'Off Day (SCU)'
                else:
                    _is_sb = (cat in ('standby', 'sby')
                              or _du.startswith('SB') or _du.startswith('STBY'))
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
                    # ⚠️ ACHTUNG (Tibor 2026-08-02): dieses Event ragt damit
                    # EINEN TAG über den letzten Hotel-Tag hinaus — bei einem
                    # Fenster-Import (Historie endet am Monatsletzten) fasst es
                    # den ersten Tag NACH dem Fenster an und der REPLACE-Aufbau
                    # entkernte dort die geflogenen Legs. Die Schutzplanke
                    # `_preserve_past_flown_days` (app.py) fängt das ab; das
                    # Log hier macht sichtbar, WANN dieser Zweig überhaupt zieht.
                    log.info('[lh_flightops] hotel-span %s %s..%s nicht '
                             'ableitbar → Datums-Fallback bis %s',
                             (to or frm), _first, _last, _shift(_last, 2))
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


# Serialisiert die Read-Modify-Write-Zyklen auf den Link-Cache. Die Merges
# (_links_load → filtern → _links_save) liefen ungeschützt in gunicorn-Threads;
# zwei gleichzeitige Merges für denselben User verwarfen gegenseitig ihre neuen
# Links. (Full-Review 2026-08-01)
_links_lock = threading.Lock()

# ── Durabler Link-Cache (Owner 18.08.) ──────────────────────────────────────
# `folinks_<token>.json` liegt auf der UNGEMOUNTETEN Container-Disk
# (`Mounts: []`) und ist nach JEDEM Deploy leer — die Folge waren
# 404-`no_access_code`-Wellen, bis jeder User sein Tages-Fenster einmal live
# nachgeladen hatte (Log 28.07. 06:00:35: der Nachlade-Call fiel zusätzlich
# unterm Hintergrund-Key-Deckel aus). Deshalb wird der Link-Cache jetzt
# zusätzlich in einer Supabase-Tabelle gespiegelt (Muster
# `flightops_crew_cache`): EINE jsonb-Zeile pro Token, Disk bleibt der heiße
# Lesepfad, SB füllt die Disk nach einem Deploy einmalig wieder auf.
# Fehlt die Tabelle (Migration supabase_migrations/
# 20260818_flightops_links_cache.sql noch nicht angewandt) oder ist SB weg,
# degradiert alles aufs alte Disk-Verhalten — fail-open, kein Crash; nach
# einem Fehler 5 min Ruhe, damit ein PostgREST-404 nicht jeden Request kostet.
_LINKS_TABLE = 'flightops_links_cache'
_links_tbl_state = [0.0, True]   # (letzter Fehlversuch, Tabelle nutzbar?)
_LINKS_TBL_RETRY_S = 300.0
# RAM-Memo VOR Disk und SB: _links_load läuft im Crewlist-/Rotation-Hot-Path
# teils mehrfach pro Request. Kurz gehalten (Cross-Worker-Merges sollen wie
# bisher zeitnah sichtbar werden); gekeyt auf den Datei-PFAD, nicht den Token
# (Tests/Container mit anderem _flow_dir teilen sich sonst Einträge).
_LINKS_MEMO_TTL_S = 10.0
_links_memo = {}                 # links_path → (expires_at, links)


def _links_tbl_ok():
    """True wenn die Link-Tabelle gerade als nutzbar gilt (wie _crew_tbl_ok)."""
    try:
        import app as _app
        if not getattr(_app, 'SB_AVAILABLE', False):
            return False
    except Exception:
        return False
    if (not _links_tbl_state[1]
            and (time.time() - _links_tbl_state[0]) < _LINKS_TBL_RETRY_S):
        return False
    return True


def _links_tbl_fail(exc):
    _links_tbl_state[0], _links_tbl_state[1] = time.time(), False
    log.warning('[lh_flightops] links_cache-Tabelle nicht nutzbar (%s) — '
                'Disk-only bis zur Migration', type(exc).__name__)


def _links_sb_get(user_token):
    """Links-Liste aus der Tabelle — None bei Miss/leer/nicht verfügbar."""
    if not (user_token and _links_tbl_ok()):
        return None
    try:
        import app as _app
        r = (_app.sb.table(_LINKS_TABLE).select('links')
             .eq('token', user_token).limit(1).execute())
        _links_tbl_state[1] = True
        rows = getattr(r, 'data', None) or []
    except Exception as e:
        _links_tbl_fail(e)
        return None
    if rows and isinstance(rows[0].get('links'), list) and rows[0]['links']:
        return rows[0]['links']
    return None


def _links_sb_put(user_token, links):
    """Best-effort-Spiegel nach SB. Leere Listen werden nicht geschrieben —
    ein frischer Container ohne Disk-Bestand darf den durablen Stand nicht
    mit [] überschreiben (gleiche Füllen-nie-überschreiben-Regel wie bei
    Toleranz-Fenstern)."""
    if not (user_token and isinstance(links, list) and links
            and _links_tbl_ok()):
        return False
    try:
        import app as _app
        (_app.sb.table(_LINKS_TABLE).upsert(
            {'token': user_token, 'links': links, 'ts': time.time()},
            on_conflict='token').execute())
        _links_tbl_state[1] = True
        return True
    except Exception as e:
        _links_tbl_fail(e)
        return False


def _links_disk_write(p, links):
    """Atomar schreiben: erst in eine Temp-Datei neben dem Ziel, dann
    `os.replace`. Das alte `open(p, 'w')` kürzte die Datei SOFORT auf 0 Bytes —
    ein Absturz oder ein Neustart des Containers mitten im Dump hinterließ eine
    kaputte JSON-Datei, und der accessCode-Cache (die Grundlage für Crewlist/
    Rotation ohne zusätzliche LH-Calls) war für diesen User verloren."""
    tmp = None
    try:
        tmp = f'{p}.tmp{os.getpid()}'
        with open(tmp, 'w') as f:
            json.dump({'ts': time.time(), 'links': links}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except Exception as e:
        log.warning('[lh_flightops] links_save: %s', type(e).__name__)
        try:
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)
        except Exception:
            pass


def _links_memo_put(p, links):
    if len(_links_memo) > 2000:
        _links_memo.clear()      # grober Deckel reicht — Memo ist 10-s-Ware
    _links_memo[p] = (time.time() + _LINKS_MEMO_TTL_S, links)


def _links_save(user_token, links):
    """Disk (atomar) + RAM-Memo + SB-Spiegel (best-effort)."""
    p = _links_path(user_token)
    if not p or not isinstance(links, list):
        return
    _links_disk_write(p, links)
    _links_memo_put(p, links)
    _links_sb_put(user_token, links)


def _links_load(user_token):
    """RAM-Memo → Disk → SB. Der SB-Treffer rehydriert die Disk, damit der
    Hot-Path nach einem Deploy sofort wieder lokal liest."""
    p = _links_path(user_token)
    if not p:
        return []
    now = time.time()
    hit = _links_memo.get(p)
    if hit and hit[0] > now:
        return hit[1]
    links = None
    try:
        if os.path.exists(p):
            with open(p) as f:
                d = json.load(f)
            links = d.get('links') or []
    except Exception:
        links = None
    if not links:
        sb_links = _links_sb_get(user_token)
        if sb_links:
            links = sb_links
            _links_disk_write(p, links)
    if not isinstance(links, list):
        links = []
    _links_memo_put(p, links)
    return links


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


def _resolve_link_params(user_token, service, flight, date, dep=None, arr=None,
                         interactive=False):
    """Link-Params aus dem Cache; bei Miss das Tages-Fenster live nachladen
    (1 Duty-Events-Call) und Cache erneuern. None wenn der Flug nicht im
    eigenen Roster ist (dann gibt es auch keinen accessCode).

    `interactive=True` reicht bis ins Key-Budget-Gate durch: der Link-Cache
    liegt auf der ungemounteten Container-Disk (`Mounts: []`) und ist nach
    JEDEM Deploy leer — ein Crew-Tap braucht dann genau diesen Nachlade-Call.
    Unter dem Hintergrund-Deckel fiel er in vollen Stunden aus und der User
    bekam 404 no_access_code (Log 28.07. 06:00:35)."""
    p = _links_find(_links_load(user_token), service, flight, date, dep, arr)
    if p:
        return p
    if not date:
        return None
    resp = duty_events(user_token, date, date, interactive=interactive)
    if not isinstance(resp, dict):
        return None
    fresh = extract_duty_links(resp)
    if fresh:
        # Lesen+Schreiben unter EINEM Lock — sonst überschreiben sich zwei
        # gleichzeitige Merges desselben Users gegenseitig.
        with _links_lock:
            merged = [l for l in _links_load(user_token)
                      if not any(l == g for g in fresh)] + fresh
            _links_save(user_token, merged[-800:])
    return _links_find(fresh, service, flight, date, dep, arr)


# ── SIM-Crewliste: eigene Referenz-Suche (Mark Elser, Forum 2026-07-30) ─────
# Ein Simulator-Termin hat KEINE Flugnummer. Die Referenz aus den Duty-Events
# wird allein über das Datum adressiert (live belegt 2026-07-30):
#     {"forDate": "2026-07-07Z", "accessCode": "…"}
# gegenüber der Flug-Crewliste
#     {"flightDesignator": "LH1558", "flightDate": "…", "departureAirport": …}
# `_links_find` verlangt zwingend einen passenden `flightDesignator` und kann
# eine SIM-Referenz deshalb NIE finden — genau deshalb blieb der Service
# unerreichbar, obwohl die Funktion seit Langem im Code steht.
def _links_find_sim(links, date):
    """SIM-Referenz für einen Tag oder None. `date` als YYYY-MM-DD; LH hängt
    ein 'Z' an (`forDate`), deshalb Präfix-Vergleich. Pure/testbar."""
    dt_ = (date or '')[:10]
    if not dt_:
        return None
    for l in links or []:
        if not isinstance(l, dict) or l.get('service') != 'simulatorcrewlist':
            continue
        p = l.get('params') or {}
        if (p.get('forDate') or '').startswith(dt_):
            return p
    return None


def _resolve_sim_link_params(user_token, date, interactive=False):
    """Wie `_resolve_link_params`, nur für den SIM-Tag: Cache → bei Miss das
    Tages-Fenster live nachladen (1 Duty-Events-Call) und Cache erneuern.
    None, wenn an dem Tag kein SIM im eigenen Plan steht."""
    p = _links_find_sim(_links_load(user_token), date)
    if p:
        return p
    if not date:
        return None
    resp = duty_events(user_token, date, date, interactive=interactive)
    if not isinstance(resp, dict):
        return None
    fresh = extract_duty_links(resp)
    if fresh:
        # Lesen+Schreiben unter EINEM Lock — sonst überschreiben sich zwei
        # gleichzeitige Merges desselben Users gegenseitig.
        with _links_lock:
            merged = [l for l in _links_load(user_token)
                      if not any(l == g for g in fresh)] + fresh
            _links_save(user_token, merged[-800:])
    return _links_find_sim(fresh, date)


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


def _abbrev_parts(name):
    """„Marco C." → ('Marco', 'C'); voller Name/Einzelwort → None.

    Das LETZTE Token muss ein einzelner Buchstabe sein (optional mit Punkt) —
    genau so kürzt LH ab. Pure/testbar.
    """
    toks = [t for t in str(name or '').split() if t]
    if len(toks) < 2:
        return None
    last = toks[-1].rstrip('.')
    if len(last) != 1 or not last.isalpha():
        return None
    return toks[0], last.upper()


def _roster_tokens_for_leg(flight, date):
    """Tokens der AeroX-User, die DIESEN Flug an DIESEM Tag im EIGENEN Roster
    stehen haben.

    ⚠️ DAS IST DER BEWEIS, DEN DER NAME NICHT LIEFERN KANN. Die Crew-Liste
    nennt Kollegen abgekürzt („Marco C.") und ohne Personalnummer — daraus ist
    eine Identität nicht ableitbar (s. Banner in `_match_aerox_profiles`). Aber
    wer seinen Dienstplan in AeroX hat, hat SELBST hinterlegt, dass er auf
    diesem Leg sitzt. Das ist eine Aussage aus eigener Quelle statt einer
    Vermutung über fremde Daten — und es ist genau die Tatsache, an der die
    Marco-Verwechslung gescheitert wäre: Marco Christ stand nie auf LH762.

    Läuft über `payload @> {...}` (jsonb-Containment) und damit über den GIN-
    Index `roster_snapshots_payload_gin`: 2,6 ms statt 212 ms Seq-Scan. Ohne
    den Index gehört dieser Aufruf NICHT in den Serve-Pfad.

    Wirft nie — ohne Treffer bleibt es beim pk-Beweis.
    """
    try:
        import app as _app
        if not getattr(_app, 'SB_AVAILABLE', False):
            return []
        f = re.sub(r'\s+', '', str(flight or '')).upper()
        d = str(date or '')[:10]
        if not f or not re.match(r'^\d{4}-\d{2}-\d{2}$', d):
            return []
        probe = {'tage': [{'datum': d, 'ical_sectors': [{'flight': f}]}]}
        r = (_app.sb.table('roster_snapshots').select('token')
             .contains('payload', probe).limit(60).execute())
        return [row.get('token') for row in (r.data or []) if row.get('token')]
    except Exception as e:
        log.warning('[lh_flightops] roster_probe: %s', type(e).__name__)
        return []


def _match_aerox_profiles(members, flight=None, date=None):
    """Crew-Listen-Mitglieder → AeroX-PUBLIC-Profile (best-effort, wirft nie).
    Primär EXAKT über die LH-Personalnummer (metadata.lh_pk_number — beim
    Duty-Events-Import jedes verbundenen Users gespeichert), Fallback exakter
    VOLLER Name (case-insensitiv, NUR eindeutige Treffer) + Lufthansa-Airline.

    ⚠️ ABGEKÜRZTE NAMEN („Marco C.") VERKNÜPFEN NIE — kein unscharfer
    Namens-Match mehr (Owner-Entscheid 2026-07-30 nach der Marco-C.-
    Verwechslung; ausführliche Begründung im Rumpf unten). Ohne Treffer bleibt
    das Mitglied einfach ohne `aerox`-Feld, und die App zeigt es ohne Badge
    und ohne Profil-Link.

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

        # ══════════════════════════════════════════════════════════════════
        #  KEIN UNSCHARFER NAMENS-FALLBACK MEHR — ABGEKÜRZTE NAMEN
        #  VERKNÜPFEN NIE (Owner-Entscheid 2026-07-30).
        # ══════════════════════════════════════════════════════════════════
        # Hier stand ein Fuzzy-Match: aus „Marco C." wurden Vorname + Nachname-
        # Initial gezogen und mit LH-Profilen verglichen; bei GENAU EINEM
        # Kandidaten wurde verknüpft.
        #
        # ⚠️ DER VORFALL (30.07.): Die Kabinen-Crew-Liste führte „Marco C." —
        # in Wahrheit **Marco Comajuncosas Grether** (pk 450460I), der GAR
        # NICHT auf AeroX ist. Von den LH-Marcos auf AeroX (Pravisani, Sturm,
        # Reitsema, Sunday, Christ, Saas) hat nur **Marco Christ** einen
        # „C"-Nachnamen → genau ein Kandidat → verknüpft. Die App zeigte
        # „Auf AeroX" und öffnete beim Tap das ECHTE Profil eines Unbeteiligten.
        # Eine AeroX-Mitgliedschaft wurde also frei erfunden.
        #
        # ⚠️ WARUM DIE „EINDEUTIGKEIT" NICHTS TAUGTE — SIE PRÜFTE DIE FALSCHE
        # SEITE. Geprüft wurde, ob der Treffer INNERHALB VON AEROX eindeutig
        # ist, nicht ob die Abkürzung selbst eindeutig ist. „Marco C." kann bei
        # LH Comajuncosas, Christ, Conrad, Clausen sein. Damit wird die
        # Absicherung STÄRKER, je mehr Marcos fehlen — je WENIGER AeroX-User es
        # gibt, desto „eindeutiger" der falsche Treffer. Das ist genau verkehrt.
        #
        # Zweiter, unabhängiger Defekt derselben Zeilen: verglichen wurde
        # `ntoks[-1]`, also das LETZTE Namens-Token. LH kürzt Doppelnachnamen
        # auf den ERSTEN ab („Comajuncosas Grether" ⇒ „C."), der echte Mensch
        # wäre also auf „Grether" geprüft worden und hätte seine eigene
        # Abkürzung nie treffen können.
        #
        # ⚠️ WARUM ES KEINEN ERSATZ MIT „ZWEITEM SIGNAL" GIBT (geprüft, bewusst
        # verworfen): Die Idee war, unscharf nur mit Bestätigung durch Homebase
        # UND Position zu verknüpfen. **Die Crew-Liste trägt keine Homebase** —
        # `parse_crew_list` liefert ausschliesslich {position, name, pk, duty}.
        # Bleibt die Position, und die identifiziert niemanden: auf einem Flug
        # sitzen viele FB, und mehrere Marcos an derselben Base teilen sie sich.
        # Ein zweites Signal, das nichts beweist, erzeugt nur teurere
        # Fehltreffer.
        #
        # ⚠️ UND ES BRAUCHT IHN NICHT (Owner 2026-07-30: „wenn mensch sich mit
        # LH verbindet weiß man doch wer wer genau ist"): Genau so ist es. Die
        # Crew-Liste liefert `pkNumber` für JEDES Mitglied, und `_store_own_pk`
        # legt sie beim LH-Connect jedes Users ab (Stand 30.07.: 876 von 1734
        # LH-Profilen haben `lh_pk_number`). Wer verbunden ist, wird oben EXAKT
        # und fehlerfrei getroffen. Wer nicht verbunden ist, ist über den Namen
        # nicht beweisbar — für den existiert schlicht keine Wahrheit, die man
        # matchen könnte. Ein Raten ist dann kein „best effort", sondern eine
        # Behauptung über die Identität eines Menschen.
        #
        # Owner-Regel, die hier entscheidet: „lieber keine Zeile als ein
        # falscher Wert" — für Identitäten erst recht. Fehlt der Match, zeigt
        # iOS den abgekürzten Namen ohne „Auf AeroX"-Badge und ohne Profil-Tap
        # (`FlightCrewSheet`: `m.aerox == nil` ⇒ `CrewMemberDetailView`), also
        # genau das gewünschte Verhalten.
        #
        # Wer das wieder aufwerten will, braucht eine echte Identitätsquelle —
        # NICHT mehr Heuristik auf einem Buchstaben. Genau das ist der Weg
        # unten.

        # ══════════════════════════════════════════════════════════════════
        #  DRITTER WEG: ROSTER-BEWEIS (30.07.)
        # ══════════════════════════════════════════════════════════════════
        # Owner-Befund 30.07.: „mit dem neusten update sehe ich keinen mehr auf
        # AeroX". Stimmt — und die Zahlen erklären warum: von 25.874 gecachten
        # Crew-Einträgen sind 22.584 (87 %) abgekürzt OHNE pk, also aus LHs
        # Daten grundsätzlich nicht auflösbar. Von den 3.291 mit pk sind 2.847
        # die EIGENE Nummer des Abrufers — LH verrät einem also fast nur, wer
        # man selbst ist. Das alte Fuzzy-Matching hat diese Lücke mit Raten
        # gefüllt; das ist weg und bleibt weg.
        #
        # Aber es gibt eine zweite Quelle, die NICHT geraten ist: den Roster,
        # den unsere Nutzer selbst mitbringen. Wer LH762 am 30.07. im eigenen
        # Dienstplan stehen hat, sitzt nachweislich auf diesem Leg. Das ist
        # eine Aussage über sich selbst, kein Rückschluss auf einen fremden
        # Namen — und es ist die Tatsache, die den Vorfall verhindert hätte:
        # Marco Christ stand nie auf LH762, er wäre hier nie Kandidat.
        #
        # DREI BEDINGUNGEN, alle nötig:
        #   1. Der Mensch steht mit diesem Flug an diesem Tag im eigenen Roster.
        #   2. Vorname exakt, und das Initial passt auf IRGENDEIN Nachnamen-
        #      Token — LH kürzt Doppelnachnamen auf den ERSTEN ab
        #      („Comajuncosas Grether" ⇒ „C."), ein Vergleich nur gegen das
        #      letzte Token verfehlt genau diese Menschen.
        #   3. Unter den Roster-Belegten bleibt GENAU EINER übrig, und sein
        #      Profil ist nicht schon an ein anderes Mitglied vergeben.
        by_roster = {}
        abbrev_need = [m for m in need
                       if m['name'].strip().lower() not in by_name
                       and _abbrev_parts(m.get('name'))]
        if abbrev_need and flight and date:
            rostered = []
            tokens = _roster_tokens_for_leg(flight, date)
            if tokens:
                try:
                    r = (_app.sb.table('user_profiles').select(sel)
                         .in_('token', tokens[:60]).limit(60).execute())
                    rostered = list(r.data or [])
                except Exception:
                    rostered = []
            # Schon per pk/Name belegte Profile sind vergeben — dieselbe Person
            # darf nicht zweimal in derselben Liste stehen.
            taken = {str((row or {}).get('token'))
                     for row in list(by_pk.values()) + list(by_name.values())}
            for m in abbrev_need:
                first, initial = _abbrev_parts(m.get('name'))
                if len(first) < 2:
                    continue          # zu kurzer Vorname → zu unspezifisch
                cand = []
                for row in rostered:
                    if str(row.get('token')) in taken:
                        continue
                    ntoks = [t for t in str(row.get('name') or '').split() if t]
                    if len(ntoks) < 2 or ntoks[0].lower() != first.lower():
                        continue
                    if not any(t[:1].upper() == initial for t in ntoks[1:]):
                        continue
                    cand.append(row)
                uniq = {row.get('token') for row in cand}
                if len(uniq) == 1:
                    by_roster[m['name'].strip().lower()] = cand[0]
                    taken.add(str(cand[0].get('token')))

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
                   or by_name.get(str(m.get('name') or '').strip().lower())
                   or by_roster.get(str(m.get('name') or '').strip().lower()))
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


# ── LH-Platzhalter (Keine-Fake-Werte-Regel) ─────────────────────────────────
#
# LH füllt Namens-/Raumfelder statt mit `null` mit LITERALEN Platzhaltern
# ('N/A', live am `briefingRoom` DUS gesehen) oder mit internen Codes
# ('H9941671', so in der Doku-Fixture beim Hotelnamen). Beides sieht wie ein
# echter Wert aus und stand ungegated schon einmal als „Hotel | N/A (0:30*)"
# auf der Karte (adversarialer Review 27.07.). Die Regel lebte bis heute nur
# in `daily_briefing._valid_lh_hotel_name`; sie ist aber eine Eigenschaft der
# LH-ANTWORT, nicht des Daily Briefings — deshalb steht sie jetzt hier, und
# daily_briefing benutzt genau DIESE Funktion weiter (eine Wahrheit, kein
# Klon, der beim nächsten Platzhalter-Fund auseinanderläuft).
_LH_PLACEHOLDER_RE = re.compile(r'^(H\d{4,}|N/?A|NA|TBD|UNKNOWN|[-.]+)$',
                                re.IGNORECASE)


def is_lh_placeholder(value):
    """True für literale LH-Platzhalter, interne Codes und Leerwerte. Pure."""
    s = re.sub(r'\s', '', str(value or ''))
    if not s:
        return True
    return bool(_LH_PLACEHOLDER_RE.match(s))


# ── BOARDING-ZEIT für Widget/Live-Aktivität (Owner 2026-07-27 nachgefordert) ─
#
# DIE QUELLE. `boardingBegin` aus COMMON_CHECK_IN_TIMES ist die EINZIGE echte
# Boarding-Zeit, die dieses System kennt. Sie war bis heute serverseitig
# geparst und wurde weggeworfen: die beiden Aufrufer von
# `parse_check_in_times` sind der Endpoint `/api/lh/flightops/checkin` (den
# ruft kein Client) und `daily_briefing._checkin` (der liest nur
# `briefingRoom`). Der iOS-Roster-Pfad (`/api/user/briefing/<token>`) kannte
# sie gar nicht — deshalb fehlte der Schritt „Boarding" in der Kette.
#
# WARUM NUR `boardingBegin` UND NICHT `paxOnBoard`. Das sind zwei
# verschiedene Ereignisse: Beginn des Einsteigens gegenüber „Passagiere an
# Bord" (= Boarding fertig). LH liefert am Layover oft nur das zweite (Beleg
# im Docstring von `check_in_times`: BOM 19:37Z paxOnBoard, kein
# boardingBegin). Es als „Boarding" auf den Sperrbildschirm zu schreiben wäre
# eine falsch beschriftete Zeit — dieselbe Klasse Fehler wie „Abflug minus
# 30 min". Fehlt `boardingBegin`, FÄLLT DER SCHRITT WEG.
#
# WARUM DAS DIE LH-QUOTE NICHT REISST (Tagesdeckel am 29.07. gerissen):
#  · HINTERGRUND-Call (`interactive=False`). Bei gerissenem Deckel stirbt er
#    ZUERST und lässt den reservierten Headroom echter Taps unangetastet.
#    Folge: kein Boarding-Schritt — genau das richtige Verhalten.
#  · NIE synchron im Request. Der Serve-Pfad liest ausschliesslich den Cache;
#    ein Miss WÄRMT im Daemon-Thread, der nächste Roster-Poll sieht den Wert.
#    Ein Render löst damit nie einen Call aus, auf den jemand wartet.
#  · Enges Fenster: nur das nächste noch nicht abgeflogene Leg und nur, wenn
#    der Abflug in `_BOARDING_LEAD_MAX_S` (6 h) bevorsteht. Die Live-Aktivität
#    beginnt ~45 min vor dem Pickup (~2–2,5 h vor Abflug); früher braucht die
#    Zahl niemand.
#  · NEGATIV-Cache. LH trägt Check-in-Zeiten erst spät nach; ohne ihn würde
#    jeder Poll dieselbe Leere erneut erfragen.
# Grössenordnung: ≤ 1 Treffer-Call je User und Dienst (6 h TTL) plus höchstens
# zwei Fehlversuche (3 h TTL) im 6-h-Fenster.
# WELLE 2 (Owner 31.07.): DIESELBE bezahlte Antwort trägt mehr als
# `boardingBegin`. `crewAtSecurityCheck`, `crewBusDeparture` (= APRON-Bus
# Briefing→Flieger, NICHT der Hotel-Transfer!) und `briefingRoom` hängen jetzt
# additiv am selben Sektor — 0 zusätzliche LH-Calls, derselbe Fetch, derselbe
# Cache-Eintrag, dasselbe enge Fenster. `paxOnBoard` bleibt bewusst DRAUSSEN
# (Plan-Korrektur 31.07.: es ist die Zeit „Boarding fertig", keine Anzahl, und
# als zweite „Boarding"-Marke nur verwirrend).
#
# KONTRAKT zur App: genau diese optionalen Sektor-Felder können entstehen —
# nie mit `null`, nie leer, jedes einzeln fehlend (dann lässt die Kette den
# Schritt weg). ISO-Zeiten 1:1 von LH (UTC).
_SECTOR_MARK_FIELDS = ('boarding_iso', 'security_iso', 'crewbus_iso',
                       'briefing_room')
_BOARDING_CACHE = {}                     # key -> (ts, marks-dict)
_BOARDING_LOCK = threading.Lock()
_BOARDING_HIT_TTL_S = 6 * 3600           # gefundene Zeit
_BOARDING_MISS_TTL_S = 3 * 3600          # NEGATIV-Cache (LH trägt spät nach)
_BOARDING_LATE_REFRESH_S = 2 * 3600       # Teilantwort einmal vor Abflug erneuern
_BOARDING_CACHE_MAX = 500
_BOARDING_LEAD_MAX_S = 6 * 3600          # so früh interessiert Boarding
_BOARDING_LEAD_MIN_S = -3600             # bis 1 h nach dem Plan-Abflug
_BOARDING_INFLIGHT = set()               # laufende Wärm-Threads (kein Sturm)


def _boarding_key(user_token, flight, date, dep):
    return '|'.join([(user_token or '')[:64],
                     (flight or '').upper().replace(' ', ''),
                     (date or '')[:10], (dep or '').upper()])


def _boarding_marks_normalize(payload):
    """Cache-Nutzlast → Marken-Dict. TOLERANT gegenüber dem ALTEN Format
    (`iso|None`): der Cache ist rein prozess-lokal (ein Modul-Dict, nichts
    davon liegt auf Disk oder in Supabase), ein Deploy startet ihn also
    ohnehin leer. Die Toleranz kostet drei Zeilen und deckt den einzigen Fall
    ab, in dem beide Formate je zusammentreffen könnten — ein Reload im
    laufenden Prozess."""
    if isinstance(payload, dict):
        return {k: v for k, v in payload.items()
                if k in _SECTOR_MARK_FIELDS and v}
    if isinstance(payload, str) and payload.strip():
        return {'boarding_iso': payload.strip()}
    return {}


def _marks_cache_state(ts, marks, now, departure_epoch=None):
    """Eine LH-Teilantwort genau einmal kurz vor Abflug erneuern.

    `COMMON_CHECK_IN_TIMES` nennt Security/Crewbus oft schon beim Eintritt ins
    6-h-Fenster, `boardingBegin` aber erst später. Der normale 6-h-Hit-TTL
    würde dann die frühe Teilantwort bis nach STD festhalten. Sobald noch kein
    echtes Boarding vorliegt, wird deshalb ein VOR der 2-h-Schwelle geholter
    Treffer beim ersten Request NACH der Schwelle stale. Der neue Treffer trägt
    einen Zeitstempel nach der Schwelle und wird nicht erneut angefragt: maximal
    ein zusätzlicher LH-Call je Flug/Kategorie, durch den Shared Cache geteilt.
    """
    ttl = _BOARDING_HIT_TTL_S if marks else _BOARDING_MISS_TTL_S
    if (now - ts) > ttl:
        return 'expired'
    if marks and 'boarding_iso' not in marks and departure_epoch is not None:
        refresh_at = departure_epoch - _BOARDING_LATE_REFRESH_S
        if now >= refresh_at and ts < refresh_at:
            return 'refresh'
    return 'hit'


def _boarding_cache_get(key, now=None, departure_epoch=None):
    """(hit, marks). Bei später Nachabfrage bleibt die Teilantwort sichtbar:
    `(False, marks)` bedeutet stale-while-refresh. Auch nach TTL bleibt ein
    zuvor BELEGTER Wert sichtbar, waehrend er neu geladen wird: das exakte Leg
    steckt im Cache-Key, daher kann die bekannte Boarding-Zeit nicht ploetzlich
    zu einem anderen Flug gehoeren. Nur ein echter/negativer Miss bleibt leer.
    Ein leerer Negativ-Treffer zählt innerhalb TTL als hit."""
    now = now if now is not None else time.time()
    with _BOARDING_LOCK:
        e = _BOARDING_CACHE.get(key)
    if not e:
        return False, {}
    ts, payload = e
    marks = _boarding_marks_normalize(payload)
    state = _marks_cache_state(ts, marks, now, departure_epoch)
    if state == 'expired':
        return False, marks
    if state == 'refresh':
        return False, marks
    return True, marks


def _boarding_cache_put(key, marks, now=None, preserve_known=False):
    now = now if now is not None else time.time()
    normalized = _boarding_marks_normalize(marks)
    with _BOARDING_LOCK:
        if preserve_known:
            old = _boarding_marks_normalize(
                (_BOARDING_CACHE.get(key) or (None, {}))[1])
            # Exakter Flug-Key: neue belegte Werte gewinnen, eine leere oder
            # partielle Refresh-Antwort widerruft bekannte Marken nicht.
            old.update(normalized)
            normalized = old
        _BOARDING_CACHE[key] = (now, normalized)
        if len(_BOARDING_CACHE) > _BOARDING_CACHE_MAX:
            for k in sorted(_BOARDING_CACHE,
                            key=lambda k: _BOARDING_CACHE[k][0])[:100]:
                _BOARDING_CACHE.pop(k, None)


def boarding_begin_from_times(times):
    """`boardingBegin` aus einer `parse_check_in_times`-Map — oder None.
    Bewusst OHNE Fallback auf `paxOnBoard` (siehe Kommentarblock). Pure."""
    if not isinstance(times, dict):
        return None
    v = times.get('boardingBegin')
    if not isinstance(v, str):
        return None
    v = v.strip()
    return v or None


_BRIEFING_ROOM_MAX_LEN = 40


def briefing_room_from_times(times):
    """`briefingRoom` — aber NUR, wenn es ein plausibler Raum ist ('B4.123',
    'C2.007', 'Cabin Briefing 3'). Literale Platzhalter ('N/A') und interne
    LH-Codes ('H9941671') fallen durch `is_lh_placeholder` — dieselbe
    Filterlogik, die am Hotelnamen schon einen 'N/A' von der Karte geholt hat.
    Lieber KEINE Raumzeile als eine erfundene (Keine-Fake-Werte-Regel). Pure."""
    if not isinstance(times, dict):
        return None
    r = str(times.get('briefingRoom') or '').strip()
    if not r or len(r) > _BRIEFING_ROOM_MAX_LEN:
        return None
    if is_lh_placeholder(r):
        return None
    if not re.search(r'[A-Za-z0-9]', r):
        return None
    return r


def _checkin_iso(times, key):
    """Zeitfeld aus der Check-in-Map — nur bei PARSBAREM ISO. LH schreibt in
    Zeitfelder gelegentlich denselben Müll wie in Namensfelder; eine Zeile
    'Security · N/A' im Widget wäre exakt der Fehler, den die Owner-Regel
    verbietet. Pure."""
    if not isinstance(times, dict):
        return None
    v = times.get(key)
    if not isinstance(v, str):
        return None
    v = v.strip()
    if not v or _boarding_dep_epoch(v) is None:
        return None
    return v


def duty_marks_from_times(times):
    """Check-in-Map → alle Sektor-Felder, die EINE Antwort hergibt. Nur echte
    Werte, nie ein `None`-Schlüssel. `boardingBegin` behält seinen bisherigen
    1:1-Pfad (`boarding_begin_from_times`) — die Welle-2-Felder sind additiv
    und ändern an der Boarding-Semantik nichts. `paxOnBoard` bleibt draussen
    (Zeit „Boarding fertig", keine Anzahl — Plan-Korrektur 31.07.). Pure."""
    out = {}
    if not isinstance(times, dict):
        return out
    iso = boarding_begin_from_times(times)
    if iso:
        out['boarding_iso'] = iso
    for field, key in (('security_iso', 'crewAtSecurityCheck'),
                       ('crewbus_iso', 'crewBusDeparture')):
        v = _checkin_iso(times, key)
        if v:
            out[field] = v
    room = briefing_room_from_times(times)
    if room:
        out['briefing_room'] = room
    return out


def _boarding_fetch(user_token, flight, date, dep, arr, departure_epoch=None):
    """EIN COMMON_CHECK_IN_TIMES-Call im PRIORISIERTEN Budget → Cache.
    Wirft nie. Läuft ausschliesslich im Daemon-Thread. Aus DERSELBEN Antwort
    fallen seit Welle 2 alle Marken (`duty_marks_from_times`) — ein Call, ein
    Cache-Eintrag, mehrere Felder.

    NEGATIV-CACHE NUR NACH EINER ECHTEN ANTWORT (Owner-Befund 09.08.2026):
    Ein leeres Ergebnis wird 3 h lang festgeschrieben (`_BOARDING_MISS_TTL_S`)
    — das ist richtig, wenn LH nichts hat, und falsch, wenn wir gar nicht
    gefragt haben. Wurde der Call am Budget-Gate abgewiesen, bliebe die halbe
    Restlaufzeit des 6-h-Fensters tot, obwohl das Band Minuten später wieder
    offen sein kann. Deshalb: Budget-Block schreibt NICHTS in den Cache, der
    nächste Roster-Poll fragt erneut."""
    key = _boarding_key(user_token, flight, date, dep)
    marks = {}
    blocked = False
    try:
        # EIGENER TOPF ZUERST (s. `_DM_DAY_CEILING`). Ist er leer, gar nicht
        # erst fragen — sonst nimmt dieser Verbraucher den anderen das Band
        # weg, das die Prioritäts-Stufe für ihn geöffnet hat.
        if _dm_day_used() >= _DM_DAY_CEILING:
            log.warning('[lh_flightops] marks-Tagesdeckel %s >= %s — '
                        'Dienst-Marken übersprungen (%s %s)',
                        _dm_day_used(), _DM_DAY_CEILING, flight, date)
            blocked = True
            return marks
        p = _resolve_link_params(user_token, 'checkintimes', flight, date,
                                 dep, arr)
        # GETEILTER FLUG-CACHE VOR DEM CALL. Die Kategorie kommt aus den
        # Link-Params (die tragen dutyType/crewCategory korrekt); ohne sie
        # wird NICHT geteilt, s. `_marks_shared_key`.
        cat = (p or {}).get('crewCategory') if isinstance(p, dict) else None
        skey = _marks_shared_key(flight, date, dep, cat)
        shared_hit, shared_marks = _marks_shared_get(
            skey, departure_epoch=departure_epoch)
        if shared_hit:
            # Ein Kollege desselben Fluges hat die Antwort schon geholt —
            # kein zweiter LH-Call fuer dieselben Fakten.
            marks = shared_marks
            return marks
        st = {}
        if p:
            resp = service_get(user_token, 'COMMON_CHECK_IN_TIMES', p,
                               priority=True, status_out=st)
        else:
            resp = check_in_times(user_token, flight, date, dep, arr,
                                  priority=True, status_out=st)
        kind = st.get('kind')
        # Nur GESENDETE Calls buchen — exakt wie `_lb_budget_book` es macht:
        # ein am Gate abgewiesener Call hat LH nie erreicht und darf den
        # eigenen Topf nicht leeren.
        if kind in ('hour_budget', 'day_budget'):
            blocked = True
            return marks
        if kind != 'no_access':
            _dm_budget_book()
        marks = duty_marks_from_times(parse_check_in_times(resp))
        # Nur eine ECHTE Antwort teilen. Ein `no_access`-Leerergebnis ist eine
        # Eigenschaft DIESES Users, keine des Fluges — es darf den Kollegen
        # nicht als Negativ-Treffer im Weg stehen.
        if kind != 'no_access':
            _marks_shared_put(skey, marks, preserve_known=True)
    except Exception as e:
        log.warning('[lh_flightops] boarding_fetch: %s', type(e).__name__)
    finally:
        if not blocked:
            _boarding_cache_put(key, marks, preserve_known=True)
        with _BOARDING_LOCK:
            _BOARDING_INFLIGHT.discard(key)
    return marks


# ── EIGENER TAGESTOPF DER DIENST-MARKEN (Muster: `lhfoD-landing:`) ──────────
# Der Prioritäts-Deckel oben schützt die interaktiven Flows VOR diesem
# Verbraucher. Dieser Topf hier schützt die anderen HINTERGRUND-Verbraucher
# vor ihm: ohne ihn könnte ein Fehler in der Fenster-Logik (oder ein Roster
# mit vielen Sektoren) das Band in einem Rutsch leerlaufen lassen.
#
# 3.000 (10.08.2026): Es ist EIN Call pro FLUG und Diensttag, nicht pro Nutzer
# — der geteilte Flug-Cache unten fasst die ~20 Crew einer Langstrecke zu
# einer Abfrage zusammen, `boarding_candidate_index` wählt einen einzigen
# Sektor, `_BOARDING_INFLIGHT` verhindert Parallelläufe. Bei ~1.600 Grants
# sind das ein paar hundert Flüge pro Tag.
#
# Der ursprüngliche Wert war 300 — bemessen gegen ein Tageskontingent von
# 6.000, das es nie gab (s. Korrektur oben). Er wäre bei diesem Nutzerstand
# selbst zur Bremse geworden: das Feature hätte mittags aufgehört zu
# funktionieren, diesmal an unserem eigenen Topf statt am Haupt-Gate.
_DM_BUDGET_PREFIX = 'lhfoD-marks:'
_DM_DAY_CEILING = 500

# ── GETEILTER FLUG-CACHE DER MARKEN (Owner-Auftrag 09.08.: „smart bleiben") ──
# Briefing-Raum, Security, Crewbus und Boarding sind FLUG-Fakten, keine
# persönlichen Werte — auf einem Langstreckenflug fragen sonst ~20 Crew
# dieselbe Antwort einzeln ab. Bei 1.603 Grants ist das der grösste Hebel,
# den es ohne jeden Qualitätsverlust gibt: gleiche Daten, ein Call.
#
# ⚠️ DIE GRENZE, DIE HIER ZÄHLT: Cockpit und Kabine können VERSCHIEDENE
# Briefing-Räume haben. Der Schlüssel trägt deshalb die `crewCategory`
# (COC/CAB), die die Duty-Events-Link-Params schon korrekt mitbringen. Ein
# geteilter Eintrag ohne diese Trennung schickte die Kabine in den Raum der
# Piloten — ein falscher Raum ist schlimmer als gar keiner
# (Keine-Fake-Werte-Regel). Kategorie unbekannt ⇒ eigener Abruf, kein Teilen.
#
# KEIN LECK: gelesen wird ausschliesslich für Sektoren, die im EIGENEN Roster
# des Users stehen (`enrich_sectors_boarding` läuft auf dessen eigenen Legs).
# Geteilt werden nur die vier Marken-Felder, nichts Personenbezogenes —
# dasselbe Prinzip wie die Whitelist des Landing-Report-Caches.
_MARKS_SHARED = {}                       # (flight,date,dep,cat) -> (ts, marks)
_MARKS_SHARED_MAX = 2000


def _marks_shared_key(flight, date, dep, cat):
    c = (cat or '').strip().upper()
    if not c:
        return None                      # unbekannte Kategorie ⇒ nicht teilen
    return '|'.join([(flight or '').upper().replace(' ', ''),
                     (date or '')[:10], (dep or '').upper(), c])


def _marks_shared_get(key, now=None, departure_epoch=None):
    """(hit, marks) aus dem geteilten Flug-Cache. Gleiche TTL-Semantik wie der
    Nutzer-Cache: gefundene Marken 6 h, Negativ-Treffer 3 h."""
    if not key:
        return False, {}
    now = now if now is not None else time.time()
    with _BOARDING_LOCK:
        e = _MARKS_SHARED.get(key)
    if not e:
        return False, {}
    ts, payload = e
    marks = _boarding_marks_normalize(payload)
    state = _marks_cache_state(ts, marks, now, departure_epoch)
    if state == 'expired':
        return False, {}
    if state == 'refresh':
        return False, marks
    return True, marks


def _marks_shared_put(key, marks, now=None, preserve_known=False):
    if not key:
        return
    now = now if now is not None else time.time()
    normalized = _boarding_marks_normalize(marks)
    with _BOARDING_LOCK:
        if preserve_known:
            old = _boarding_marks_normalize(
                (_MARKS_SHARED.get(key) or (None, {}))[1])
            old.update(normalized)
            normalized = old
        _MARKS_SHARED[key] = (now, normalized)
        if len(_MARKS_SHARED) > _MARKS_SHARED_MAX:
            for k in sorted(_MARKS_SHARED,
                            key=lambda k: _MARKS_SHARED[k][0])[:200]:
                _MARKS_SHARED.pop(k, None)
_dm_day_memo = {'ts': 0.0, 'day': '', 'used': 0}
_DM_DAY_MEMO_S = 60.0
_dm_day_local = {'day': '', 'n': 0}      # in DIESEM Prozess gebuchte Calls


def _dm_budget_key(now=None):
    return _DM_BUDGET_PREFIX + time.strftime('%Y%m%d', time.gmtime(now))


def _dm_day_used(now=None):
    """Tagesstand des EIGENEN Marken-Zählers (max aus persistiertem Stand und
    dem, was dieser Prozess seit dem letzten Flush gebucht hat — der Flusher
    schreibt träge, ein Schub darf den Deckel nicht überrennen). Wirft nie."""
    day = time.strftime('%Y%m%d', time.gmtime(now))
    if _dm_day_local['day'] != day:
        _dm_day_local['day'], _dm_day_local['n'] = day, 0
    ts = time.time()
    if _dm_day_memo['day'] != day or (ts - _dm_day_memo['ts']) >= _DM_DAY_MEMO_S:
        try:
            from blueprints.aerox_data_blueprint import _budget_key_used
            used = int(_budget_key_used(_dm_budget_key(now)) or 0)
        except Exception:
            used = 0
        _dm_day_memo['ts'], _dm_day_memo['day'] = ts, day
        _dm_day_memo['used'] = used
    return max(_dm_day_memo['used'], _dm_day_local['n'])


def _dm_budget_book(now=None):
    """Einen Marken-Call im Tageszähler buchen. Wirft nie."""
    day = time.strftime('%Y%m%d', time.gmtime(now))
    if _dm_day_local['day'] != day:
        _dm_day_local['day'], _dm_day_local['n'] = day, 0
    _dm_day_local['n'] += 1
    try:
        from blueprints.lh_open_api import budget_inc_key
        budget_inc_key(_dm_budget_key(now))
    except Exception:
        pass


def _boarding_warm_async(user_token, flight, date, dep, arr,
                          departure_epoch=None):
    """Wärmt den Cache im Daemon-Thread. Pro Schlüssel höchstens ein Lauf."""
    key = _boarding_key(user_token, flight, date, dep)
    with _BOARDING_LOCK:
        if key in _BOARDING_INFLIGHT:
            return False
        _BOARDING_INFLIGHT.add(key)
    try:
        threading.Thread(target=_boarding_fetch,
                         args=(user_token, flight, date, dep, arr,
                               departure_epoch),
                         daemon=True).start()
        return True
    except Exception:
        with _BOARDING_LOCK:
            _BOARDING_INFLIGHT.discard(key)
        return False


def _boarding_dep_epoch(iso):
    """ISO-UTC → Epoch, oder None. Toleriert 'Z' und Sekundenbruchteile."""
    s = (iso or '').strip()
    if not s:
        return None
    try:
        return _dt.datetime.fromisoformat(
            s.replace('Z', '+00:00')).timestamp()
    except Exception:
        return None


def boarding_candidate_index(sectors, now_ts):
    """Index des Legs, für das eine Boarding-Zeit ÜBERHAUPT interessiert —
    das erste, dessen Plan-Abflug im Fenster
    `[now - 1 h, now + 6 h]` liegt. Sonst None. Pure/testbar.

    Ein Index statt „alle Legs": ein Umlauf mit vier Sektoren würde sonst
    vier LH-Calls kosten, und die Kette zeigt ohnehin nur den nächsten
    Abflug."""
    if not isinstance(sectors, list):
        return None
    for i, s in enumerate(sectors):
        if not isinstance(s, dict):
            continue
        # Live delay enrichment may move ``dep_iso``.  Windowing and cache
        # identity must stay anchored to the roster's plan time, otherwise a
        # delayed flight can jump out of the six-hour duty-mark window or be
        # cached under the wrong operating date.
        ep = _boarding_dep_epoch(s.get('sched_dep_iso') or s.get('dep_iso'))
        if ep is None:
            continue
        lead = ep - now_ts
        if _BOARDING_LEAD_MIN_S <= lead <= _BOARDING_LEAD_MAX_S:
            return i
    return None


def enrich_sectors_boarding(user_token, sectors, now_ts=None):
    """Hängt die Duty-Marken (`boarding_iso`, `security_iso`, `crewbus_iso`,
    `briefing_room`; alle ISO-UTC bzw. Raum-String 1:1 von LH) an den EINEN
    Sektor, für den sie zeitlich relevant sind. Rein additiv; fehlt ein Wert,
    wird sein Schlüssel NICHT gesetzt (die App lässt den Schritt dann weg —
    nie 'N/A', nie eine abgeleitete Zeit).

    Das Fenster-Gating gilt unverändert für ALLE Felder: nur das nächste noch
    nicht abgeflogene Leg, nur im 6-h-Vorlauf. Diese Marken sind für die
    Widget-/NextDutyHero-Kette am Diensttag, nicht für die Historie. Ob am
    Layover überhaupt Briefing/Security/Crewbus gezeigt werden, entscheidet
    die Anzeige (iOS/daily_briefing) — das Backend reicht durch, was LH für
    dieses Leg wirklich liefert.

    Liest im Request NUR den Cache. Ein Miss startet einen Hintergrund-Lauf und
    liefert für DIESEN Request nichts — der nächste Roster-Poll trägt die Zahl.
    Kein Netz auf dem Antwortpfad, kein Warten des Users. Wirft nie."""
    try:
        now_ts = now_ts if now_ts is not None else time.time()
        i = boarding_candidate_index(sectors, now_ts)
        if i is None:
            return False
        sec = sectors[i]
        flight = (sec.get('flight') or '').strip()
        dep = (sec.get('from') or '').strip().upper()
        arr = (sec.get('to') or '').strip().upper()
        plan_dep_iso = sec.get('sched_dep_iso') or sec.get('dep_iso') or ''
        date = plan_dep_iso[:10]
        if not flight or not dep or not date:
            return False
        # Kein Grant, kein Call. `_access_state` refresht selbst nie.
        if _access_state(user_token)[0] != 'ok':
            return False
        key = _boarding_key(user_token, flight, date, dep)
        dep_epoch = _boarding_dep_epoch(plan_dep_iso)
        hit, marks = _boarding_cache_get(
            key, now=now_ts, departure_epoch=dep_epoch)
        if not hit:
            _boarding_warm_async(user_token, flight, date, dep, arr,
                                  dep_epoch)
            # Stale-while-refresh: eine frühe echte Security-Marke darf beim
            # gezielten Boarding-Nachabruf nicht für einen Poll verschwinden.
            if not marks:
                return False
        wrote = False
        for field in _SECTOR_MARK_FIELDS:
            v = marks.get(field)
            if v:
                sec[field] = v
                wrote = True
        return wrote
    except Exception as e:
        log.warning('[lh_flightops] enrich_boarding: %s', type(e).__name__)
        return False


# ── Endpoints ────────────────────────────────────────────────────────────────
def _flightops_oauth_start_for(user_token):
    """Build a PKCE authorize URL for one already-authorized AeroX owner."""
    if not flightops_configured():
        return jsonify({'ok': False, 'error': 'not_configured'}), 503
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


@lh_flightops_bp.route('/api/lh/flightops/oauth/start', methods=['GET'])
def flightops_oauth_start():
    """Legacy iOS start route; its token-bearing query is retained unchanged."""
    return _flightops_oauth_start_for((request.args.get('token') or '').strip())


def _flightops_oauth_exchange_for(expected_owner=None):
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
    flow = _flow_take(state, expected_user_token=expected_owner)
    if not flow:
        return jsonify({'ok': False, 'error': 'state_invalid_or_expired'}), 400
    tok = _exchange_code(code, flow['verifier'])
    if not tok:
        return jsonify({'ok': False, 'error': 'exchange_failed'}), 502
    # GEBURTSSTUNDE DES GRANTS festhalten (Owner-Frage 2026-07-28: „wie viele
    # Accounts sind noch verbunden — und schon wie lange?"). Die zweite Hälfte
    # war bis hier UNBEANTWORTBAR: `relogin_at` markiert nur den TOD eines
    # Grants, für sein Alter gab es keinen Zeitstempel. `_tokens_save` ersetzt
    # den ganzen flightops_tokens-Blob, deshalb muss der Wert HIER am frischen
    # `tok` hängen — und deshalb verschwinden needs_relogin/relogin_at beim
    # Neu-Verbinden von selbst.
    _prev = _tokens_mirror_raw(flow['user_token']) or {}
    tok['connected_at'] = time.time()
    # Erst-Verbindung vs. Wieder-Verbindung unterscheiden: `first_connected_at`
    # überlebt jedes Relogin und misst die Bindung des Users an LH insgesamt,
    # `reconnects` zählt, wie oft er dafür schon nachlegen musste (= der Preis,
    # den die Grant-Burns ihn gekostet haben).
    tok['first_connected_at'] = _prev.get('first_connected_at') or tok['connected_at']
    try:
        tok['reconnects'] = int(_prev.get('reconnects') or 0) + (1 if _prev.get('refresh') or _prev.get('relogin_at') else 0)
    except (TypeError, ValueError):
        tok['reconnects'] = 0
    if not _tokens_save(flow['user_token'], tok):
        # Save NICHT bestätigt ⇒ ehrlich scheitern: ein »verbunden« ohne
        # durablen RT wäre eine Familie, die beim ersten Refresh stirbt.
        # Der User loggt sich schlicht erneut ein (neuer Grant, kein Schaden).
        log.error('[lh_flightops] exchange-save unbestätigt token=%s',
                  (flow.get('user_token') or '')[:8])
        return jsonify({'ok': False, 'error': 'store_failed'}), 503
    return jsonify({'ok': True, 'connected': True, 'scope': tok.get('scope')})


@lh_flightops_bp.route('/api/lh/flightops/oauth/exchange', methods=['POST'])
def flightops_oauth_exchange():
    """Legacy iOS exchange; ownership continues to come from the PKCE state."""
    return _flightops_oauth_exchange_for()


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
    out = {'ok': True,
           'connected': bool(t.get('access')
                             and not t.get('needs_relogin')),
           'needs_relogin': bool(t.get('needs_relogin')),
           'scope': t.get('scope'),
           'configured': flightops_configured()}
    # Verbindungs-ALTER (seit 2026-07-28). Nur ausgeben, was wirklich
    # dasteht — Grants von VOR diesem Deploy haben keinen Zeitstempel, und
    # ein geschätztes Alter wäre eine erfundene Zahl. Fehlt das Feld, fehlt
    # die Zeile (Design-Regel: nie Daten erfinden).
    for k in ('connected_at', 'first_connected_at', 'relogin_at'):
        if t.get(k):
            out[k] = t[k]
    if t.get('reconnects'):
        out['reconnects'] = t['reconnects']
    if t.get('connected_at'):
        out['connected_days'] = round(
            (time.time() - float(t['connected_at'])) / 86400.0, 1)
    return jsonify(out)


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
        # FENSTER-ANFANG = MONATSANFANG, mindestens aber −7 Tage (Owner
        # 2026-07-30: „aktuellen Monat so rausgeben wie Lufthansa ihn hat,
        # da immer golden truth").
        #
        # Vorher stand hier hart −7 Tage. Am 30. eines Monats begann das
        # Fenster damit am 23. — der 1. bis 22. wurde NIE wieder abgeglichen.
        # Trägt LH rückwirkend etwas in den laufenden Monat ein (Krankmeldung,
        # gestrichener Umlauf, nachgetragene Ist-Zeiten), sah AeroX das nicht.
        #
        # Und es hängt am Reconcile: der zieht den Räum-Anfang ohnehin auf den
        # MONATSANFANG hoch und friert nur GANZ vergangene Monate ein. Solange
        # das Import-Fenster erst am 23. begann, lag der Monatsanfang außerhalb
        # der Feed-Spanne — es wurde dort also weder geschrieben noch geräumt.
        # Mit dem Monatsanfang als Fensterstart deckt sich beides, und der
        # laufende Monat wird zur exakten Kopie dessen, was LH hat: was LH
        # nicht mehr führt, verschwindet auch bei uns.
        #
        # KOSTET KEINEN EINZIGEN CALL EXTRA: duty_events nimmt eine Spanne,
        # das Fenster wird nur breiter, nicht die Anzahl der Abrufe.
        _month_start = today.replace(day=1)
        _minus7 = today - _td(days=7)
        fd = (body.get('from_date')
              or min(_month_start, _minus7).strftime('%Y-%m-%d'))
        td = body.get('to_date') or (today + _td(days=45)).strftime('%Y-%m-%d')
    # HISTORIEN-IMPORT (Owner 2026-07-30 „Ältere Daten laden"): der Knopf holt
    # abgeschlossene Monate nach. Zwei Zugaben, die nur für das LAUFENDE
    # Fenster Sinn ergeben, entfallen dabei bewusst — beide kosten LH-Calls:
    #   · Hotel-Pickup (COMMON_CREW_ROTATION) ist Horizont-gebunden (Stunden),
    #     für einen Monat aus der Vergangenheit also per Definition leer.
    #   · Crew-Prefetch wärmt die Crew-Listen der NÄCHSTEN Legs vor.
    # Der Roster-Import selbst bleibt identisch — nur die Zugaben schweigen.
    # (Gemessen 2026-07-30: LH liefert rund 6 Monate zurück, davor leer.)
    _history = bool(body.get('history'))
    # Interaktiv = alles, was NICHT der refresh-all-Hintergrundlauf ist
    # (der markiert sich via body.background) — Connect-Erstimport und
    # manuelles „Jetzt aktualisieren" bekommen die höhere Budget-Grenze,
    # damit die Re-Login-Heilung nie an Hintergrund-Syncs verhungert.
    resp = duty_events(token, fd, td,
                       interactive=not bool(body.get('background')))
    # FORENSIK (Tibor 2026-08-02: Fenster/History des zerstörenden Laufs war
    # aus den Logs nicht rekonstruierbar — alter Container weg, keine Zeile):
    # EIN INFO-Log pro Import mit Fenster, History-Flag und Antwort-Umfang.
    try:
        _n_days = len(_as_list((resp or {}).get('rosterDays')
                               or (resp or {}).get('days') or []))
        log.info('[lh_flightops] import tok=%s fenster=%s..%s history=%s '
                 'bg=%s tage=%s', token[:8], fd, td, _history,
                 bool(body.get('background')), _n_days)
    except Exception:
        pass
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
    _rot_facts = None
    if not _history:
        try:
            # `known_anchors` schaltet den Fern-Horizont (30→36 h) NUR für Legs frei,
            # für die noch kein Pickup bekannt ist — siehe _ROT_PICKUP_HORIZON_FAR_H.
            _rns = pickup_rotation_ids(
                resp, known_anchors=pickup_last_good_anchors(token))
            if _rns:
                # WELLE 0: dh/ac_change/hotel fallen in DERSELBEN Antwort ab —
                # kein zusätzlicher Call, kein erweiterter Horizont.
                _rot_facts = {}
                _pickups = rotation_pickups_for(token, _rns,
                                                facts_out=_rot_facts)
        except Exception as e:
            log.warning('[lh_flightops] pickup lookup: %s', type(e).__name__)
            _pickups = None
            _rot_facts = None
        # TZ-AUDIT (Welle 0, Phase 1): reine Telemetrie gegen unsere eigene
        # Stations-Tabelle. Keine Verhaltensänderung, max. 1 Zeile pro Station.
        try:
            tz_audit(resp)
        except Exception:
            pass
    ics = duty_events_to_ics(resp, pickups=_pickups, rot_legs=_rot_facts,
                             enrich=not _history)
    if not ics:
        return jsonify({'ok': True, 'events_count': 0, 'source': 'flightops',
                        'detail': 'no_events'}), 200
    # FALLBACK-EBENE (Owner 2026-07-27): fehlt der Pickup in der Primärquelle,
    # holt ihn der gespeicherte Kalender-Link (myTime) nach — pro Tag, nur wo
    # oben nichts stand. Siehe apply_ical_pickup_fallback. Nie eine Vorbedingung.
    # HISTORIE: beide Pickup-Ebenen bleiben aus. `apply_pickup_last_good`
    # schreibt seinen Stand ins Profil FORT — ein Monat aus der Vergangenheit
    # würde damit den Anker des laufenden Umlaufs überschreiben.
    if not _history:
        ics = apply_ical_pickup_fallback(token, ics, body.get('pickup_ical_url'))
        # NIE-LÖSCHEN-RIEGEL (Owner 2026-07-29): schweigen BEIDE Quellen, hängt der
        # zuletzt ausgelieferte Marker wieder dran — aber nur bei unverändertem
        # Anker-Leg. Schreibt danach den neuen Stand ins Profil fort (deploy-fest).
        ics = apply_pickup_last_good(token, ics)
    try:
        import app as _app
        # `source` sagt der Pipeline, WER hier hereinreicht — sonst landet
        # 'pdf' im gespeicherten calendar_feed und jede Diagnose liest die
        # falsche Quelle (Gotcha 2026-07-31). Die Korrektur unten am
        # `payload` betraf immer nur die HTTP-Antwort, nie das Profil.
        with _app.app.test_request_context(json={'ics_text': ics,
                                                 'source': 'flightops'}):
            rv = _app.import_calendar_feed(token)
        resp_obj, status = (rv if isinstance(rv, tuple) else (rv, 200))
        payload = resp_obj.get_json() or {}
    except Exception as e:
        log.warning('[lh_flightops] import pipeline fail: %s', type(e).__name__)
        return jsonify({'ok': False, 'error': 'pipeline_failed'}), 500
    if status == 200 and payload.get('ok'):
        payload['source'] = 'flightops'
        # „Crew-Liste: alles laden, wenn verbunden" (Owner 2026-07-28) — die
        # Crew-Listen der nächsten Legs im Hintergrund vorwärmen, damit der
        # Crew-Button nach dem Verbinden sofort (und offline) gefüllt ist.
        # Fire-and-forget mit eigenem, tieferem Budget-Deckel; siehe Banner
        # über _crew_prefetch_kick. Nie eine Vorbedingung für den Import.
        # Beim Historien-Import aus: die Crew-Liste eines abgeflogenen Monats
        # wärmt nichts vor, kostet aber pro Leg einen LH-Call.
        if not _history:
            _crew_prefetch_kick(token, resp)
    return jsonify(payload), status


def _me_flightops_owner():
    """Resolve the Android owner from Authorization, never a client field."""
    try:
        import app as _app
        token, error = _app._header_only_owner()
    except Exception:
        return None, (jsonify({'ok': False, 'error': 'auth_unavailable'}), 503)
    return token, error


def _me_flightops_no_query():
    if request.args:
        return jsonify({'ok': False, 'error': 'query_not_allowed'}), 400
    return None


def _me_flightops_valid_date(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        return False
    try:
        _dt.datetime.strptime(value, '%Y-%m-%d')
        return True
    except ValueError:
        return False


@lh_flightops_bp.route('/api/me/lh/flightops/status', methods=['GET'])
def me_flightops_status():
    """Header-authenticated FlightOps grant status for Android."""
    error = _me_flightops_no_query()
    if error is not None:
        return error
    token, error = _me_flightops_owner()
    if error is not None:
        return error
    return flightops_status(token)


@lh_flightops_bp.route('/api/me/lh/flightops/oauth/start', methods=['GET'])
def me_flightops_oauth_start():
    """Start an owner-bound PKCE flow without putting credentials in the URL."""
    error = _me_flightops_no_query()
    if error is not None:
        return error
    token, error = _me_flightops_owner()
    if error is not None:
        return error
    return _flightops_oauth_start_for(token)


@lh_flightops_bp.route('/api/me/lh/flightops/oauth/exchange', methods=['POST'])
def me_flightops_oauth_exchange():
    """Exchange only a PKCE state created by this Authorization owner."""
    error = _me_flightops_no_query()
    if error is not None:
        return error
    body = request.get_json(silent=True)
    if (not isinstance(body, dict) or set(body) != {'code', 'state'} or
            not all(isinstance(body.get(key), str) and body[key].strip()
                    for key in ('code', 'state'))):
        return jsonify({'ok': False, 'error': 'invalid_body'}), 400
    token, error = _me_flightops_owner()
    if error is not None:
        return error
    return _flightops_oauth_exchange_for(expected_owner=token)


@lh_flightops_bp.route('/api/me/lh/flightops/import', methods=['POST'])
def me_flightops_import():
    """Import the authenticated owner's official LH roster in a bounded window."""
    error = _me_flightops_no_query()
    if error is not None:
        return error
    body = request.get_json(silent=True)
    if body is None and not request.data:
        body = {}
    if (not isinstance(body, dict) or
            not set(body).issubset({'from_date', 'to_date'}) or
            any(not _me_flightops_valid_date(value)
                for value in body.values()) or
            ('from_date' in body and 'to_date' in body and
             body['from_date'] > body['to_date'])):
        return jsonify({'ok': False, 'error': 'invalid_body'}), 400
    token, error = _me_flightops_owner()
    if error is not None:
        return error
    return flightops_import(token)


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
# SPEICHERORT seit 2026-07-28: eigene Supabase-Tabelle `flightops_crew_cache`
# statt des Profil-Mirrors. Grund — Owner-Befund „der Crew-Cache funktioniert
# nicht", live am Owner-Token vermessen:
#
#   (c) NICHT die Durabilität. Der Profil-Mirror liegt in Supabase; Einträge
#       vom 24.07. haben die DREI Deploys des 27./28.07. überlebt. („Cache
#       liegt ephemer im Container" war der Verdacht — er ist widerlegt.)
#   (a) Das SCHREIBEN klappt ebenfalls: ein Live-Abruf LH454/28.07. landete
#       binnen Sekunden im Cache.
#   (b) DIE URSACHE ist der Deckel: `_CREW_CACHE_MAX = 8` Legs, verdrängt in
#       EINFÜGE-Reihenfolge. Genau jener Abruf schob den RÜCKFLUG
#       LH455/30.07. aus dem Cache — ein frisch angesehenes Leg löscht ein
#       künftiges. Bei ~10 angetippten Legs im Monat ist der Miss garantiert,
#       und ein Miss heißt für den User 401/404/502 mit leerer Fläche.
#       Zusätzlich war der Key auf das Datum EXAKT — Red-Eyes und der
#       Z-vs-Lokalzeit-Rollover fielen daneben.
#
# Warum nicht einfach höher deckeln: ein Eintrag wiegt ~1,4 KB, und der
# Profil-Blob wird auf nahezu JEDEM Request gelesen (_profile_load). 60 Legs
# wären +85 KB pro Request für alle Endpoints — der Cache hätte die App
# verlangsamt, statt sie zu retten. Die eigene Tabelle wird NUR im
# Crew-Endpoint gelesen, deckelt nach Datum statt nach Anzahl und ist damit
# faktisch unbegrenzt.
#
# Fehlt die Tabelle (Migration supabase_migrations/20260728_flightops_crew_cache.sql
# noch nicht angewandt) oder ist SB weg, degradiert alles auf den alten
# Profil-Cache — kein Hard-Fail, keine Client-Änderung nötig.
_CREW_CACHE_TABLE = 'flightops_crew_cache'
# Deckel des LEGACY-Profil-Fallbacks. Bleibt klein — der Blob ist heiß.
_CREW_CACHE_MAX = 8
# Tabellen-Deckel: Legs, deren Flugdatum länger als das her ist, fliegen raus.
_CREW_CACHE_KEEP_DAYS = 120
# Pro Token höchstens stündlich räumen (ein DELETE je Schreibserie reicht).
_CREW_CACHE_PRUNE_EVERY_S = 3600.0
# Datums-Toleranz beim LESEN: Roster-Datum (LT) vs. LH-Flugdatum (Z) können
# bei Red-Eyes um einen Tag auseinanderliegen.
_CREW_CACHE_DATE_SLACK = (0, -1, 1)
# Simulator-Sessions haben keine Flugnummer. Unter diesem reservierten Key
# nutzt die SIM-Crew denselben privaten, RLS-geschuetzten Last-Good-Speicher
# wie Flug-Crewlisten. Der Payload im jsonb-Feld `crew` ist fuer diesen Key
# ein Objekt {members,session}; normale Flug-Keys bleiben unveraendert Listen.
_SIM_CREW_CACHE_FLIGHT = '__SIMULATOR__'

# (letzter Fehlversuch, Tabelle nutzbar?) — nach einem Fehler 5 min Ruhe,
# damit ein fehlendes Table/PostgREST-404 nicht jeden Request bezahlt.
_crew_tbl_state = [0.0, True]
_CREW_TBL_RETRY_S = 300.0
_crew_prune_seen = {}            # token → ts des letzten Prunes


def _crew_tbl_ok():
    """True wenn die Cache-Tabelle gerade als nutzbar gilt."""
    try:
        import app as _app
        if not getattr(_app, 'SB_AVAILABLE', False):
            return False
    except Exception:
        return False
    if (not _crew_tbl_state[1]
            and (time.time() - _crew_tbl_state[0]) < _CREW_TBL_RETRY_S):
        return False
    return True


def _crew_tbl_fail(exc):
    _crew_tbl_state[0], _crew_tbl_state[1] = time.time(), False
    log.warning('[lh_flightops] crew_cache-Tabelle nicht nutzbar (%s) — '
                'Profil-Fallback bis zur Migration', type(exc).__name__)


def _crew_key(flight, date):
    """(normalisierte Flugnummer, YYYY-MM-DD)."""
    return (str(flight or '').upper().replace(' ', ''), str(date or '')[:10])


def _crew_date_candidates(date):
    """Exaktes Datum zuerst, dann ±1 Tag (Red-Eye / Z-vs-LT-Rollover)."""
    d = str(date or '')[:10]
    out = [d]
    try:
        base = _dt.date.fromisoformat(d)
        for off in _CREW_CACHE_DATE_SLACK[1:]:
            out.append((base + _dt.timedelta(days=off)).isoformat())
    except Exception:
        pass
    return out


def _crew_cache_get_sb(token, flight, date):
    """Tabellen-Lesepfad. None bei Miss/nicht verfügbar."""
    if not (token and _crew_tbl_ok()):
        return None
    f, _d = _crew_key(flight, date)
    if not f:
        return None
    cands = _crew_date_candidates(date)
    try:
        import app as _app
        r = (_app.sb.table(_CREW_CACHE_TABLE)
             .select('flight,flight_date,crew,cached_at')
             .eq('token', token).eq('flight', f)
             .in_('flight_date', cands).limit(len(cands)).execute())
        _crew_tbl_state[1] = True
        rows = getattr(r, 'data', None) or []
    except Exception as e:
        _crew_tbl_fail(e)
        return None
    by_date = {str(x.get('flight_date') or '')[:10]: x for x in rows}
    for cand in cands:              # exaktes Datum schlägt die Toleranz
        row = by_date.get(cand)
        if row and row.get('crew'):
            return {'flight': row.get('flight'), 'date': cand,
                    'crew': row['crew'], 'cached_at': row.get('cached_at')}
    return None


def _crew_cache_put_sb(token, flight, date, crew):
    """Tabellen-Schreibpfad. False wenn die Tabelle nicht nutzbar ist."""
    f, d = _crew_key(flight, date)
    if not (token and f and d and crew and _crew_tbl_ok()):
        return False
    try:
        import app as _app
        (_app.sb.table(_CREW_CACHE_TABLE).upsert(
            {'token': token, 'flight': f, 'flight_date': d,
             'crew': crew, 'cached_at': time.time()},
            on_conflict='token,flight,flight_date').execute())
        _crew_tbl_state[1] = True
    except Exception as e:
        _crew_tbl_fail(e)
        return False
    _crew_cache_prune(token)
    return True


def _crew_cache_prune(token):
    """Alte Legs räumen (best-effort, höchstens stündlich pro Token)."""
    now = time.time()
    if (now - _crew_prune_seen.get(token, 0.0)) < _CREW_CACHE_PRUNE_EVERY_S:
        return
    _crew_prune_seen[token] = now
    if len(_crew_prune_seen) > 4000:
        for k in sorted(_crew_prune_seen, key=_crew_prune_seen.get)[:2000]:
            _crew_prune_seen.pop(k, None)
    try:
        import app as _app
        cutoff = (_dt.date.today()
                  - _dt.timedelta(days=_CREW_CACHE_KEEP_DAYS)).isoformat()
        (_app.sb.table(_CREW_CACHE_TABLE).delete()
         .eq('token', token).lt('flight_date', cutoff).execute())
    except Exception:
        pass


# ── Legacy-Profil-Cache (Fallback + Alt-Bestand) ────────────────────────────
def _crew_cache_legacy_entries(token):
    try:
        import app as _app
        prof = ((_app._profile_load(token) or {}).get('profile') or {})
        lst = prof.get('flightops_crew_cache')
        return lst if isinstance(lst, list) else []
    except Exception:
        return []


def _crew_cache_put_profile(token, flight, date, crew):
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


def _crew_cache_migrate_profile(token, entries):
    """Alt-Bestand EINMALIG in die Tabelle heben und den Profil-Key leeren.
    Der Key wiegt ~1,4 KB pro Leg in einem Blob, der auf fast jedem Request
    gelesen wird — nach der Migration ist der Hot-Path wieder schlank.
    Geleert statt gelöscht: der SB-Profil-Save ist ein `||`-Merge, ein
    entfernter Key würde in der DB stehen bleiben."""
    if not entries:
        return
    for e in entries:
        if isinstance(e, dict) and e.get('crew'):
            if not _crew_cache_put_sb(token, e.get('flight'), e.get('date'),
                                      e['crew']):
                return          # Tabelle weg → Alt-Bestand NICHT anfassen
    try:
        import app as _app
        pf = _app._profile_load(token) or {}
        prof = (pf.get('profile') or {})
        prof['flightops_crew_cache'] = []
        _app._profile_save(token, prof)
    except Exception as e:
        log.warning('[lh_flightops] crew_cache_migrate: %s', type(e).__name__)


def _crew_cache_get(token, flight, date):
    """Last-Good-Liste für ein Leg — Tabelle zuerst, Legacy-Profil als
    Fallback (inkl. einmaliger Migration)."""
    hit = _crew_cache_get_sb(token, flight, date)
    if hit:
        return hit
    entries = _crew_cache_legacy_entries(token)
    if not entries:
        return None
    f, _d = _crew_key(flight, date)
    out = None
    for cand in _crew_date_candidates(date):
        for e in entries:
            if (isinstance(e, dict) and e.get('crew')
                    and str(e.get('flight') or '').upper().replace(' ', '') == f
                    and str(e.get('date') or '')[:10] == cand):
                out = e
                break
        if out:
            break
    if _crew_tbl_ok():
        _crew_cache_migrate_profile(token, entries)
    return out


def _crew_cache_put(token, flight, date, crew):
    if not crew:
        return
    if _crew_cache_put_sb(token, flight, date, crew):
        return
    _crew_cache_put_profile(token, flight, date, crew)


def _sim_crew_cache_get(token, date):
    """Exakter Last-Good-Treffer fuer eine Simulator-Crew.

    Anders als Flug-Crewlisten darf ein SIM nie mit dem Nachbartag bedient
    werden: zwei Sessions an aufeinanderfolgenden Tagen sind fachlich nicht
    austauschbar. `_crew_cache_get` darf wegen Red-Eyes ±1 Tag liefern; dieser
    Wrapper verwirft deshalb jeden nicht exakt datierten Treffer.
    """
    wanted = str(date or '')[:10]
    e = _crew_cache_get(token, _SIM_CREW_CACHE_FLIGHT, wanted)
    if not e or str(e.get('date') or '')[:10] != wanted:
        return None
    payload = e.get('crew')
    if isinstance(payload, dict):
        members = payload.get('members')
        session = payload.get('session')
    else:
        # Vorwaertskompatibler Notnagel, falls waehrend eines gestaffelten
        # Deploys bereits eine reine Liste geschrieben worden sein sollte.
        members, session = payload, {}
    if not isinstance(members, list) or not members:
        return None
    return {'crew': members,
            'session': session if isinstance(session, dict) else {},
            'cached_at': e.get('cached_at'), 'date': wanted}


def _sim_crew_cache_put(token, date, crew, session):
    """Persistiert die rohe LH-SIM-Liste samt Session-Kopf als Last-Good."""
    if not isinstance(crew, list) or not crew:
        return
    _crew_cache_put(token, _SIM_CREW_CACHE_FLIGHT, date, {
        'members': crew,
        'session': session if isinstance(session, dict) else {},
    })


# ── GETEILTER Cache über User-Grenzen hinweg ────────────────────────────────
# Owner 2026-07-28: „Crew mit dem gleichen Flug hat die Liste im Backend."
# COMMON_CREWLIST ist FLUG-bezogen, nicht personenbezogen: die Antwort hängt
# ausschliesslich an flightDesignator/flightDate/dep/arr — der accessCode ist
# reine BERECHTIGUNG, kein Filter. Die Liste für LH454/2026-07-28/FRA-SFO ist
# für jedes Mitglied dieselbe. Also darf der Tap von Kollege B mit der Zeile
# bedient werden, die Kollege A (oder dessen Prefetch) geschrieben hat — ohne
# EINEN LH-Call.
#
# BERECHTIGUNG (Sicherheits-Kern, nicht wegoptimieren): der accessCode ist der
# Beweis „dieses Leg steht in MEINEM Roster". Ohne ihn dürfte sonst jeder
# AeroX-User mit geratenem {flight,date} die Klarnamen einer fremden Crew
# abrufen — die Tabelle ist PII. Der Shortcut verlangt deshalb einen von zwei
# Nachweisen, die BEIDE ohne LH-Call auskommen:
#   (1) eine EIGENE Cache-Zeile für dieses Leg — die entsteht nur nach einem
#       accessCode-geprüften Abruf bzw. dem eigenen Prefetch, oder
#   (2) ein Treffer im lokalen Link-Cache (_links_load, gefüllt aus den EIGENEN
#       Duty-Events).
# Fehlt beides, läuft der Request unverändert den alten Weg (Link auflösen →
# LH). Der Shortcut verschenkt dann nichts, er greift nur nicht.
#
# FRISCHE — nach SCHADEN gestaffelt, nicht nach Volatilität. Was der Code
# WIRKLICH weiss, ist das Flugdatum (keine Abflugzeit ohne Extra-Call), also
# staffeln wir daran:
#   • Flugtag (== heute): 45 min. Hier ist die Liste operativ relevant (man
#     trifft diese Leute gleich) und genau hier passieren die Last-Minute-
#     Wechsel (Krankmeldung, Reserve rückt nach). 45 min begrenzt den Irrtum
#     und amortisiert trotzdem: eine 12-köpfige Crew, die im Briefing-Fenster
#     nachschaut, kostet EINEN Call statt zwölf.
#   • Künftiger Tag: 6 h. Die Liste ist dort provisorisch und ändert sich öfter
#     — aber niemand HANDELT danach, es ist Vorfreude-Blättern. Bewusst LÄNGER
#     als am Flugtag: die Staffel folgt dem Schaden einer falschen Zeile, nicht
#     ihrer Wahrscheinlichkeit. Wer es exakt braucht, bekommt es am Flugtag.
#   • Vergangener Tag: 30 Tage. Der Flug ist gelaufen, die Liste ist
#     Geschichte — LH RÄUMT sie nach dem Flug sogar weg, die gecachte Zeile ist
#     dann die beste existierende Wahrheit („mit wem war ich unterwegs").
# `force:true` im Body übergeht den Shortcut (Pull-to-Refresh) — der Weg
# darunter ist weiterhin budget-gegatet.
_CREW_SHARED_TTL_TODAY_S = 45 * 60
_CREW_SHARED_TTL_FUTURE_S = 6 * 3600
_CREW_SHARED_TTL_PAST_S = 30 * 86400
# Wieviele Zeilen der flug-weite Select höchstens zieht (3 Datums-Kandidaten ×
# Crew-Grösse; 60 deckt auch einen A380 mit vielen verbundenen Usern).
_CREW_SHARED_SCAN_LIMIT = 60


def _crew_shared_ttl_s(flight_date, today=None):
    """Erlaubtes Cache-Alter für dieses Flugdatum (Begründung im Banner)."""
    d = str(flight_date or '')[:10]
    today = today or _dt.date.today()
    try:
        fd = _dt.date.fromisoformat(d)
    except Exception:
        return _CREW_SHARED_TTL_TODAY_S      # unklar ⇒ strengste Regel
    if fd < today:
        return _CREW_SHARED_TTL_PAST_S
    if fd == today:
        return _CREW_SHARED_TTL_TODAY_S
    return _CREW_SHARED_TTL_FUTURE_S


def _crew_shared_fresh(flight_date, cached_at, now=None, today=None):
    """Darf diese Zeile OHNE LH-Call ausgeliefert werden? Pure/testbar."""
    try:
        ts = float(cached_at or 0)
    except Exception:
        return False
    if ts <= 0:
        return False
    return ((now or time.time()) - ts) < _crew_shared_ttl_s(flight_date, today)


def _crew_cache_scan(flight, date):
    """ALLE Cache-Zeilen (jeder User) zu diesem Leg — [] bei Miss/ohne Tabelle.

    Der Select filtert NUR auf flight + flight_date, nicht auf den Token. Dafür
    gibt es den Index aus supabase_migrations/20260729_crew_cache_shared.sql;
    fehlt die Migration, liefert derselbe Select dasselbe Ergebnis, nur über
    einen Seq-Scan — langsamer, nie falsch. Degradiert wie der Rest des Caches
    lautlos auf [] (dann bleibt es beim alten Verhalten: LH-Call)."""
    if not _crew_tbl_ok():
        return []
    f, _d = _crew_key(flight, date)
    if not f:
        return []
    cands = _crew_date_candidates(date)
    try:
        import app as _app
        r = (_app.sb.table(_CREW_CACHE_TABLE)
             .select('token,flight,flight_date,crew,cached_at')
             .eq('flight', f).in_('flight_date', cands)
             .limit(_CREW_SHARED_SCAN_LIMIT).execute())
        _crew_tbl_state[1] = True
        rows = getattr(r, 'data', None) or []
    except Exception as e:
        _crew_tbl_fail(e)
        return []
    return [x for x in rows if isinstance(x, dict) and x.get('crew')]


def _crew_pick_best(rows, date):
    """Beste Zeile aus dem Scan: exaktes Flugdatum schlägt die ±1-Tag-Toleranz
    (wie im Einzel-Getter), innerhalb desselben Datums gewinnt die JÜNGSTE
    Zeile — egal von welchem User sie stammt. Pure/testbar."""
    for cand in _crew_date_candidates(date):
        same = [r for r in rows or []
                if str(r.get('flight_date') or '')[:10] == cand]
        if not same:
            continue

        def _ts(r):
            try:
                return float(r.get('cached_at') or 0)
            except Exception:
                return 0.0
        return sorted(same, key=_ts)[-1]
    return None


def _crew_reenrich(crew, flight=None, date=None):
    """Gecachte Liste → Kopie mit FRISCH gematchten AeroX-Profilen.

    VERIFIZIERT 2026-07-28: `_match_aerox_profiles` ist VIEWER-UNABHÄNGIG — sie
    nimmt nur die Crew-Mitglieder, sucht über lh_pk_number/Name in
    user_profiles und gibt ausschliesslich die Public-Shape zurück
    (token/name/airline/homebase/position/avatar_url, Family-Accounts nie).
    Kein Viewer-Token, kein Freundes-/Sichtbarkeits-Filter. In einer geteilten
    Zeile steckt also NICHTS Viewer-Spezifisches.
    Trotzdem wird hier neu gematcht, aus zwei Gründen:
      (a) Beleg-und-Gürtel: würde die Verknüpfung je viewer-abhängig (z. B.
          „nur Freunde zeigen"), servierte der geteilte Cache sonst fremde
          Sicht — dieser Re-Match verhindert das strukturell.
      (b) Frische: wer nach dem Cache-Schreiben zu AeroX gekommen ist,
          erscheint sofort. Kostet nur Supabase-Reads, KEINEN LH-Call.

    `flight`/`date` schalten den Roster-Beweis frei (s. `_match_aerox_profiles`).
    Ohne sie bleibt es beim pk-/Namens-Beweis — nie wird geraten."""
    out = []
    for m in crew or []:
        out.append({k: v for k, v in m.items() if k != 'aerox'}
                   if isinstance(m, dict) else m)
    try:
        matches = _match_aerox_profiles(
            [m for m in out if isinstance(m, dict)], flight=flight, date=date)
    except Exception:
        matches = {}
    for m in out:
        if not isinstance(m, dict):
            continue
        p = matches.get(str(m.get('pk') or m.get('name') or ''))
        if p:
            m['aerox'] = p
    return out


def _crew_shared_serve(token, flight, date, dep=None, arr=None, now=None):
    """Flask-Antwort aus dem geteilten Cache — oder None (⇒ normaler LH-Weg).
    Wirft nie: jeder Fehler bedeutet „kein Shortcut", nicht „kein Crew"."""
    try:
        rows = _crew_cache_scan(flight, date)
        if not rows:
            return None
        own = any(str(r.get('token') or '') == token for r in rows)
        if not own and not _links_find(_links_load(token), 'crewlist',
                                       flight, date, dep, arr):
            return None                  # kein Berechtigungs-Nachweis
        best = _crew_pick_best(rows, date)
        if not best or not _crew_shared_fresh(best.get('flight_date'),
                                              best.get('cached_at'), now):
            return None
        crew = _crew_reenrich(best.get('crew') or [], flight=flight, date=date)
        if not crew:
            return None
        shared = str(best.get('token') or '') != token
        if shared:
            log.info('[lh_flightops] crewlist %s/%s aus GETEILTEM Cache '
                     '(kein LH-Call)', flight, str(date or '')[:10])
        served = str(best.get('flight_date') or '')[:10]
        if served and served != str(date or '')[:10]:
            # NACHBARTAG bedient (±1-Slack). Auf WARNING, nicht INFO: bis
            # 2026-08-09 nannte die Antwort das bediente Datum NICHT, der
            # Client schrieb sie als Historie DIESES Legs fest und
            # `replacingCrew` löschte dabei die echte Besetzung des Tages.
            # Messung 2026-08-09 (02:30 UTC, ganze `flightops_crew_cache`,
            # 6.484 Zeilen): von 1.218 Nachbartags-Paaren derselben Flugnummer
            # hatten nur 5 (0,4 %) dieselbe Crew, Median-Namensüberlappung
            # 0,000 — die Nachbartags-Liste ist praktisch IMMER eine fremde
            # Besetzung. Diese Zeile macht die Trefferquote ab jetzt messbar.
            log.warning('[lh_flightops] crewlist %s: Nachbartag bedient '
                        '(angefragt %s, geliefert %s) — Client entscheidet '
                        'per flight_date', flight, str(date or '')[:10], served)
        return jsonify({'ok': True, 'crew': crew, 'cached': True,
                        'shared': shared, 'cached_at': best.get('cached_at'),
                        # ADDITIV seit 2026-08-09 — siehe Banner am Endpoint.
                        'flight_date': served or str(date or '')[:10]})
    except Exception as e:
        log.warning('[lh_flightops] shared_serve: %s', type(e).__name__)
        return None


# Cache-only batch lookup for a complete roster month.  This endpoint must
# never call COMMON_CREWLIST: its purpose is to fan IN the durable/shared
# cache with one app request, not to reintroduce the old month-wide LH fanout.
_CREW_CACHE_BATCH_MAX_LEGS = 80


def _crew_cache_batch_hits(token, legs, now=None):
    """Return authorised, fresh cache hits for up to one roster month.

    A shared cache row is PII.  It is returned only when the requesting user's
    own Duty-Events links prove that the exact leg belongs to their roster, or
    when the row itself belongs to that user.  No LH service is called here.
    """
    links = _links_load(token)
    out, seen = [], set()
    for raw in (legs or [])[:_CREW_CACHE_BATCH_MAX_LEGS]:
        if not isinstance(raw, dict):
            continue
        flight = str(raw.get('flight') or '').upper().replace(' ', '')
        date = str(raw.get('date') or '')[:10]
        dep = str(raw.get('dep') or '').upper().strip()
        arr = str(raw.get('arr') or '').upper().strip()
        key = (flight, date, dep, arr)
        if not flight or len(date) != 10 or key in seen:
            continue
        seen.add(key)

        rows = _crew_cache_scan(flight, date)
        if not rows:
            continue
        own = any(str(row.get('token') or '') == token for row in rows)
        if not own and not _links_find(links, 'crewlist', flight, date, dep, arr):
            continue
        best = _crew_pick_best(rows, date)
        if not best or not _crew_shared_fresh(best.get('flight_date'),
                                              best.get('cached_at'), now):
            continue
        crew = best.get('crew') or []
        if not crew:
            continue
        # Wie ALLE Einzel-Serve-Pfade (5254/5635/5719): AeroX-Profile FRISCH
        # matchen statt die rohe Cache-Zeile durchzureichen. Ohne das blieb die
        # Monats-Historie im Client unvervollständigt (Tester 12.08.: „kollegen
        # von aero x hier nicht vervollständigt") — der Batch war der einzige
        # Pfad ohne Re-Match. Kostet keinen LH-Call, nur Supabase-Reads.
        crew = _crew_reenrich(crew, flight=flight, date=date)
        out.append({
            'flight': flight, 'date': date, 'dep': dep, 'arr': arr,
            'flight_date': str(best.get('flight_date') or '')[:10] or date,
            'crew': crew, 'cached_at': best.get('cached_at'),
            'shared': str(best.get('token') or '') != token,
        })
    return out


# ── PREFETCH: „alles laden, wenn verbunden" (Owner 2026-07-28) ──────────────
# Nach jedem erfolgreichen Roster-Import werden die Crew-Listen der nächsten
# Legs im Hintergrund vorgewärmt, damit der Crew-Button sofort (und offline)
# gefüllt ist. Quelle sind die crewlist-_links, die in der Duty-Events-Response
# OHNEHIN schon stecken (accessCode/dep/arr fertig) — der Prefetch kostet also
# KEINEN Duty-Events-Call extra, nur den COMMON_CREWLIST je Leg.
#
# QUOTA-RECHNUNG (Key laut LH 10.08.2026: 20.000/h · 20/s · kein Tageslimit):
#   • ~130 verbundene User. Bestandslast heute: refresh-all alle 2 h mit
#     Kadenz-Gate (3,5 h / 11,5 h) ⇒ ~4 Duty-Events-Calls pro User und Tag
#     (~520) + Rotations-Pickups (~300–400) + interaktive Flows.
#   • Prefetch-Deckel (Stand 29.07.): 8 Legs pro Lauf, Horizont 3 Tage, und ein
#     Leg wird nur angefasst, wenn KEINE Zeile (egal welcher User) jünger als
#     20 h ist. Ein Leg kostet damit ≤ 1 Call/20 h — für die ganze Crew
#     zusammen, sobald zwei AeroX-User denselben Flug haben.
#   • Worst Case: 141 User × 8 Legs × 1×/20 h ≈ 1.350 Calls/Tag; GEMESSEN sind
#     es 247 Legs im 3-Tage-Fenster ⇒ ≈ 300 Calls/Tag (Herleitung unten).
#   • Gegenrechnung: der geteilte Cache SPART interaktive Calls (jeder Tap
#     innerhalb der TTL ist gratis, statt 1–2 Calls). Netto ist das Paket
#     quotenneutraler, als der Prefetch allein aussieht.
# NOTBREMSE: Prefetch hört als ERSTES auf — eigene, tiefere Deckel (550/h,
# 3.800/Tag) ÜBER dem regulären Hintergrund-Gate in _api_get. Der Roster darf
# nie an einer Zugabe verhungern (gleiche Logik wie _ROT_LHFO_HOUR_CEILING).
#
# ── HORIZONT-KORREKTUR 2026-07-29 (Monats-Prefetch riss das Tagesbudget) ────
# GEMESSEN, nicht geschätzt:
#   • ax_api_budget 28.07. → 29.07., common_crewlist: 92 → 2.876 Calls/Tag
#     (Faktor 31) — exakt seit dem Monats-Prefetch (Horizont 31 Tage/48 Legs).
#     Am 29.07. 12:50 UTC riss der Tages-Deckel (lhfoD 5.303 ≥ 5.000) und ALLE
#     Hintergrund-Calls aller User starben.
#   • RECHNUNG (Tabelle flightops_crew_cache, 29.07. gezählt): im 32-Tage-
#     Fenster liegen 2.281 Leg-Zeilen (≈71/Tag über die ganze Nutzerbasis).
#     Jedes Leg wird durch _CREW_PREFETCH_MIN_AGE_S alle 20 h EINMAL geholt
#     ⇒ 2.281 × 24/20 ≈ 2.740 Calls/Tag. Gemessen: 2.876. Die Rechnung geht
#     auf — der Monats-Horizont IST die Ursache, kein anderer Pfad.
# NEUER HORIZONT: 3 Tage (heute + 2). Begründung, in dieser Reihenfolge:
#   (a) FRISCHE: ein Roster ändert sich. Eine Crew-Liste für ein Leg in drei
#       Wochen ist bis dahin meist veraltet — sie wäre teuer UND falsch.
#   (b) HEBEL: der Cache ist GETEILT (_crew_cache_scan über alle User). Der
#       Spar-Effekt entsteht dort, wo VIELE gleichzeitig dasselbe Leg öffnen —
#       also bei den nahen Legs (heutiger/nächster Dienst), nicht bei fernen.
#       Messung 29.07.: pro Datum ≈ so viele DISTINCT flights wie Zeilen
#       (84 Zeilen/84 Flüge heute) ⇒ ferne Legs teilen praktisch niemanden,
#       jeder Fern-Prefetch ist ein Voll-Call für genau einen Menschen.
#   (c) NUTZUNG: angetippt wird der heutige/nächste Dienst. Alles Fernere lädt
#       beim Antippen nach (flightops_crewlist, interactive=True, schnell).
#   Kosten im 3-Tage-Fenster (gemessen): 247 Legs über alle User
#     ⇒ 247 × 24/20 ≈ 296 Calls/Tag statt 2.876 (−~2.580/Tag).
#     Zum Vergleich: 4 Tage = 317 Legs (≈380/Tag), 7 Tage = 557 (≈670/Tag) —
#     das Ziel „deutlich unter 500" hält nur der 3-Tage-Horizont mit Luft.
# HARTES LEG-BUDGET pro User und Lauf: 8. Verteilung im 3-Tage-Fenster
# (gemessen, 95 User mit Legs): Median 1, p90 7, Maximum 11. 8 lässt also ~95%
# der User vollständig durch und deckelt nur den Kurzstrecken-Tail; der Rest
# bleibt dem On-Demand-Pfad. Gekappt wird SICHTBAR (Log in
# _crew_prefetch_legs) — stilles Abschneiden ist genau das, was uns in den
# Monats-Prefetch hineingeritten hat.
_CREW_PREFETCH_DAYS = 3
_CREW_PREFETCH_MAX_LEGS = 8
_CREW_PREFETCH_MIN_AGE_S = 20 * 3600
# LEER-Legs: LH füllt die Crew-Liste erst kurz vor Abflug. Eine leere Antwort
# erzeugt KEINE Cache-Zeile (_crew_cache_put steigt bei leerer Liste aus, und
# _crew_cache_scan filtert Zeilen ohne crew weg) — der 20-h-Amortisierer greift
# dort also NICHT, und dasselbe leere Leg würde bei jedem Kick neu geholt.
# Deshalb ein prozess-lokaler Leer-Marker: der Hintergrund-Prefetch lebt im
# Poll-Container, ein Prozess-Gedächtnis reicht für genau diesen Pfad. Der
# INTERAKTIVE Tap ist davon unberührt (er läuft nie durch diese Funktion).
_CREW_PREFETCH_EMPTY_TTL_S = 6 * 3600
_CREW_PREFETCH_EMPTY_MAX = 4000
_CREW_PREFETCH_HOUR_CEILING = 550
_CREW_PREFETCH_DAY_CEILING = 3800
# Pro Token höchstens alle 6 h vorwärmen (refresh-all läuft alle 2 h und
# importiert denselben User mehrfach am Tag — der Prefetch muss nicht mit).
_CREW_PREFETCH_COOLDOWN_S = 6 * 3600
# Gleichzeitige Prefetch-Threads im Prozess (refresh-all iteriert 130 User;
# ohne Deckel stapeln sich die Threads).
_CREW_PREFETCH_MAX_THREADS = 2
# QPS-Schonung zwischen zwei Legs (wie im refresh-all-Loop).
_CREW_PREFETCH_SLEEP_S = 0.7
_crew_prefetch_seen = {}                 # token → ts des letzten Prefetch-Starts
_crew_prefetch_lock = threading.Lock()
_crew_prefetch_active = [0]
_crew_prefetch_empty = {}                # (flight, date) → ts der LEER-Antwort


def _crew_prefetch_empty_recent(flight, date, now=None):
    """True, wenn LH für dieses Leg zuletzt eine LEERE Crew-Liste geliefert hat
    (innerhalb _CREW_PREFETCH_EMPTY_TTL_S). Siehe Banner: leere Antworten
    hinterlassen KEINE Cache-Zeile, der 20-h-Amortisierer greift dort nicht.
    Pure genug für Tests; wirft nie."""
    now = now or time.time()
    with _crew_prefetch_lock:
        ts = _crew_prefetch_empty.get((str(flight or ''), str(date or '')[:10]))
    return bool(ts and 0 <= (now - ts) < _CREW_PREFETCH_EMPTY_TTL_S)


def _crew_prefetch_empty_mark(flight, date, now=None):
    """Leer-Antwort merken (prozess-lokal, beschränkt)."""
    now = now or time.time()
    with _crew_prefetch_lock:
        _crew_prefetch_empty[(str(flight or ''), str(date or '')[:10])] = now
        if len(_crew_prefetch_empty) > _CREW_PREFETCH_EMPTY_MAX:
            for k in sorted(_crew_prefetch_empty,
                            key=_crew_prefetch_empty.get)[
                                :_CREW_PREFETCH_EMPTY_MAX // 2]:
                _crew_prefetch_empty.pop(k, None)


def _crew_prefetch_legs(resp, today=None):
    """Duty-Events-Response → Legs, deren Crew-Liste vorgewärmt werden soll
    (nächste _CREW_PREFETCH_DAYS Tage, dedupliziert, früheste zuerst, gekappt).
    Pure/testbar."""
    t = str(today or _dt.date.today().isoformat())[:10]
    try:
        # _CREW_PREFETCH_DAYS zählt KALENDERTAGE INKLUSIVE heute (3 = heute + 2
        # Tage). Die Grenze unten ist inklusiv, deshalb −1 — vorher deckte
        # „31" faktisch 32 Tage ab, und genau diese stille Zugabe zahlt man am
        # Tagesbudget.
        hi = (_dt.date.fromisoformat(t)
              + _dt.timedelta(days=max(1, _CREW_PREFETCH_DAYS) - 1)).isoformat()
    except Exception:
        return []
    out, seen = [], set()
    for l in extract_duty_links(resp) or []:
        if not isinstance(l, dict) or l.get('service') != 'crewlist':
            continue
        p = l.get('params') or {}
        f = (p.get('flightDesignator') or '').upper().replace(' ', '')
        d = str(p.get('flightDate') or '')[:10]
        acc = (p.get('accessCode') or '').strip()
        if not (f and len(d) == 10 and acc and t <= d <= hi):
            continue
        if (f, d) in seen:
            continue
        seen.add((f, d))
        out.append({'flight': f, 'date': d,
                    'dep': (p.get('departureAirport') or '').upper(),
                    'arr': (p.get('arrivalAirport') or '').upper(),
                    'access': acc})
    # ZEITLICH NÄCHSTE ZUERST: der Deckel darf nur den fernen Rest abschneiden,
    # nie den Dienst von heute. Feinere Sortierung als das Flugdatum gibt es
    # hier nicht — die crewlist-_links tragen nur flightDate (keine Uhrzeit);
    # innerhalb eines Tages entscheidet daher die Flugnummer (stabil/deterministisch).
    out.sort(key=lambda x: (x['date'], x['flight']))
    if len(out) > _CREW_PREFETCH_MAX_LEGS:
        # SICHTBAR kappen (Lehre 29.07.): der Monats-Prefetch schnitt still ab
        # und niemand sah, dass ein Vielflieger 48 Calls pro Lauf auslöste.
        # Der Rest ist NICHT verloren — er lädt beim Antippen nach.
        log.info('[lh_flightops] crew-prefetch gedeckelt: %d von %d Legs '
                 '(Horizont %d Tage, Deckel %d) — Rest bleibt On-Demand',
                 _CREW_PREFETCH_MAX_LEGS, len(out), _CREW_PREFETCH_DAYS,
                 _CREW_PREFETCH_MAX_LEGS)
    return out[:_CREW_PREFETCH_MAX_LEGS]


def _crew_prefetch_run(token, legs, now=None):
    """Legs vorwärmen. HINTERGRUND-Priorität (interactive=False) + eigene,
    tiefere Deckel. Gibt Zähler zurück, wirft nie.

    Absichtlich OHNE AeroX-Anreicherung: die kostet pro Leg bis zu ~25
    Supabase-Reads, und der Auslieferungs-Pfad matcht ohnehin frisch
    (_crew_reenrich). Der Prefetch schreibt die rohe Liste — das ist genau
    das, was geteilt wird."""
    done = skipped = failed = 0
    for leg in legs or []:
        try:
            if _refresh_all_state.get('drain'):
                break                     # Deploy läuft — kein neuer LH-Call
            if (_rot_hour_used() >= _CREW_PREFETCH_HOUR_CEILING
                    or _lhfo_day_used() >= _CREW_PREFETCH_DAY_CEILING):
                log.info('[lh_flightops] crew-prefetch pausiert (Budget) — '
                         '%d Legs offen', len(legs) - done - skipped - failed)
                break
            if _crew_prefetch_empty_recent(leg['flight'], leg['date'], now):
                skipped += 1              # LH hatte hier zuletzt nichts —
                continue                  # nicht im Stundentakt nachbohren
            best = _crew_pick_best(_crew_cache_scan(leg['flight'], leg['date']),
                                   leg['date'])
            if best:
                try:
                    age = (now or time.time()) - float(best.get('cached_at') or 0)
                except Exception:
                    age = 0.0
                if 0 <= age < _CREW_PREFETCH_MIN_AGE_S:
                    skipped += 1          # jemand hat dieses Leg schon geholt
                    continue
            resp = crew_list(token, leg['flight'], leg['date'], leg.get('dep'),
                             leg.get('arr'), leg.get('access'),
                             interactive=False)
            if not isinstance(resp, dict) or resp.get('processingErrors'):
                failed += 1               # Budget-Gate/LH-Fehler → nur eine
                continue                  # verpasste Zugabe, kein Fehlerfall
            crew = parse_crew_list(resp)
            if not crew:
                # LH füllt erst kurz vor Abflug. KORREKTUR 2026-07-29: der alte
                # Kommentar behauptete einen persistenten „Leer-Marker" — den
                # gab es nie, `_crew_cache_put` steigt bei leerer Liste sofort
                # aus (und `_crew_cache_scan` filtert crew-lose Zeilen weg).
                # Das Leg wurde deshalb bei JEDEM Kick neu geholt. Marker jetzt
                # prozess-lokal (s. Banner), TTL 6 h. Ein interaktiver Tap geht
                # weiter live — dieser Marker gilt nur für den Prefetch.
                _crew_prefetch_empty_mark(leg['flight'], leg['date'], now)
                skipped += 1
                continue
            _crew_cache_put(token, leg['flight'], leg['date'], crew)
            done += 1
            time.sleep(_CREW_PREFETCH_SLEEP_S)
        except Exception as e:
            failed += 1
            log.warning('[lh_flightops] crew-prefetch %s: %s',
                        (leg or {}).get('flight'), type(e).__name__)
    return {'prefetched': done, 'skipped': skipped, 'failed': failed}


def _crew_prefetch_kick(token, resp):
    """Fire-and-forget nach erfolgreichem Import. Blockiert die Import-Antwort
    NICHT (eigener Daemon-Thread), respektiert Cooldown und Thread-Deckel und
    wirft nie. True = ein Lauf wurde gestartet."""
    try:
        now = time.time()
        with _crew_prefetch_lock:
            if (now - _crew_prefetch_seen.get(token, 0.0)) < _CREW_PREFETCH_COOLDOWN_S:
                return False
            if _crew_prefetch_active[0] >= _CREW_PREFETCH_MAX_THREADS:
                return False
            legs = _crew_prefetch_legs(resp)
            if not legs:
                return False
            _crew_prefetch_seen[token] = now
            if len(_crew_prefetch_seen) > 4000:
                for k in sorted(_crew_prefetch_seen,
                                key=_crew_prefetch_seen.get)[:2000]:
                    _crew_prefetch_seen.pop(k, None)
            _crew_prefetch_active[0] += 1

        def _work():
            try:
                r = _crew_prefetch_run(token, legs)
                log.info('[lh_flightops] crew-prefetch tok=%s legs=%d %s',
                         (token or '')[:8], len(legs), r)
            except Exception as e:
                log.warning('[lh_flightops] crew-prefetch: %s', type(e).__name__)
            finally:
                with _crew_prefetch_lock:
                    _crew_prefetch_active[0] = max(0, _crew_prefetch_active[0] - 1)

        threading.Thread(target=_work, daemon=True).start()
        return True
    except Exception as e:
        log.warning('[lh_flightops] crew-prefetch-kick: %s', type(e).__name__)
        return False


@lh_flightops_bp.route('/api/lh/flightops/sim-crewlist/<token>', methods=['POST'])
def flightops_sim_crewlist(token):
    """„Wer sitzt mit im SIM" für einen Simulator-Termin. Body {date}.

    Owner-Zusage an Mark Elser (Forum 2026-07-26: „gibt es eine Möglichkeit
    die SIM Crewlisten abzufragen?" — „Werde ich mit aufnehmen!"). Die
    Funktion `simulator_crewlist` lag seit Langem im Code, wurde aber von
    NIRGENDWO aufgerufen: Ein SIM-Termin hat keine Flugnummer, und die
    gemeinsame Referenz-Suche `_links_find` verlangt zwingend einen
    `flightDesignator`. Die SIM-Referenz wird allein über `forDate`
    adressiert — sie war damit strukturell unauffindbar.

    Antwort-Form ABSICHTLICH identisch zur Flug-Crewliste
    ({ok, crew:[{position,name,pk,duty}]}), damit die App dieselbe Fläche
    benutzt; zusätzlich `session` mit Gerät/Schicht für die Überschrift.

    STATUS-CODES wie bei flightops_crewlist:
      401 not_connected · 503 token_refresh_pending ·
      404 no_sim_that_day (kein SIM an dem Tag im eigenen Plan) ·
      502 simulator_crewlist_unavailable.

    INTERAKTIV: hängt am Crew-Button, läuft also unter dem interaktiven
    Budget-Deckel — nicht unter dem Hintergrund-Deckel, der in vollen Tagen
    reißt."""
    b = request.get_json(silent=True) or {}
    date = (b.get('date') or '')[:10]
    if not date:
        return jsonify({'ok': False, 'error': 'date_required'}), 400

    def _cached():
        e = _sim_crew_cache_get(token, date)
        if not e:
            return None
        crew = e['crew']
        try:
            crew = _crew_reenrich(crew, flight=None, date=date)
        except Exception as exc:
            log.warning('[lh_flightops] cached sim-crew reenrich: %s',
                        type(exc).__name__)
        return jsonify({'ok': True, 'crew': crew,
                        'session': e.get('session') or {},
                        'cached': True, 'cached_at': e.get('cached_at')})

    _st, _acc = _access_state(token)
    if _st == 'pending':
        return _cached() or (jsonify({'ok': False,
                                      'error': 'token_refresh_pending'}), 503)
    if _st != 'ok':
        return _cached() or (jsonify({'ok': False,
                                      'error': 'not_connected'}), 401)

    p = _resolve_sim_link_params(token, date, interactive=True) or {}
    access = (p.get('accessCode') or '').strip()
    if not access:
        return _cached() or (jsonify({'ok': False,
                                      'error': 'no_sim_that_day'}), 404)

    resp = simulator_crewlist(token, forDate=p.get('forDate') or f'{date}Z',
                              accessCode=access, interactive=True)
    if resp is None:
        return _cached() or (jsonify({'ok': False,
                                      'error': 'simulator_crewlist_unavailable'}), 502)
    raw_crew = parse_simulator_crewlist(resp)
    session = simulator_session_info(resp)
    if not raw_crew:
        # LH kann eine Liste vor dem SIM noch nicht oder danach nicht mehr
        # liefern. Eine leere Live-Antwort darf die letzte gute Liste nicht
        # verdraengen — gleiches Verhalten wie bei Flug-Crewlisten.
        return _cached() or jsonify({'ok': True, 'crew': [],
                                     'session': session})
    _sim_crew_cache_put(token, date, raw_crew, session)
    crew = raw_crew
    # AeroX-Verknüpfung wie bei der Flug-Crewliste: wer von den Kollegen ein
    # AeroX-Profil hat, wird über die Personalnummer gefunden (NUR über die PK
    # — Namens-Fuzzy ist bewusst raus, siehe Crew-Match-Regel).
    try:
        crew = _crew_reenrich(crew, flight=None, date=date)
    except Exception as e:
        log.warning('[lh_flightops] sim-crew reenrich: %s', type(e).__name__)
    return jsonify({'ok': True, 'crew': crew, 'session': session})


@lh_flightops_bp.route('/api/me/lh/flightops/sim-crewlist', methods=['POST'])
def me_flightops_sim_crewlist():
    """Credential-free simulator-crew alias for Android.

    Keep the mature FlightOps handler as the sole authority for grant state,
    roster ownership and LH response parsing.  The wrapper merely derives the
    owner from the normalized Authorization bearer and makes the response safe
    for the `/api/me` public-reference boundary.
    """
    try:
        import app as _app
        token, error = _app._header_only_owner()
    except Exception:
        return jsonify({'ok': False, 'error': 'auth_unavailable'}), 503
    if error is not None:
        return error
    if request.args:
        return jsonify({'ok': False, 'error': 'query_not_allowed'}), 400
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or set(body) != {'date'}:
        return jsonify({'ok': False, 'error': 'invalid_body'}), 400
    response = _app.app.make_response(flightops_sim_crewlist(token))
    if not response.is_json:
        return response
    payload = response.get_json(silent=True)
    if payload is None:
        return jsonify({'ok': False, 'error': 'invalid_response'}), 503
    return _app.app.make_response((
        jsonify(_app._publicize_foreign_user_refs(payload)), response.status_code,
    ))


@lh_flightops_bp.route('/api/lh/flightops/crewlist/<token>', methods=['POST'])
def flightops_crewlist(token):
    """„Wer fliegt mit" für ein Leg (COMMON_CREWLIST → normalisiert). Body
    {flight, date, dep, arr, access?}. Ohne `access` wird der accessCode aus
    den Duty-Events-_links aufgelöst (Cache → Live-Nachladen des Tages) — die
    App muss ihn also NICHT kennen. Parser gegen echte Shape verifiziert.

    LAST-GOOD-CACHE (Owner 2026-07-24): jede erfolgreiche Liste wird pro Leg
    durabel persistiert (Tabelle flightops_crew_cache, siehe oben). Ist der
    Grant tot (needs_relogin), der accessCode nicht auflösbar oder LH down,
    kommt die LETZTE Liste mit `cached:true` statt eines Fehlers.

    GETEILT SEIT 2026-07-28: liegt eine FRISCHE Zeile dieses Legs im Backend —
    auch die eines Kollegen derselben Crew —, antwortet der Endpoint daraus
    (`cached:true`, `shared:true`) OHNE LH-Call. Berechtigungs-Nachweis und
    TTL-Staffel siehe Banner über _crew_shared_serve; `force:true` im Body
    erzwingt den Live-Abruf.

    `flight_date` — ADDITIVES ANTWORT-FELD SEIT 2026-08-09 (Pflicht-Lektüre):
    beide Cache-Pfade lesen mit `_CREW_CACHE_DATE_SLACK = (0, -1, 1)`, dürfen
    also die Zeile des NACHBARTAGS ausliefern (gedacht als Rollover-Hilfe für
    Red-Eyes, wo Roster-Datum (LT) und LH-Flugdatum (Z) auseinanderfallen).
    Bis heute NANNTE die Antwort das bediente Datum nicht — der Client konnte
    eine Fremdtags-Besetzung nicht erkennen, schrieb sie als Crew-Historie
    DIESES Legs fest, und `CrewLogbookStore.replacingCrew` löschte dabei die
    echten Kolleginnen und Kollegen des Tages.
    Gemessen 2026-08-09 02:30 UTC über die komplette `flightops_crew_cache`
    (6.484 Zeilen · 5.797 (Flug,Datum) · 1.191 Flugnummern · 2026-05-08 bis
    2026-09-04): 1.218 Nachbartags-Paare derselben Flugnummer, davon nur 5
    (0,4 %) mit identischer Crew; Median-Namensüberlappung 0,000; 1.169 Paare
    ohne EINEN gemeinsamen Namen. In den Datums-Spannen der Flugnummern gibt
    es 5.919 Kalendertage ohne eigene Zeile, aber MIT Nachbar-Zeile — genau
    die Fälle, in denen der Fallback greift. Also Alltag, kein Randfall.
    `flight_date` nennt deshalb das Datum, zu dem die gelieferte Liste
    WIRKLICH gehört. Es wird auf JEDEM ok-Pfad gesetzt (live, leer, eigener
    Cache, geteilter Cache). Der Fallback selbst bleibt unangetastet — er
    füllt weiter die Fläche; nur der Client entscheidet jetzt anhand des
    Feldes, ob er die Liste auch als Historie festschreiben darf.
    RÜCKWÄRTSKOMPATIBEL: rein additiv, kein Feld entfernt, kein Typ geändert.
    Alte Clients dekodieren `{ok, crew}` unverändert; neue Clients behandeln
    ein FEHLENDES `flight_date` (altes Backend) wie „Datum stimmt".

    STATUS-CODES (die App unterscheidet sie, siehe FlightCrewSheet):
      401 not_connected          — Grant tot/nie da ⇒ App bietet „Mit
                                   Lufthansa verbinden" an. NUR hier!
      503 token_refresh_pending  — Access-Token abgelaufen, der zentrale
                                   Refresher ist dran ⇒ „gleich nochmal",
                                   KEIN Relogin-Angebot (der Grant ist heil).
      404 no_access_code · 502 crewlist_unavailable — Leg/LH-seitig.
    Jeder dieser Pfade liefert vorher die Cache-Liste aus, wenn es eine gibt.

    INTERAKTIV: dieser Endpoint hängt am Crew-BUTTON, ist also nutzerausgelöst
    — LH-Calls laufen mit `interactive=True` (Budget-Gate 950/h statt 700/h).
    Vorher lief der Tap unter dem Hintergrund-Deckel und wurde in vollen
    Stunden verworfen (Log 28.07. 06:00/06:52/06:56: „Stundenbudget 748 >= 700
    — Hintergrund-Call /COMMON_CREWLIST übersprungen" → 502 beim User)."""
    b = request.get_json(silent=True) or {}
    flight, date = b.get('flight'), b.get('date')
    dep, arr = b.get('dep'), b.get('arr')

    def _cached():
        e = _crew_cache_get(token, flight, date)
        if e and e.get('crew'):
            served = str(e.get('date') or '')[:10]
            if served and served != str(date or '')[:10]:
                log.warning('[lh_flightops] crewlist %s: Nachbartag aus '
                            'EIGENEM Cache bedient (angefragt %s, geliefert '
                            '%s)', flight, str(date or '')[:10], served)
            # AEROX-VERKNÜPFUNG NACHZIEHEN (Owner-Regression 2026-07-29:
            # „Crew lädt jetzt instant, aber es steht nicht mehr, wer bei
            # AeroX ist"): der Prefetch schreibt bewusst die ROHE Liste
            # (Anreicherung kostet ~25 SB-Reads/Leg) — seit er läuft, ist
            # eine rohe Zeile der Normalfall statt der Ausnahme. Der geteilte
            # Pfad reicherte längst an, dieser Eigen-Cache-Pfad NICHT: die
            # „Auf AeroX"-Zeile verschwand. Gleiche Funktion, gleiche Kosten
            # (nur Supabase-Reads, KEIN LH-Call).
            return jsonify({'ok': True,
                            'crew': _crew_reenrich(e['crew'], flight=flight,
                                                   date=date),
                            'cached': True, 'cached_at': e.get('cached_at'),
                            # ADDITIV seit 2026-08-09 — siehe Banner oben.
                            'flight_date': served or str(date or '')[:10]})
        return None

    _st, _acc = _access_state(token)
    if _st == 'pending':
        return _cached() or (jsonify({'ok': False,
                                      'error': 'token_refresh_pending'}), 503)
    if _st != 'ok':
        return _cached() or (jsonify({'ok': False, 'error': 'not_connected'}), 401)
    # GETEILTER CACHE (Owner 2026-07-28): liegt die Liste dieses Legs schon
    # frisch im Backend — egal ob von diesem User oder einem Kollegen derselben
    # Crew —, wird sie ohne LH-Call ausgeliefert. Berechtigung und TTL-Regeln
    # siehe Banner über _crew_shared_serve; `force:true` übergeht den Shortcut.
    if not b.get('force'):
        _shared = _crew_shared_serve(token, flight, date, dep, arr)
        if _shared is not None:
            return _shared
    access = (b.get('access') or '').strip()
    if not access:
        p = _resolve_link_params(token, 'crewlist', flight, date, dep, arr,
                                 interactive=True) or {}
        access = p.get('accessCode') or ''
        dep = dep or p.get('departureAirport')
        arr = arr or p.get('arrivalAirport')
    if not access:
        # Kein Link = Flug nicht im eigenen Roster → LH würde eh 401/403 geben.
        return _cached() or (jsonify({'ok': False, 'error': 'no_access_code'}), 404)
    resp = crew_list(token, flight, date, dep, arr, access, interactive=True)
    if not isinstance(resp, dict) or resp.get('processingErrors'):
        return _cached() or (jsonify({'ok': False, 'error': 'crewlist_unavailable'}), 502)
    crew = parse_crew_list(resp)
    if not crew:
        # LH füllt die Liste erst kurz vor Abflug und räumt sie nach dem Flug
        # wieder weg. Eine LEERE Live-Antwort darf die letzte gute nicht
        # verdrängen — sonst steht die Fläche ausgerechnet dann leer, wenn man
        # nach dem Flug nachschlägt „mit wem war ich unterwegs".
        return _cached() or jsonify({'ok': True, 'crew': [],
                                     'flight_date': str(date or '')[:10]})
    # AeroX-Profil-Verknüpfung (Owner 2026-07-23): wer aus der Crew ist selbst
    # auf AeroX? → Avatar/Profil direkt aus der Liste öffnen.
    matches = _match_aerox_profiles(crew, flight=flight, date=date)
    for m in crew:
        p = matches.get(str(m.get('pk') or m.get('name') or ''))
        if p:
            m['aerox'] = p
    _crew_cache_put(token, flight, date, crew)
    # LIVE-Pfad: `crew_list` fragt LH mit GENAU diesem Datum, und der
    # accessCode kommt aus `_links_find`, das das Flugdatum EXAKT vergleicht
    # (`startswith(dt_)`) — hier gibt es keine Datums-Toleranz, `flight_date`
    # ist per Konstruktion das angefragte Datum.
    return jsonify({'ok': True, 'crew': crew,
                    'flight_date': str(date or '')[:10]})


@lh_flightops_bp.route('/api/lh/flightops/crewlist-batch/<token>', methods=['POST'])
def flightops_crewlist_batch(token):
    """Cache-only monthly crew-list hydration.

    The client sends its roster legs and receives every currently fresh,
    authorised backend-cache hit in one response.  Misses are deliberately
    omitted: the existing three-day prefetch and interactive single-leg route
    remain the only paths allowed to spend COMMON_CREWLIST quota.
    """
    body = request.get_json(silent=True) or {}
    legs = body.get('legs')
    if not isinstance(legs, list):
        return jsonify({'ok': False, 'error': 'invalid_legs'}), 400
    requested = min(len(legs), _CREW_CACHE_BATCH_MAX_LEGS)
    hits = _crew_cache_batch_hits(token, legs)
    return jsonify({'ok': True, 'items': hits, 'requested': requested,
                    'hits': len(hits), 'cache_only': True})


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
    # INTERAKTIV (Fund 29.07.): dieser Endpoint hängt an einem Nutzer-Tap, lief
    # aber unter dem HINTERGRUND-Deckel — nach dem Reißen des Tagesdeckels
    # (lhfoD 5.303 ≥ 5.000 um 12:50 UTC) bekam der User hier 502, während der
    # Crew-Button (interactive=True) weiterlief. Nur Hintergrundarbeit darf
    # sterben; Taps gehören in den reservierten Headroom (900/h · 5.600/Tag).
    p = _resolve_link_params(token, 'checkintimes', flight, date,
                             b.get('dep'), b.get('arr'), interactive=True)
    if p:
        resp = service_get(token, 'COMMON_CHECK_IN_TIMES', p, interactive=True)
    else:
        resp = check_in_times(token, flight, date, b.get('dep'), b.get('arr'),
                              duty_type=(b.get('duty_type') or 'OD'),
                              crew_category=(b.get('crew_category') or 'COC'),
                              interactive=True)
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
    # INTERAKTIV (Fund 29.07., gleiche Begründung wie beim Check-in): Layover-
    # Hotel schaut man nach, während man im Bus sitzt — ein Tap, kein Cron.
    resp = crew_hotel(token, b.get('station'), b.get('provider'),
                      interactive=True)
    return jsonify({'ok': True, 'hotels': parse_crew_hotel(resp),
                    'station': (b.get('station') or '').upper()})


# ════════════════════════════════════════════════════════════════════════════
# LANDING REPORT → FLUGBUCH-ABGLEICH (Welle 1, 2026-07-31)
# ════════════════════════════════════════════════════════════════════════════
# Das Flugbuch ist ein RECHTSDOKUMENT. Dieser Pfad SCHREIBT NICHTS: er liefert
# ausschließlich VORSCHLÄGE („LH sagt zu diesem Leg: …"). Die Übernahme macht
# die App über den bestehenden Leg-Save-Endpoint nach einem Nutzer-Tap —
# einzeln oder als Monats-Batch. Kein Auto-Write, kein Auto-Merge.
#
# BEWUSST OHNE CRON/HINTERGRUND-ANBINDUNG (Plan-Doc, Welle 1): der Abruf hängt
# NUR am Nutzer-Tap „Mit LH abgleichen". Eine Hintergrund-Anbindung kommt erst
# nach dem Beweis der Quota-Diät (Verbrauch < 5.000/Tag) — heute (31.07.) lag
# der lhfo-Tagesstand mittags schon bei ~3.900 von 5.900.
#
# DREI GATES, alle drei nötig:
#   1. FRISCHE (Doku + live belegt): ein frisch gelandetes Leg liefert HTTP 404
#      bzw. response:null — der Report ist erst ~24 h nach Ankunft da. Wir
#      fragen deshalb gar nicht erst. 404 heißt NIE „nicht gelandet", sondern
#      immer 'pending'.
#   2. EIGENER TAGESZÄHLER `lhfoD-landing:<YYYYMMDD>` (ax_api_budget-Muster,
#      Deckel 400). Der globale lhfo-Tagesdeckel schützt die LH-Quote; dieser
#      hier schützt die ANDEREN Features davor, dass ein neuer Verbraucher
#      ihnen das gemeinsame Kontingent wegfrisst. Ist er erreicht, sagt der
#      Endpoint das (status 'budget' pro Leg + Log) — er kappt nicht still.
#   3. HARTER DECKEL pro Aufruf (30 Calls), Rest als 'skipped_budget'.
#
# CACHE-SEMANTIK — die wichtigste Regel dieses Blocks:
#   · OOOI-Zeiten, tailsign, destinationAirport sind FLUG-Fakten. Eine gelandete
#     Zeit ändert sich nie ⇒ geteilter Disk-Cache pro (flight,date,dep), TTL 30
#     Tage. Zwei Kollegen desselben Legs teilen sie legitim.
#   · `landingPerformed`/`pkNumber` sind PER-USER („die ANFRAGENDE Person hat
#     gelandet"). Sie dürfen NIEMALS in den geteilten Cache — sonst stünde im
#     Flugbuch eines Kabinenmitglieds die Landung des Kapitäns. Der geteilte
#     Cache filtert beim LESEN auf eine Whitelist von Flug-Fakt-Keys: selbst
#     eine von Hand vergiftete Datei kann `self_landed` nicht einschleusen.
#     Das eigene Flag liegt in einer PRO-USER-Datei und kommt sonst nur aus der
#     user-eigenen Live-Response.
_LB_BUDGET_PREFIX = 'lhfoD-landing:'
_LB_DAY_CEILING = 400            # eigener Tagesdeckel dieses Verbrauchers
_LB_MAX_CALLS = 30               # harter Deckel je Endpoint-Aufruf
_LB_SPACING_S = 0.7              # Abstand zwischen zwei LH-Calls (Quota-Diät)
_LB_DEFAULT_DAYS = 14
_LB_MAX_DAYS = 31
# Frische-Gate in TAGEN auf dem Flugdatum. Der Link-Cache trägt nur das
# Flugdatum, keine Ankunftszeit — als Ankunfts-Anker gilt deshalb das ENDE des
# UTC-Flugtags. `days_back >= 2` heißt: der Flugtag ist mindestens 24 h vorbei.
# Live belegt (31.07.): ein Leg von gestern (days_back=1) liefert response:null,
# eines von vorgestern/älter liefert den Report.
_LB_MIN_AGE_DAYS = 2
_LB_SHARED_TTL_S = 30 * 86400    # gelandete Zeiten ändern sich nie
_LB_SHARED_MAX = 4000            # Einträge in der geteilten Datei
_LB_SELF_MAX = 800               # Einträge je User-Datei
# Was aus dem GETEILTEN Cache überhaupt gelesen werden darf (Whitelist!).
_LB_SHARED_KEYS = ('arr', 'tail', 'dep_iso', 'arr_iso', 'off_iso', 'on_iso',
                   'block_min', 'air_min')

_lb_day_memo = {'ts': 0.0, 'day': '', 'used': 0}
_LB_DAY_MEMO_S = 60.0
_lb_day_local = {'day': '', 'n': 0}      # in DIESEM Prozess gebuchte Calls


def _lb_utc_day(now=None):
    return time.strftime('%Y%m%d', time.gmtime(now))


def _lb_budget_key(now=None):
    return _LB_BUDGET_PREFIX + _lb_utc_day(now)


def _lb_day_used(now=None):
    """Tagesstand des EIGENEN Landing-Report-Zählers (max aus persistiertem
    Stand und dem, was dieser Prozess seit dem letzten Flush gebucht hat —
    der Flusher schreibt nur alle 30 s, ein einzelner Aufruf darf den Deckel
    aber nicht in einem Rutsch überrennen). Wirft nie."""
    day = _lb_utc_day(now)
    if _lb_day_local['day'] != day:
        _lb_day_local['day'], _lb_day_local['n'] = day, 0
    ts = time.time()
    if _lb_day_memo['day'] != day or (ts - _lb_day_memo['ts']) >= _LB_DAY_MEMO_S:
        try:
            from blueprints.aerox_data_blueprint import _budget_key_used
            used = int(_budget_key_used(_lb_budget_key(now)) or 0)
        except Exception:
            used = 0
        _lb_day_memo['ts'], _lb_day_memo['day'] = ts, day
        _lb_day_memo['used'] = used
    return max(_lb_day_memo['used'], _lb_day_local['n'])


def _lb_budget_book(now=None):
    """Einen Landing-Report-Call im Tageszähler buchen. Wirft nie."""
    day = _lb_utc_day(now)
    if _lb_day_local['day'] != day:
        _lb_day_local['day'], _lb_day_local['n'] = day, 0
    _lb_day_local['n'] += 1
    try:
        from blueprints.lh_open_api import budget_inc_key
        budget_inc_key(_lb_budget_key(now))
    except Exception:
        pass


# ── Cache-Dateien (Muster: folinks_-Disk-Cache) ─────────────────────────────
def _lb_key(flight, date, dep):
    return '%s|%s|%s' % ((flight or '').upper().replace(' ', ''),
                         (date or '')[:10], (dep or '').upper())


def _lb_json_load(path):
    try:
        if path and os.path.exists(path):
            with open(path) as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        pass
    return {}


def _lb_json_save(path, obj):
    """Atomar schreiben (tmp + replace) — mehrere gunicorn-Worker teilen sich
    die geteilte Datei; ein halb geschriebenes JSON wäre schlimmer als ein
    verlorener Eintrag. Einträge sind unveränderliche Fakten, „last writer
    wins" ist damit unkritisch. Wirft nie."""
    try:
        if not path:
            return
        tmp = path + '.tmp%d' % os.getpid()
        with open(tmp, 'w') as f:
            json.dump(obj, f)
        os.replace(tmp, path)
    except Exception as e:
        log.warning('[lh_flightops] landing cache save: %s', type(e).__name__)


def _lb_prune(d, cap, now=None):
    """Abgelaufene (TTL) und überzählige Einträge entfernen. Pure-nah."""
    ts = now if now is not None else time.time()
    live = {k: v for k, v in (d or {}).items()
            if isinstance(v, dict) and (ts - float(v.get('ts') or 0)) < _LB_SHARED_TTL_S}
    if len(live) > cap:
        keep = sorted(live.items(), key=lambda kv: float(kv[1].get('ts') or 0),
                      reverse=True)[:cap]
        live = dict(keep)
    return live


def _lb_shared_path():
    return os.path.join(_flow_dir(), 'folanding_shared.json')


def _lb_shared_get(key, now=None):
    """Flug-Fakten eines Legs aus dem GETEILTEN Cache oder None.
    Liest ausschließlich die Whitelist `_LB_SHARED_KEYS` — `self_landed`/
    `pkNumber` können hier konstruktiv nicht herauskommen."""
    e = _lb_json_load(_lb_shared_path()).get(key)
    if not isinstance(e, dict):
        return None
    ts = now if now is not None else time.time()
    if (ts - float(e.get('ts') or 0)) >= _LB_SHARED_TTL_S:
        return None
    return {k: e[k] for k in _LB_SHARED_KEYS if e.get(k) is not None}


def logbook_cached_completion_proofs(candidates, now=None):
    """Lokale Landing-Report-Belege für mehrere Roster-Legs in EINEM Read.

    Rückgabe: ``{(flight, date, dep): actual_arrival_iso}``. Ausschließlich
    unveränderliche gemeinsame Flug-Fakten werden gelesen; das pro User
    gespeicherte ``self_landed`` ist in dieser Datei konstruktiv nicht enthalten.
    Abgelaufene, naive oder noch in der Zukunft liegende Zeiten beweisen nichts.
    """
    ts = time.time() if now is None else float(now)
    try:
        ref = _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc)
        shared = _lb_json_load(_lb_shared_path())
    except Exception:
        return {}
    out = {}
    for c in candidates or []:
        if not isinstance(c, dict):
            continue
        flight = (c.get('flight') or '').upper().replace(' ', '')
        date = (c.get('date') or '')[:10]
        dep = (c.get('dep') or '').upper()
        if not (flight and date and dep):
            continue
        row = shared.get(_lb_key(flight, date, dep))
        if not isinstance(row, dict):
            continue
        try:
            if ts - float(row.get('ts') or 0) >= _LB_SHARED_TTL_S:
                continue
        except (TypeError, ValueError):
            continue
        raw = row.get('arr_iso') or row.get('on_iso')
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            actual = _dt.datetime.fromisoformat(
                raw.strip().replace('Z', '+00:00'))
            if actual.tzinfo is None or actual.astimezone(_dt.timezone.utc) > ref:
                continue
            out[(flight, date, dep)] = actual.astimezone(
                _dt.timezone.utc).isoformat().replace('+00:00', 'Z')
        except (TypeError, ValueError, OverflowError):
            continue
    return out


def _lb_shared_put(key, facts, now=None):
    """Flug-Fakten teilen. Nimmt NUR die Whitelist auf — der per-User-Anteil
    (self_landed/pkNumber) wird hier hart abgeschnitten, nicht „vergessen"."""
    if not key or not isinstance(facts, dict):
        return
    row = {k: facts[k] for k in _LB_SHARED_KEYS if facts.get(k) is not None}
    if not row:
        return
    row['ts'] = now if now is not None else time.time()
    d = _lb_json_load(_lb_shared_path())
    d[key] = row
    _lb_json_save(_lb_shared_path(), _lb_prune(d, _LB_SHARED_MAX, now))


def _lb_self_path(user_token):
    safe = re.sub(r'[^A-Za-z0-9_-]', '', user_token or '')[:64]
    return os.path.join(_flow_dir(), f'folanding_{safe}.json') if safe else None


def _lb_self_get(user_token, key, now=None):
    """Das EIGENE `self_landed`-Flag für ein Leg (True/False) oder None, wenn
    unbekannt. Strikt pro User — eigene Datei, nie geteilt."""
    e = _lb_json_load(_lb_self_path(user_token)).get(key)
    if not isinstance(e, dict) or not isinstance(e.get('self_landed'), bool):
        return None
    ts = now if now is not None else time.time()
    if (ts - float(e.get('ts') or 0)) >= _LB_SHARED_TTL_S:
        return None
    return e['self_landed']


def logbook_cached_self_landing_flags(user_token, candidates, now=None):
    """Frische, strikt personengebundene Landing-Flags in EINEM Datei-Read.

    Rückgabe: ``{(flight, date, dep): bool}``. Anders als der geteilte
    Completion-Cache darf diese Funktion ausschließlich mit dem Token des
    anfragenden Users aufgerufen werden. Dadurch kann das Flugbuch eine von LH
    bestätigte eigene Landung als PF-Beleg darstellen, ohne Flags zwischen
    Crew-Mitgliedern zu vermischen oder im GET-Pfad etwas zu persistieren.
    """
    if not user_token:
        return {}
    ts = time.time() if now is None else float(now)
    try:
        own = _lb_json_load(_lb_self_path(user_token))
    except Exception:
        return {}
    out = {}
    for c in candidates or []:
        if not isinstance(c, dict):
            continue
        flight = (c.get('flight') or '').upper().replace(' ', '')
        date = (c.get('date') or '')[:10]
        dep = (c.get('dep') or '').upper()
        if not (flight and date and dep):
            continue
        row = own.get(_lb_key(flight, date, dep))
        if not isinstance(row, dict) or not isinstance(
                row.get('self_landed'), bool):
            continue
        try:
            if ts - float(row.get('ts') or 0) >= _LB_SHARED_TTL_S:
                continue
        except (TypeError, ValueError):
            continue
        out[(flight, date, dep)] = row['self_landed']
    return out


def _lb_self_put(user_token, key, self_landed, now=None):
    p = _lb_self_path(user_token)
    if not p or not isinstance(self_landed, bool):
        return       # None = „unbekannt" wird NICHT als Tatsache konserviert
    d = _lb_json_load(p)
    d[key] = {'self_landed': self_landed,
              'ts': now if now is not None else time.time()}
    _lb_json_save(p, _lb_prune(d, _LB_SELF_MAX, now))


def _lb_candidates(links, days, today=None):
    """Link-Cache (`extract_duty_links`) → Kandidaten-Legs fürs Flugbuch:
    [{flight, date, dep, arr}], aufsteigend nach Datum, dedupliziert.
    PURE/testbar.

    Die landingReport-Referenzen liegen seit jeher in `folinks_<token>.json`
    (jeder Roster-Import schreibt sie mit) und wurden bisher NIE genutzt — sie
    tragen flightDesignator/flightDate/departureAirport fix und fertig.

    FRISCHE-GATE: nur Legs, deren Flugtag `_LB_MIN_AGE_DAYS`…`days` Tage
    zurückliegt. Untergrenze = „Ankunft ≥ 24 h her" (s. Banner), Obergrenze =
    das vom Client gewünschte Fenster."""
    try:
        ref = today or _dt.datetime.now(_dt.timezone.utc).date()
    except Exception:
        return []
    out, seen = [], set()
    for l in links or []:
        if not isinstance(l, dict) or l.get('service') != 'landingreport':
            continue
        p = l.get('params') or {}
        flight = (p.get('flightDesignator') or '').upper().replace(' ', '')
        date = (p.get('flightDate') or '')[:10]
        dep = (p.get('departureAirport') or '').upper()
        if not (flight and date and dep):
            continue
        try:
            d = _dt.date(int(date[0:4]), int(date[5:7]), int(date[8:10]))
        except Exception:
            continue
        back = (ref - d).days
        if back < _LB_MIN_AGE_DAYS or back > days:
            continue
        k = (flight, date, dep)
        if k in seen:
            continue
        seen.add(k)
        out.append({'flight': flight, 'date': date, 'dep': dep,
                    'arr': (p.get('arrivalAirport') or '').upper() or None})
    out.sort(key=lambda c: (c['date'], c['flight']))
    return out


def _lb_candidates_from_roster(briefings, days, today=None):
    """Kandidaten-Legs aus dem EIGENEN Roster (`ical_sectors`) — derselbe
    Shape wie `_lb_candidates`. PURE/testbar.

    WARUM ES DIESEN ZWEITEN WEG BRAUCHT (Befund 2026-07-31, am Live-System
    nachgesehen): `_lb_candidates` liest ausschließlich `landingreport`-
    Referenzen aus dem Duty-Events-Link-Cache. In `folinks_<token>.json` des
    Owners lagen 16 Referenzen — flightInfo, airportWeather, crewList,
    checkInTimes, crewHotel — und KEINE EINZIGE landingReport. Der Abgleich
    lieferte deshalb `legs: []` bei `calls: 0`: er hatte nie einen Kandidaten,
    und der Nachlade-Zweig fand ebenfalls keinen. Für den Nutzer sah das aus
    wie „LH hat nichts", tatsächlich hat nie jemand gefragt.

    COMMON_LANDING_REPORT braucht — anders als COMMON_CREWLIST — KEINEN
    accessCode, sondern nur (flightDesignator, flightDate, departureAirport).
    Diese drei Werte stehen in jedem geflogenen Roster-Sektor. Der Roster ist
    damit die verlässlichere Kandidatenquelle; die Links bleiben als erster
    Weg bestehen (sie sind gratis und exakt).

    Deadheads und Nicht-Flug-Sektoren fallen raus (`_pb_is_flight_sector`):
    für einen Deadhead gibt es keinen eigenen Landing Report."""
    try:
        ref = today or _dt.datetime.now(_dt.timezone.utc).date()
    except Exception:
        return []
    out, seen = [], set()
    for d in sorted((briefings or {}).keys()):
        if not isinstance(d, str) or len(d) < 10:
            continue
        day = briefings.get(d)
        if not isinstance(day, dict):
            continue
        try:
            dd = _dt.date(int(d[0:4]), int(d[5:7]), int(d[8:10]))
        except Exception:
            continue
        back = (ref - dd).days
        if back < _LB_MIN_AGE_DAYS or back > days:
            continue
        for s in (day.get('ical_sectors') or []):
            if not (isinstance(s, dict) and _pb_is_flight_sector(s)):
                continue
            flight = re.sub(r'\s+', '', str(s.get('flight') or '')).upper()
            dep = str(s.get('from') or '').upper()
            if not (flight and len(dep) == 3):
                continue
            k = (flight, d[:10], dep)
            if k in seen:
                continue
            seen.add(k)
            out.append({'flight': flight, 'date': d[:10], 'dep': dep,
                        'arr': str(s.get('to') or '').upper() or None})
    out.sort(key=lambda c: (c['date'], c['flight']))
    return out


def _lb_merge_candidates(primary, extra):
    """Link-Kandidaten zuerst (sie tragen die exakten LH-Parameter), fehlende
    Roster-Kandidaten hinten dran — dedupliziert über (flight, date, dep)."""
    seen = {(c['flight'], c['date'], c['dep']) for c in (primary or [])}
    out = list(primary or [])
    for c in extra or []:
        k = (c['flight'], c['date'], c['dep'])
        if k not in seen:
            seen.add(k)
            out.append(c)
    out.sort(key=lambda c: (c['date'], c['flight']))
    return out


def _lb_leg_row(cand, facts, self_landed, status):
    """Ein Ergebnis-Leg in der vertraglich fixen Shape. Fehlendes bleibt None —
    ein Flugbuch-Vorschlag mit erfundenen Werten wäre schlimmer als keiner."""
    f = facts or {}
    return {'flight': cand['flight'], 'date': cand['date'], 'dep': cand['dep'],
            'arr': f.get('arr') or cand.get('arr'),
            'tail': f.get('tail'),
            'block_min': f.get('block_min'), 'air_min': f.get('air_min'),
            'off_iso': f.get('off_iso'), 'on_iso': f.get('on_iso'),
            'out_iso': f.get('dep_iso'), 'in_iso': f.get('arr_iso'),
            'self_landed': self_landed, 'status': status}


@lh_flightops_bp.route('/api/lh/flightops/logbook-verify/<token>',
                       methods=['POST'])
def flightops_logbook_verify(token):
    """Flugbuch-Abgleich mit dem LH Landing Report — VORSCHLÄGE, kein Write.

    Body (optional): {"days": N}  — Rückschau-Fenster, Default 14, max 31.

    Antwort:
        {ok, days, legs: [{flight, date, dep, arr, tail, block_min, air_min,
                           off_iso, on_iso, out_iso, in_iso, self_landed,
                           status}], calls, budget: {...}}
    `status` je Leg:
        'ok'             — Report da, Werte gültig
        'pending'        — LH hat (noch) keinen Report (HTTP 404 / null). NIE
                           als „nicht gelandet" lesen!
        'budget'         — eigener Tagesdeckel erreicht, heute nicht mehr
        'skipped_budget' — Deckel von 30 Calls je Aufruf erreicht, erneut tappen
        'error'          — LH-/Netzfehler
    Nur bei 'ok' ist `self_landed` belastbar; sonst None. Flug-Fakten können
    auch ohne 'ok' gefüllt sein — dann stammen sie aus dem geteilten
    Flug-Fakten-Cache (echte Werte desselben Legs, nur nicht aus DIESEM Call).

    `self_landed` = „die anfragende Person hat die Landung durchgeführt"
    (PER-USER, Landungs-Zähler fürs Cockpit-Flugbuch) — KEIN Flug-Status.

    Auth wie die übrigen owner-scoped FlightOps-Routen (Pfad-Token + Bearer,
    Gate in app.py) + Grant-Zustand hier."""
    _st, _acc = _access_state(token)
    if _st == 'pending':
        return jsonify({'ok': False, 'error': 'token_refresh_pending'}), 503
    if _st != 'ok':
        return jsonify({'ok': False, 'error': 'not_connected'}), 401
    b = request.get_json(silent=True) or {}
    try:
        days = int(b.get('days') or _LB_DEFAULT_DAYS)
    except Exception:
        days = _LB_DEFAULT_DAYS
    days = max(_LB_MIN_AGE_DAYS, min(_LB_MAX_DAYS, days))

    cands = _lb_candidates(_links_load(token), days)
    if not cands:
        # MISS wie im crewlist-Pfad: das Fenster EINMAL nachladen (1 Call) —
        # der Link-Cache liegt auf der ungemounteten Container-Disk und ist
        # nach jedem Deploy leer. duty_events nimmt eine Spanne, das kostet
        # also genau einen Call für das ganze Fenster.
        today = _dt.datetime.now(_dt.timezone.utc).date()
        resp = duty_events(token,
                           (today - _dt.timedelta(days=days)).isoformat(),
                           today.isoformat(), interactive=True)
        fresh = extract_duty_links(resp) if isinstance(resp, dict) else []
        if fresh:
            with _links_lock:
                merged = [l for l in _links_load(token)
                          if not any(l == g for g in fresh)] + fresh
                _links_save(token, merged[-800:])
        cands = _lb_candidates(fresh, days)

    # ZWEITE QUELLE: der eigene Roster (Befund 2026-07-31 — im Link-Cache
    # steht oft KEINE einzige landingReport-Referenz, s.
    # `_lb_candidates_from_roster`). Immer ergänzend, nicht nur im Miss-Fall:
    # der Link-Cache kann einen Teil des Fensters kennen und den Rest nicht,
    # und ein halb abgeglichenes Flugbuch ist genau die Sorte stiller Lücke,
    # die niemand meldet.
    try:
        import app as _app
        _briefs = dict(_app._ical_briefings_load(token) or {})
    except Exception as e:
        log.warning('[lh_flightops] logbook-verify briefings: %s',
                    type(e).__name__)
        _briefs = {}
    cands = _lb_merge_candidates(cands,
                                 _lb_candidates_from_roster(_briefs, days))

    legs, calls, stopped = [], 0, None   # stopped = Status für den Rest
    for c in cands:
        key = _lb_key(c['flight'], c['date'], c['dep'])
        shared = _lb_shared_get(key)
        own = _lb_self_get(token, key)
        if shared and own is not None:
            # Beides bekannt → kein LH-Call. Zeiten geteilt, Flag aus der
            # EIGENEN Historie — nie aus der geteilten Datei.
            legs.append(_lb_leg_row(c, shared, own, 'ok'))
            continue
        if stopped:                     # zu — der Rest bekommt denselben Grund
            legs.append(_lb_leg_row(c, shared, None, stopped))
            continue
        if calls >= _LB_MAX_CALLS:
            legs.append(_lb_leg_row(c, shared, None, 'skipped_budget'))
            continue
        if _lb_day_used() >= _LB_DAY_CEILING:
            log.warning('[lh_flightops] landing-Tagesdeckel %s >= %s — '
                        'Flugbuch-Abgleich stoppt (Rest als status=budget)',
                        _lb_day_used(), _LB_DAY_CEILING)
            stopped = 'budget'
            legs.append(_lb_leg_row(c, shared, None, 'budget'))
            continue
        if calls and _LB_SPACING_S > 0:
            time.sleep(_LB_SPACING_S)       # 0,7 s Abstand (Quota-Diät)
        st = {}
        resp = landing_report(token, c['flight'], c['date'], c['dep'],
                              interactive=True, status_out=st)
        kind, code = st.get('kind'), st.get('code')
        if kind not in ('no_access', 'hour_budget', 'day_budget'):
            # Nur GESENDETE Calls buchen — exakt wie _api_get seine eigenen
            # Zähler erst hinter den Gates füllt.
            _lb_budget_book()
            calls += 1
        facts = landing_report_parse(resp)
        if facts:
            _lb_shared_put(key, facts)      # NUR Flug-Fakten (Whitelist)
            _lb_self_put(token, key, facts.get('self_landed'))
            legs.append(_lb_leg_row(c, facts, facts.get('self_landed'), 'ok'))
            continue
        if kind == 'ok' or (kind == 'http' and code == 404):
            # LH kennt das Leg, hat aber (noch) keinen Report: response:null
            # bzw. 404. Das ist „noch nicht da" — NICHT „nicht gelandet".
            status = 'pending'
        elif kind in ('hour_budget', 'day_budget'):
            # Der GLOBALE lhfo-Deckel hat zugemacht — der Rest des Fensters
            # bekommt dieselbe ehrliche Antwort statt 30 Leerläufe.
            status = stopped = 'budget'
            log.warning('[lh_flightops] Flugbuch-Abgleich gestoppt (%s) — '
                        'Rest des Fensters als status=budget', kind)
        elif kind == 'no_access':
            # Grant zwischen Auth-Gate und Call gestorben — ehrlich 'error',
            # aber weiterfragen bringt nichts.
            status = stopped = 'error'
        else:
            status = 'error'
        legs.append(_lb_leg_row(c, shared, None, status))

    used = _lb_day_used()
    return jsonify({'ok': True, 'days': days, 'legs': legs, 'calls': calls,
                    'budget': {'key': _lb_budget_key(), 'used': used,
                               'ceiling': _LB_DAY_CEILING,
                               'max_calls_per_request': _LB_MAX_CALLS,
                               'stopped': bool(stopped),
                               'stop_reason': stopped}})


# ═════════════════════════════════════════════════════════════════════════════
# PLAN-BLOCKZEITEN-BACKFILL (Task #23, Befund 2026-07-31)
# ═════════════════════════════════════════════════════════════════════════════
# Der Owner-Report Juli sagt 57:35, unsere Karte sagte 56:24. Ursache ist NICHT
# unsere Rechnung: LH mutiert die duty_events-Zeiten selbst IN PLACE, unser
# Import ersetzt die Zeile — der PLAN wurde nie erfasst (s. Banner über
# `_preserve_plan_times` in app.py). Ab jetzt schreibt der Import ihn beim
# ERSTEN Sehen mit; für alles, was VOR diesem Fix gelaufen ist, gibt es genau
# eine belastbare Quelle:
#
#   COMMON_FLIGHT_LEG_DETAILS.scheduledTimeOfDeparture/-Arrival
#   == die Zeiten des Released Reports, exakt (verifiziert 31.07.).
#
# BEWUSST KEIN MASSEN-BACKFILL. Der Tagesdeckel liegt bei 5.900 lhfo-Calls für
# die ganze Flotte; ein Leg-Detail-Call pro Leg × ~690 verbundene User × Monate
# wäre ein zweiter Grant-Burn. Deshalb:
#   1. NUR auf Anforderung (dieser Endpoint), nie aus Cron/Hintergrund.
#   2. Eigener Tagesdeckel `lhfoD-plan:<YYYYMMDD>` — schützt die ANDEREN
#      Verbraucher vor diesem hier (Muster: `lhfoD-landing:`).
#   3. Harter Deckel pro Aufruf + 0,7 s Abstand.
#   4. Nur Legs, die (a) in der VERGANGENHEIT liegen und (b) noch KEINE
#      gespeicherte Plan-Zeit haben. Ein Leg mit Plan wird nie nachgekauft.
#   5. Geteilter Disk-Cache OHNE TTL: die Plan-Zeit eines vergangenen Legs
#      ändert sich per Definition nie mehr. Zwei Kollegen desselben Legs
#      teilen sie legitim (reine Flug-Fakten, kein Personenbezug).
_PB_BUDGET_PREFIX = 'lhfoD-plan:'
_PB_DAY_CEILING = 300            # eigener Tagesdeckel dieses Verbrauchers
_PB_MAX_CALLS = 40               # harter Deckel je Endpoint-Aufruf
_PB_SPACING_S = 0.7
_PB_MAX_WINDOW_DAYS = 62         # zwei Monate am Stück, mehr nie auf einmal
_PB_SHARED_MAX = 6000
_PB_KEYS = ('sched_dep_iso', 'sched_arr_iso')

_pb_day_memo = {'ts': 0.0, 'day': '', 'used': 0}
_pb_day_local = {'day': '', 'n': 0}


def _pb_budget_key(now=None):
    return _PB_BUDGET_PREFIX + time.strftime('%Y%m%d', time.gmtime(now))


def _pb_day_used(now=None):
    """Tagesstand des EIGENEN Plan-Backfill-Zählers (Muster `_lb_day_used`:
    max aus persistiertem Stand und dem, was dieser Prozess seit dem letzten
    Flush gebucht hat). Wirft nie."""
    day = time.strftime('%Y%m%d', time.gmtime(now))
    if _pb_day_local['day'] != day:
        _pb_day_local['day'], _pb_day_local['n'] = day, 0
    ts = time.time()
    if _pb_day_memo['day'] != day or (ts - _pb_day_memo['ts']) >= _LB_DAY_MEMO_S:
        try:
            from blueprints.aerox_data_blueprint import _budget_key_used
            used = int(_budget_key_used(_pb_budget_key(now)) or 0)
        except Exception:
            used = 0
        _pb_day_memo['ts'], _pb_day_memo['day'] = ts, day
        _pb_day_memo['used'] = used
    return max(_pb_day_memo['used'], _pb_day_local['n'])


def _pb_budget_book(now=None):
    day = time.strftime('%Y%m%d', time.gmtime(now))
    if _pb_day_local['day'] != day:
        _pb_day_local['day'], _pb_day_local['n'] = day, 0
    _pb_day_local['n'] += 1
    try:
        from blueprints.lh_open_api import budget_inc_key
        budget_inc_key(_pb_budget_key(now))
    except Exception:
        pass


def _pb_iso_z(v):
    """LH-Zeitstempel → '…Z'-ISO oder None. LH liefert hier durchgängig UTC mit
    'Z' (Fixture `flightops_COMMON_FLIGHT_LEG_DETAILS.json`); ein Wert ohne
    Zonenangabe wird als UTC gelesen, aber NIE geraten-verschoben."""
    s = str(v or '').strip()
    if len(s) < 16 or 'T' not in s:
        return None
    if s.endswith('Z'):
        return s
    if s[-6] in '+-' and s[-3] == ':':
        return s
    return s + 'Z'


def flight_leg_details_plan(resp):
    """COMMON_FLIGHT_LEG_DETAILS-Response → Plan-Zeiten. PURE/testbar.

        {'sched_dep_iso', 'sched_arr_iso', 'dep', 'arr', 'tail'}

    Gibt None, wenn KEINE der beiden Plan-Zeiten dasteht — ein halber
    Datensatz ist hier kein Fund, sondern eine Einladung zum Raten."""
    if not isinstance(resp, dict):
        return None
    d = _pb_iso_z(resp.get('scheduledTimeOfDeparture'))
    a = _pb_iso_z(resp.get('scheduledTimeOfArrival'))
    if not (d or a):
        return None
    out = {'sched_dep_iso': d, 'sched_arr_iso': a,
           'dep': (resp.get('departureAirport') or '').upper() or None,
           'arr': (resp.get('arrivalAirport') or '').upper() or None,
           'tail': (resp.get('aircraftRegistration') or '').upper() or None}
    return out


def _pb_key(flight, date, dep, arr):
    return '%s|%s|%s|%s' % ((flight or '').upper().replace(' ', ''),
                            (date or '')[:10], (dep or '').upper(),
                            (arr or '').upper())


def _pb_shared_path():
    return os.path.join(_flow_dir(), 'foplan_shared.json')


def _pb_shared_get(key):
    """Plan-Zeiten eines vergangenen Legs aus dem geteilten Cache oder None.
    KEIN TTL — die Plan-Zeit eines gewesenen Legs ist unveränderlich."""
    e = _lb_json_load(_pb_shared_path()).get(key)
    if not isinstance(e, dict):
        return None
    row = {k: e[k] for k in _PB_KEYS if e.get(k)}
    return row or None


def _pb_shared_put(key, plan, now=None):
    if not (key and isinstance(plan, dict)):
        return
    row = {k: plan[k] for k in _PB_KEYS if plan.get(k)}
    if not row:
        return
    row['ts'] = now if now is not None else time.time()
    d = _lb_json_load(_pb_shared_path())
    d[key] = row
    if len(d) > _PB_SHARED_MAX:
        d = dict(sorted(d.items(), key=lambda kv: float(
            (kv[1] or {}).get('ts') or 0), reverse=True)[:_PB_SHARED_MAX])
    _lb_json_save(_pb_shared_path(), d)


def _pb_block_min(sec):
    """Plan-Blockminuten EINES Sektors aus sched_dep/sched_arr. None, wenn der
    Plan fehlt oder unplausibel ist (>20 h = kein Leg, sondern ein Parser-
    Unfall; dieselbe Grenze wie im iOS-`TariffHoursBuilder`)."""
    try:
        d = (sec or {}).get('sched_dep_iso')
        a = (sec or {}).get('sched_arr_iso')
        if not (d and a):
            return None
        dd = _dt.datetime.fromisoformat(str(d).replace('Z', '+00:00'))
        aa = _dt.datetime.fromisoformat(str(a).replace('Z', '+00:00'))
        m = int(round((aa - dd).total_seconds() / 60.0))
        if m < 0:
            m += 24 * 60                      # Tageswechsel im Feed
        return m if 0 < m <= 20 * 60 else None
    except Exception:
        return None


def _pb_is_flight_sector(sec):
    """Nur echte, selbst geflogene Legs zählen in die Plan-Summe: eine Strecke
    und KEIN Deadhead (`dh` kommt aus der Gratis-Ernte, Welle 0). Der Released
    Report zählt Deadheads ebenfalls nicht als Blockzeit."""
    s = sec or {}
    return bool(s.get('from') and s.get('to')
                and s.get('from') != s.get('to') and not s.get('dh'))


def _pb_collect(briefings, ymd_from, ymd_to, now=None):
    """(alle Flug-Sektoren im Fenster, offene Backfill-Kandidaten). PURE.

    Kandidat ist ein Sektor, dem die Plan-Zeit fehlt UND dessen Abflug schon
    vorbei ist — für die Zukunft schreibt der Import den Plan selbst (write-
    once, s. app.py), da wäre ein LH-Call reine Verschwendung."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    all_secs, todo = [], []
    for d in sorted((briefings or {}).keys()):
        if not (isinstance(d, str) and ymd_from <= d[:10] <= ymd_to):
            continue
        day = briefings.get(d)
        if not isinstance(day, dict):
            continue
        for s in (day.get('ical_sectors') or []):
            if not (isinstance(s, dict) and _pb_is_flight_sector(s)):
                continue
            all_secs.append((d, s))
            if s.get('sched_dep_iso') and s.get('sched_arr_iso'):
                continue
            try:
                dep = _dt.datetime.fromisoformat(
                    str(s.get('dep_iso') or '').replace('Z', '+00:00'))
                if dep.tzinfo is None:
                    dep = dep.replace(tzinfo=_dt.timezone.utc)
            except Exception:
                continue
            if dep >= now:
                continue
            todo.append((d, s))
    return all_secs, todo


def _pb_plan_sum(all_secs):
    """(Plan-Minuten, Legs mit Plan, Legs ohne Plan) über die gesammelten
    Sektoren. Ein Leg OHNE Plan wird NICHT aus dep_iso/arr_iso rekonstruiert —
    dann stünde eine Ist-Zeit als „Plan" in der Abrechnungsreferenz."""
    total, have, miss = 0, 0, 0
    for _d, s in all_secs or []:
        m = _pb_block_min(s)
        if m is None:
            miss += 1
        else:
            total += m
            have += 1
    return total, have, miss


def _pb_hhmm(minutes):
    try:
        m = int(minutes or 0)
        return '%d:%02d' % (m // 60, m % 60)
    except Exception:
        return '0:00'


@lh_flightops_bp.route('/api/lh/flightops/plan-backfill/<token>',
                       methods=['POST'])
def flightops_plan_backfill(token):
    """Plan-Blockzeiten vergangener Legs aus COMMON_FLIGHT_LEG_DETAILS
    nachtragen — ON DEMAND, gedrosselt, nie im Hintergrund (s. Banner oben).

    Body: {"from": "YYYY-MM-DD", "to": "YYYY-MM-DD"} (max 62 Tage).
    Ohne Angabe: der laufende Kalendermonat.

    Antwort:
        {ok, from, to, legs: [{datum, flight, from, to, status,
                               sched_dep_iso, sched_arr_iso, block_min}],
         calls, written,
         plan: {block_min, block_hhmm, legs_with_plan, legs_without_plan,
                legs_total}}
    `status`: 'ok' (frisch von LH) · 'cache' (geteilter Plan-Cache) ·
    'have' (Plan lag schon in der Zeile) · 'future' (Abflug noch offen — der
    Import stempelt selbst) · 'not_found' (LH kennt das Leg nicht mehr) ·
    'budget'/'skipped_budget' · 'error'.

    Der Plan-Block wird NIE aus dep_iso/arr_iso rekonstruiert: diese Werte
    sind nach dem Flug LH-seitig Ist-nah, und ein synthetisierter Plan in
    einer Abrechnungsreferenz wäre schlimmer als eine Lücke."""
    _st, _acc = _access_state(token)
    if _st == 'pending':
        return jsonify({'ok': False, 'error': 'token_refresh_pending'}), 503
    if _st != 'ok':
        return jsonify({'ok': False, 'error': 'not_connected'}), 401
    b = request.get_json(silent=True) or {}
    today = _dt.datetime.now(_dt.timezone.utc).date()
    f_raw = str(b.get('from') or '')[:10]
    t_raw = str(b.get('to') or '')[:10]
    try:
        d_from = (_dt.date.fromisoformat(f_raw) if f_raw
                  else today.replace(day=1))
        d_to = _dt.date.fromisoformat(t_raw) if t_raw else today
    except ValueError:
        return jsonify({'ok': False, 'error': 'bad_range'}), 400
    if d_to < d_from:
        return jsonify({'ok': False, 'error': 'bad_range'}), 400
    if (d_to - d_from).days + 1 > _PB_MAX_WINDOW_DAYS:
        return jsonify({'ok': False, 'error': 'range_too_wide',
                        'max_days': _PB_MAX_WINDOW_DAYS}), 400
    ymd_from, ymd_to = d_from.isoformat(), d_to.isoformat()

    import app as _app
    try:
        briefings = dict(_app._ical_briefings_load(token) or {})
    except Exception as e:
        log.warning('[lh_flightops] plan-backfill load: %s', type(e).__name__)
        return jsonify({'ok': False, 'error': 'briefings_unavailable'}), 503

    all_secs, todo = _pb_collect(briefings, ymd_from, ymd_to)
    todo_ids = {id(s) for _d, s in todo}
    rows, calls, written, stopped = [], 0, 0, None

    for d, s in all_secs:
        flight = re.sub(r'\s+', '', str(s.get('flight') or '')).upper()
        row = {'datum': d, 'flight': flight or None,
               'from': s.get('from'), 'to': s.get('to'),
               'sched_dep_iso': s.get('sched_dep_iso'),
               'sched_arr_iso': s.get('sched_arr_iso'),
               'block_min': _pb_block_min(s)}
        if id(s) not in todo_ids:
            row['status'] = ('have' if (s.get('sched_dep_iso')
                                        and s.get('sched_arr_iso'))
                             else 'future')
            rows.append(row)
            continue
        if not flight:
            row['status'] = 'error'
            rows.append(row)
            continue
        key = _pb_key(flight, d, s.get('from'), s.get('to'))
        cached = _pb_shared_get(key)
        if cached:
            s.update(cached)
            written += 1
            row.update(cached)
            row['block_min'] = _pb_block_min(s)
            row['status'] = 'cache'
            rows.append(row)
            continue
        if stopped:
            row['status'] = stopped
            rows.append(row)
            continue
        if calls >= _PB_MAX_CALLS:
            row['status'] = 'skipped_budget'
            rows.append(row)
            continue
        if _pb_day_used() >= _PB_DAY_CEILING:
            log.warning('[lh_flightops] plan-Tagesdeckel %s >= %s — Backfill '
                        'stoppt (Rest als status=budget)',
                        _pb_day_used(), _PB_DAY_CEILING)
            stopped = 'budget'
            row['status'] = 'budget'
            rows.append(row)
            continue
        if calls and _PB_SPACING_S > 0:
            time.sleep(_PB_SPACING_S)
        _lh = {}
        try:
            resp = flight_leg_details(token, flight, d,
                                      s.get('from'), s.get('to'),
                                      interactive=True, status_out=_lh)
        except Exception as e:
            log.warning('[lh_flightops] plan-backfill %s %s: %s',
                        flight, d, type(e).__name__)
            resp, _lh = None, {'kind': 'error'}
        _kind = _lh.get('kind')
        if _kind not in ('no_access', 'hour_budget', 'day_budget'):
            # Nur GESENDETE Calls buchen — exakt wie im Landing-Report-Pfad.
            _pb_budget_book()
            calls += 1
        plan = flight_leg_details_plan(resp)
        if not plan:
            # LH liefert für ein zu altes/unbekanntes Leg schlicht nichts.
            # Das ist eine Lücke, kein Wert — nichts rekonstruieren.
            if _kind in ('hour_budget', 'day_budget'):
                # Der GLOBALE lhfo-Deckel hat zugemacht — der Rest des
                # Fensters bekommt dieselbe ehrliche Antwort statt Leerläufe.
                row['status'] = stopped = 'budget'
                log.warning('[lh_flightops] plan-backfill gestoppt (%s) — '
                            'Rest des Fensters als status=budget', _kind)
            elif _kind == 'no_access':
                row['status'] = stopped = 'error'
            elif _kind == 'http' and _lh.get('code') == 404:
                row['status'] = 'not_found'
            else:
                row['status'] = ('not_found' if (_kind == 'ok'
                                                 or isinstance(resp, dict))
                                 else 'error')
            rows.append(row)
            continue
        keep = {k: plan[k] for k in _PB_KEYS if plan.get(k)}
        _pb_shared_put(key, keep)
        s.update(keep)
        written += 1
        row.update(keep)
        row['block_min'] = _pb_block_min(s)
        row['status'] = 'ok'
        rows.append(row)

    if written:
        try:
            _app._ical_briefings_save(token, briefings)
        except Exception as e:
            log.warning('[lh_flightops] plan-backfill save: %s',
                        type(e).__name__)
            return jsonify({'ok': False, 'error': 'briefings_persist_failed',
                            'calls': calls}), 503

    total, have, miss = _pb_plan_sum(all_secs)
    log.info('[lh_flightops] plan-backfill %s..%s legs=%d todo=%d calls=%d '
             'written=%d plan=%s', ymd_from, ymd_to, len(all_secs), len(todo),
             calls, written, _pb_hhmm(total))
    return jsonify({'ok': True, 'from': ymd_from, 'to': ymd_to,
                    'legs': rows, 'calls': calls, 'written': written,
                    'plan': {'block_min': total, 'block_hhmm': _pb_hhmm(total),
                             'legs_with_plan': have, 'legs_without_plan': miss,
                             'legs_total': len(all_secs)},
                    'budget': {'key': _pb_budget_key(),
                               'used': _pb_day_used(),
                               'ceiling': _PB_DAY_CEILING,
                               'max_calls_per_request': _PB_MAX_CALLS,
                               'stopped': bool(stopped),
                               'stop_reason': stopped}})


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
    PostgREST kappt bei 1000 → paginiert lesen. Leer ohne SB (Dev).

    KADENZ-HYDRATION (2026-07-30): liest den durablen Sync-Stempel
    metadata->fo_bg_sync_at GLEICH MIT (gleiche Query, keine Zusatz-Kosten)
    und füllt damit die prozess-lokale Kadenz-Map nach Neustarts wieder auf.
    Messbefund 30.07.: der 16:23-Lauf lief mit deferred=0 über 927 User,
    weil ein Worker-Restart zwischen zwei Cron-Läufen die Map geleert hatte
    — die adaptive Kadenz war damit wirkungslos und JEDER Lauf plante die
    volle Flotte ein."""
    try:
        import app as _app
        if not getattr(_app, 'SB_AVAILABLE', False):
            return []
        out, page, size = [], 0, 500
        _now = time.time()
        while len(out) < limit:
            r = (_app.sb.table('user_profiles')
                 .select('token,homebase,metadata->' + _FO_SYNC_STAMP_KEY)
                 .filter('metadata->flightops_tokens', 'not.is', 'null')
                 .range(page * size, page * size + size - 1).execute())
            rows = r.data or []
            for row in rows:
                tok = row.get('token')
                if not tok:
                    continue
                out.append(tok)
                _fo_hydrate_stamp(tok, row.get(_FO_SYNC_STAMP_KEY), _now)
                # Homebase für die „heute/morgen"-Ankerung der Kadenz-Klassen
                # (gleiche Query, keine Zusatz-Kosten; s. _fo_local_today).
                _hb = (row.get('homebase') or '').strip().upper()
                if _hb:
                    _fo_homebase[tok] = _hb
            if len(rows) < size:
                break
            page += 1
        return out
    except Exception as e:
        log.warning('[lh_flightops] connected_tokens: %s', type(e).__name__)
        return []


# ── ADAPTIVE SYNC-KADENZ (Quota-Diät 2026-07-28 · Klassen-Ausbau 2026-07-31) ─
# Der Host-Cron ruft refresh-all weiter alle 2 h (unverändert) — aber nicht
# mehr jeder Lauf synct jeden User. Owner-Auftrag 31.07.: „max sparen aber
# ohne mit Qualität der App zu leiden" — bei 988 Grants (Verdopplung in zwei
# Tagen) riss die alte Zwei-Klassen-Kadenz (48-h-Horizont: 3,5 h / sonst
# 11,5 h) den Tagesdeckel trotzdem wieder (gestern 5.203, heute 11:28 UTC
# schon 3.775 — davon duty_events 2.382).
#
# NEU: VIER Klassen, abgeleitet aus dem GESPEICHERTEN Roster (kostet nie
# einen LH-Call) und bei JEDEM Lauf NEU berechnet (nie gecacht — nur so
# erkennt der nächste Lauf selbst, dass ein User von langsam auf schnell
# gewechselt ist, weil im letzten Sync neuer Dienst auftauchte):
#   fast_sb  Standby/Reserve heute/morgen ohne zugewiesene Legs → 1,9 h
#            (= jeder Cron-Lauf). Abruf-Risiko: der Roster dieses Users kann
#            sich JEDE Stunde materiell ändern.
#   fast     Dienst (Legs/Layover/Office) heute oder morgen → 3,5 h
#            (effektiv alle 4 h — identisch zur bisherigen near-Klasse,
#            per Definition KEINE Qualitäts-Regression für Dienst-User).
#   mid      Dienst in 2–7 Tagen ODER Roster-Abdeckung endet in ≤7 Tagen
#            (Monats-Veröffentlichung: da wollen alle frische Daten) → 11,5 h
#            (effektiv alle 12 h — exakt die alte far-Kadenz, also keine
#            Regression; DB-Messung 31.07.: 395 von 991 Grants sind mid,
#            bei 7,5 h wären das allein 1.185 Calls/Tag statt 790. Wird der
#            Dienst „morgen", wechselt der User beim nächsten Lauf von
#            selbst auf fast — die Klasse wird ja pro Lauf neu gerechnet).
#   slow     kein Dienst im 7-Tage-Horizont (Urlaub, langer Off-Block,
#            Roster reicht weit) → 21,5 h (effektiv 1×/Tag). LH pflegt Tage
#            voraus, und App-Öffnen liefert via Demand-Poke ohnehin sofort
#            frisch — der User merkt davon nichts.
# VERBINDUNGS-WAHRHEIT (Owner explizit): Grants hält der Ein-Refresher am
# Leben (oauth-Host, zählt NICHT aufs Gateway-Kontingent). Roster-Pulls sind
# reine Daten-Frische — NULL Login-Risiko durch diese Reduktion.
# FAIL-SAFE: Einstufung nicht bestimmbar (kaputter Roster, leerer Store,
# Fehler) ⇒ fast. Lieber ein Call zu viel als ein staler Dienstplan.
# ZEITZONEN (teuerste Fehlerklasse): Kadenz-Alter rechnet in UTC-Epochen;
# WELCHER Kalendertag „heute"/„morgen" ist, entscheidet die HOMEBASE-Zone
# des Users (s. _fo_local_today) — nie die Container-Lokalzeit.
# KOPPLUNG Crewlist-Prefetch / Pickup-Rotation: strukturell über die
# Horizonte — der Prefetch nimmt nur Legs der nächsten 3 Tage
# (_CREW_PREFETCH_DAYS), die Pickup-Rotation nur Umläufe in 30/36 h
# (_ROT_PICKUP_HORIZON_H). Ein slow-User hat dort per Definition nichts,
# ein seltener synct also automatisch auch seltener Zugaben. Bewusst KEIN
# zusätzliches Klassen-Gate in flightops_import: taucht im frisch geholten
# Roster NEUER naher Dienst auf, sollen die Zugaben sofort mitlaufen —
# ein Gate auf der (dann veralteten) Klasse würde genau das verhindern.
# `pickup_last_good` bleibt unangetastet (Pickup-Löschbug 29.07.).
# ══ NACHGEZOGEN 10.08.2026 — die Sparsamkeit war gegen ein Phantom ═════════
# Diese vier Zahlen wurden am 28./31.07. immer weiter gestreckt, um einen
# Tagesdeckel von 6.000 zu halten, den es nie gab (s. Korrektur oben: LH nennt
# 20.000/Stunde und 20/Sekunde, kein Tageslimit). Die Kosten trug der Nutzer:
# ein Kollege ohne Dienst im 7-Tage-Horizont sah seinen Roster nur noch
# EINMAL AM TAG frisch.
#
# GERECHNET, nicht geschätzt (1.603 Grants, Cron alle 2 h):
#   Extremfall „jeder bei JEDEM Lauf" = 19.236 Calls/Tag = 802/Stunde
#     → 4 % der erlaubten 20.000/h
#   Burst eines Laufs @0,7-s-Takt = 1,4 Calls/s
#     → 7 % des erlaubten 20/s, Faktor 14 Abstand
# Selbst das Maximum bliebe also zweistellig unter beiden Grenzen. Die Werte
# unten sind bewusst NICHT das Maximum — sie halten den Abstand, den Alex'
# „mit den optimierten Requests" verdient, und lassen Raum fürs Wachstum.
#
# Die Klassen-LOGIK bleibt unverändert (sie war nie das Problem): abgeleitet
# aus dem GESPEICHERTEN Roster, kostet also keinen LH-Call, und wird bei jedem
# Lauf neu berechnet — wer von langsam auf schnell wechselt, merkt es beim
# nächsten Lauf selbst.
# ⚠️ ZURÜCKGENOMMEN 10.08.2026 ABENDS — VORFALL. Die oben gerechnete
# Verschärfung (1,9/1,9/3,9/11,9) verdoppelte den Tagesverbrauch von ~5.500 auf
# 10.025, und ab 20:00 UTC antwortete LH auf JEDEN Duty-Events-Call mit HTTP 403
# — auch auf interaktive, auch für den Owner selbst. Die STUNDE lag dabei bei 52
# von 20.000. Es gibt also sehr wahrscheinlich doch eine Tagesgrenze, die in
# Alex' Mail nicht vorkam (sie nannte nur 20.000/h und 20/s).
#
# Lehre, schon wieder dieselbe: eine Limit-Zusage ist eine Aussage über EINE
# Dimension. Dass die anderen unbegrenzt sind, folgt daraus nicht — und der
# Beweis kostete eine Nacht ohne Roster-Sync für die ganze Flotte.
#
# Zurück auf die Werte, die nachweislich vier Wochen ohne 403 liefen.
_FO_SYNC_FAST_SB_S = 1.9 * 3600
_FO_SYNC_FAST_S = 3.5 * 3600
_FO_SYNC_MID_S = 11.5 * 3600
_FO_SYNC_SLOW_S = 21.5 * 3600
_FO_FAST_MAX_D = 1               # Diensttag heute/morgen (Homebase-Kalender)
_FO_MID_MAX_D = 7                # Dienst in 2–7 Tagen
_FO_ROSTER_END_GUARD_D = 7       # Abdeckung endet in ≤7 Tagen → mind. mid
# Letzter Sync je Token. Die Prozess-Map ist der Schnellpfad; DURABEL lebt
# der Stempel seit 2026-07-30 zusätzlich im Profil (metadata.fo_bg_sync_at,
# atomarer Merge) und wird in _connected_tokens kostenlos mitgelesen.
# WARUM: „nach einem Deploy einmal alle synchronisieren ist billig" stimmte
# nicht — der Poll-Worker startet auch ZWISCHEN Deploys neu (gunicorn-Recycle,
# OOM), und jede leere Map heißt volle Flotte „first" = 900+ Extra-Imports.
# Gemessen 30.07.: 16:23-Lauf mit deferred=0 über 927 User, Tagesdeckel 5000
# um 16:49 gerissen, Abend-Primetime flottenweit ohne Hintergrund-Sync.
_fo_last_sync = {}
_FO_LAST_SYNC_CAP = 8000
_FO_SYNC_STAMP_KEY = 'fo_bg_sync_at'
# Homebase je Token (IATA) — in _connected_tokens KOSTENLOS mitgelesen
# (gleiche user_profiles-Query). Nur für die „heute/morgen"-Ankerung der
# Kadenz-Klassen; Größe ist durch die Grant-Zahl beschränkt (≤ limit).
_fo_homebase = {}
# Wellen-Steuerung des Laufs (ersetzt den blinden 120-s-Demand-Vorlauf):
# harte Lauf-Obergrenze deutlich unter dem 2-h-Cron-Takt, Pause zwischen zwei
# Wellen etwas über dem Refresher-Tick (60 s), Stall-Limit gegen Grants, deren
# Rotation im Backoff hängt (bis 1 h — darauf wartet kein Lauf).
_FO_RUN_DEADLINE_S = 90 * 60
_FO_WAVE_WAIT_S = 75.0
_FO_WAVE_STEP_S = 5.0
_FO_WAVE_STALL_MAX = 3
# Sanfte Schrittweite zwischen zwei _access_state-Reads auf pending-Grants
# (reiner Supabase-Read — aber 400+ davon ohne Pause wären eine Lastspitze).
_FO_PENDING_CHECK_GAP_S = 0.05
# Tages-Reserve für die lockerste Kadenz-Klasse (slow): User OHNE Dienst im
# 7-Tage-Horizont syncen nur, solange bis zum Hintergrund-Tagesdeckel
# mindestens diese Zahl Calls frei ist — das Restbudget gehört den Usern mit
# nahem Dienst (deren Roster ist das Kernprodukt, s. Kadenz-Banner oben).
_FO_FAR_DAY_HEADROOM = 800


def _fo_day_has_duty(ev):
    """Trägt dieser Briefing-Tag Flug-/Dienst-Evidenz? Nutzt AUSSCHLIESSLICH
    Felder, die die Briefing-Row wirklich führt (ical_sectors, ical_klass,
    ical_summary) — nichts Erfundenes. Wirft nie."""
    if not isinstance(ev, dict):
        return False
    secs = ev.get('ical_sectors')
    if isinstance(secs, list) and secs:
        return True                       # echte Legs = Dienst, fertig
    if is_cancelled_standby_marker(ev.get('ical_summary')):
        return False                      # SCU = gestrichener Standby
    klass = str(ev.get('ical_klass') or '').strip().lower()
    if klass in ('hotel_layover', 'standby'):
        return True                       # unterwegs bzw. Bereitschaft
    try:
        from blueprints.crew_live_state import duty_from_roster_day
        d = duty_from_roster_day(ev.get('ical_klass'), ev.get('ical_summary'))
    except Exception:
        d = None
    # ÜBERNACHT-SPLIT-Zeilen („(Tag 2/2)", SFO-FRA-Ankunftstag u.ä.) tragen
    # oft KEINE ical_sectors — der Flug steht nur in der Summary. Ein
    # IATA-Paar (XXX-YYY) dort ist Dienst-Evidenz. Beleg (DB 31.07.): der
    # Owner-Flugtag 'LH 455: SFO-FRA (Tag 2/2) · X' hatte sectors=leer und
    # klass=None — ohne diesen Fallback wäre ein FLUGTAG als frei eingestuft
    # worden. Nur wenn duty_from_roster_day nichts erkannt hat (None): ein
    # explizites 'free'/'vacation'/'visa' bleibt Nicht-Dienst.
    if d is None and re.search(r'(?<![A-Z])[A-Z]{3}-[A-Z]{3}(?![A-Z])',
                               str(ev.get('ical_summary') or '').upper()):
        return True
    # 'free'/'vacation'/'visa' sind explizit KEIN Dienst; None heißt „nicht
    # erkannt" und gilt hier als kein Nachweis (die Kadenz fällt dann auf die
    # lockere Regel zurück — sie verschiebt nur, sie verliert nichts).
    return d in ('standby', 'reserve')


def _fo_day_is_standby(ev):
    """Standby-/Reserve-Evidenz OHNE zugewiesene Legs. Sind schon Legs am
    Tag, ist der Abruf passiert — dann gilt die normale fast-Klasse.
    RB zählt als Reserve (Anita, Forum 22.07.); die SB-Substring-Falle
    (LISBOA u.ä.) ist in duty_from_roster_day gelöst — dort matchen nur
    SBY/STANDBY/STBY bzw. RB als ganzes Wort. Wirft nie."""
    if not isinstance(ev, dict):
        return False
    secs = ev.get('ical_sectors')
    if isinstance(secs, list) and secs:
        return False
    if is_cancelled_standby_marker(ev.get('ical_summary')):
        return False
    if str(ev.get('ical_klass') or '').strip().lower() == 'standby':
        return True
    try:
        from blueprints.crew_live_state import duty_from_roster_day
        return duty_from_roster_day(
            ev.get('ical_klass'), ev.get('ical_summary')) in ('standby',
                                                              'reserve')
    except Exception:
        return False


def _fo_local_today(token, now):
    """Kalenderdatum „heute" in der HOMEBASE-Zeitzone des Users (date-Objekt).
    Das Kadenz-ALTER rechnet in UTC-Epochen; nur die Frage „ist der Dienst
    heute/morgen?" braucht den Kalender des Users — und der tickt an seiner
    Homebase (Zeitzonen-Fehlerklasse: 22–24 UTC ist Berlin schon am
    Folgetag). Fallback Europe/Berlin (LH-Homebases sind DE), dann UTC.
    NIE die Container-/Geräte-Lokalzeit. Wirft nie."""
    from datetime import datetime as _d, timezone as _tz
    tzname = None
    try:
        hb = (_fo_homebase.get(token) or '').strip().upper()
        if hb:
            from airport_tz import airport_tz as _atz
            tzname = _atz(hb)
    except Exception:
        tzname = None
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tzname or 'Europe/Berlin')
    except Exception:
        tz = _tz.utc
    return _d.fromtimestamp(now, tz).date()


def _fo_cadence_class(token, now=None):
    """→ (klasse, tage_bis_dienst|None) aus dem GESPEICHERTEN Roster.
    klasse ∈ fast_sb | fast | mid | slow (Bedeutung im Kadenz-Banner).
    Liest user_ical_briefings (Supabase/Disk — kostet KEINEN LH-Call) und
    wird pro Lauf neu gerechnet, nie gecacht. FAIL-SAFE: jeder Fehler und
    ein leerer Store (Neuverbindung!) ⇒ fast — lieber ein Call zu viel als
    ein staler Dienstplan. Wirft nie."""
    now = now or time.time()
    try:
        import app as _app
        briefs = _app._ical_briefings_load(token)
    except Exception:
        return 'fast', None
    if not isinstance(briefs, dict) or not briefs:
        return 'fast', None
    try:
        from datetime import date as _date
        today = _fo_local_today(token, now)
        next_days, sb_near, last_day = None, False, None
        for datum, ev in briefs.items():
            try:
                d = _date.fromisoformat(str(datum)[:10])
            except Exception:
                continue
            if last_day is None or d > last_day:
                last_day = d
            delta = (d - today).days
            if delta < 0:
                continue
            if _fo_day_has_duty(ev):
                if next_days is None or delta < next_days:
                    next_days = delta
                if delta <= _FO_FAST_MAX_D and _fo_day_is_standby(ev):
                    sb_near = True
        if last_day is None:
            return 'fast', None          # nur Müll-Keys → fail-safe
        if next_days is not None and next_days <= _FO_FAST_MAX_D:
            return ('fast_sb' if sb_near else 'fast'), next_days
        # ROSTER-ENDE: die Abdeckung reicht nur noch ≤7 Tage (oder ist schon
        # ganz Vergangenheit) — die Monats-Veröffentlichung steht an, da
        # wollen alle frische Daten. Mindestens mid, nie slow.
        if (last_day - today).days <= _FO_ROSTER_END_GUARD_D:
            return 'mid', next_days
        if next_days is not None and next_days <= _FO_MID_MAX_D:
            return 'mid', next_days
        return 'slow', next_days
    except Exception:
        return 'fast', None


_FO_SYNC_THRESH = {'fast_sb': _FO_SYNC_FAST_SB_S, 'fast': _FO_SYNC_FAST_S,
                   'mid': _FO_SYNC_MID_S, 'slow': _FO_SYNC_SLOW_S}


def _fo_should_sync(token, now=None):
    """(bool, grund) — synct DIESER refresh-all-Lauf diesen Token?
    Erst-Kontakt immer ('first'); sonst entscheidet die Kadenz-Klasse
    (Grund = Klassenname bei fällig, 'skip_<klasse>' bei Aufschub).
    Unter der schnellsten Schwelle (1,9 h) wird der Briefings-Read gespart
    — schneller synct ohnehin niemand."""
    now = now or time.time()
    last = _fo_last_sync.get(token)
    if last is None:
        return True, 'first'
    age = now - last
    if age < _FO_SYNC_FAST_SB_S:
        return False, 'too_soon'
    klass, _nd = _fo_cadence_class(token, now)
    if age >= _FO_SYNC_THRESH.get(klass, _FO_SYNC_FAST_S):
        return True, klass
    return False, 'skip_' + klass


def _fo_mark_synced(token, now=None):
    """Sync-ERFOLG stempeln — im Prozess UND durabel im Profil
    (metadata.fo_bg_sync_at, atomarer Top-Level-Merge, nie der ganze Blob).
    Der durable Stempel repariert das deferred=0-Loch vom 30.07.: die
    Prozess-Map stirbt mit jedem Worker-Restart, und ohne Stempel galt danach
    die GANZE Flotte als „first" — die adaptive Kadenz existierte nur auf dem
    Papier. Gestempelt wird seit dem Review-Fund 2026-07-28 nur der Erfolg:
    ein LH-Schluckauf darf den User nicht 3,5–11,5 h ohne Retry lassen.
    Wirft nie."""
    try:
        now = now or time.time()
        _fo_last_sync[token] = now
        if len(_fo_last_sync) > _FO_LAST_SYNC_CAP:
            for k in sorted(_fo_last_sync,
                            key=lambda k: _fo_last_sync[k])[:_FO_LAST_SYNC_CAP // 2]:
                _fo_last_sync.pop(k, None)
        try:
            import app as _app
            if getattr(_app, 'SB_AVAILABLE', False):
                _app._profile_metadata_merge_sb(
                    token, {_FO_SYNC_STAMP_KEY: int(now)})
        except Exception:
            pass
    except Exception:
        pass


def _fo_hydrate_stamp(token, stamp, now=None):
    """Durablen Sync-Stempel (metadata.fo_bg_sync_at) in die Prozess-Map
    übernehmen. Der neuere Stand gewinnt; Zukunfts- und Müll-Werte werden
    verworfen (ein kaputter Stempel darf einen User nie dauerhaft aus der
    Kadenz drängen). Wirft nie."""
    try:
        ts = float(stamp)
    except (TypeError, ValueError):
        return
    try:
        now = now or time.time()
        if ts <= 0 or ts > now + 3600:
            return
        if ts > (_fo_last_sync.get(token) or 0):
            _fo_last_sync[token] = ts
    except Exception:
        pass


def _fo_background_budget_open(far=False):
    """(offen, grund) — ist im Stunden-/Tages-Gate noch Hintergrund-Budget
    frei? Spiegelt exakt die Deckel aus _api_get (dort fällt weiterhin die
    finale Entscheidung) — hier geht es darum, einen Lauf gar nicht erst
    durch hunderte Imports zu treiben, deren Calls das Key-Gate ohnehin
    verwirft: am 30.07. produzierte genau das 228 `fail` in einem Lauf,
    und der Demand-Vorlauf ließ den Refresher parallel hunderte Grants für
    Imports rotieren, die nie stattfanden.

    `far=True` (Kadenz-Klasse slow) hält zusätzlich _FO_FAR_DAY_HEADROOM
    Calls Abstand zum Tagesdeckel: lockere Roster syncen nur, solange der
    Tag entspannt ist. Im Zweifel (Zählerfehler) offen — das Key-Gate in
    _api_get bleibt die letzte Instanz. Wirft nie."""
    try:
        if _rot_hour_used() >= _LHFO_HOUR_BACKGROUND_CEILING:
            return False, 'budget_hour'
        limit = _LHFO_DAY_BACKGROUND_CEILING - (_FO_FAR_DAY_HEADROOM
                                                if far else 0)
        if _lhfo_day_used() >= limit:
            return False, ('budget_day_far_reserve' if far else 'budget_day')
        return True, ''
    except Exception:
        return True, ''


def _refresh_all_work(tokens):
    """Der 2-h-Hintergrund-Sync — seit 2026-07-30 in WELLEN statt mit blindem
    120-s-Demand-Vorlauf.

    MESSBEFUND 30.07. (16:23-UTC-Lauf): 927 geplant, 570 Grants abgelaufen,
    nach 120 s Wartezeit waren erst 138 rotiert — der Refresher braucht für
    570 Grants ~24 min (~2,5 s/Grant), der Importer wartete 120 s. Ergebnis:
    561 skipped, und die trotzdem angestoßenen Rotationen liefen für Imports,
    die nie stattfanden. Dazu 228 `fail` ohne Grund-Aufschlüsselung (großteils
    Tagesdeckel-Verwurf im Key-Gate, s. _fo_background_budget_open).

    NEU: Welle 1 importiert sofort alle Grants mit gültigem AT und meldet
    abgelaufene beim Refresher an (die Import-Arbeit IST die Wartezeit);
    jede Folgewelle nimmt die inzwischen rotierten mit. Budget-Stopp bricht
    den Lauf ab statt hunderte Zombie-Fails zu produzieren, und jeder fail/
    skip trägt seinen Grund in die done-Zeile (nie wieder „228 fail und
    niemand weiß warum")."""
    ok = fail = skipped = deferred = 0
    fail_reasons, skip_reasons, waves = {}, {}, 0
    due_classes, defer_reasons = {}, {}
    try:
        import app as _app
        _now0 = time.time()
        _deadline = _now0 + _FO_RUN_DEADLINE_S

        def _note_skip(reason, n=1):
            nonlocal skipped
            skipped += n
            skip_reasons[reason] = skip_reasons.get(reason, 0) + n

        def _note_fail(reason):
            nonlocal fail
            fail += 1
            fail_reasons[reason] = fail_reasons.get(reason, 0) + 1

        # KADENZ-PLANUNG vor allem anderen: was dieser Lauf ohnehin nicht
        # synct, braucht auch keinen Demand und keine Rotation. Reihenfolge
        # nach Dringlichkeit: Standby/Dienst heute-morgen zuerst, dann
        # Erstkontakt, dann mid, zuletzt slow — wird das Budget knapp,
        # trifft es die User, denen Frische am wenigsten fehlt.
        _prio = {'fast_sb': 0, 'fast': 0, 'first': 1, 'mid': 2, 'slow': 3}
        plan = []
        for _tok in tokens:
            _do, _why = _fo_should_sync(_tok, _now0)
            if _do:
                plan.append((_prio.get(_why, 2), _tok, _why))
                due_classes[_why] = due_classes.get(_why, 0) + 1
            else:
                deferred += 1
                defer_reasons[_why] = defer_reasons.get(_why, 0) + 1
        plan.sort(key=lambda e: e[0])
        # TRANSPARENZ (Owner-Tagesreport): aufgeschobene Syncs je Grund in
        # die ax_api_budget-Zähler (lhfo_skip:<YYYYMMDDHH>[:<grund>]) —
        # gebatcht, EIN Increment pro Grund und Lauf. Plus eine Log-Zeile
        # mit der Klassen-Verteilung dieses Laufs.
        try:
            from blueprints.lh_open_api import budget_inc
            for _r, _n in defer_reasons.items():
                budget_inc('lhfo_skip', _r, units=_n)
        except Exception:
            pass
        log.info('[flightops-refresh-all] kadenz users=%d due=%s deferred=%s',
                 len(tokens), due_classes or '{}', defer_reasons or '{}')
        remaining = [(t, w) for _p, t, w in plan]
        demanded = set()
        stall = 0
        aborted = None
        while remaining and aborted is None:
            waves += 1
            next_wave, progressed = [], False
            for i, (tok, why) in enumerate(remaining):
                # DEPLOY-DRAIN (Grant-Burn #3, 2026-07-26): dieser Daemon-
                # Thread wurde beim Container-Recreate HART gekillt — traf der
                # Kill das Fenster zwischen LH-Rotation und _tokens_save, war
                # der neue Refresh-Token weg und der naechste Versuch
                # verbrannte per Reuse-Detection die ganze Familie (29/126
                # Grants, Cluster exakt an den Deploy-Zeitpunkten).
                # deploy-hetzner.sh setzt vor dem Recreate das drain-Flag und
                # wartet bis running=False — hier deshalb VOR jedem Grant
                # pruefen und sauber abbrechen (der aktuelle Grant persistiert
                # fertig, kein neuer LH-Call startet).
                if _refresh_all_state.get('drain'):
                    aborted = 'drain'
                else:
                    # BUDGET-STOPP: ist das Hintergrund-Budget zu, würde JEDER
                    # weitere Call im Key-Gate verworfen (16:23-Lauf 30.07.:
                    # 200+ sinnlose fails). slow-User halten zusätzlich die
                    # Tages-Reserve für die Dienst-in-Sicht-Klassen frei — sie
                    # werden einzeln übersprungen, der Lauf läuft weiter.
                    _open, _bwhy = _fo_background_budget_open(
                        far=(why == 'slow'))
                    if not _open:
                        if _bwhy == 'budget_day_far_reserve':
                            _note_skip(_bwhy)
                            continue
                        aborted = _bwhy
                if aborted:
                    next_wave.extend(remaining[i:])
                    break
                try:
                    _st, _acc = _access_state(tok)
                    if _st == 'pending':
                        # AT abgelaufen: EINMAL beim Refresher anmelden (falls
                        # dessen Demand-Deckel Platz hat) und in die nächste
                        # Welle — importiert wird, sobald die Rotation durch
                        # ist. Rotiert wird hier NIE selbst (Umbau 2026-07-27).
                        if tok not in demanded and _refresher_demand_add(tok):
                            demanded.add(tok)
                            # Best-effort auch übers Netz — falls refresh-all
                            # je aus einem Container ohne Refresher läuft.
                            _rotate_poke_remote(tok)
                        next_wave.append((tok, why))
                        time.sleep(_FO_PENDING_CHECK_GAP_S)
                        continue
                    if _st != 'ok':
                        # needs_relogin / Tokens weg — heilt nur ein Re-Login.
                        _note_skip('disconnected')
                        continue
                    # background-Flag → niedrigere Budget-Grenze im Key-Gate
                    # (interaktive Connects/Refreshes behalten Headroom).
                    with _app.app.test_request_context(json={'background': 1}):
                        rv = flightops_import(tok)
                    status = rv[1] if isinstance(rv, tuple) else 200
                    progressed = True
                    if status == 200:
                        ok += 1
                        # NUR Erfolg stempelt den Sync (Review-Fund
                        # 2026-07-28): ein LH-Schluckauf darf den User nicht
                        # 3,5–11,5 h ohne Retry lassen — Fehlläufe bleiben
                        # fällig für den nächsten 2-h-Cron-Lauf.
                        _fo_mark_synced(tok)
                    else:
                        _reason = 'http_%s' % status
                        try:
                            _body = rv[0] if isinstance(rv, tuple) else rv
                            _get = getattr(_body, 'get_json', None)
                            _payload = _get(silent=True) if _get else None
                            _reason = ((_payload or {}).get('error')
                                       or _reason)
                        except Exception:
                            pass
                        _note_fail(_reason)
                except Exception as e:
                    _note_fail(type(e).__name__)
                    log.warning('[flightops-refresh-all] tok=%s %s',
                                (tok or '')[:8], type(e).__name__)
                # Service-QPS-Schonung (Sandbox zeigte 2/sec pro Service → 403
                # „Developer Over Qps"; Prod-Plan 20/sec, trotzdem sanft).
                time.sleep(0.7)
            if aborted:
                log.info('[flightops-refresh-all] %s — Abbruch, %d/%d Grants '
                         'unbearbeitet (bleiben fällig)', aborted,
                         len(next_wave), len(plan))
                _note_skip(aborted, len(next_wave))
                break
            if not next_wave:
                break
            if time.time() >= _deadline:
                _note_skip('deadline', len(next_wave))
                break
            # Stall-Erkennung: eine komplette Welle ohne einen einzigen
            # Import und ohne Schrumpfen heißt: die Rotationen kommen nicht
            # (Refresher-Backoff bis 1 h / degradiert). Darauf wartet kein
            # Lauf — die Grants bleiben für den nächsten Cron fällig.
            stall = (0 if progressed or len(next_wave) < len(remaining)
                     else stall + 1)
            if stall >= _FO_WAVE_STALL_MAX:
                _note_skip('rotation_pending', len(next_wave))
                break
            # WELLEN-PAUSE: dem Refresher einen vollen Tick (60 s) Zeit
            # geben, bevor die pending-Grants erneut geprüft werden.
            _wt = 0.0
            while _wt < _FO_WAVE_WAIT_S and not _refresh_all_state.get('drain'):
                time.sleep(_FO_WAVE_STEP_S)
                _wt += _FO_WAVE_STEP_S
            remaining = next_wave
    finally:
        with _refresh_all_lock:
            _refresh_all_state['running'] = False
            _refresh_all_state['last'] = {
                'ts': time.time(), 'users': len(tokens),
                'ok': ok, 'fail': fail, 'skipped': skipped,
                'deferred': deferred, 'waves': waves,
                'fail_reasons': fail_reasons, 'skip_reasons': skip_reasons,
                'due_classes': due_classes, 'defer_reasons': defer_reasons}
        log.info('[flightops-refresh-all] done users=%d ok=%d fail=%d '
                 'skipped=%d deferred=%d waves=%d fail_reasons=%s '
                 'skip_reasons=%s due=%s defer=%s',
                 len(tokens), ok, fail, skipped, deferred, waves,
                 fail_reasons or '{}', skip_reasons or '{}',
                 due_classes or '{}', defer_reasons or '{}')


@lh_flightops_bp.route('/api/internal/flightops/refresh-drain', methods=['POST'])
def flightops_refresh_drain():
    """Deploy-Vorbereitung: laufenden refresh-all-Lauf UND den Refresher-Loop
    sauber auslaufen lassen (kein neuer LH-Call startet, der aktuelle Grant
    persistiert fertig). Der Deploy pollt bis running=False.

    Der Refresher wird PAUSIERT, nicht beendet (Vorfall 31.07. 12:56, s.
    Banner bei _REFRESHER_PAUSE_S): der Container wird nach einem Drain
    normalerweise neu erstellt und der frische Prozess startet ohne Pause —
    bleibt der Recreate aber aus (zweiter, paralleler Deploy), nimmt derselbe
    Loop nach dem Fenster von selbst wieder auf. Grant-Burn-Schutz unberührt:
    während der Pause rotiert nichts."""
    if not _internal_secret_ok():
        return jsonify({'ok': False, 'error': 'forbidden'}), 403
    with _refresh_all_lock:
        _refresh_all_state['drain'] = True
        running = bool(_refresh_all_state['running'])
    until = _refresher_pause()
    log.info('[fo-refresher] Drain angefordert -> Pause bis %s (%ds)',
             time.strftime('%H:%M:%SZ', time.gmtime(until)),
             int(until - time.time()))
    running = running or bool(_refresher_state.get('busy'))
    return jsonify({'ok': True, 'running': running,
                    'refresher_active': bool(_refresher_state.get('active')),
                    'refresher_paused_until': until})


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
    # Ab hier steht `running=True`. JEDER Weg aus dieser Funktion, der den
    # Worker-Thread nicht wirklich startet, MUSS das Flag zurücksetzen —
    # sonst hält ein einzelner Fehlschlag (Supabase-Aussetzer in
    # _connected_tokens, RuntimeError beim Thread-Start unter Speicherdruck)
    # den Cron für immer mit `already_running` ab und die Roster-Aktualisierung
    # aller User stirbt still bis zum nächsten Prozess-Neustart.
    # (Full-Review 2026-08-01)
    try:
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
    except Exception as e:
        with _refresh_all_lock:
            _refresh_all_state['running'] = False
        log.exception('[lh_flightops] refresh-all konnte nicht starten: %s', e)
        return jsonify({'ok': False, 'error': 'start_failed',
                        'detail': type(e).__name__}), 500
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
# ── DRAIN-ÜBERLEBEN (Vorfall 31.07. 12:56) ──────────────────────────────────
# Ein zweiter, parallel anlaufender Deploy feuerte seinen Drain (Schritt 2b von
# deploy-hetzner.sh) auf den 32 s zuvor frisch gebooteten poll-Container — und
# erstellte ihn danach NIE neu. Das Drain-Flag war aber ein EINWEG-Schalter:
# `[fo-refresher] beendet (drain/exit)`, Thread weg, niemand rotierte mehr,
# Wächter-Alarm, manueller `docker restart`.
#
# Konsequenz: der HTTP-Drain tötet den Loop nicht mehr, er PAUSIERT ihn mit
# Auto-Resume. Der Grant-Burn-Schutz bleibt dabei vollständig erhalten —
# während der Pause rotiert definitiv NICHTS (weder ein neuer Tick noch der
# Rest eines laufenden). Nur der EWIGE Tod fällt weg: kommt innerhalb von
# _REFRESHER_PAUSE_S kein Container-Recreate (Deploy-Recreate dauert normal
# < 5 min), nimmt der Loop die Rotation von selbst wieder auf.
#
# `drain` bleibt der HARTE Schalter und gehört ab jetzt allein dem
# Prozess-Ende (atexit/SIGTERM/gunicorn-Recycle, s. _refresher_exit_drain) —
# dort IST der Thread-Tod richtig, weil der Prozess ohnehin geht.
_REFRESHER_PAUSE_S = 600
# Wiederbelebungs-Karenz des Wächters: erst ab diesem Prozess-Alter zieht er
# einen toten Loop hoch. Ein junger Container steckt evtl. mitten in einem
# Deploy (Recreate < 5 min) — dort soll der Wächter nicht dagegenlaufen.
_REFRESHER_REVIVE_MIN_AGE_S = 5 * 60
_REFRESHER_BOOT_TS = time.time()
_refresher_state = {'active': False, 'drain': False, 'busy': False,
                    'last_tick': 0.0, 'last': None, 'active_since': 0.0,
                    # Pause-Fenster des HTTP-Drains: Unix-ts, ab dem wieder
                    # rotiert werden darf (0 = keine Pause). `paused` ist reine
                    # Log-Flanken-Erkennung, nie die Entscheidungsquelle.
                    'pause_until': 0.0, 'paused': False,
                    # Prozess geht wirklich zu Ende (atexit) — verbietet dem
                    # Wächter, den Loop im Sterben nochmal hochzuziehen.
                    'exiting': False, 'revived': 0}
_refresher_thread = [None]
_refresher_lock_fh = [None]      # offenes flock-Handle (hält den Lock am Leben)


def _refresher_pause_s():
    """Pause-Fenster des HTTP-Drains in Sekunden (Env-übersteuerbar, damit ein
    Vorfall ohne Deploy nachjustiert werden kann). Unsinnige Werte fallen auf
    den Default zurück; 0/negativ ist bewusst NICHT erlaubt — „Pause aus"
    hieße wieder „ewiger Tod"."""
    try:
        v = int((os.environ.get('LH_FLIGHTOPS_DRAIN_PAUSE_S') or '').strip()
                or _REFRESHER_PAUSE_S)
    except ValueError:
        return _REFRESHER_PAUSE_S
    return v if 30 <= v <= 3600 else _REFRESHER_PAUSE_S


def _refresher_paused(now=None):
    """Darf der Loop JETZT nicht rotieren, weil ein Drain ihn pausiert hat?
    Seiteneffektfrei bis auf das memoisierte Datei-Lesen (die Log-Flanke macht
    `_refresher_pause_gate`) — so bleibt die Regel mit Mock-Zeit testbar."""
    now = now or time.time()
    return _refresher_pause_until(now) > now


# ── HERZSCHLAG statt Thread-Introspektion (Fehlalarm 31.07.) ────────────────
# Der Wächter fragte bis heute `_refresher_state['active']` ab — MODUL-State,
# also nur in DEM Prozess wahr, der den Thread trägt. Jede Konstellation, in
# der die HTTP-Anfrage von einem anderen Prozess beantwortet wird als dem
# thread-tragenden, meldete „Refresher-Loop NICHT aktiv", obwohl der Loop
# nachweislich tickte:
#   · gunicorn-Worker-Recycle (--max-requests 10000): der neue Worker hat den
#     Thread noch nicht (er wartet ggf. bis zu 15 s auf den flock des alten);
#   · jede künftige Erhöhung von --workers;
#   · und — bis zum Drain-Guard oben — der Drain selbst.
# Ein Wächter, der falsch alarmiert, wird ignoriert und ist beim echten
# Ausfall wertlos. Deshalb schreibt der Tick-Loop SELBST einen Herzschlag in
# eine Datei neben dem Lockfile (dieselbe Container-FS, die sich ALLE Prozesse
# dieses Containers teilen) — und der Wächter liest genau das.
#
# Der Herzschlag trägt auch `pause_until`: eine legitime Deploy-Pause ist
# damit von einem toten Loop unterscheidbar (sonst würde jeder Deploy einen
# Alarm auslösen, sobald der Wächter in die Pause fällt).
_REFRESHER_BEAT_FILE = '/tmp/lh_flightops_refresher.beat'
# Ab wann gilt ein Herzschlag als tot? Der Takt ist 60 s, ABER ein Tick mit
# vielen fälligen Grants läuft lange (gemessen 31.07.: 16:26:10 → 16:38:46 =
# 12,6 min für 236 Rotationen). Deshalb schreibt der Tick den Schlag AUCH
# zwischen den einzelnen Grants — und die Grenze bleibt großzügig bei 15 min.
_REFRESHER_BEAT_STALE_S = 15 * 60


def _refresher_beat_write(now=None):
    """Herzschlag schreiben. Wirft nie — ein fehlgeschlagener Schreibversuch
    darf den Rotierer niemals stören."""
    try:
        payload = {'ts': now or time.time(), 'pid': os.getpid(),
                   'pause_until': _refresher_pause_until(),
                   'active_since': _refresher_state.get('active_since') or 0,
                   'busy': bool(_refresher_state.get('busy')),
                   'last': _refresher_state.get('last')}
        tmp = _REFRESHER_BEAT_FILE + '.tmp%d' % os.getpid()
        with open(tmp, 'w') as f:
            json.dump(payload, f)
        os.replace(tmp, _REFRESHER_BEAT_FILE)
    except Exception:
        pass


def _refresher_beat_read():
    """Letzter Herzschlag als dict ({} wenn keiner da/lesbar). Wirft nie."""
    try:
        with open(_REFRESHER_BEAT_FILE) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _refresher_health(now=None, beat=None):
    """Zustand des Rotierers aus dem HERZSCHLAG — prozessunabhängig, damit
    jeder Worker des Refresher-Containers dieselbe Antwort gibt. PURE (bis auf
    das Datei-Lesen), damit der Wächter testbar bleibt.

        {'state': 'alive'|'paused'|'stale'|'never',
         'beat_age_s': float|None, 'paused_until': float, 'pid': int|None,
         'last': dict|None}

    'never'  = es gab in diesem Container noch nie einen Schlag (frischer
               Boot ODER wirklich nie gestartet — der Aufrufer entscheidet
               über die Boot-Karenz).
    'stale'  = es gab einen, aber er ist älter als _REFRESHER_BEAT_STALE_S.
    'paused' = frisch UND der Drain-Guard hält gerade eine Pause (kein
               Alarmgrund, das ist der geplante Deploy-Zustand)."""
    now = now or time.time()
    b = _refresher_beat_read() if beat is None else (beat or {})
    ts = float(b.get('ts') or 0)
    out = {'state': 'never', 'beat_age_s': None,
           'paused_until': float(b.get('pause_until') or 0),
           'pid': b.get('pid'), 'last': b.get('last')}
    if ts <= 0:
        return out
    out['beat_age_s'] = round(now - ts, 1)
    if (now - ts) >= _REFRESHER_BEAT_STALE_S:
        out['state'] = 'stale'
    elif out['paused_until'] > now:
        out['state'] = 'paused'
    else:
        out['state'] = 'alive'
    return out


# Die Pause muss CONTAINER-weit gelten, nicht nur im antwortenden Prozess:
# der Drain kommt als HTTP-Request herein und landet auf irgendeinem
# gunicorn-Worker — der Rotations-Thread lebt aber genau in EINEM. Stünde die
# Pause nur im Modul-State des Antwortenden, würde der Refresher munter
# weiterrotieren (exakt die Fehlerklasse, die den Wächter falsch alarmieren
# ließ). Deshalb dieselbe Mechanik wie beim Herzschlag: eine Datei auf der
# geteilten Container-FS, Modul-State nur als schneller Zwilling.
_REFRESHER_PAUSE_FILE = '/tmp/lh_flightops_refresher.pause'
_PAUSE_MEMO_S = 2.0                       # Datei-Leseschutz für die Grant-Schleife
_pause_memo = {'read_at': 0.0, 'until': 0.0}


def _refresher_pause_file_until(now=None):
    """Pause-Ende aus der Datei (memoisiert, wirft nie)."""
    now = now or time.time()
    try:
        if (now - _pause_memo['read_at']) < _PAUSE_MEMO_S:
            return _pause_memo['until']
        try:
            with open(_REFRESHER_PAUSE_FILE) as f:
                until = float((json.load(f) or {}).get('until') or 0)
        except Exception:
            until = 0.0
        _pause_memo['read_at'], _pause_memo['until'] = now, until
        return until
    except Exception:
        return 0.0


def _refresher_pause_until(now=None):
    """Wirksames Pause-Ende = das SPÄTERE aus Modul-State und Datei."""
    return max(float(_refresher_state.get('pause_until') or 0.0),
               _refresher_pause_file_until(now))


def _refresher_pause(seconds=None, now=None):
    """Drain ⇒ Pause setzen (nie verkürzen: zwei Deploys hintereinander sollen
    das Fenster verlängern, nicht gegeneinander arbeiten). Schreibt in BEIDE
    Quellen. Gibt das neue Pause-Ende zurück."""
    now = now or time.time()
    until = now + float(seconds if seconds is not None else _refresher_pause_s())
    until = max(until, _refresher_pause_until(now))
    _refresher_state['pause_until'] = until
    try:
        tmp = _REFRESHER_PAUSE_FILE + '.tmp%d' % os.getpid()
        with open(tmp, 'w') as f:
            json.dump({'until': until, 'set_at': now, 'pid': os.getpid()}, f)
        os.replace(tmp, _REFRESHER_PAUSE_FILE)
        _pause_memo['read_at'], _pause_memo['until'] = now, until
    except Exception as e:
        log.warning('[fo-refresher] Pause-Datei: %s', type(e).__name__)
    return until


def _refresher_pause_gate(now=None):
    """Wie `_refresher_paused`, aber mit den beiden Log-Flanken (Eintritt/
    Auto-Resume) — der Beleg dafür, dass ein Drain ohne Container-Recreate
    NICHT mehr das Ende des Rotierers ist. Nur der Loop ruft das."""
    now = now or time.time()
    paused = _refresher_paused(now)
    if paused and not _refresher_state.get('paused'):
        _refresher_state['paused'] = True
        # WARNING statt INFO: das ist die eine Zeile, an der der Live-Beweis
        # hängt („Drain ohne Recreate tötet nicht mehr").
        log.warning('[fo-refresher] PAUSE durch Drain — keine Rotation für '
                    '%ds (Auto-Resume, falls kein Container-Recreate folgt)',
                    int(_refresher_pause_until(now) - now))
    elif not paused and _refresher_state.get('paused'):
        _refresher_state['paused'] = False
        _refresher_state['pause_until'] = 0.0
        log.warning('[fo-refresher] Pause abgelaufen ohne Container-Recreate '
                    '— nehme Rotation wieder auf')
    return paused


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
# nicht idle sterben. Laut Gateway-Betreiber (Mail Alex 05.08.2026) beträgt
# die RT-Lebensdauer 14 h; enforced wurde das bisher beobachtbar NICHT
# (Grants überlebten tagelanges Ruhen), aber ab September 2026 zieht Mashery
# die Enforcement-Schrauben an (AT 1 h → 15 min). Deshalb 12 h: selbst mit
# maximalem Fehler-Backoff (1 h, s. Bremse unten) bleibt die späteste
# Rotation bei ~13 h < 14 h. LH rotiert den RT bei jedem Refresh — jede
# Keepalive-Rotation verlängert die Familie also um volle 14 h.
_REFRESHER_KEEPALIVE_S = 12 * 3600
# Angenommene AT-Lebensdauer für die Rückrechnung des letzten Rotations-
# Zeitpunkts (s. _refresher_due). LH liefert expires_in=3600, _token_request
# speichert expires_at = now + (expires_in − 60).
_REFRESHER_AT_LIFETIME_S = 3600
# Rotations-Status, nach denen der Demand-Eintrag STEHEN bleibt (der Versuch
# ist vertagt, nicht erledigt). Alle anderen quittiert der Refresher.
_REFRESHER_DEMAND_RETRY_STATES = frozenset((
    'transient', 'skipped_claim_unavailable', 'skipped_claim_foreign',
    'refused', 'error', 'save_pending'))


# ── ROTATIONS-BREMSE (Verstärkungs-Audit 2026-07-29) ────────────────────────
# MESSUNG 29.07.: 4.057 oauth_refresh bei 601 gesunden Grants = 6,75 Rotationen
# pro Grant und Tag. Rechnerisch erklärbar ist davon nur ein Teil:
#   · Keepalive           601 × 24/18 h            ≈  801/Tag
#   · Sync-Kadenz (Demand-Vorlauf des 2-h-Crons; AT lebt 59 min < 3,5-h-Takt,
#     also kostet JEDER Sync-Lauf genau eine Rotation) ≤ 6/Grant/Tag
# Der Rest kommt aus einer RÜCKKOPPLUNG, die im Code stand:
#   `transient`/`error` LIESSEN den Demand-Eintrag STEHEN (oben) — und der
#   'fresh'-Kurzschluss in _refresher_refresh_grant greift NUR, wenn ein
#   gültiger AT da ist. Ein Grant, dessen Rotation dauerhaft scheitert, hat
#   keinen gültigen AT ⇒ er war in JEDEM 60-s-Tick erneut fällig:
#       86.400 s / 60 s = 1.440 LH-Token-Calls pro Tag und Grant.
#   Genau der Kreis, den man bei einer Drosselung NICHT haben will:
#   Über-Quota → Fehlschläge → mehr Versuche → mehr Über-Quota.
# Zwei Bremsen, beide hier:
#   (1) MINDESTABSTAND _ROT_MIN_GAP_S zwischen zwei LH-Rotationsversuchen
#       DESSELBEN Grants — unabhängig vom Status. 5 min ist der ehrliche
#       Boden: ein AT lebt 59 min, eine zweite Rotation innerhalb von 5 min
#       kann NIE nötig sein. Für „Nutzer braucht es jetzt" kostenlos: nach
#       einer ERFOLGREICHEN Rotation ist der AT 59 min gültig, der Nutzer
#       wartet also nie; und ein frisch abgelaufener AT wird weiterhin im
#       nächsten Tick (≤60 s) rotiert.
#   (2) EXPONENTIELLER RÜCKZUG nach echten Fehlschlägen (_ROT_FAIL_STATES):
#       120 s · 2^(n−1), gedeckelt bei 1 h. Ein dauerhaft scheiternder Grant
#       macht damit ~26 statt 1.440 Versuche/Tag (Faktor ~55).
# HEILIG BLEIBT: der Keepalive. Der Deckel (1 h) liegt um Größenordnungen
# unter _REFRESHER_KEEPALIVE_S (12 h) — ein Refresh-Token kann durch die
# Bremse NIE idle sterben. Und die Bremse macht nichts auf, sie macht nur zu:
# Claim-RPC, Choke-Point-Gate und der Asymmetrie-Vertrag in _tokens_save sind
# unberührt.
_ROT_MIN_GAP_S = 300
_ROT_BACKOFF_BASE_S = 120
_ROT_BACKOFF_MAX_S = 3600
# Nur Status, bei denen wirklich ein LH-Token-Call rausging und scheiterte.
# Bewusst NICHT dabei: skipped_claim_* (kein LH-Call, Supabase degradiert —
# schnelles Wiederkommen ist dort richtig und kostet kein Kontingent) und
# save_pending (Nachsave-Heilung; die bremst schon der Mindestabstand).
_ROT_FAIL_STATES = frozenset(('transient', 'error'))
# token -> {'last': ts des letzten Versuchs, 'fails': Fehlschläge in Folge}
_rot_gate = {}
_ROT_GATE_CAP = 4000


def _rot_backoff_s(fails):
    """Rückzugsfenster nach `fails` Fehlschlägen in Folge (0 ⇒ nur der
    Mindestabstand). Verdopplung ab 120 s, hart gedeckelt bei 1 h."""
    if fails <= 0:
        return 0.0
    return float(min(_ROT_BACKOFF_MAX_S, _ROT_BACKOFF_BASE_S * (2 ** (fails - 1))))


def _rot_gate_ok(tok, now, gate=None):
    """Darf dieser Grant JETZT einen LH-Rotationsversuch machen? Wirft nie —
    im Zweifel True (die Bremse darf niemals einen Grant dauerhaft aussperren;
    ein verpasster Refresh ist billiger als ein blockierter Keepalive)."""
    try:
        g = (_rot_gate if gate is None else gate).get(tok)
        if not g:
            return True
        wait = max(_ROT_MIN_GAP_S, _rot_backoff_s(g.get('fails') or 0))
        return (now - (g.get('last') or 0)) >= wait
    except Exception:
        return True


def _rot_gate_note(tok, st, now=None):
    """Versuch stempeln und Fehlschlagszähler fortschreiben. Bewusst beim
    VERSUCH (nicht erst beim Erfolg) — der LH-Call ist raus und hat, falls LH
    Token-Calls aufs Kontingent bucht, bereits gekostet. Wirft nie."""
    try:
        now = now or time.time()
        if len(_rot_gate) > _ROT_GATE_CAP:
            # Deckel: ältesten Stempel-Block halbieren. Ein verlorener Stempel
            # heißt nur »darf sofort wieder« — nie »rotiert doppelt«.
            for k in sorted(_rot_gate,
                            key=lambda k: _rot_gate[k].get('last') or 0
                            )[:_ROT_GATE_CAP // 2]:
                _rot_gate.pop(k, None)
        g = _rot_gate.setdefault(tok, {'last': 0.0, 'fails': 0})
        g['last'] = now
        g['fails'] = (g.get('fails') or 0) + 1 if st in _ROT_FAIL_STATES else 0
    except Exception:
        pass


# ── VERTEILUNGS-MESSUNG (Auftrag 1d) ────────────────────────────────────────
# Die Zähler lhfoR/lhfoRD sagen nur, WIE VIELE Token-Calls rausgingen — nicht,
# ob das viele Nutzer je einmal zu oft oder wenige Nutzer sehr oft waren.
# Genau diese Frage war heute nicht beantwortbar. Deshalb ein schlanker
# In-Process-Zähler pro Grant und UTC-Tag (nur im Refresher-Container, der
# ist der einzige Rotierer): Token gekürzt auf 8 Zeichen, Reset bei Tageswechsel,
# Ausgabe über /api/internal/flightops/relogin-watch (Top-Verbraucher).
_rot_day = {'day': '', 'n': {}}
_ROT_DAY_TOP = 10


def _rot_day_note(tok, now=None):
    """Einen tatsächlich abgesetzten LH-Token-Call diesem Grant zuschreiben."""
    try:
        day = time.strftime('%Y%m%d', time.gmtime(now or time.time()))
        if _rot_day['day'] != day:
            _rot_day['day'], _rot_day['n'] = day, {}
        k = (tok or '')[:8]
        _rot_day['n'][k] = _rot_day['n'].get(k, 0) + 1
    except Exception:
        pass


def _rot_day_report():
    """{'day', 'grants', 'calls', 'top': [[tok8, n], …]} — Verteilung der
    heutigen Rotationen. Leer, solange nichts rotiert wurde."""
    try:
        n = _rot_day['n']
        top = sorted(n.items(), key=lambda kv: -kv[1])[:_ROT_DAY_TOP]
        return {'day': _rot_day['day'], 'grants': len(n),
                'calls': sum(n.values()), 'top': [[k, v] for k, v in top]}
    except Exception:
        return {}


def _refresher_due(scan, now=None, demand=None, out=None):
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
         her. Damit rotiert jeder gesunde Grant garantiert ~1×/18 h und der RT
         kann nicht idle ablaufen.

    EHRLICHE ABLEITUNG von `last_rotated`: es gibt KEINEN rotated_at-Stempel im
    Token-Dict. Da der Refresher der einzige Schreiber von expires_at ist und
    _token_request expires_at = now + (expires_in−60) setzt, gilt
    last_rotated ≈ expires_at − _REFRESHER_AT_LIFETIME_S. Der Fehler ist die
    60-s-Sicherheitsmarge (die Schätzung liegt ~1 min ZU FRÜH ⇒ minimal
    eifriger, nie zu spät). Fehlt expires_at ganz (0), ist der Grant nach
    dieser Rechnung uralt ⇒ keepalive-fällig — die sichere Richtung.

    BREMSE (2026-07-29): zusätzlich muss der Grant das Rotations-Gate passieren
    (Mindestabstand 5 min, danach exponentieller Rückzug nach Fehlschlägen —
    s. _rot_gate_ok). Gefiltert wird HIER und nicht erst in
    _refresher_refresh_grant, damit der Tick für gebremste Grants weder einen
    Supabase-Read noch den QPS-Schlafschritt bezahlt. Der Rückgabewert bleibt
    die reine Fällig-Liste (fünf Tests hängen an dieser Form); wie viele Grants
    die Bremse zurückgehalten hat, landet — falls übergeben — in `out['gated']`
    fürs Tick-Log."""
    now = now or time.time()
    demand = _refresher_demand if demand is None else demand
    due, gated = [], 0
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
        if not _rot_gate_ok(tok, now):
            gated += 1
            continue
        due.append((exp, tok))
    due.sort()
    if out is not None:
        out['gated'] = gated
    return [tok for _exp, tok in due]


def _refresher_tick():
    # Die Vormerkliste ist echte Nachfrage: sie füllt sich nur, wenn ein
    # Consumer IN DIESEM Prozess auf einen abgelaufenen AT gelaufen ist.
    # Deshalb zählt sie (wie bisher für die Reihenfolge) jetzt auch als
    # Demand-Quelle für die Fällig-Entscheidung.
    wanted = _refresh_wanted_drain()
    _out = {}
    scan = _refresher_scan()
    due = _refresher_due(scan, demand=set(_refresher_demand) | wanted,
                         out=_out)
    # Vorgemerkte (ein Worker sah einen abgelaufenen AT) zuerst — aber nur,
    # wenn der durable Stand die Fälligkeit bestätigt; sonst war die
    # Vormerkung stale und wird verworfen.
    ordered = ([t for t in due if t in wanted]
               + [t for t in due if t not in wanted])
    stats = {}
    for tok in ordered:
        # GRANT-BURN-SCHUTZ: harter Prozess-Drain UND Deploy-Pause brechen den
        # Lauf sofort ab — ein pausierter Refresher rotiert definitiv nichts.
        if _refresher_state['drain'] or _refresher_paused():
            break
        # Herzschlag AUCH zwischen den Grants: ein Tick mit 236 fälligen
        # Grants lief gemessen 12,6 min — ohne das sähe er wie ein toter Loop
        # aus (Fehlalarm-Klasse, s. Banner bei _REFRESHER_BEAT_FILE).
        _refresher_beat_write()
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
        # BREMSE stempeln (2026-07-29): Versuch gezählt, Fehlschlagszähler
        # fortgeschrieben. Muss VOR dem Demand-Quittieren stehen, damit ein
        # stehen bleibender Demand den Grant nicht im nächsten 60-s-Tick
        # erneut gegen LH schickt (das war die Rückkopplung, 1.440 Calls/Tag).
        _rot_gate_note(tok, st)
        # Demand quittieren — außer bei den Status, die exakt „nochmal
        # versuchen" bedeuten (Claim-Infra weg, LH transient): dort bleibt der
        # Bedarf stehen, sonst müsste der User erneut anklopfen.
        if st not in _REFRESHER_DEMAND_RETRY_STATES:
            _refresher_demand.discard(tok)
        # QPS-Schonung + Jitter: Rotationen entzerren sich selbst, statt
        # stündliche Refresh-Wellen zu bilden.
        time.sleep(_REFRESHER_GRANT_GAP_S + secrets.randbelow(600) / 1000.0)
    _refresher_state['last'] = {'ts': time.time(), 'due': len(ordered),
                                'gated': _out.get('gated', 0),
                                'scan': len(scan),
                                'demand': len(_refresher_demand),
                                'stats': stats}
    # SICHTBARKEIT (Auftrag 3, 2026-07-29): JEDER Tick loggt — vorher gab es
    # nur bei Arbeit eine Zeile, und die kam wegen des fehlenden Root-Handlers
    # ohnehin nie an (s. app.py, Boot-Setup). Eine Zeile/Minute ist der Preis
    # dafür, dass die Rotationsmechanik künftig aus `docker logs aerotax-poll`
    # ablesbar ist statt aus Vermutungen.
    log.info('[fo-refresher] tick scan=%d due=%d gated=%d demand=%d %s',
             len(scan), len(ordered), _out.get('gated', 0),
             len(_refresher_demand), stats or '{}')


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
    # ERSTER Herzschlag sofort nach dem flock — nicht erst nach dem ersten
    # Tick. Der Scan über 1.000+ Grants kann dauern, und bis dahin soll der
    # Wächter „lebt, arbeitet gerade" sehen statt „nie getickt".
    _refresher_beat_write(_refresher_state['active_since'])
    log.warning('[fo-refresher] aktiv pid=%s — einziger RT-Rotierer des '
                'Systems', os.getpid())
    # Deploy-/Recycle-Überlebende einsammeln, BEVOR irgendein Grant rotiert:
    # ein geparkter neuer RT muss den Nachsave bekommen, sonst verbrennt der
    # nächste Refresh-Versuch seine Familie (Vorfall 31.07.–01.08.2026).
    _parked_disk_restore()
    try:
        while not _refresher_state['drain']:
            try:
                # Pause (Deploy-Drain) überspringt NUR die Arbeit — der Loop
                # selbst lebt weiter und nimmt nach dem Fenster von allein auf.
                if not _refresher_pause_gate():
                    _refresher_tick()
            except Exception as e:
                log.warning('[fo-refresher] tick: %s', type(e).__name__)
            # Herzschlag auch während der Pause: der Loop IST am Leben, der
            # Wächter darf ihn nicht als „steht" melden (er sieht die Pause
            # separat über `paused_until` im Schlag selbst).
            _refresher_state['last_tick'] = time.time()
            _refresher_beat_write(_refresher_state['last_tick'])
            for _i in range(_REFRESHER_TICK_S):
                if _refresher_state['drain']:
                    break
                time.sleep(1)
    finally:
        _refresher_state['active'] = False
        # flock freigeben — sonst könnte ein In-Process-Wiederbeleben (s.
        # _refresher_revive) sich am eigenen, noch offenen Handle aussperren:
        # flock hängt an der OPEN FILE DESCRIPTION, ein zweites open() im
        # SELBEN Prozess kollidiert genauso wie ein fremder Prozess.
        try:
            if _refresher_lock_fh[0] is not None:
                _refresher_lock_fh[0].close()
        except Exception:
            pass
        _refresher_lock_fh[0] = None
        log.info('[fo-refresher] beendet (drain/exit)')


def _refresher_revive(now=None):
    """ZWEITE LEITPLANKE zum Pause-Umbau: Loop tot, Container aber ALT ⇒
    in-process neu starten (der Wächter ruft das, s. flightops_relogin_watch).

    Deckt genau den Vorfall 31.07. ab, falls der Thread aus einem anderen
    Grund als dem HTTP-Drain endet: der Container wird nicht neu erstellt,
    also muss jemand den Rotierer zurückholen — sonst veralten alle ATs still.

    Bewusst eng: nur wenn die Rolle gesetzt ist, der Thread wirklich tot ist,
    der Prozess NICHT gerade beendet wird (atexit hat `exiting` gesetzt) und
    der Prozess alt genug ist (_REFRESHER_REVIVE_MIN_AGE_S) — ein frisch
    gebooteter Container steckt evtl. mitten in einem Deploy, dort darf der
    Wächter nicht gegen den Deploy anlaufen. Wirft nie; True = neu gestartet."""
    try:
        if not _refresher_enabled() or _refresher_state.get('exiting'):
            return False
        th = _refresher_thread[0]
        if th is not None and getattr(th, 'is_alive', lambda: False)():
            return False
        now = now or time.time()
        if (now - _REFRESHER_BOOT_TS) < _REFRESHER_REVIVE_MIN_AGE_S:
            return False
        # HERZSCHLAG-GATE: schlägt es noch, lebt der Loop in einem ANDEREN
        # Prozess dieses Containers (gunicorn-Worker-Recycle/Overlap). Dann
        # wäre ein zweiter Loop hier bestenfalls ein flock-Wartezimmer und
        # schlimmstenfalls ein zweiter Rotierer — beides nicht gewollt.
        if _refresher_health(now=now).get('state') in ('alive', 'paused'):
            return False
        try:
            if _refresher_lock_fh[0] is not None:
                _refresher_lock_fh[0].close()
        except Exception:
            pass
        _refresher_lock_fh[0] = None
        # Der Wiederanlauf hebt genau die beiden Schalter auf, die den Loop
        # stillgelegt haben — die Rotations-BREMSE (_rot_gate) bleibt stehen,
        # der Grant-Burn-Schutz wird also nicht mit zurückgesetzt.
        _refresher_state.update(drain=False, busy=False, paused=False,
                                pause_until=0.0, last_tick=0.0)
        # Auch die Container-weite Pause-Datei raeumen — sonst startete der
        # wiederbelebte Loop direkt wieder in eine (fremde) Pause.
        try:
            os.remove(_REFRESHER_PAUSE_FILE)
        except OSError:
            pass
        _pause_memo['read_at'], _pause_memo['until'] = 0.0, 0.0
        _refresher_thread[0] = None
        started = _maybe_start_refresher()
        if started is None:
            return False
        _refresher_state['revived'] = int(_refresher_state.get('revived') or 0) + 1
        log.error('[fo-refresher] WIEDERBELEBT — Loop war tot, Container läuft '
                  'seit %ds (Drain ohne Container-Recreate?); Neustart #%d',
                  int(now - _REFRESHER_BOOT_TS), _refresher_state['revived'])
        return True
    except Exception as e:
        log.warning('[fo-refresher] revive: %s', type(e).__name__)
        return False


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
    killen. Wirft nie.

    HIER — und nur hier — bleibt `drain` der harte Einweg-Schalter: der
    Prozess geht ohnehin. `exiting` sperrt zusätzlich die Wiederbelebung
    (_refresher_revive), damit der Wächter im Sterben nichts hochzieht."""
    try:
        _refresher_state['exiting'] = True
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
    # ── Refresher-Urteil AUS DEM HERZSCHLAG (Umbau 31.07.) ─────────────────
    # Vorher hing hier alles an `_refresher_state['active']`/`last_tick` —
    # MODUL-State, nur im thread-tragenden Prozess wahr. Beweis vom 31.07.:
    # derselbe Wächter meldete auf :8081 `reasons: []` (Thread tickte) und auf
    # :8080 einen ALARM, obwohl der Web-Container die Rolle korrekt NICHT
    # trägt. Jetzt entscheidet der Schlag, den der Tick-Loop selbst schreibt.
    revived = False
    health = _refresher_health(now=now)
    if _refresher_enabled():
        st = health['state']
        if st == 'paused':
            # Kein Alarm: das ist der geplante Deploy-Drain. Auffällig wird es
            # erst, wenn das Fenster unplausibel weit in der Zukunft liegt.
            if health['paused_until'] - now > 2 * _refresher_pause_s():
                reasons.append('Refresher-Pause unplausibel lang — Drain-Sturm?')
        elif st == 'never':
            # BOOT-KARENZ (Fehlalarm 27.07. 21:07: der Cron traf 38 s nach dem
            # Containerstart). Ohne JEMALS einen Schlag gibt es nichts zu
            # vergleichen — erst melden, wenn der Prozess >10 min alt ist.
            if (now - _REFRESHER_BOOT_TS) > 10 * 60:
                revived = _refresher_revive(now=now)
                reasons.append('Refresher-Loop hat seit Boot NIE geschlagen '
                               '(>10min Prozesslaufzeit, kein Herzschlag)'
                               + (' — WIEDERBELEBT' if revived else ''))
        elif st == 'stale':
            # ECHTER Ausfall: es GAB einen Schlag, und er ist alt. Das ist der
            # Vorfall 31.07. 12:56 (Drain ohne Container-Recreate) und jeder
            # sonstige Thread-Tod. Selbstheilung versuchen, trotzdem melden.
            revived = _refresher_revive(now=now)
            reasons.append('Refresher-Loop steht (kein Herzschlag seit %ss) — '
                           'niemand rotiert' % int(health['beat_age_s'] or 0)
                           + (' — WIEDERBELEBT' if revived else ''))
    elif flightops_configured():
        # KEIN ALARM MEHR aus einem Container ohne die Rolle (Fehlalarm-Quelle,
        # live reproduziert 31.07. 17:07 auf :8080): Web- und MQTT-Container
        # tragen `LH_FLIGHTOPS_REFRESHER` ABSICHTLICH nicht — die Rolle lebt
        # per Architektur allein im Poll-Container, und der Herzschlag ist
        # Container-lokal, also von hier aus prinzipiell unsichtbar. Ein
        # Prozess, der es nicht wissen KANN, darf nichts behaupten; er sagt
        # nur, dass er nicht zuständig ist (der Wächter-Cron fragt :8081).
        health['state'] = 'not_my_role'
    # ── RÜCKSTAU-ALARM (Lücke des 02.08.-Vorfalls: 291 Grants hingen in der
    # Rotations-Bremse, 199 needs_relogin — und der Wächter schwieg, weil nur
    # der STÜNDLICHE Delta-Anstieg alarmierte, nicht der Dauerzustand) ──────
    if _refresher_enabled():
        _beat_last = health.get('last')
        _gated = (_beat_last or {}).get('gated') if isinstance(_beat_last, dict) else None
        if isinstance(_gated, int) and _gated >= 50:
            reasons.append(f'{_gated} Grants im Rotations-Rückstau '
                           f'(Dauer-Fehlschlag — GRANT-BURN-Log prüfen)')
        if _rotated_pending:
            reasons.append(f'{len(_rotated_pending)} Rotation(en) geparkt '
                           f'(Nachsave klemmt — Familienverlust droht)')
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
                        # DAS Urteil (prozessunabhängig, aus dem Herzschlag):
                        # 'alive' | 'paused' | 'stale' | 'never' |
                        # 'not_my_role'. Alles darunter ist Diagnose.
                        'state': health['state'],
                        'beat_age_s': health['beat_age_s'],
                        'beat_pid': health['pid'],
                        'last': health['last'] or _refresher_state.get('last'),
                        # Drain-Pause (Deploy) sichtbar machen — sonst sähe
                        # ein stiller Refresher wie ein kaputter aus.
                        'paused_until': health['paused_until'],
                        'revived': int(_refresher_state.get('revived') or 0),
                        'revived_now': bool(revived),
                        # In-Prozess-Sicht NUR noch als Diagnose — sie ist
                        # genau das Signal, das die Fehlalarme erzeugt hat.
                        'active_in_this_process': bool(
                            _refresher_state.get('active')),
                        # Verstärkungs-Audit 2026-07-29: Rotationen pro Grant
                        # und Tag (Top-Verbraucher). Ohne das war „6,75 pro
                        # Grant" ein Mittelwert ohne Verteilung.
                        'rotations_today': _rot_day_report()}})


# Rollen-gesteuerter Autostart (No-Op ohne LH_FLIGHTOPS_REFRESHER=1 bzw. ohne
# Creds — Tests und Web-Container starten hier nie einen Thread).
_maybe_start_refresher()
