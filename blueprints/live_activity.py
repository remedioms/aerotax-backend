"""Live Activities — Backend-Seite (P6, 2026-07-27).

Der iOS-Client startet die Dienst-Live-Activity selbst, aktualisiert sie aber
NICHT: Owner-Entscheidung ist „per Push vom Backend". Der Client lädt nur die
beiden ActivityKit-Tokens hoch (PushService.uploadLiveActivityToken), dieses
Modul hält sie und schickt die APNs-Pushes.

Zwei Token-Sorten, NICHT austauschbar
─────────────────────────────────────
  kind='start'   Push-to-Start-Token. GERÄTE-WEIT, einer pro
                 ActivityAttributes-Typ, trägt keine activity_id. Damit kann
                 das Backend eine Activity ERZEUGEN, ohne dass die App läuft
                 (aps.event='start' + attributes-type + attributes).
  kind='update'  Update-Token EINER laufenden Activity. Er ROTIERT während der
                 Laufzeit (bis zu 8 h) — iOS liefert über
                 `Activity.pushTokenUpdates` immer wieder einen neuen. Jede
                 Erneuerung MUSS die vorige Zeile derselben activity_id
                 überschreiben; sonst pusht das Backend gegen einen toten Token
                 und die Activity friert still ein (ActivityKit meldet nichts,
                 Apple antwortet nur BadDeviceToken).

Datums-Kodierung in `content-state` — die wichtigste Falle
──────────────────────────────────────────────────────────
ActivityKit dekodiert `aps.content-state` mit einem PLAIN `JSONDecoder()`, also
mit der DEFAULT-`dateDecodingStrategy` `.deferredToDate`. Das ist Swifts
eigenes `Date`-Codable-Format: eine ZAHL = `timeIntervalSinceReferenceDate`
(Sekunden seit 2001-01-01T00:00:00Z), NICHT ISO-8601 und NICHT Unix-Epoche.
(WWDC23-10185 „Update Live Activities with push notifications": „Content state
JSON will always be decoded using a JSONDecoder with default decoding
strategies" — Custom-Strategien/snake_case sind explizit verboten.)

Ein ISO-8601-String würde also NICHT dekodieren, und ActivityKit verwirft ein
nicht dekodierbares ContentState STILL: die Activity friert auf dem alten Stand
ein, ohne Fehler irgendwo. Deshalb emittiert dieses Modul für JEDES Date-Feld
in `content-state` und in `attributes` eine Zahl (Apple-Referenzdatum).
`_APPLE_EPOCH_OFFSET` macht die Umrechnung.

ACHTUNG, zwei Epochen im selben Payload:
  • `aps.content-state` / `aps.attributes` Dates → Sekunden seit 2001-01-01
    (weil Swift-`Codable` sie dekodiert).
  • `aps.timestamp`, `aps.stale-date`, `aps.dismissal-date` → Sekunden seit
    1970-01-01 (die liest ActivityKit selbst, nicht der Codable-Decoder).
Das ist kein Tippfehler, das ist Apples Vertrag.

Der Swift-Vertrag (`Shared/DutyActivityAttributes.swift`) braucht dafür KEINE
Änderung: `ContentState` benutzt die synthetisierte `Codable`-Conformance mit
`Date`-Feldern — genau das erwartet `.deferredToDate`. Was NICHT gilt, ist die
ISO-8601-Regel des `DutySnapshot`-Transports (dort setzt der App-Code selbst
`dateDecodingStrategy = .iso8601`); die betrifft ausschließlich den
Widget-Snapshot, nicht `content-state`.

Zweite Codable-Falle: Swifts synthetisierter Decoder benutzt KEINE
Default-Werte. `stateVersion`, `phase`, `kicker`, `mainTime` und `generatedAt`
sind nicht-optional ⇒ die Keys MÜSSEN im JSON stehen, sonst wirft der Decoder
`keyNotFound` und das komplette Update fällt still weg. Darum sind sie hier
Pflichtfelder (`_REQUIRED_CONTENT_KEYS`).

Rate-Limit
──────────
Apple drosselt Live-Activity-Pushes hart. Gesendet wird NUR bei ECHTER
Änderung: sha256 über den normalisierten content-state OHNE `generatedAt`
(= `content_digest` an der Registry-Zeile). Gleicher Digest ⇒ skip.
Der COUNTDOWN braucht per Design NIE einen Push — die Activity rendert ihn
client-seitig mit `Text(timerInterval:)` aus `countdownTarget`. Ein „Push pro
Minute, damit die Zahl stimmt" wäre der klassische Weg in Apples Throttle.

Endpoints
─────────
  POST /api/push/register-live-activity   (User, Bearer == body.token)
  POST /api/live-activity/end             (User, Bearer == body.token)
  POST /api/internal/live-activity/push   (nur Daemon: X-Poll-Secret /localhost)

Wiring (bewusst NUR diese eine Zeile in app.py):

    ('blueprints.live_activity', 'live_activity_bp'),

MQTT-Hook — VERDRAHTET (P7, 2026-07-27)
───────────────────────────────────────
`push_for_affected(affected, kind, flight_disp, topic_date, facts=None)` ist die
öffentliche Funktion für den Fanout aus `blueprints/lh_mqtt.py`. Der Aufruf
steht dort in `lh_mqtt_event` (Abschnitt „LIVE-ACTIVITY-FANOUT"), NACH dem
Alert-Push-Block und nur bei nicht-leerem `affected`; der Zähler läuft als
`la_sent` getrennt von `pushed` ins Event-Log. `push_for_affected` gate't
selbst (nicht anzeige-relevante Event-Arten → 0).

Migration: `supabase_migrations/20260727_live_activities.sql` (Tabelle
`public.live_activities` + RPCs `upsert_live_activity`, `end_live_activity`,
`live_activity_mark_result`).
"""
import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

try:
    # IANA-Zeitzone pro IATA (kuratierte Tabelle, keine Heuristik). Gebraucht
    # für `fromTZIdentifier`/`toTZIdentifier` im MQTT-Fanout — ohne sie zeigt
    # die Live Activity die Marke in Homebase- statt in Ortszeit.
    from airport_tz import airport_tz as _airport_tz
except Exception:                                     # pragma: no cover
    def _airport_tz(iata):
        return None

log = logging.getLogger('aerotax')
live_activity_bp = Blueprint('live_activity_bp', __name__)

# Sekunden zwischen Unix-Epoche (1970-01-01) und Apples Referenzdatum
# (2001-01-01). Swifts `Date`-Codable rechnet in Referenzdatum-Sekunden.
_APPLE_EPOCH_OFFSET = 978307200

# APNs-Topic-Suffix für Live Activities. OHNE dieses Suffix antwortet Apple mit
# 400 BadTopic — das ist der häufigste Erstfehler auf diesem Pfad.
_LA_TOPIC_SUFFIX = '.push-type.liveactivity'

_ATTRIBUTES_TYPE = 'DutyActivityAttributes'

# Reason-Codes, bei denen ein Token als tot gilt (identisch zu app.py:27256).
_DEAD_REASONS = ('Unregistered', 'BadDeviceToken', 'ExpiredToken')

_DEFAULT_BUNDLE_ID = 'aerotax.AeroTax'

# Expiration-Fenster des Pushes: eine Live Activity, die eine Stunde alt ist,
# will niemand mehr nachgeliefert bekommen.
_EXPIRATION_S = 3600

# In-Process-Spiegel von (content_digest, last_timestamp) pro Registry-Zeile.
# Zweck ist NICHT Caching, sondern zwei harte Garantien, wenn Supabase gerade
# nicht schreiben kann:
#   • Digest: kein Doppel-Push mit identischem Inhalt.
#   • Timestamp: aps.timestamp bleibt strikt monoton, auch wenn zwei Pushes in
#     dieselbe Sekunde fallen (iOS verwirft ein Update mit <= timestamp).
_state_lock = threading.Lock()
_LAST_SENT = {}
_LAST_SENT_MAX = 2000


# ── Late-Binding an app.py (Blueprint bleibt ohne app importierbar) ──────────

def _app_attr(name, default=None):
    """Greift bei JEDEM Call frisch auf ein app.py-Attribut zu. Ein Top-Level
    `from app import X` würde beim Modul-Import nur Fallbacks einfangen —
    app.py ist zu dem Zeitpunkt noch nicht fertig initialisiert."""
    try:
        import app as _app_mod
        return getattr(_app_mod, name, default)
    except Exception:
        return default


def _sb():
    """Test-Seam: Supabase-Client oder None (lazy, wirft nie)."""
    try:
        from app import sb, SB_AVAILABLE
        return sb if (SB_AVAILABLE and sb is not None) else None
    except Exception:
        return None


def _token_ref(token):
    """PII-freie Korrelation für Logs. NIE ein rohes User- oder Push-Token
    loggen — auch nicht gekürzt."""
    fn = _app_attr('_push_token_ref')
    if callable(fn):
        try:
            return fn(token)
        except Exception:
            pass
    return hashlib.sha256(str(token or '').encode('utf-8')).hexdigest()[:10]


def _normalized_env(value):
    fn = _app_attr('_push_normalized_environment')
    if callable(fn):
        try:
            return fn(value)
        except Exception:
            pass
    v = (value or '').strip().lower()
    return v if v in ('prod', 'sandbox') else 'unknown'


# ── Auth ────────────────────────────────────────────────────────────────────

def _secret_ok():
    """Interner Daemon-Pfad: gleiche Auth wie /api/internal/poll-boards und
    lh-mqtt — Secret-Header, ohne gesetztes Secret nur localhost."""
    import hmac as _hmac
    secret = os.environ.get('ADSB_POLL_SECRET', '').strip()
    if secret:
        provided = (request.headers.get('X-Poll-Secret') or '').strip()
        return bool(provided) and _hmac.compare_digest(provided, secret)
    return (request.remote_addr or '') in ('127.0.0.1', '::1')


def _auth_body_token(user_token):
    """Body-Token-Routen umgehen das globale Pfad-Gate (app.py `before_request`
    matcht nur AT-Tokens IM PFAD) → hier explizit prüfen. Returns None wenn ok,
    sonst die fertige Fehler-Response.

    Reihenfolge mit Absicht: erst Binding (billig, constant-time), dann
    Existenz. Ein Angreifer bekommt so nie eine Aussage darüber, ob ein
    fremdes Token existiert.
    """
    if not _request_bearer_matches(user_token):
        return jsonify({'ok': False, 'error': 'token_binding_required'}), 401

    validator = _app_attr('_validate_token')
    if not callable(validator):
        # Kein Auth-Store erreichbar ⇒ NICHT fail-open.
        return _auth_unavailable()
    try:
        result = validator(user_token)
    except Exception as exc:
        log.warning('[live-activity] auth validator unavailable: %s',
                    type(exc).__name__)
        return _auth_unavailable()
    state = str(getattr(getattr(result, 'state', None), 'name', '')).upper()
    if state == 'UNAVAILABLE':
        return _auth_unavailable()
    if state != 'VALID':
        return jsonify({'ok': False, 'error': 'invalid_token'}), 401
    return None


def _request_bearer_matches(user_token):
    fn = _app_attr('_request_bearer_matches')
    if callable(fn):
        try:
            return bool(fn(user_token))
        except Exception:
            return False
    # Fallback (Blueprint ohne app, z.B. isolierter Unit-Test): dieselbe
    # constant-time-Semantik wie app.py:655.
    import hmac as _hmac
    auth = request.headers.get('Authorization') or ''
    parts = auth.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != 'bearer':
        return False
    bearer = parts[1].strip()
    if not bearer or not user_token:
        return False
    try:
        return _hmac.compare_digest(bearer, user_token)
    except Exception:
        return False


def _auth_unavailable():
    fn = _app_attr('_auth_store_unavailable_response')
    if callable(fn):
        try:
            return fn()
        except Exception:
            pass
    resp = jsonify({'ok': False, 'error': 'auth_store_unavailable'})
    resp.status_code = 503
    resp.headers['Retry-After'] = '5'
    return resp


# ── content-state: Vertrag gegen DutyActivityAttributes.ContentState ─────────
#
# Feldnamen SIND der Wire-Contract (Swift `Codable`, synthetisierte Keys).
# Jede Umbenennung bricht JEDE bereits ausgelieferte App-Version — Activities
# laufen bis zu 8 h weiter und werden von alten Builds empfangen. Nur additiv
# ändern, `stateVersion` mitzählen.
_CONTENT_FIELDS = {
    'stateVersion':        'int',
    'phase':               'str',
    'kicker':              'str',
    'mainTime':            'date',
    'countdownTarget':     'date',
    'route':               'str',
    'deltaMin':            'int',
    'generatedAt':         'date',
    # ── P6 / stateVersion 2, in Swift alle optional ──────────────────────────
    'displayTZIdentifier': 'str',
    'mainLabel':           'str',
    'phaseLabel':          'str',
    'fromIATA':            'str',
    'toIATA':              'str',
    'fromCity':            'str',
    'toCity':              'str',
    'schedDep':            'date',
    'estDep':              'date',
    'schedArr':            'date',
    'estArr':              'date',
    'progress':            'num',
    'chain':               'chain',
    'footLeading':         'str',
    'footTrailing':        'str',
    'cancelled':           'bool',
    'rosterFrozen':        'bool',
    # In Swift `String?` (DutyActivityAttributes.ContentState.rosterFrozenNote,
    # z.B. „Dienstplan-Verbindung erneuern"). Fehlte hier: der Normalizer warf
    # den Key als unknown_key weg und die Lockscreen-Karte verlor den Satz.
    'rosterFrozenNote':    'str',
    # ── Nachzug 2026-07-29: die Felder, die Swift seit dem 27.07. hat ────────
    #
    # ⚠️ ALLE FÜNF WAREN HIER NICHT GELISTET — der Normalizer hat sie als
    # `unknown_key` VERWORFEN. Der Vertrag ist damit still auseinandergelaufen:
    # ein Sender durfte sie schicken, angekommen sind sie nie.
    #
    # `fromTZIdentifier`/`toTZIdentifier` sind der Grund für diesen Nachzug.
    # Der Client leitet daraus die ORTSZONE der Marke ab
    # (`DutyActivityAttributes.ContentState.markZone` → `DutyAnchor.markZone`,
    # dieselbe Regel wie im Home-Widget). Fehlen sie, rendert die Karte in der
    # Anzeige-Zone — bei einem Dienstbeginn in San Francisco also die
    # Frankfurter Uhrzeit, während das Widget daneben die Ortszeit zeigt.
    # Ein Push OHNE diese Felder überschreibt einen Zustand MIT ihnen (APNs
    # ersetzt das ContentState vollständig) und dreht den Fehler damit zurück.
    'fromTZIdentifier':    'str',
    'toTZIdentifier':      'str',
    # Flugnummer des AKTIVEN Legs. Wandert mit der Phase (nach dem Turnaround
    # LH583 statt der beim Start eingefrorenen LH582) — sie hier zu verwerfen
    # hieß: die Pille zeigt weiter den Hinflug.
    'flightNo':            'str',
    # Tages-Angaben des Writers („Do 30.07"), Fußzeile der aufgeklappten Insel.
    'mainTimeIsToday':     'bool',
    'mainTimeDayLabel':    'str',
}

# Nicht-optional in Swift ⇒ Key MUSS im JSON stehen (der synthetisierte
# Decoder nutzt KEINE Property-Defaults, auch nicht bei `= 2`).
_REQUIRED_CONTENT_KEYS = ('stateVersion', 'phase', 'kicker', 'mainTime',
                          'generatedAt')

# DutyActivityAttributes (statisch, nur beim Push-to-Start). `startedAt` ist
# nicht-optional ⇒ Pflicht.
_ATTRIBUTE_FIELDS = {'flightNo': 'str', 'from': 'str', 'to': 'str',
                     'startedAt': 'date'}
_REQUIRED_ATTRIBUTE_KEYS = ('startedAt',)

_CHAIN_FIELDS = {'label': 'str', 'time': 'date', 'state': 'str'}


def _to_apple_date(value):
    """Beliebige Zeitangabe → Sekunden seit Apples Referenzdatum (Float) oder
    None. Die EINZIGE Umrechnungsstelle im Modul.

    Akzeptiert `datetime` (naiv = UTC), ISO-8601-String und Zahlen.
    ZAHLEN SIND IMMER UNIX-SEKUNDEN (`time.time()`-Domäne) — bewusst KEINE
    Heuristik „ist das schon konvertiert?": Referenzdatum-Sekunden für 2026
    (~8.1e8) und Unix-Sekunden für 1995 (~8.1e8) sind nicht unterscheidbar, und
    ein falsch geratenes Datum steht dem User auf dem Lockscreen. Wer schon
    umgerechnet hat, übergibt keinen Rohwert an diese Funktion.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        d = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return d.timestamp() - _APPLE_EPOCH_OFFSET
    if isinstance(value, (int, float)):
        return float(value) - _APPLE_EPOCH_OFFSET
    s = str(value).strip()
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace('Z', '+00:00'))
    except Exception:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp() - _APPLE_EPOCH_OFFSET


def _coerce(kind, value):
    """(ok, coerced). `ok=False` ⇒ Wert passt nicht zum Swift-Typ."""
    if kind == 'str':
        if isinstance(value, str):
            return True, value
        return False, None
    if kind == 'int':
        if isinstance(value, bool):
            return False, None
        if isinstance(value, int):
            return True, value
        if isinstance(value, float) and float(value).is_integer():
            return True, int(value)
        return False, None
    if kind == 'num':
        if isinstance(value, bool):
            return False, None
        if isinstance(value, (int, float)):
            return True, float(value)
        return False, None
    if kind == 'bool':
        if isinstance(value, bool):
            return True, value
        return False, None
    if kind == 'date':
        conv = _to_apple_date(value)
        return (conv is not None), conv
    return False, None


def _normalize_chain(raw, problems):
    """`chain` → Liste flacher ChainStep-Dicts. Ein kaputter Schritt kippt die
    ganze Kette (eine halb dekodierte Kette gibt es in Swift nicht: `[ChainStep]`
    ist all-or-nothing, ein Fehler verwirft das GESAMTE ContentState)."""
    if not isinstance(raw, list):
        problems.append('bad_type:chain')
        return None
    out = []
    for i, step in enumerate(raw):
        if not isinstance(step, dict):
            problems.append(f'bad_type:chain[{i}]')
            return None
        clean = {}
        for key, value in step.items():
            if key not in _CHAIN_FIELDS:
                problems.append(f'unknown_key:chain[].{key}')
                continue
            if value is None:
                continue
            ok, conv = _coerce(_CHAIN_FIELDS[key], value)
            if not ok:
                problems.append(f'bad_type:chain[{i}].{key}')
                return None
            clean[key] = conv
        missing = [k for k in _CHAIN_FIELDS if k not in clean]
        if missing:
            problems.append('missing:chain[%d].%s' % (i, ','.join(missing)))
            return None
        out.append(clean)
    return out


def _normalize_content_state(d):
    """Roh-Dict → APNs-taugliches `content-state`. Returns (state, problems).

    Regeln (alle drei sind Bugfixes, keine Kosmetik):
      • `None` wird GEDROPPT, nie als JSON-`null` gesendet. Ein `null` auf einem
        nicht-optionalen Swift-Feld sprengt das Decoding; ein optionales Feld
        verträgt Absenz problemlos.
      • Unbekannte Keys werden verworfen + geloggt („refused"). Swifts
        synthetisierter Decoder ignoriert Extra-Keys zwar, aber ein unbekannter
        Key heißt fast immer Tippfehler oder Contract-Drift — still schlucken
        wäre der teure Weg.
      • Date-Felder werden in Apple-Referenzdatum-Sekunden umgerechnet (siehe
        Modul-Docstring).
    Fatal (⇒ Caller antwortet 400) sind nur `missing:` und `bad_type:`.
    """
    problems = []
    if not isinstance(d, dict):
        return None, ['bad_type:content_state']
    state = {}
    for key, value in d.items():
        kind = _CONTENT_FIELDS.get(key)
        if kind is None:
            problems.append(f'unknown_key:{key}')
            continue
        if value is None:
            continue
        if kind == 'chain':
            chain = _normalize_chain(value, problems)
            if chain is None:
                continue
            state['chain'] = chain
            continue
        ok, conv = _coerce(kind, value)
        if not ok:
            problems.append(f'bad_type:{key}')
            continue
        state[key] = conv
    missing = [k for k in _REQUIRED_CONTENT_KEYS if k not in state]
    if missing:
        problems.append('missing:' + ','.join(missing))
    refused = [p.split(':', 1)[1] for p in problems if p.startswith('unknown_key:')]
    if refused:
        log.warning('[live-activity] content-state refused unknown keys: %s',
                    ','.join(sorted(refused)))
    return state, problems


def _normalize_attributes(d):
    """Statische `attributes` für den Push-to-Start. Returns (attrs, problems)."""
    problems = []
    if not isinstance(d, dict):
        return None, ['bad_type:attributes']
    attrs = {}
    for key, value in d.items():
        kind = _ATTRIBUTE_FIELDS.get(key)
        if kind is None:
            problems.append(f'unknown_key:attributes.{key}')
            continue
        if value is None:
            continue
        ok, conv = _coerce(kind, value)
        if not ok:
            problems.append(f'bad_type:attributes.{key}')
            continue
        attrs[key] = conv
    missing = [k for k in _REQUIRED_ATTRIBUTE_KEYS if k not in attrs]
    if missing:
        problems.append('missing:attributes.' + ','.join(missing))
    return attrs, problems


def _fatal_problems(problems):
    """Nur `missing:`/`bad_type:` sind fatal — unbekannte Keys sind verworfen
    und geloggt, aber kein Grund, ein sonst gültiges Update fallen zu lassen."""
    return [p for p in (problems or []) if not p.startswith('unknown_key:')]


def _content_digest(state):
    """sha256 des content-state OHNE `generatedAt`.

    `generatedAt` ist ein reiner Stand-Stempel und ändert sich bei JEDEM Lauf —
    wäre er im Digest, wäre der Rate-Limit-Schutz wirkungslos. Ebenfalls
    ausgenommen: nichts weiter. `progress` z.B. IST eine echte Änderung.
    """
    payload = {k: v for k, v in (state or {}).items() if k != 'generatedAt'}
    blob = json.dumps(payload, sort_keys=True, separators=(',', ':'),
                      default=str)
    return hashlib.sha256(blob.encode('utf-8')).hexdigest()


# ── Registry ────────────────────────────────────────────────────────────────

_ROW_SELECT = ('id,user_token,kind,activity_id,la_token,bundle_id,environment,'
               'active,content_digest,last_timestamp,failure_count')


def _active_rows(user_token, kind, activity_id=None):
    """Aktive Registry-Zeilen eines Users. Wirft nie; [] bei SB-Ausfall."""
    client = _sb()
    if client is None or not user_token:
        return []
    try:
        q = (client.table('live_activities').select(_ROW_SELECT)
             .eq('user_token', user_token).eq('kind', kind)
             .eq('active', True))
        if activity_id:
            q = q.eq('activity_id', activity_id)
        rows = (q.execute().data or [])
        return [r for r in rows if (r or {}).get('la_token')]
    except Exception as exc:
        log.warning('[live-activity] rows read failed user_ref=%s kind=%s: %s',
                    _token_ref(user_token), kind, type(exc).__name__)
        return []


def _rpc(name, params):
    """(ok, data). Wirft nie — ein fehlendes RPC (Migration nicht appliziert)
    ist ein Betriebsfehler, kein 500 für den Client."""
    client = _sb()
    if client is None:
        return False, None
    try:
        return True, client.rpc(name, params).execute().data
    except Exception as exc:
        log.warning('[live-activity] rpc %s failed: %s: %s', name,
                    type(exc).__name__, str(exc)[:140])
        return False, None


def _mark_result(row, ok, digest=None, timestamp=None, dead=False,
                 reason=None, environment=None):
    """Sende-Ergebnis festschreiben — DB (autoritativ) + In-Process-Spiegel."""
    row_id = (row or {}).get('id')
    with _state_lock:
        if len(_LAST_SENT) > _LAST_SENT_MAX:
            _LAST_SENT.clear()
        slot = _LAST_SENT.setdefault(row_id, {})
        slot['activity_id'] = (row or {}).get('activity_id')
        if timestamp is not None:
            slot['ts'] = max(int(timestamp), int(slot.get('ts') or 0))
        if ok and digest:
            slot['digest'] = digest
        if dead:
            slot['dead'] = True
    if not row_id:
        return
    _rpc('live_activity_mark_result', {
        'p_id': row_id, 'p_ok': bool(ok), 'p_digest': digest,
        'p_timestamp': int(timestamp) if timestamp is not None else None,
        'p_dead': bool(dead), 'p_reason': reason, 'p_environment': environment,
    })


def _row_digest(row):
    """Zuletzt ERFOLGREICH gesendeter Digest. In-Process gewinnt, weil er auch
    dann stimmt, wenn der DB-Write gerade nicht durchkam."""
    with _state_lock:
        local = (_LAST_SENT.get((row or {}).get('id')) or {}).get('digest')
    return local or (row or {}).get('content_digest')


def _next_timestamp(row):
    """Strikt monotoner `aps.timestamp` (Unix-Sekunden). iOS verwirft ein
    Update, dessen timestamp <= dem letzten gesehenen ist — zwei Pushes in
    derselben Sekunde (Roster-Refresh + MQTT-Event) wären sonst einer."""
    with _state_lock:
        local = int((_LAST_SENT.get((row or {}).get('id')) or {}).get('ts') or 0)
    try:
        stored = int((row or {}).get('last_timestamp') or 0)
    except (TypeError, ValueError):
        stored = 0
    return max(int(time.time()), max(local, stored) + 1)


# ── APNs-Sender (Live-Activity-eigen) ───────────────────────────────────────
#
# app.py `_send_apns` ist hier NICHT benutzbar: es hardcodet
# `apns-push-type: 'alert'` und kennt kein Topic-Suffix. JWT und der
# HTTP/2-Pool werden dagegen wiederverwendet (ein Prozess-weiter Pool, ein
# 50-min-gecachtes ES256-JWT — beides nicht duplizieren).

def _send_live_activity_apns(la_token, bundle_id, payload, use_sandbox,
                             priority='10', retry_env_planned=False):
    """Returns (ok, reason). `reason` = APNs-`reason` für die Token-Hygiene."""
    get_jwt = _app_attr('_apns_get_jwt')
    http_client = _app_attr('_apns_http_client')
    if not callable(get_jwt) or not callable(http_client):
        return False, 'no_apns_infra'
    jwt = get_jwt()
    if not jwt:
        return False, 'no_jwt'
    topic_base = (bundle_id or os.environ.get('APNS_TOPIC')
                  or _DEFAULT_BUNDLE_ID).strip()
    host = ('api.sandbox.push.apple.com' if use_sandbox
            else 'api.push.apple.com')
    try:
        client = http_client()
    except ImportError:
        log.warning("[live-activity] httpx missing — add 'httpx[http2]'")
        return False, 'no_httpx'
    except Exception as exc:
        log.warning('[live-activity] apns client unavailable: %s',
                    type(exc).__name__)
        return False, 'no_httpx'
    headers = {
        'authorization': f'bearer {jwt}',
        # Das Suffix ist PFLICHT. Ohne es: 400 BadTopic für JEDEN Push.
        'apns-topic': topic_base + _LA_TOPIC_SUFFIX,
        'apns-push-type': 'liveactivity',
        'apns-priority': str(priority),
        'apns-expiration': str(int(time.time()) + _EXPIRATION_S),
    }
    try:
        resp = client.post(f'https://{host}/3/device/{la_token}',
                           headers=headers,
                           content=json.dumps(payload).encode('utf-8'))
    except Exception as exc:
        log.warning('[live-activity] transport error: %s', type(exc).__name__)
        return False, 'transport_error'
    if getattr(resp, 'status_code', 0) == 200:
        return True, None
    reason = None
    try:
        reason = (resp.json() or {}).get('reason')
    except Exception:
        pass
    env_name = 'sandbox' if use_sandbox else 'prod'
    if retry_env_planned and reason in _DEAD_REASONS:
        # ERWARTETER Erstversuch in der falschen Umgebung (Debug-Build =
        # Sandbox-Token, prod antwortet BadDeviceToken). Kein „send failed"-Log,
        # sonst feuert der Log-Metric-Alert bei jedem Dev-Gerät.
        log.info('[live-activity] env probe rejected env=%s reason=%s '
                 '(retrying other env)', env_name, reason)
    else:
        log.warning('[live-activity] send failed status=%s env=%s reason=%s',
                    getattr(resp, 'status_code', '?'), env_name, reason)
    return False, reason or f'http_{getattr(resp, "status_code", 0)}'


def _build_aps(state, event, timestamp, attributes=None, stale_after_s=None,
               dismiss_after_s=None, relevance=None, alert=None):
    """Der `aps`-Block. Epochen NICHT vertauschen: timestamp/stale-date/
    dismissal-date sind Unix-Sekunden, die Dates IN content-state/attributes
    sind Apple-Referenzdatum-Sekunden (siehe Modul-Docstring)."""
    aps = {'timestamp': int(timestamp), 'event': event,
           'content-state': state}
    if event == 'start':
        aps['attributes-type'] = _ATTRIBUTES_TYPE
        aps['attributes'] = attributes or {}
    if stale_after_s:
        try:
            aps['stale-date'] = int(timestamp) + int(stale_after_s)
        except (TypeError, ValueError):
            pass
    if event == 'end' and dismiss_after_s is not None:
        # dismissal-date gibt es NUR bei event='end'. Bei 'update' ignoriert
        # iOS ihn stillschweigend — das wäre eine Activity, die nie verschwindet.
        try:
            aps['dismissal-date'] = int(timestamp) + int(dismiss_after_s)
        except (TypeError, ValueError):
            pass
    if relevance is not None:
        try:
            aps['relevance-score'] = float(relevance)
        except (TypeError, ValueError):
            pass
    if isinstance(alert, dict) and alert:
        aps['alert'] = alert
    return {'aps': aps}


def _push_row(row, state, event='update', attributes=None, stale_after_s=None,
              dismiss_after_s=None, relevance=None, priority='10', alert=None,
              digest=None, force=False):
    """Ein Push an EINE Registry-Zeile. Returns dict mit `status` in
    ('sent', 'unchanged', 'failed', 'dead')."""
    digest = digest or _content_digest(state)
    if not force and event != 'end' and _row_digest(row) == digest:
        # Rate-Limit: identischer Inhalt ⇒ kein Push. Apple drosselt hart, und
        # ein Update, das nichts ändert, kostet nur Budget.
        return {'status': 'unchanged', 'activity_id': row.get('activity_id')}

    timestamp = _next_timestamp(row)
    payload = _build_aps(state, event, timestamp, attributes=attributes,
                         stale_after_s=stale_after_s,
                         dismiss_after_s=dismiss_after_s,
                         relevance=relevance, alert=alert)
    bundle_id = row.get('bundle_id') or _DEFAULT_BUNDLE_ID
    la_token = row.get('la_token') or ''

    env_pref = _normalized_env(row.get('environment'))
    if env_pref in ('prod', 'sandbox'):
        first_sandbox = env_pref == 'sandbox'
    else:
        first_sandbox = os.environ.get('APNS_USE_SANDBOX', '').strip() == '1'

    ok, reason = _send_live_activity_apns(
        la_token, bundle_id, payload, first_sandbox, priority=priority,
        retry_env_planned=True)
    used_env = 'sandbox' if first_sandbox else 'prod'
    dead = False
    if not ok and reason in _DEAD_REASONS:
        # GEGEN-UMGEBUNG. Genau hier ist der Fehler, der dieses Projekt schon
        # einmal Geld gekostet hat: ein Debug-Build liefert einen SANDBOX-Token,
        # der prod-Send antwortet 400 BadDeviceToken — eine naive Implementierung
        # löscht damit einen völlig gesunden Token. Erst wenn BEIDE Umgebungen
        # „tot" sagen, ist er tot.
        alt_sandbox = not first_sandbox
        ok2, reason2 = _send_live_activity_apns(
            la_token, bundle_id, payload, alt_sandbox, priority=priority)
        if ok2:
            ok, reason = True, None
            used_env = 'sandbox' if alt_sandbox else 'prod'
        else:
            reason = reason2 or reason
            dead = reason2 in _DEAD_REASONS

    _mark_result(row, ok, digest=digest, timestamp=timestamp, dead=dead,
                 reason=('apns_' + str(reason).lower()) if dead else reason,
                 environment=used_env if ok else None)
    if ok:
        log.info('[live-activity] push ok user_ref=%s kind=%s event=%s env=%s '
                 'token_ref=%s', _token_ref(row.get('user_token')),
                 row.get('kind'), event, used_env, _token_ref(la_token))
        return {'status': 'sent', 'activity_id': row.get('activity_id'),
                'env': used_env, 'timestamp': timestamp}
    if dead:
        log.warning('[live-activity] token dead in BOTH envs — row deactivated '
                    'user_ref=%s token_ref=%s reason=%s',
                    _token_ref(row.get('user_token')), _token_ref(la_token),
                    reason)
    return {'status': 'dead' if dead else 'failed',
            'activity_id': row.get('activity_id'), 'reason': reason}


def push_live_activity(user_token, content_state, event='update',
                       attributes=None, activity_id=None, stale_after_s=None,
                       dismiss_after_s=None, relevance=None, priority='10',
                       alert=None, force=False):
    """ÖFFENTLICHE Push-Funktion (auch für andere Blueprints/Daemons).

    Zielwahl: alle aktiven `update`-Zeilen des Users. Existiert keine, aber eine
    `start`-Zeile UND `attributes`, wird per Push-to-Start eine Activity
    ERZEUGT (der Fall „App war nie offen, Dienst beginnt").

    Returns dict {'ok', 'sent', 'unchanged', 'failed', 'dead', 'event', ...}.
    Wirft nie.
    """
    state, problems = _normalize_content_state(content_state)
    fatal = _fatal_problems(problems)
    if fatal:
        return {'ok': False, 'error': 'invalid_content_state',
                'problems': fatal}

    rows = _active_rows(user_token, 'update', activity_id=activity_id)
    used_event = event if event in ('update', 'end') else 'update'
    attrs = None
    if not rows and used_event != 'end' and attributes:
        attrs, attr_problems = _normalize_attributes(attributes)
        attr_fatal = _fatal_problems(attr_problems)
        if attr_fatal:
            return {'ok': False, 'error': 'invalid_attributes',
                    'problems': attr_fatal}
        rows = _active_rows(user_token, 'start')
        if rows:
            used_event = 'start'
    if not rows:
        return {'ok': True, 'sent': 0, 'skipped': 'no_target',
                'event': used_event}

    digest = _content_digest(state)
    results = []
    for row in rows:
        try:
            results.append(_push_row(
                row, state, event=used_event, attributes=attrs,
                stale_after_s=stale_after_s, dismiss_after_s=dismiss_after_s,
                relevance=relevance, priority=priority, alert=alert,
                digest=digest, force=force))
        except Exception as exc:
            log.warning('[live-activity] push crashed user_ref=%s: %s',
                        _token_ref(user_token), type(exc).__name__)
            results.append({'status': 'failed', 'reason': 'internal'})
    counts = {k: sum(1 for r in results if r.get('status') == k)
              for k in ('sent', 'unchanged', 'failed', 'dead')}
    out = {'ok': True, 'event': used_event, 'targets': len(rows), **counts}
    if counts['sent'] == 0 and counts['unchanged'] == len(rows):
        out['skipped'] = 'unchanged'
    return out


# ── Routes ──────────────────────────────────────────────────────────────────

@live_activity_bp.route('/api/push/register-live-activity', methods=['POST'])
def register_live_activity():
    """Body: {token, la_token, kind, bundle_id, apns_env, platform,
    activity_id?, device_id?} + `Authorization: Bearer <token>`.

    Shape 1:1 aus iOS `PushService.uploadLiveActivityToken`. Der Client setzt
    seinen lastSent-Marker NUR bei 2xx — ein 503 hier heißt also „nächster
    Launch versucht es erneut", genau richtig bei SB-Ausfall. Deshalb wird hier
    NIE ein Erfolg vorgetäuscht.
    """
    body = request.get_json(silent=True) or {}
    user_token = (body.get('token') or '').strip()
    la_token = (body.get('la_token') or '').strip()
    kind = (body.get('kind') or '').strip().lower()
    if not user_token or not la_token or kind not in ('start', 'update'):
        return jsonify({'ok': False, 'error': 'missing_fields'}), 400

    denied = _auth_body_token(user_token)
    if denied is not None:
        return denied

    activity_id = (body.get('activity_id') or '').strip() or None
    if kind == 'update' and not activity_id:
        # Toleriert (der Token ist echt und pushbar), aber auffällig: ohne
        # activity_id kann /api/live-activity/end ihn nicht gezielt schließen
        # und zwei parallele Activities würden dieselbe Zeile teilen.
        log.warning('[live-activity] update token without activity_id '
                    'user_ref=%s', _token_ref(user_token))

    ok, row_id = _rpc('upsert_live_activity', {
        'p_user_token': user_token,
        'p_kind': kind,
        'p_la_token': la_token,
        'p_activity_id': activity_id,
        'p_bundle_id': (body.get('bundle_id') or '').strip() or None,
        'p_environment': _normalized_env(body.get('apns_env')),
        'p_device_id': (body.get('device_id') or '').strip() or None,
        'p_platform': (body.get('platform') or 'ios').strip() or 'ios',
    })
    if not ok:
        return jsonify({'ok': False,
                        'error': 'live_activity_store_unavailable'}), 503
    log.info('[live-activity] registered user_ref=%s kind=%s env=%s '
             'token_ref=%s activity=%s', _token_ref(user_token), kind,
             _normalized_env(body.get('apns_env')), _token_ref(la_token),
             (activity_id or '-')[:24])
    return jsonify({'ok': True, 'kind': kind, 'activity_id': activity_id,
                    'stored': True})


@live_activity_bp.route('/api/live-activity/optout', methods=['POST'])
def optout_live_activity():
    """Body: {token}. Owner 2026-07-29 („option to turn off live activities"):
    ALLE gespeicherten Live-Activity-Tokens des Users löschen — auch die
    push-to-START-Tokens, sonst erzeugt das Backend die Lockscreen-Karte beim
    nächsten Flug einfach neu. Re-Opt-in braucht keine Server-Seite: der
    Client lädt seine Tokens beim nächsten Start wieder hoch (PushService).
    Idempotent — doppeltes Opt-out ist ok."""
    body = request.get_json(silent=True) or {}
    user_token = (body.get('token') or '').strip()
    if not user_token:
        return jsonify({'ok': False, 'error': 'missing_fields'}), 400
    denied = _auth_body_token(user_token)
    if denied is not None:
        return denied
    client = _sb()
    if client is None:
        return jsonify({'ok': False,
                        'error': 'live_activity_store_unavailable'}), 503
    try:
        client.table('live_activities').delete() \
            .eq('user_token', user_token).execute()
    except Exception as exc:
        log.warning('[live-activity] optout failed user_ref=%s: %s',
                    _token_ref(user_token), type(exc).__name__)
        return jsonify({'ok': False,
                        'error': 'live_activity_store_unavailable'}), 503
    with _state_lock:
        _LAST_SENT.clear()
    log.info('[live-activity] OPTOUT user_ref=%s — alle Tokens gelöscht',
             _token_ref(user_token))
    return jsonify({'ok': True})


@live_activity_bp.route('/api/live-activity/end', methods=['POST'])
def end_live_activity():
    """Body: {token, activity_id}. Der Client hat die Activity beendet oder
    der User hat sie weggewischt → Zeile stilllegen, damit der Fanout nicht
    weiter gegen einen toten Token pusht. Idempotent: `ended: 0` ist ok."""
    body = request.get_json(silent=True) or {}
    user_token = (body.get('token') or '').strip()
    activity_id = (body.get('activity_id') or '').strip()
    if not user_token or not activity_id:
        return jsonify({'ok': False, 'error': 'missing_fields'}), 400

    denied = _auth_body_token(user_token)
    if denied is not None:
        return denied

    ok, data = _rpc('end_live_activity', {
        'p_user_token': user_token, 'p_activity_id': activity_id,
        'p_reason': 'client'})
    if not ok:
        return jsonify({'ok': False,
                        'error': 'live_activity_store_unavailable'}), 503
    try:
        ended = int(data if not isinstance(data, list) else (data or [0])[0])
    except (TypeError, ValueError):
        ended = 0
    with _state_lock:
        for key, slot in list(_LAST_SENT.items()):
            if slot.get('activity_id') == activity_id:
                _LAST_SENT.pop(key, None)
    log.info('[live-activity] ended user_ref=%s activity=%s rows=%d',
             _token_ref(user_token), activity_id[:24], ended)
    return jsonify({'ok': True, 'ended': ended})


@live_activity_bp.route('/api/internal/live-activity/push', methods=['POST'])
def internal_live_activity_push():
    """NUR intern (Daemon/Cron). Body: {user_token, content_state, event?,
    attributes?, activity_id?, stale_after_s?, dismiss_after_s?, relevance?,
    priority?, alert?, force?}.

    Kein User-facing Endpoint: wer hier reindarf, kann jedem User beliebige
    Inhalte auf den Lockscreen schreiben — daher dasselbe Secret-Gate wie
    /api/internal/lh-mqtt/event.
    """
    if not _secret_ok():
        return jsonify({'ok': False, 'error': 'forbidden'}), 403
    body = request.get_json(silent=True) or {}
    user_token = (body.get('user_token') or body.get('token') or '').strip()
    content_state = body.get('content_state')
    if not user_token or not isinstance(content_state, dict):
        return jsonify({'ok': False, 'error': 'missing_fields'}), 400
    event = (body.get('event') or 'update').strip().lower()
    if event not in ('update', 'end'):
        return jsonify({'ok': False, 'error': 'bad_event'}), 400

    result = push_live_activity(
        user_token, content_state, event=event,
        attributes=body.get('attributes'),
        activity_id=(body.get('activity_id') or '').strip() or None,
        stale_after_s=body.get('stale_after_s'),
        dismiss_after_s=body.get('dismiss_after_s'),
        relevance=body.get('relevance'),
        priority=str(body.get('priority') or '10'),
        alert=body.get('alert'),
        force=bool(body.get('force')))
    if not result.get('ok'):
        return jsonify(result), 400
    return jsonify(result)


# ── MQTT-Fanout-Hook (öffentlich, VERDRAHTET aus lh_mqtt.lh_mqtt_event) ─────

_MQTT_PHASE_KICKER = {
    'est_dep':   ('briefing', 'ABFLUG'),
    'cancelled': ('briefing', 'GESTRICHEN'),
    'diverted':  ('inFlight', 'UMLEITUNG'),
    'departed':  ('inFlight', 'GESTARTET'),
    # est_arr fehlte hier KOMPLETT (Owner 2026-07-28: „arrival time was wrong
    # the whole time, animation not flowing") — LHs ACARS-ETA-Updates während
    # des Flugs wurden verworfen, die Karte zählte bis zur beim Abflug
    # eingefrorenen (oft: Roster-SOLL-)Ankunft herunter.
    'est_arr':   ('inFlight', 'ANKUNFT'),
    'arrived':   ('turnaround', 'GELANDET'),
}

# Event-Arten, deren Karten-Zeitpunkt die ANKUNFT ist (nicht der Abflug).
_MQTT_ARRIVAL_KINDS = ('departed', 'diverted', 'est_arr', 'arrived')


def push_for_affected(affected, kind, flight_disp, topic_date, facts=None):
    """Live-Activity-Fanout für ein LH-MQTT-Event. Returns Anzahl gesendeter
    Pushes (unverändert/skip zählt NICHT).

    `affected` ist exakt die Liste aus `lh_mqtt._users_for_flight(...)`:
    `[(user_token, sector_dict), …]`.

    VERDRAHTET (P7, 2026-07-27): der Aufruf steht in `lh_mqtt.lh_mqtt_event`
    (Abschnitt „LIVE-ACTIVITY-FANOUT", nach dem Alert-Push-Block, nur bei
    nicht-leerem `affected`; Zähler `la_sent`). ⚠️ KEINEN zweiten Aufruf
    ergänzen — ein Doppel-Fanout wäre der direkte Weg in Apples
    Live-Activity-Throttle.

    Bewusst konservativ: nur Events, die den ANGEZEIGTEN Zustand ändern, und
    kein erfundenes Datum — fehlt eine Zeit in den Fakten, bleibt das Feld weg
    (Swift rendert die Zeile dann nicht, statt „+0" zu behaupten).
    """
    if not affected or kind not in _MQTT_PHASE_KICKER:
        return 0
    facts = facts or {}
    phase, kicker = _MQTT_PHASE_KICKER[kind]
    now_iso = datetime.now(timezone.utc)
    sent = 0
    for entry in affected:
        try:
            user_token, sector = entry[0], (entry[1] or {})
        except Exception:
            continue
        frm = (sector.get('from') or '').strip().upper() or None
        to = (sector.get('to') or '').strip().upper() or None
        est_dep = facts.get('est_dep') or sector.get('dep_iso')
        est_arr = facts.get('est_arr') or sector.get('arr_iso')
        delta = facts.get('dep_delay_min')
        state = {
            'stateVersion': 2,
            'phase': phase,
            'kicker': kicker,
            # mainTime = der Moment, auf den die Karte zeigt. Ab dem Abflug
            # (und für jedes ETA-Update) ist das die ANKUNFT — mit frisch
            # geforcten `facts` (lh_mqtt) also die echte LH-Schätzung, nicht
            # mehr die beim Abflug eingefrorene Plan-Zeit.
            'mainTime': est_arr if kind in _MQTT_ARRIVAL_KINDS else est_dep,
            'countdownTarget': (est_arr if kind in _MQTT_ARRIVAL_KINDS
                                else est_dep),
            'route': f'{frm}–{to}' if frm and to else None,
            'deltaMin': delta if isinstance(delta, int) else None,
            'generatedAt': now_iso,
            'fromIATA': frm,
            'toIATA': to,
            # ORTSZONEN DER BEIDEN STRECKENENDEN (2026-07-29).
            #
            # Ohne sie fällt der Client auf die Anzeige-Zone zurück — und weil
            # dieser Fanout das ContentState VOLLSTÄNDIG ersetzt, hätte ein
            # einziges MQTT-Event die Ortszeit auf dem Sperrbildschirm wieder
            # in Homebase-Zeit gedreht, während das Home-Widget weiter die
            # Ortszeit zeigt. Genau diesen Widerspruch soll die Runde beenden.
            #
            # Nachgeschlagen wird in der kuratierten IATA→IANA-Tabelle, nicht
            # geraten: unbekannter Code ⇒ None ⇒ Feld fällt weg ⇒ der Client
            # behauptet keine Ortszeit (`DutyAnchor.markZone` liefert dann
            # `nil`).
            'fromTZIdentifier': _airport_tz(frm),
            'toTZIdentifier': _airport_tz(to),
            'schedDep': facts.get('sched_dep') or sector.get('dep_iso'),
            'estDep': est_dep,
            'schedArr': facts.get('sched_arr') or sector.get('arr_iso'),
            'estArr': est_arr,
            'cancelled': True if kind == 'cancelled' else None,
            'footLeading': flight_disp or None,
        }
        if state['mainTime'] is None:
            # Ohne Ziel-Zeitpunkt gibt es keine ehrliche Karte.
            continue
        try:
            res = push_live_activity(user_token, state, event='update',
                                     priority='10')
            sent += int(res.get('sent') or 0)
        except Exception as exc:
            log.warning('[live-activity] mqtt fanout failed user_ref=%s: %s',
                        _token_ref(user_token), type(exc).__name__)
    if sent:
        log.info('[live-activity] mqtt fanout flight=%s date=%s kind=%s sent=%d',
                 flight_disp, topic_date, kind, sent)
    return sent
