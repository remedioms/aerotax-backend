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
from datetime import datetime, timedelta, timezone

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
    # ── Nachzug 2026-07-31: BESTÄTIGT gegen GEPLANT ─────────────────────────
    #
    # Owner-Entscheid nach seinem eigenen Flug und Tibors Screenshot: „wenn
    # bestätigte zeiten sind dann kann die aktivität weiterlaufen … aber keine
    # infos geht nicht. muss schon richtig sein sonst weg."
    #
    # Der Client darf NUR dann ohne Netz fortschalten, wenn ein Fakt BELEGT ist
    # (Board sagt departed/airborne/landed) — eine bloß verstrichene PLANZEIT
    # belegt nichts, die Maschine kann am Gate stehen. Genau das war der Fehler
    # „Flug-Animation ohne Pushback".
    #
    # ⚠️ DRITTES MAL DIESELBE FALLE: fehlt ein Feld hier, wirft der Normalizer
    # es als `unknown_key` weg — der Sender darf es schicken, ankommen tut es
    # nie. So verschwand am 27.07. die Turnaround-Korrektur für drei Tage, und
    # am 29.07. fünf Felder auf einmal (s. Banner oben). Wer in Swift ein Feld
    # an `ContentState` hängt, MUSS es hier eintragen.
    #
    # Fehlen sie (alter Sender), ist das konservativ richtig: der Client wertet
    # `nil` als „nicht bestätigt" und schaltet dann gar nicht fort.
    'depConfirmed':        'bool',
    'arrConfirmed':        'bool',
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


def _normalize_content_state(d, with_cleared=False):
    """Roh-Dict → APNs-taugliches `content-state`. Returns (state, problems)
    bzw. (state, problems, cleared) mit `with_cleared=True`.

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

    GRABSTEIN (`cleared`, 2026-08-13): ein explizites JSON-`null` wird zwar
    weiterhin GEDROPPT (s.o. — Swift verträgt kein `null`), sein Key landet
    aber in `cleared`. Nur so kann die Feld-Erhaltung unten zwischen „kennt
    das Feld nicht" (⇒ alten Wert behalten) und „hat es BEWUSST geleert"
    (⇒ nicht wieder einsetzen) unterscheiden; ohne den Grabstein klebte eine
    einmal gesendete Dienst-Kette für immer auf dem Sperrbildschirm.
    Der `with_cleared`-Schalter hält die bestehende 2-Tupel-Signatur intakt.
    """
    problems = []
    cleared = set()
    if not isinstance(d, dict):
        return ((None, ['bad_type:content_state'], cleared) if with_cleared
                else (None, ['bad_type:content_state']))
    state = {}
    for key, value in d.items():
        kind = _CONTENT_FIELDS.get(key)
        if kind is None:
            problems.append(f'unknown_key:{key}')
            continue
        if value is None:
            cleared.add(key)
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
    return (state, problems, cleared) if with_cleared else (state, problems)


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

# Der ALTE Spaltensatz (vor Migration 20260813_live_activity_last_state.sql).
_ROW_SELECT_LEGACY = ('id,user_token,kind,activity_id,la_token,bundle_id,'
                      'environment,active,content_digest,last_timestamp,'
                      'failure_count')
_ROW_SELECT = _ROW_SELECT_LEGACY + ',last_content_state'
# DEPLOY-SCHUTZ (Lehre fcm_token 01.08.): fehlt die Spalte, antwortet PostgREST
# mit 42703 und der Read lieferte [] — JEDER Push wäre still übersprungen
# gewesen, obwohl Tokens da sind. Ein fehlendes Feld darf ein Feature bremsen,
# nie abschalten. `until` = weiche Sperre; sie heilt von selbst, sobald die
# Migration durch ist (kein Neustart nötig).
_ROW_SELECT_FALLBACK = {'until': 0.0}
_ROW_SELECT_FALLBACK_S = 10 * 60


def _rows_query(client, select, user_token, kind, activity_id=None):
    q = (client.table('live_activities').select(select)
         .eq('user_token', user_token).eq('kind', kind).eq('active', True))
    if activity_id:
        q = q.eq('activity_id', activity_id)
    return q


def _active_rows(user_token, kind, activity_id=None):
    """Aktive Registry-Zeilen eines Users. Wirft nie; [] bei SB-Ausfall."""
    client = _sb()
    if client is None or not user_token:
        return []
    legacy_first = time.time() < (_ROW_SELECT_FALLBACK.get('until') or 0.0)
    try:
        rows = (_rows_query(client,
                            _ROW_SELECT_LEGACY if legacy_first else _ROW_SELECT,
                            user_token, kind, activity_id).execute().data or [])
        return [r for r in rows if (r or {}).get('la_token')]
    except Exception as exc:
        if legacy_first:
            log.warning('[live-activity] rows read failed user_ref=%s kind=%s: '
                        '%s', _token_ref(user_token), kind, type(exc).__name__)
            return []
    # EIN Retry ohne die neue Spalte — schlägt der auch fehl, ist es ein echter
    # Store-Ausfall und nicht die fehlende Migration.
    try:
        rows = (_rows_query(client, _ROW_SELECT_LEGACY, user_token, kind,
                            activity_id).execute().data or [])
    except Exception as exc2:
        log.warning('[live-activity] rows read failed user_ref=%s kind=%s: %s',
                    _token_ref(user_token), kind, type(exc2).__name__)
        return []
    _ROW_SELECT_FALLBACK['until'] = time.time() + _ROW_SELECT_FALLBACK_S
    log.warning('[live-activity] SPALTE last_content_state FEHLT — Migration '
                'supabase_migrations/20260813_live_activity_last_state.sql '
                'einspielen! Pushes laufen weiter, die Feld-Erhaltung ist bis '
                'dahin nur In-Process.')
    return [r for r in rows if (r or {}).get('la_token')]


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
        # Besitzer merken: beim /end müssen auch die Slots dieses Users fallen,
        # die (noch) keine activity_id tragen — sonst erbt die NÄCHSTE Activity
        # derselben Zeile den Zustand der alten (Ketten-Geist).
        slot['user_token'] = (row or {}).get('user_token')
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


# Felder, die ein TEIL-Absender strukturell nicht kennt und die ein
# APNs-Vollersatz deshalb löschen würde. Phasen-abhängige Beschriftungen
# (mainLabel, phaseLabel, footTrailing, mainTimeIsToday, mainTimeDayLabel,
# progress) stehen bewusst NICHT hier — sie gehören zur Phase des Absenders.
_MERGE_PRESERVED_FIELDS = ('displayTZIdentifier', 'rosterFrozen',
                           'rosterFrozenNote')

# Diese Angaben gehoeren zur konkreten Flug-Instanz. Ein MQTT-Ereignis kennt
# regelmaessig nur EINEN davon (zum Beispiel nur `estArr`). Da APNs den
# ContentState komplett ersetzt, muessen die uebrigen bekannten Werte aus dem
# letzten erfolgreichen Stand mitgenommen werden. Das passiert nur bei sicher
# derselben Flug-Instanz; ein Turnaround darf nichts vom vorherigen Leg erben.
_FLIGHT_CACHE_FIELDS = (
    'flightNo', 'fromIATA', 'toIATA', 'route', 'chain',
    'fromCity', 'toCity', 'footLeading',
    'fromTZIdentifier', 'toTZIdentifier',
    'schedDep', 'estDep', 'schedArr', 'estArr',
    'deltaMin', 'depConfirmed', 'arrConfirmed', 'cancelled',
)

_DISPLAY_CACHE_FIELDS = (
    'countdownTarget', 'mainLabel', 'phaseLabel', 'footTrailing',
    'mainTimeIsToday', 'mainTimeDayLabel',
)


def _same_state_flight(a, b):
    """True nur bei derselben ausreichend belegten Flug-Instanz.

    Flugnummer + Strecke sind der primaere Schluessel. Die Plan-Abflugzeit
    darf bis zu sechs Stunden abweichen, weil `dep_iso` nach dem Abflug die
    beobachtete Zeit tragen kann; die taegliche Nachbar-Instanz (~24 h) bleibt
    damit sicher getrennt. Fehlen starke Belege, wird konservativ NICHT
    gemergt.
    """
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    matches = 0
    for key in ('flightNo', 'fromIATA', 'toIATA'):
        av = str(a.get(key) or '').strip().upper()
        bv = str(b.get(key) or '').strip().upper()
        if key == 'flightNo':
            av = ''.join(av.split())
            bv = ''.join(bv.split())
        if av and bv:
            if av != bv:
                return False
            matches += 1
    ad = _stored_date_to_unix(a.get('schedDep'))
    bd = _stored_date_to_unix(b.get('schedDep'))
    if ad is not None and bd is not None:
        if abs(float(ad) - float(bd)) > 6 * 3600:
            return False
        matches += 2
    return matches >= 2


def _same_display_moment(a, b):
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    if a.get('phase') != b.get('phase') or a.get('kicker') != b.get('kicker'):
        return False
    at = _stored_date_to_unix(a.get('mainTime'))
    bt = _stored_date_to_unix(b.get('mainTime'))
    return at is not None and bt is not None and abs(at - bt) < 1


def _merge_chain_facts(fresh, previous):
    """Vervollstaendigt eine partielle Vorflug-Kette mit bekannten Fakten.

    `chain` ist als Ganzes ein ContentState-Feld, inhaltlich aber eine Liste
    unabhaengiger belegter Marken. Ein Absender, der nur Briefing + Abflug
    kennt, widerruft damit keine zuvor gelieferte Security-/Crewbus-/Boarding-
    Zeit. Ein explizites JSON-null wird schon vor diesem Aufruf als `cleared`
    behandelt und erreicht die Funktion nicht.
    """
    if not isinstance(fresh, list) or not isinstance(previous, list):
        return fresh
    merged = [dict(step) for step in fresh if isinstance(step, dict)]
    labels = {str(step.get('label') or '').strip().upper() for step in merged}
    for step in previous:
        if not isinstance(step, dict):
            continue
        label = str(step.get('label') or '').strip().upper()
        if not label or label in labels:
            continue
        merged.append(dict(step))
        labels.add(label)
    merged.sort(key=lambda step: float(step.get('time') or 0))
    return merged


def _merge_cached_state(state, previous, cleared=()):
    """Vollstaendiger APNs-Zustand aus frischem Teilstand + letztem Cache.

    Ein explizites JSON-null bleibt ein Grabstein. Fehlende Keys dagegen sind
    bei Teil-Absendern kein Widerruf. Phasen-Texte werden nur erhalten, wenn
    Phase, Kicker UND Hauptzeit gleich geblieben sind.
    """
    if not isinstance(previous, dict):
        return state
    cleared = set(cleared or ())
    fresh_keys = set(state)
    for key in _MERGE_PRESERVED_FIELDS:
        if key not in cleared and key not in state and previous.get(key) is not None:
            state[key] = previous[key]
    same_flight = _same_state_flight(state, previous)
    if same_flight:
        for key in _FLIGHT_CACHE_FIELDS:
            if key not in cleared and key not in state and previous.get(key) is not None:
                state[key] = previous[key]
        # Ein vorhandener, aber VERKUERZTER Array darf die anderen bekannten
        # Marken nicht loeschen. Fehlendes `chain` wurde oben bereits komplett
        # erhalten; explizites null bleibt wegen `cleared` ein Grabstein.
        if state.get('phase') in ('preDuty', 'briefing', 'turnaround', 'layover') \
                and 'chain' not in cleared \
                and isinstance(state.get('chain'), list) \
                and isinstance(previous.get('chain'), list):
            state['chain'] = _merge_chain_facts(state['chain'], previous['chain'])
    if same_flight:
        state = _preserve_prepickup_phase(state, previous)
    # Der Zielzeitpunkt muss denselben Cache benutzen wie die sichtbare
    # Zeitzeile. Sonst bleibt `estArr` zwar im Payload erhalten, Countdown und
    # Lichtbogen springen beim naechsten Teil-Event trotzdem auf `schedArr`.
    if same_flight and state.get('phase') in ('inFlight', 'turnaround') \
            and 'estArr' not in fresh_keys and 'estArr' not in cleared \
            and state.get('estArr') is not None:
        state['mainTime'] = state['estArr']
        state['countdownTarget'] = state['estArr']
    elif same_flight and state.get('phase') == 'briefing' \
            and 'estDep' not in fresh_keys and 'estDep' not in cleared \
            and state.get('estDep') is not None:
        state['mainTime'] = state['estDep']
        state['countdownTarget'] = state['estDep']
    if _same_display_moment(state, previous):
        for key in _DISPLAY_CACHE_FIELDS:
            if key not in cleared and key not in state and previous.get(key) is not None:
                state[key] = previous[key]
    return state


def _preserve_prepickup_phase(state, previous):
    """Ein ETA-Event ist kein Dienstbeginn.

    `est_dep` kommt oft Stunden vor dem Abflug und der MQTT-Absender nennt den
    groben Zielzustand `briefing`. Wenn der vollstaendige iPhone-State noch
    `preDuty`/Pickup ist und weder Abflug noch Streichung belegt sind, bleiben
    Phase, Kicker und naechste Marke vom Client erhalten. Sonst springt die
    Karte per Push auf ABFLUG und beim App-Oeffnen wieder auf AUS DEM HAUS.
    """
    if not isinstance(previous, dict):
        return state
    if previous.get('phase') not in ('preDuty', 'layover'):
        return state
    if state.get('phase') != 'briefing':
        return state
    if state.get('depConfirmed') is True or state.get('cancelled') is True:
        return state
    for key in ('phase', 'kicker', 'mainTime', 'countdownTarget',
                'mainLabel', 'phaseLabel', 'mainTimeIsToday',
                'mainTimeDayLabel'):
        if key in previous:
            state[key] = previous[key]
        else:
            state.pop(key, None)
    return state


def _stored_state(rows):
    """Zuletzt erfolgreich gesendeter Zustand über die Registry-Zeilen —
    In-Process-Spiegel gewinnt (stimmt auch bei SB-Ausfall), sonst die
    DB-Spalte `last_content_state` der frischesten Zeile. None wenn nichts da."""
    best, best_ts = None, -1
    for row in rows or []:
        row_id = (row or {}).get('id')
        with _state_lock:
            slot = _LAST_SENT.get(row_id) or {}
        cand = slot.get('state') or (row or {}).get('last_content_state')
        if not isinstance(cand, dict):
            continue
        try:
            ts = int(slot.get('ts') or row.get('last_timestamp') or 0)
        except Exception:
            ts = 0
        if ts >= best_ts:
            best, best_ts = cand, ts
    return best


def _store_last_state(sent_row_ids, state):
    """Gesendeten (normalisierten) Zustand festschreiben — Grundlage der
    Feld-Erhaltung beim nächsten Teil-Absender. Wirft nie; ein Fehlschlag
    kostet nur die Erhaltung, nie den Push. `generatedAt`/Datumswerte sind
    nach der Normalisierung ISO-Strings, das Dict ist JSON-tauglich."""
    if not sent_row_ids or not isinstance(state, dict):
        return
    with _state_lock:
        for rid in sent_row_ids:
            slot = _LAST_SENT.setdefault(rid, {})
            slot['state'] = state
    client = _sb()
    if client is None:
        return
    try:
        (client.table('live_activities')
         .update({'last_content_state': state})
         .in_('id', list(sent_row_ids)).execute())
    except Exception as exc:
        log.warning('[live-activity] last_state write failed: %s',
                    type(exc).__name__)


def _store_registered_state(row_id, raw_state, user_token=None):
    """Vom CLIENT hochgeladenen ContentState als Erhaltungs-Grundlage ablegen.
    True ⇒ gespeichert. Wirft nie — die Registrierung darf daran nie scheitern.

    Bewusst TOLERANT gegenüber `missing:`: dieser Zustand wird NIE gesendet, er
    ist nur der Steinbruch für `_MERGE_PRESERVED_FIELDS`. Ein Typ-Fehler
    (`bad_type:`) ist dagegen Contract-Drift und wird abgelehnt statt still
    übernommen."""
    if not isinstance(raw_state, dict) or not raw_state:
        return False
    rid = row_id
    if isinstance(rid, list):
        rid = (rid or [None])[0]
    if isinstance(rid, dict):
        rid = rid.get('id')
    if not rid:
        return False
    try:
        state, problems = _normalize_content_state(raw_state)
    except Exception:                                   # pragma: no cover
        return False
    bad = [p for p in (problems or []) if p.startswith('bad_type:')]
    if bad or not state:
        log.warning('[live-activity] register content_state verworfen '
                    'user_ref=%s problems=%s', _token_ref(user_token),
                    ','.join(bad[:4]) or 'empty')
        return False
    _store_last_state([rid], state)
    return True


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


# ── Push-to-Start: die EINE Bremse gegen Start-Spam (2026-08-11) ─────────────
#
# DIE GEFAHR, die diese Konstante abwendet: ein Push-to-Start ERZEUGT jedes Mal
# eine NEUE Activity — ActivityKit dedupliziert nichts, weder über die
# `attributes` noch sonstwie. Der einzige Grund, warum das im Normalfall nicht
# passiert, ist die Reihenfolge in `push_live_activity`: existiert eine aktive
# `update`-Zeile, gewinnt sie IMMER, und dann wird nur aktualisiert.
#
# Das Loch dazwischen ist real und dauert Sekunden bis Minuten: iOS startet die
# Activity, weckt die App im Hintergrund, die App beobachtet
# `Activity.activityUpdates` und lädt ERST DANN den Update-Token hoch. Trifft in
# diesem Fenster das nächste MQTT-Event ein (LH schickt bei einer Verspätung
# gern mehrere `est_dep` hintereinander), sähe der Server weiterhin keine
# `update`-Zeile — und würde eine ZWEITE Karte starten. Der Tester hätte dann
# zwei Live Activities für denselben Flug.
#
# Deshalb: eine `start`-Zeile, die KÜRZLICH ERFOLGREICH gestartet hat, ist für
# `_LA_START_COOLDOWN_S` gesperrt. Nur der Erfolg zählt (`content_digest` wird
# vom RPC ausschliesslich bei `p_ok` geschrieben) — ein gescheiterter Versuch
# darf den nächsten nicht blockieren, sonst reicht ein einzelner APNs-Aussetzer,
# um den ganzen Dienst ohne Karte zu lassen.
#
# 20 min ist bewusst grosszügig: das Nachreichen des Update-Tokens braucht
# Sekunden, und wenn es NICHT klappt (App vom User beendet, Gerät offline),
# hilft ein schneller zweiter Start auch nicht — er erzeugte nur die zweite
# Karte. `force=True` übersteuert (Operator-Pfad).
_LA_START_COOLDOWN_S = 20 * 60


def _start_cooldown_active(row, now_ts=None):
    """True ⇒ diese `start`-Zeile hat innerhalb des Cooldowns schon ERFOLGREICH
    eine Activity erzeugt. Pure genug: liest nur Zeile + In-Process-Spiegel.

    Zwei Quellen, weil beide für sich Lücken haben:
      • In-Process (`_LAST_SENT[id]['start_ts']`) — überlebt keinen Neustart.
      • DB (`content_digest` gesetzt ⇒ es gab einen erfolgreichen Push;
        `last_timestamp` sagt wann) — überlebt ihn, ist aber nur so frisch wie
        der letzte RPC.
    """
    if not row:
        return False
    now_ts = time.time() if now_ts is None else now_ts
    with _state_lock:
        slot = _LAST_SENT.get(row.get('id')) or {}
        local = int(slot.get('start_ts') or 0)
    stored = 0
    if row.get('content_digest'):
        try:
            stored = int(row.get('last_timestamp') or 0)
        except (TypeError, ValueError):
            stored = 0
    last = max(local, stored)
    return last > 0 and (now_ts - last) < _LA_START_COOLDOWN_S


def _note_started(row, timestamp):
    """Erfolgreichen Push-to-Start im In-Process-Spiegel vermerken."""
    row_id = (row or {}).get('id')
    if not row_id:
        return
    with _state_lock:
        slot = _LAST_SENT.setdefault(row_id, {})
        slot['start_ts'] = max(int(timestamp or 0), int(slot.get('start_ts') or 0))


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
        if event == 'start':
            # NUR der Erfolg sperrt den Cooldown (s. `_start_cooldown_active`).
            _note_started(row, timestamp)
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

    `event='start'` ist ausdrücklich KEIN „starte auf jeden Fall": es heisst
    „diese Karte darf auch entstehen, wenn noch keine läuft". Läuft schon eine,
    gewinnt sie weiterhin und bekommt ein Update — genau die Regel „EINE Live
    Activity pro Dienst" aus der Feature-Doku. Deshalb ist `start` hier
    gleichbedeutend mit `update` + `attributes`; die Unterscheidung existiert
    nur, damit ein Aufrufer die Absicht ausdrücken (und der Endpoint fehlende
    `attributes` melden) kann.

    Returns dict {'ok', 'sent', 'unchanged', 'failed', 'dead', 'event', ...}.
    Wirft nie.
    """
    state, problems, cleared = _normalize_content_state(content_state,
                                                        with_cleared=True)
    fatal = _fatal_problems(problems)
    if fatal:
        return {'ok': False, 'error': 'invalid_content_state',
                'problems': fatal}

    rows = _active_rows(user_token, 'update', activity_id=activity_id)
    # ── FELD-ERHALTUNG GEGEN TEIL-ABSENDER (Vorfall Nr. 4, 2026-08-13) ──────
    # APNs ERSETZT das ContentState vollständig. Ein Absender, der sein Dict
    # von Hand baut (MQTT-Fanout), löschte damit alles, was er nicht kennt —
    # zuletzt die komplette Dienst-Kette samt Boarding vom Sperrbildschirm
    # (davor: flightNo 11.08., Bestätigungen 02.08., Zonen 29.07.). Statt den
    # x-ten Absender nachzuziehen, erhält der SERVER jetzt strukturell: Felder
    # aus dem zuletzt erfolgreich gesendeten Zustand, die der neue Absender
    # NICHT setzt, bleiben stehen. Bewusst NUR phasen-UNABHÄNGIGE Felder —
    # eine alte Beschriftung (mainLabel/…) zur neuen Phase wäre eine Lüge,
    # dafür hat der Client Fallbacks.
    # GRABSTEIN: ein Absender, der einen Key explizit auf `null` setzt, LÖSCHT
    # ihn bewusst (Kette zu Ende, Roster-Notiz weg). Ohne diese Ausnahme wäre
    # die Erhaltung eine Einbahnstrasse — einmal gesetzt, nie wieder löschbar.
    if event != 'end':
        preserved = _stored_state(rows)
        if preserved:
            state = _merge_cached_state(state, preserved, cleared)
    used_event = event if event in ('update', 'end') else 'update'
    attrs = None
    if not rows and used_event != 'end' and attributes:
        attrs, attr_problems = _normalize_attributes(attributes)
        attr_fatal = _fatal_problems(attr_problems)
        if attr_fatal:
            return {'ok': False, 'error': 'invalid_attributes',
                    'problems': attr_fatal}
        start_rows = _active_rows(user_token, 'start')
        if start_rows and not force:
            now_ts = time.time()
            open_rows = [r for r in start_rows
                         if not _start_cooldown_active(r, now_ts)]
            if not open_rows:
                # Wir HABEN ein Ziel, dürfen es nur gerade nicht benutzen.
                # Das ist kein Fehler und kein `no_target` — ein zweiter Start
                # wäre die zweite Karte für denselben Flug.
                return {'ok': True, 'sent': 0, 'skipped': 'start_cooldown',
                        'event': 'start', 'targets': len(start_rows)}
            start_rows = open_rows
        # Ein vorhandener Client-Snapshot ist die autoritative Startuhr. Er
        # ersetzt das alte pauschale dep-4h-Fenster; Legacy-Zeilen ohne State
        # duerfen vorerst den bisherigen Fallback benutzen. Der Cooldown steht
        # davor: eine eben per Push erzeugte Karte darf unter keinen Umstaenden
        # als `no_target` durchfallen und beim Folgeruf dupliziert werden.
        start_rows = [r for r in start_rows
                      if not isinstance(r.get('last_content_state'), dict)
                      or _stored_state_wants_start(r.get('last_content_state'))]
        if start_rows:
            rows = start_rows
            used_event = 'start'
            previous = _stored_state(start_rows)
            if previous:
                state = _merge_cached_state(state, previous, cleared)
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
    if used_event != 'end' and counts['sent']:
        _store_last_state([row.get('id') for row, res in zip(rows, results)
                           if res.get('status') == 'sent' and row.get('id')],
                          state)
    out = {'ok': True, 'event': used_event, 'targets': len(rows), **counts}
    if counts['sent'] == 0 and counts['unchanged'] == len(rows):
        out['skipped'] = 'unchanged'
    return out


# ── Routes ──────────────────────────────────────────────────────────────────

@live_activity_bp.route('/api/push/register-live-activity', methods=['POST'])
def register_live_activity():
    """Body: {token, la_token, kind, bundle_id, apns_env, platform,
    activity_id?, device_id?, content_state?} + `Authorization: Bearer <token>`.

    Shape 1:1 aus iOS `PushService.uploadLiveActivityToken`. Der Client setzt
    seinen lastSent-Marker NUR bei 2xx — ein 503 hier heißt also „nächster
    Launch versucht es erneut", genau richtig bei SB-Ausfall. Deshalb wird hier
    NIE ein Erfolg vorgetäuscht.

    `content_state` (optional, seit 2026-08-13) ist der Zustand, den die APP
    beim Start der Activity SELBST gebaut hat. Ohne ihn war die Feld-Erhaltung
    in `push_live_activity` in Produktion wirkungslos: erhalten werden kann nur,
    was der Server vorher schon einmal GESENDET hat — die Dienst-Kette der
    App-Seite hat er nie gesehen, und genau sie löschte der erste Teil-Absender
    (Vorfall Nr. 4). Ungültige Felder kosten NUR die Erhaltung, nie die
    Registrierung: der Token ist das Wichtige.
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
    state_stored = _store_registered_state(row_id, body.get('content_state'),
                                           user_token)
    log.info('[live-activity] registered user_ref=%s kind=%s env=%s '
             'token_ref=%s activity=%s state=%s', _token_ref(user_token), kind,
             _normalized_env(body.get('apns_env')), _token_ref(la_token),
             (activity_id or '-')[:24], state_stored)
    return jsonify({'ok': True, 'kind': kind, 'activity_id': activity_id,
                    'stored': True, 'content_state_stored': state_stored})


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
                continue
            # Registry-Zeilen werden über Activities hinweg WIEDERVERWENDET.
            # Ein Slot dieses Users ohne (bekannte) activity_id kann zu der
            # gerade beendeten Karte gehören — sein Zustand darf die nächste
            # nicht mehr befüllen (DB-Seite: 20260813b-Migration).
            if (user_token and slot.get('user_token') == user_token
                    and not slot.get('activity_id')):
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
    # `start` war bis 2026-08-11 hier VERBOTEN — und damit war der ganze
    # Push-to-Start-Pfad über diesen Endpoint unerreichbar, obwohl
    # `push_live_activity` ihn seit P6 kann. Genau daran hing der Tester-Befund
    # „nächster Flug erscheint erst nach App-Öffnen": ohne laufende Activity
    # gab es kein Ziel, und niemand durfte eines erzeugen.
    #
    # Es bleibt bei der Bedeutung aus `push_live_activity`: „darf entstehen",
    # nicht „starte auf jeden Fall" — eine laufende Activity gewinnt weiterhin.
    if event not in ('update', 'end', 'start'):
        return jsonify({'ok': False, 'error': 'bad_event'}), 400
    if event == 'start' and not isinstance(body.get('attributes'), dict):
        # Ohne `attributes` kann ActivityKit nichts starten (`attributes-type`
        # + `attributes` sind Pflicht im aps-Block). Lieber ein ehrliches 400
        # als ein stiller Fallback auf `update`, der nie ein Ziel findet.
        return jsonify({'ok': False, 'error': 'missing_attributes'}), 400

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

# ── KEIN FAKE-ABFLUG (Owner 2026-07-31) ─────────────────────────────────────
#
# Owner-Regel: „nur nicht Fake-Abflug wenn keins". Die Phase `inFlight` darf
# NUR aus einem echten Abflug-Beleg entstehen — einem departed-Ereignis oder
# einem Board-Actual. Eine verstrichene PLANZEIT belegt gar nichts: die
# Maschine kann am Gate stehen.
#
# ZEITBASIERTEN FLIP GIBT ES HIER NICHT — geprüft und festgenagelt.
# Die Phase kommt ausschliesslich aus `_MQTT_PHASE_KICKER`, also aus dem
# Ereignis-Typ des Brokers; nirgends im Backend wird eine Phase aus „jetzt >
# geplanter Abflug" abgeleitet (`test_keine_phase_haengt_an_der_uhr`,
# `test_verstrichene_planzeit_ohne_ereignis_macht_keinen_inflight`).
#
# WAS ES ABER GAB: `est_arr` stand in der Tabelle auf `inFlight`. LH schickt
# „New Estimated Arrival" aber auch, während der Flieger noch AM GATE steht —
# eine ETA-Korrektur wegen einer bekannten Abflugverspätung ist genau so ein
# Ereignis. Die Karte sprang damit ohne einen einzigen Abflug-Beleg in den
# Flugzustand: der Fehler „Flug-Animation ohne Pushback".
#
# Deshalb: `est_arr` aktualisiert weiterhin die ANKUNFTSZEITEN (das war der
# Sinn der 28.07.-Reparatur „arrival time was wrong the whole time"), behauptet
# aber nur dann `inFlight`, wenn der Abflug BELEGT ist. Ist er es nicht, bleibt
# die Karte ehrlich im Vor-Abflug-Zustand und zeigt weiter auf den Abflug —
# und veraltet dann von selbst über das `stale-date`.
#
# Beleg-Quellen (beide sind ECHTE Signale, keine Uhr):
#   1. das Ereignis selbst — departed/arrived/diverted kann es ohne Abflug
#      nicht geben (divert und landen setzt Fliegen voraus);
#   2. `facts['dep_status']` = LHs `FlightStatus.Definition`, also der
#      Board-Actual. Dieselbe Quelle, die aerox_data_blueprint schon als
#      Off-Block-Beleg auswertet.
_DEPARTURE_PROVING_KINDS = ('departed', 'arrived', 'diverted')

# Substrings eines dep_status, die einen ERFOLGTEN Abflug belegen (LH liefert
# die Definition mal englisch, mal deutsch). Bewusst NICHT dabei: „Scheduled",
# „On Time", „Delayed", „Boarding", „Gate Closed", „Cancelled" — das sind alles
# Zustände VOR dem Abflug, auch wenn die Planzeit längst durch ist.
_DEP_PROVEN_STATUS_HINTS = ('departed', 'abgeflog', 'airborne', 'in flight',
                            'im flug', 'en route', 'enroute', 'unterwegs',
                            'landed', 'gelandet', 'arrived', 'angekommen',
                            'diverted', 'umgeleitet')


def _departure_is_proven(kind, facts):
    """Ist der Abflug BELEGT? Pure, wirft nie.

    Nur Ereignis oder Board-Actual zählen — nie eine verstrichene Planzeit.
    Im Zweifel False: dann behauptet die Karte lieber zu wenig als zu viel.
    """
    if kind in _DEPARTURE_PROVING_KINDS:
        return True
    status = str((facts or {}).get('dep_status') or '').lower()
    return any(h in status for h in _DEP_PROVEN_STATUS_HINTS)


# ── R2a: `stale-date` — die Karte darf nicht ewig „im Flug" behaupten ────────
#
# Audit 2026-07-31: `push_for_affected` ist der EINZIGE Produzent dieser
# Live Activities, und er hat nie ein `stale_after_s` gesetzt. Geht ein
# `arrived`-Event verloren — und das passiert regelmäßig, weil der Broker mit
# QoS 0 und clean session arbeitet (kein Replay) und jeder Deploy des Daemons
# 50–60 s blind ist (~10×/Tag) —, bleibt die Karte für IMMER im Zustand
# „inFlight". Sie zählt dann auf eine Ankunft herunter, die längst vorbei ist.
#
# `aps.stale-date` ist das einzige Mittel, das OHNE App-Update wirkt: ActivityKit
# liest den Header selbst und markiert die Activity ab diesem Zeitpunkt als
# veraltet (`ActivityViewState.isStale`) — der Client muss dafür nichts können
# und nichts wissen. Es beendet die Karte NICHT; das macht der serverseitige
# Sweep weiter unten (R2b). Die beiden gehören zusammen: stale-date wirkt
# sofort und auch dann, wenn das Gerät offline ist, der Sweep räumt auf.
#
# Bezugspunkt ist bewusst `mainTime` (der Moment, auf den die Karte zeigt) und
# nicht „jetzt + fixe Dauer": eine Karte, die auf eine Ankunft in 9 h zeigt,
# darf nicht nach 2 h als veraltet gelten, und eine, die auf eine Ankunft vor
# 10 min zeigt, soll nicht noch stundenlang frisch wirken.
#
# ⚠️ DER DECKEL HÄNGT AN DER ANKUNFT, NICHT AN EINEM FESTEN FENSTER AB JETZT
# (Owner-Nachschärfung 2026-07-31: „ne Langstrecke die 12h geht kann sie mit
# guten Werten schon zeigen auch ohne Internet — nur nicht Fake-Abflug wenn
# keins").
#
# Erste Fassung klemmte bei 12 h ab jetzt. Das war zu eng und hätte genau die
# Flüge beschädigt, für die R1 gebaut wurde: LH715 HND–MUC mit 14 h 20 Block
# stand im Beweis-Set dieser Runde. Beim Abflug wäre die Marke auf jetzt + 12 h
# gefallen — also **2 h 20 VOR der Landung** — und die Karte wäre mitten über
# Sibirien ausgegraut, obwohl der Abflug bestätigt und die Ankunft bekannt ist.
#
# Bei bestätigtem Abflug IST der Countdown auf die bekannte Ankunft eine
# ehrliche Extrapolation — dafür braucht das Gerät kein Netz. Der Deckel darf
# die Karte deshalb nie VOR ihrer eigenen Ankunft veralten lassen. Was bleibt,
# ist ein reiner Not-Deckel gegen Datenmüll (arr im nächsten Jahr): 20 h
# maximale Blockzeit — dieselbe Schranke wie `lh_mqtt._SUB_BLOCK_MAX_H` — plus
# die Kulanz. Es gibt keinen Linienflug, der das überschreitet.
#
# Der 15-min-BODEN bleibt unangetastet: er ist der Lügen-Fall (Ankunft längst
# vorbei, `arrived` verlorengegangen) und soll weiterhin schnell ausgrauen.
_LA_STALE_GRACE_S = 45 * 60      # nach dem gezeigten Zeitpunkt noch „frisch"
_LA_STALE_MIN_S = 15 * 60        # nie sofort veraltet (Zeitpunkt schon vorbei)
_LA_STALE_MAX_BLOCK_H = 20       # Not-Deckel gegen Datenmüll, nicht gegen Flüge
_LA_STALE_MAX_S = _LA_STALE_MAX_BLOCK_H * 3600 + _LA_STALE_GRACE_S


def _to_unix(value):
    """Zeitangabe → Unix-Sekunden (float) oder None. Nutzt bewusst dieselbe
    einzige Umrechnungsstelle wie der Payload (`_to_apple_date`), damit hier
    nie eine zweite Datumsinterpretation entsteht."""
    apple = _to_apple_date(value)
    return None if apple is None else apple + _APPLE_EPOCH_OFFSET


def _stale_after_s(target, now_ts=None):
    """Sekunden ab JETZT bis `aps.stale-date`. Pure. None ⇒ kein stale-date
    (unbekannter Bezugszeitpunkt — dann wird nichts behauptet).

    Geklemmt: mindestens `_LA_STALE_MIN_S` (ein bereits vergangener Zeitpunkt
    darf die Karte nicht rückwirkend veralten lassen, sonst flackert sie beim
    ersten Update nach der Landung), höchstens `_LA_STALE_MAX_S`.
    """
    unix = _to_unix(target)
    if unix is None:
        return None
    now_ts = time.time() if now_ts is None else now_ts
    secs = int(unix + _LA_STALE_GRACE_S - now_ts)
    return max(_LA_STALE_MIN_S, min(_LA_STALE_MAX_S, secs))


# ── DER FANOUT DARF DIE KARTE JETZT AUCH ERZEUGEN (2026-08-11) ──────────────
#
# DER BEFUND (Tester): „Live Activities aktualisieren sich nicht selbstständig —
# der nächste Flug erscheint erst nach App-Öffnen."
#
# DIE URSACHE, nachgelesen und nicht vermutet: der Push-Weg war komplett gebaut
# (`push_live_activity` kennt Push-to-Start seit P6), aber sein EINZIGER
# Produzent — dieser Fanout — rief ihn immer OHNE `attributes`. Ohne
# `attributes` nimmt `push_live_activity` die `start`-Zeile gar nicht erst in
# die Hand. Lief also gerade keine Activity (App seit der letzten Landung
# beendet, Gerät neu gestartet, Karte weggewischt), gab es schlicht kein
# Push-Ziel, und die Karte entstand erst wieder beim nächsten App-Start über
# `DutyActivityController.sync`. Der interne Endpoint verbot `start` sogar
# ausdrücklich — der Pfad war doppelt zu.
#
# ZWEI GATES, damit daraus keine Karten-Flut wird:
#
#  1. EVENT-ART. `arrived` startet NICHTS. Eine Karte, die als „GELANDET"
#     entsteht, hätte nie einen Flug gezeigt und wäre binnen Minuten wieder weg
#     (der Sweep beendet sie) — das ist Rauschen auf dem Sperrbildschirm, keine
#     Information. Aktualisieren tut `arrived` weiterhin: eine LAUFENDE Karte
#     muss die Landung sehen.
#
#  2. ZEITFENSTER. Gestartet wird frühestens `_LA_SWEEP_LEAD_S` (4 h) vor dem
#     Abflug — exakt der Beginn des Fensters, in dem `_plausible_sectors` eine
#     Live Activity überhaupt für plausibel hält. Ein früher Start (die
#     MQTT-Topics stehen bis zu 48 h im Voraus) würde vom eigenen Sweep binnen
#     5 min wieder beendet: eine Karte, die kommt und geht, ist schlimmer als
#     keine. Ohne bekannten Abflug wird NICHT gestartet — kein geratenes
#     Fenster.
#
# Die dritte Bremse sitzt eine Ebene tiefer und gilt für alle Aufrufer:
# `_start_cooldown_active` (kein zweiter Start, solange der Update-Token der
# ersten Karte noch unterwegs ist).
_MQTT_NO_START_KINDS = ('arrived',)


def _mqtt_start_attributes(kind, flight_disp, frm, to, dep, now_utc,
                           now_ts=None):
    """`attributes` für den Push-to-Start — oder None (dann bleibt es beim
    reinen Update). Pure, wirft nie.

    Feldnamen und Typen spiegeln `DutyActivityAttributes` in
    `ios/AeroTax/Shared/DutyActivityAttributes.swift` 1:1:
    `flightNo: String?`, `from: String?`, `to: String?`, `startedAt: Date`
    (nicht-optional ⇒ Pflichtfeld, siehe `_REQUIRED_ATTRIBUTE_KEYS`).
    `None`-Werte wirft `_normalize_attributes` weg; in Swift bleiben die
    Optionals dann `nil` und der Renderer fällt auf den `ContentState` zurück
    (`flightNo(fallback:)`).
    """
    if kind in _MQTT_NO_START_KINDS:
        return None
    dep_unix = _to_unix(dep)
    if dep_unix is None:
        return None
    now_ts = time.time() if now_ts is None else now_ts
    if now_ts < dep_unix - _LA_SWEEP_LEAD_S:
        return None
    return {'flightNo': flight_disp or None,
            'from': frm,
            'to': to,
            'startedAt': now_utc}


def push_for_affected(affected, kind, flight_disp, topic_date, facts=None,
                      next_sectors=None):
    """Live-Activity-Fanout für ein LH-MQTT-Event. Returns Anzahl gesendeter
    Pushes (unverändert/skip zählt NICHT).

    `next_sectors` (nur `arrived`): {token: Folge-Sektor} aus dem Roster —
    dann zeigt die Karte statt des gelandeten Legs sofort den Abflug des
    Anschluss-Legs (Tibor 19.08.: „wär cool wenns automatisch springen würd
    auf mein Leg auf dem ich grad bin"). Ohne Eintrag bleibt GELANDET stehen
    (Dienstende/Layover — dort beendet der Sweep bzw. die App die Karte).

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
    shows_arrival = kind in _MQTT_ARRIVAL_KINDS
    if kind == 'est_arr' and not _departure_is_proven(kind, facts):
        # ETA-Korrektur, während die Maschine noch am Gate steht: Zeiten
        # aktualisieren ja, Flugzustand behaupten nein (s. Block oben).
        phase, kicker = _MQTT_PHASE_KICKER['est_dep']
        shows_arrival = False
    now_iso = datetime.now(timezone.utc)
    sent = 0
    for entry in affected:
        try:
            user_token, sector = entry[0], (entry[1] or {})
        except Exception:
            continue
        frm = (sector.get('from') or '').strip().upper() or None
        to = (sector.get('to') or '').strip().upper() or None
        # `dep_iso`/`arr_iso` sind der Roster-Fallback, NICHT automatisch eine
        # neue Beobachtung. Bis hier wurden sie trotzdem als estDep/estArr
        # verschickt und ueberschrieben damit eine bereits gecachte echte ETA
        # wieder mit der Planzeit. Fuer die sichtbare Zielzeit bleiben sie der
        # ehrliche Fallback; in den `est*`-Feldern steht nur ein echtes Faktum.
        observed_dep = facts.get('est_dep')
        observed_arr = facts.get('est_arr')
        est_dep = observed_dep or sector.get('dep_iso')
        est_arr = observed_arr or sector.get('arr_iso')
        delta = facts.get('dep_delay_min')
        state = {
            'stateVersion': 2,
            'phase': phase,
            'kicker': kicker,
            # mainTime = der Moment, auf den die Karte zeigt. Ab dem Abflug
            # (und für jedes ETA-Update) ist das die ANKUNFT — mit frisch
            # geforcten `facts` (lh_mqtt) also die echte LH-Schätzung, nicht
            # mehr die beim Abflug eingefrorene Plan-Zeit.
            'mainTime': est_arr if shows_arrival else est_dep,
            'countdownTarget': est_arr if shows_arrival else est_dep,
            'route': f'{frm}–{to}' if frm and to else None,
            # Flugnummer des AKTIVEN Legs. Bis 2026-08-11 schickte dieser
            # Fanout sie NICHT — die Pille auf dem Sperrbildschirm hing damit
            # an den beim Start eingefrorenen `attributes` und zeigte nach
            # einem Turnaround weiter den Hinflug (`flightNo(fallback:)`).
            # Seit der Server die Karte auch ERZEUGEN darf, ist das doppelt
            # relevant: bei einer per Push-to-Start entstandenen Karte ist der
            # ContentState die einzige Quelle, die mit der Phase mitwandert.
            'flightNo': flight_disp or None,
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
            'estDep': observed_dep,
            'schedArr': facts.get('sched_arr') or sector.get('arr_iso'),
            'estArr': observed_arr,
            'cancelled': True if kind == 'cancelled' else None,
            'footLeading': flight_disp or None,
            # BESTÄTIGT GEGEN GEPLANT (Nachzug 2026-08-02). Die Felder gab es
            # im Swift-Vertrag seit dem 31.07., dieser Fanout hat sie nie
            # mitgeschickt — und weil ein Push das ContentState VOLLSTÄNDIG
            # ersetzt, LÖSCHTE jedes Event die Bestätigung, die der Client
            # selbst gesetzt hatte. Die Karte fiel damit auf „nicht bestätigt"
            # zurück und durfte offline nicht mehr fortschalten. `None` (kein
            # Beleg) fällt im Normalizer weg = konservativ wie bisher.
            'depConfirmed': True if _departure_is_proven(kind, facts) else None,
            'arrConfirmed': True if kind == 'arrived' else None,
        }
        # TURNAROUND-SPRUNG (Tibor 19.08.): Nach der Landung ist das gelandete
        # Leg für die Crew Vergangenheit — sie sitzt schon im Anschluss. Liegt
        # im Roster ein Folge-Sektor (vom lh_mqtt-Aufrufer konservativ
        # bestimmt: gleicher Airport, binnen Turnaround-Fenster), zeigt die
        # Karte dessen ABFLUG — exakt die Marke, die die App selbst rechnen
        # würde (`inDutySnapshot`-Turnaround: Mark(nextDep, "ABFLUG")). Nur
        # Roster-Planzeiten, keine est*-Behauptungen und kein `arrConfirmed`
        # fürs NEUE Leg — bestätigt ist dort noch nichts.
        nxt = (next_sectors or {}).get(user_token) if kind == 'arrived' else None
        if nxt and nxt.get('dep_iso'):
            nfrm = (nxt.get('from') or '').strip().upper() or None
            nto = (nxt.get('to') or '').strip().upper() or None
            ndisp = ''.join(str(nxt.get('flight') or '').split()).upper() or None
            state.update({
                'kicker': 'ABFLUG',
                'mainTime': nxt.get('dep_iso'),
                'countdownTarget': nxt.get('dep_iso'),
                'route': f'{nfrm}–{nto}' if nfrm and nto else None,
                'flightNo': ndisp,
                'footLeading': ndisp,
                'deltaMin': None,
                'fromIATA': nfrm,
                'toIATA': nto,
                'fromTZIdentifier': _airport_tz(nfrm),
                'toTZIdentifier': _airport_tz(nto),
                'schedDep': nxt.get('dep_iso'),
                'estDep': None,
                'schedArr': nxt.get('arr_iso'),
                'estArr': None,
                'depConfirmed': None,
                'arrConfirmed': None,
            })
        else:
            nxt = None
        if state['mainTime'] is None:
            # Ohne Ziel-Zeitpunkt gibt es keine ehrliche Karte.
            continue
        # Dieser Fanout ist ein TEIL-Absender. `None` bedeutet hier "weiss ich
        # nicht" und darf nicht als explizites JSON-null den Server-Cache
        # loeschen. Echte Grabsteine bleiben dem vollstaendigen API-Absender
        # vorbehalten (`push_live_activity` unterstuetzt sie weiterhin).
        state = {key: value for key, value in state.items() if value is not None}
        # PUNKT-FORTSCHRITT (Tibor 01.08., „Punkt hinkt der Uhr hinterher"):
        # nur wenn der Abflug BELEGT ist (shows_arrival trägt genau diese
        # Gate-Semantik) und beide Instants da sind — dann bekommt jeder
        # Event-Push den frischen Zeit-Fortschritt mit. Neuere Clients
        # rechnen ihn ohnehin pro Render (liveProgress); das Feld hier
        # versorgt ältere Builds. KEINE periodischen Extra-Pushes dafür —
        # Apples Live-Activity-Throttle (s. Kopfkommentar dieses Fanouts).
        if nxt is None and shows_arrival and est_dep and est_arr:
            try:
                _d0 = datetime.fromisoformat(str(est_dep).replace('Z', '+00:00'))
                _d1 = datetime.fromisoformat(str(est_arr).replace('Z', '+00:00'))
                _span = (_d1 - _d0).total_seconds()
                if _span > 60:
                    _p = (now_iso - _d0).total_seconds() / _span
                    # QUANTISIERT auf 5-%-Stufen: ein sekundengenauer Wert
                    # änderte den Content-Digest bei JEDEM identischen
                    # MQTT-Event — der Dedupe-Schutz gegen Apples
                    # Live-Activity-Throttle wäre wirkungslos gewesen
                    # (test_stale_date_erzeugt_keinen_zusaetzlichen_push
                    # hat genau das gefangen). 5 % einer Kurzstrecke sind
                    # ~5 min Punkt-Auflösung — mehr Präzision rendert der
                    # Client ohnehin selbst (liveProgress pro Render).
                    state['progress'] = round(
                        max(0.0, min(1.0, _p)) * 20) / 20
            except Exception:
                pass
        # PUSH-TO-START: `attributes` heisst „die Karte darf auch ENTSTEHEN",
        # nicht „starte auf jeden Fall". Läuft eine Activity, gewinnt ihre
        # `update`-Zeile weiterhin (`push_live_activity`) — es bleibt bei EINER
        # Karte pro Dienst. `None` ⇒ exakt das Verhalten von vor dem 11.08.
        attributes = _mqtt_start_attributes(kind, flight_disp, frm, to,
                                            est_dep or sector.get('dep_iso'),
                                            now_iso)
        try:
            # stale-date bei JEDEM Update nachschieben: solange Events kommen,
            # wandert die Verfallsmarke mit der aktuellen Schätzung mit. Bleiben
            # sie aus, läuft genau diese zuletzt gesetzte Marke ab — und die
            # Karte sagt selbst, dass sie nichts Neues mehr weiß.
            res = push_live_activity(
                user_token, state, event='update', attributes=attributes,
                priority='10',
                stale_after_s=_stale_after_s(state['mainTime']))
            sent += int(res.get('sent') or 0)
        except Exception as exc:
            log.warning('[live-activity] mqtt fanout failed user_ref=%s: %s',
                        _token_ref(user_token), type(exc).__name__)
    if sent:
        log.info('[live-activity] mqtt fanout flight=%s date=%s kind=%s sent=%d',
                 flight_disp, topic_date, kind, sent)
    return sent


# ── R2b: zeitbasiertes ENDE — wer nichts mehr weiß, hört auf zu behaupten ────
#
# `stale-date` (oben) markiert die Karte nur als veraltet; sie bleibt liegen.
# Owner-Entscheid dazu ist eindeutig: „muss schon richtig sein sonst weg."
# Also beendet dieser Sweep Live Activities, für die es keine Grundlage mehr
# gibt — mit `event='end'` und sofortiger `dismissal-date`, die Karte
# VERSCHWINDET. Sie wird nicht mit einer erfundenen Landezeit „abgeschlossen"
# (Owner-Regel: lieber keine Zeile als ein synthetisierter Wert).
#
# WOHER DIE ZEIT KOMMT — und woher nicht:
# Die Registry-Zeile weiß nicht, welchen Flug sie zeigt (es gibt keine
# Flug-Spalte, und eine neue Spalte hieße Migration). Der Sweep fragt deshalb
# den GESPEICHERTEN Roster desselben Users: läuft irgendein Sektor gerade noch,
# darf die Karte bleiben. Das ist ein reiner Supabase-Read — KEIN LH-Call, kein
# neuer Datenpfad, keine neue Tabelle.
#
# DREI SCHUTZSCHALTUNGEN, weil eine fälschlich beendete Karte schlimmer ist als
# eine, die 90 min zu lang steht:
#   1. Kein Roster-Eintrag für den User im Fenster ⇒ NICHTS TUN. Keine Daten
#      sind kein Beleg für „ist vorbei" (dieselbe Regel wie bei der
#      Frische-Schranke in lh_mqtt).
#   2. Der Plausibilitäts-Rahmen ist großzügig: von 4 h VOR dem Abflug
#      (Briefing/Report, dann startet der Client die Activity) bis zum Ende des
#      MQTT-Abo-Fensters (Ankunft +1 h, bei fehlender Ankunft konservative
#      Blockannahme) PLUS 90 min Kulanz. Effektiv also erst ~2,5 h nach der
#      geplanten Ankunft.
#   3. Reißleine für Zeilen ohne Roster-Beleg: eine Activity, die seit
#      `_LA_SWEEP_HARD_MAX_AGE_H` niemand angefasst hat, KANN nichts Aktuelles
#      mehr zeigen — ActivityKit beendet Activities ohnehin nach spätestens
#      8 h Laufzeit (12 h auf dem Lockscreen). Diese Zeile ist dann nur noch
#      ein toter Push-Empfänger.
#
# Takt: der Sweep braucht keine neue Infrastruktur. Der MQTT-Daemon fragt alle
# 300 s `/api/internal/lh-mqtt/topics`; dieser Aufruf stößt den Sweep im
# Hintergrund an (gedeckelt über `_LA_SWEEP_MIN_GAP_S`). Zusätzlich gibt es den
# Endpoint `/api/internal/live-activity/sweep` für die Host-Cron und für
# manuelle Läufe. Kill-Switch: `AEROX_LA_SWEEP=0`.
_LA_SWEEP_LEAD_S = 4 * 3600         # so früh startet der Client die Activity
_LA_SWEEP_GRACE_S = 90 * 60         # Kulanz NACH dem Abo-Fenster des Legs
_LA_SWEEP_HARD_MAX_AGE_H = 14       # Reißleine ohne Roster-Beleg
# 300 s statt der ursprünglichen 600 s (Tibor 02.08.: „klebt 12+ min auf
# gelandet"). Seit R3 beendet der Sweep die Karte bei BESTÄTIGTER Landung —
# sein Takt ist damit die Reaktionszeit auf die Landung, nicht mehr nur eine
# Aufräum-Frequenz. 300 s ist genau der Topic-Poll-Takt, der ihn anstößt; ein
# größerer Wert hieße, jeden zweiten Anstoß zu verwerfen. Der Lauf kostet zwei
# gebündelte Supabase-Reads und pusht nur bei echter Änderung.
_LA_SWEEP_MIN_GAP_S = 300           # höchstens alle 5 min (pro Prozess)
_LA_SWEEP_MAX_ROWS = 500
_LA_SWEEP_TOKEN_CHUNK = 60
_LA_STORED_START_LEAD_S = 60 * 60   # identisch zu iOS pickupLead
_LA_STORED_START_KEEP_S = 3 * 3600  # identisch zu iOS keepAhead

_SWEEP_SELECT = _ROW_SELECT + ',updated_at'

_sweep_lock = threading.Lock()
_sweep_state = {'last': 0.0, 'running': False}


def _sweep_enabled():
    return (os.environ.get('AEROX_LA_SWEEP', '').strip() or '1') != '0'


def _active_update_rows_all(limit=_LA_SWEEP_MAX_ROWS):
    """ALLE aktiven update-Zeilen (über alle User). Wirft nie; [] bei
    SB-Ausfall — und [] heißt für den Sweep „nichts tun", nicht „alles weg"."""
    client = _sb()
    if client is None:
        return []
    try:
        rows = (client.table('live_activities').select(_SWEEP_SELECT)
                .eq('kind', 'update').eq('active', True)
                .limit(limit).execute().data or [])
        return [r for r in rows if (r or {}).get('la_token')]
    except Exception as exc:
        log.warning('[live-activity] sweep rows read failed: %s',
                    type(exc).__name__)
        return []


def _active_start_rows_all(limit=_LA_SWEEP_MAX_ROWS):
    """Push-to-Start-Zeilen samt letztem iPhone-Snapshot.

    Seit Build 347 laedt der Client den fertigen ContentState auch am
    geraeteweiten Start-Token hoch. Damit entscheidet der Server nicht mehr
    pauschal mit „Abflug minus vier Stunden", sondern mit exakt derselben
    Aus-dem-Haus-/Pickup-Marke wie die App.
    """
    client = _sb()
    if client is None:
        return []
    try:
        rows = (client.table('live_activities')
                .select(_SWEEP_SELECT)
                .eq('kind', 'start').eq('active', True)
                .limit(limit).execute().data or [])
        return [r for r in rows
                if (r or {}).get('la_token')
                and isinstance((r or {}).get('last_content_state'), dict)]
    except Exception as exc:
        log.warning('[live-activity] start rows read failed: %s',
                    type(exc).__name__)
        return []


def _stored_date_to_unix(value):
    """Datum aus `last_content_state` -> Unix.

    Die DB speichert bereits APNs-normalisierte Zahlen seit Apples Epoche.
    `_to_unix` ist fuer ROHE Senderwerte und wuerde diese Zahl ein zweites Mal
    umrechnen. Strings/Datetimes bleiben fuer schema-aeltere Zeilen tolerant.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) + _APPLE_EPOCH_OFFSET
    return _to_unix(value)


def _state_chain_times(state):
    """Belegte Kettenzeiten als Unix-Sekunden, sortiert und dedupliziert."""
    out = []
    for step in (state or {}).get('chain') or []:
        if not isinstance(step, dict):
            continue
        ts = _stored_date_to_unix(step.get('time'))
        if ts is not None:
            out.append(float(ts))
    return sorted(set(out))


def _stored_pickup_mark(state):
    """Dieselbe Lebenszyklus-Marke wie iOS `pickupMark`, ohne Schaetzung."""
    state = state or {}
    chain = state.get('chain') or []
    for step in chain:
        if not isinstance(step, dict):
            continue
        label = str(step.get('label') or '').strip().lower()
        if label in ('aus dem haus', 'leave home'):
            mark = _stored_date_to_unix(step.get('time'))
            if mark is not None:
                return float(mark)
    mark = _stored_date_to_unix(
        state.get('countdownTarget') or state.get('mainTime'))
    return None if mark is None else float(mark)


def _stored_state_wants_start(state, now_ts=None):
    """Darf der gespeicherte iPhone-State JETZT per Push entstehen?

    Pure und absichtlich eng: freie/Feierabend-/Standby-Zustaende starten nie;
    Layover nur mit echter PICKUP-Marke; preDuty ab exakt einer Stunde vor der
    vom Client gelieferten Marke. Ein alter State ist nach drei Stunden hinter
    seiner Marke kein Startgrund mehr.
    """
    if not isinstance(state, dict):
        return False
    now_ts = time.time() if now_ts is None else float(now_ts)
    phase = str(state.get('phase') or '')
    kicker = str(state.get('kicker') or '').strip().upper()
    duty_end = _stored_date_to_unix(state.get('dutyEnd'))
    if duty_end is not None and now_ts > float(duty_end) + 60 * 60:
        return False
    if phase == 'layover':
        if kicker != 'PICKUP':
            return False
    elif phase in ('briefing', 'turnaround', 'inFlight'):
        generated = _stored_date_to_unix(state.get('generatedAt'))
        return generated is None or now_ts - float(generated) <= 20 * 3600
    elif phase != 'preDuty':
        return False
    mark = _stored_pickup_mark(state)
    if mark is None:
        return False
    return (mark - _LA_STORED_START_LEAD_S
            <= now_ts <= mark + _LA_STORED_START_KEEP_S)


def _stored_start_attributes(state, now_utc):
    """ActivityKit-Attributes aus demselben gespeicherten State."""
    return {
        'flightNo': (state or {}).get('flightNo'),
        'from': (state or {}).get('fromIATA'),
        'to': (state or {}).get('toIATA'),
        'startedAt': now_utc,
    }


def _state_milestones(state):
    """Alle Instants, an denen die archivierte View neu rendern muss."""
    out = _state_chain_times(state)
    for key in ('countdownTarget', 'mainTime', 'schedDep', 'estDep',
                'schedArr', 'estArr', 'dutyEnd'):
        ts = _stored_date_to_unix((state or {}).get(key))
        if ts is not None:
            out.append(float(ts))
    # `Text(style: .relative)` und `Text(timerInterval:)` schreiben ihren Wert
    # selbst fort. Die WAHL zwischen beiden trifft SwiftUI aber nur beim
    # Rendern. Genau eine Stunde vor dem Ziel muss daher ein neuer State kommen,
    # sonst bleibt die wortreiche Relativanzeige bis zur Marke stehen.
    target = _stored_date_to_unix(
        (state or {}).get('countdownTarget') or (state or {}).get('mainTime'))
    if target is not None:
        out.append(float(target) - 60 * 60)
    return sorted(set(out))


def _milestone_due(state, last_timestamp, now_ts):
    """Neu passierter Meilenstein oder None. Pure, verhindert Push-Polling."""
    generated = _stored_date_to_unix((state or {}).get('generatedAt')) or 0
    try:
        after = max(float(last_timestamp or 0), float(generated))
    except (TypeError, ValueError):
        after = float(generated)
    passed = [ts for ts in _state_milestones(state)
              if after < ts <= float(now_ts)]
    return max(passed) if passed else None


def _next_milestone_after(state, now_ts):
    future = [ts for ts in _state_milestones(state) if ts > float(now_ts)]
    return min(future) if future else None


def _refresh_row_at_milestone(row, now_utc):
    """Rendert eine laufende Karte genau einmal pro passierter Marke neu."""
    state = (row or {}).get('last_content_state')
    if not isinstance(state, dict):
        return False
    now_ts = now_utc.timestamp()
    if _milestone_due(state, row.get('last_timestamp'), now_ts) is None:
        return False
    fresh = dict(state)
    fresh['generatedAt'] = now_ts - _APPLE_EPOCH_OFFSET
    nxt = _next_milestone_after(fresh, now_ts)
    stale_after = max(5 * 60, int(nxt - now_ts)) if nxt is not None else None
    result = _push_row(row, fresh, event='update',
                       stale_after_s=stale_after, force=True)
    if result.get('status') != 'sent':
        return False
    _store_last_state([row.get('id')], fresh)
    return True


def _roster_sectors_by_token(tokens, now_utc):
    """{token: [sector, …]} aus den gespeicherten Briefings (heute ±1 Tag).
    Nur ein Supabase-Read, gebündelt. Wirft nie; bei Störung ein LEERES Dict —
    dann findet der Sweep keinen Beleg und lässt (bis auf die Reißleine) alles
    in Ruhe."""
    want = sorted({t for t in (tokens or []) if t})
    if not want:
        return {}
    client = _sb()
    if client is None:
        return {}
    try:
        from blueprints.lh_mqtt import _SECTOR_SELECT, _iter_sectors
    except Exception:                                   # pragma: no cover
        return {}
    dates = [(now_utc.date() + timedelta(days=off)).isoformat()
             for off in (-1, 0, 1)]
    out = {}
    for i in range(0, len(want), _LA_SWEEP_TOKEN_CHUNK):
        chunk = want[i:i + _LA_SWEEP_TOKEN_CHUNK]
        try:
            rows = (client.table('user_ical_briefings').select(_SECTOR_SELECT)
                    .in_('token', chunk).in_('datum', dates)
                    .execute().data or [])
        except Exception as exc:
            log.warning('[live-activity] sweep roster read failed: %s',
                        type(exc).__name__)
            continue
        for tok, sector in _iter_sectors(rows):
            if tok:
                out.setdefault(tok, []).append(sector)
    return out


def _plausible_sectors(sectors, now_utc):
    """Die Sektoren des Users, die JETZT im Fenster einer Live Activity liegen
    (Briefing −4 h bis Abo-Ende +90 min). Liste, kann leer sein.

    Das Ende kommt aus `lh_mqtt.sector_sub_end` — bewusst dieselbe Definition
    wie beim MQTT-Abo-Fenster (Ankunft +1 h, ohne bekannte Ankunft konservative
    Blockannahme), damit es nicht zwei Auffassungen davon gibt, wie lange ein
    Leg laufen kann.
    """
    try:
        from blueprints.lh_mqtt import _parse_iso_utc, sector_sub_end
    except Exception:                                   # pragma: no cover
        return None                                     # fail-safe: „weiß nicht"
    out = []
    for s in sectors or []:
        if not isinstance(s, dict):
            continue
        dep = _parse_iso_utc(s.get('dep_iso'))
        if dep is None:
            continue
        start = dep - timedelta(seconds=_LA_SWEEP_LEAD_S)
        end = sector_sub_end(s, dep) + timedelta(seconds=_LA_SWEEP_GRACE_S)
        if start <= now_utc <= end:
            out.append(s)
    return out


def _duty_still_plausible(sectors, now_utc):
    """True, wenn IRGENDEIN Sektor des Users gerade noch laufen kann."""
    plausible = _plausible_sectors(sectors, now_utc)
    if plausible is None:                               # pragma: no cover
        return True                                     # fail-safe
    return bool(plausible)


def _last_known_arrival(sectors):
    """Späteste bekannte ECHTE Ankunftszeit der Sektoren (aware) oder None.
    Nur `arr_iso` — nichts Geschätztes, nichts Gerechnetes."""
    try:
        from blueprints.lh_mqtt import _parse_iso_utc
    except Exception:                                   # pragma: no cover
        return None
    best = None
    for s in sectors or []:
        if not isinstance(s, dict):
            continue
        arr = _parse_iso_utc(s.get('arr_iso'))
        if arr is not None and (best is None or arr > best):
            best = arr
    return best


def _row_age_h(row, now_utc):
    """Alter der Zeile in Stunden (float) oder None, wenn unlesbar."""
    ts = (row or {}).get('updated_at')
    if not ts:
        return None
    try:
        when = datetime.fromisoformat(str(ts).replace('Z', '+00:00'))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return (now_utc - when).total_seconds() / 3600.0
    except Exception:
        return None


# ── R3: die BESTÄTIGTE LANDUNG beendet die Karte (Tibor 2026-08-02) ─────────
#
# BEFUND: Tibors Live Activity klebte 12+ min nach dem Aufsetzen auf „im Flug /
# noch 0:00" (LH1457). Die Landung stand um 07:04Z als „Gelandet" in
# `airport_delay_obs` — nur hat sie NIEMAND an die Karte weitergereicht:
#   · `push_for_affected` hängt ausschließlich am LH-MQTT-Ereignis. Kommt
#     `arrived` nicht an (QoS 0, kein Replay, ~10 Deploy-Blindfenster/Tag), sieht
#     die Karte die Landung nie — und dieses Modul kannte KEINE zweite Quelle.
#   · Der Stale-Sweep (R2b) rechnete nur gegen PLANZEITEN: Abo-Fenster
#     (Ankunft +1 h) plus 90 min Kulanz. Er hätte die Karte erst ~2,5 h nach der
#     PLAN-Ankunft geräumt — und bis dahin behauptet sie weiter „im Flug".
#
# DIE OWNER-STAFFELUNG (verbindlich, Widgets-LiveActivity.md „bestätigt gegen
# geplant"), hier serverseitig vollzogen:
#   1. Bestätigter Fakt darf weiterlaufen  → eine laufende Karte mit belegtem
#      Abflug wird NIE wegen Zeitablauf abgeräumt (unverändert, R2a/R2b).
#   2. Nur-Plan nie in den Flug-Zustand    → unverändert (`_departure_is_proven`).
#   3. Trägt der letzte bestätigte Fakt nicht mehr — bestätigte Ankunft
#      + 60 min — endet die Aktivität. GENAU DAS baut dieser Abschnitt.
#
# Der Client kennt dieselbe Frist als `ContentState.landedGrace` (60 min); er
# KANN eine Activity aber nicht selbst beenden (`Activity.end` nur im
# App-Prozess oder per Push). Der Push von hier ist also die einzige Stelle, an
# der die Karte wirklich verschwindet.
#
# EIN PUSH, NICHT ZWEI: das Ende geht als `event='end'` MIT ZUKÜNFTIGER
# `dismissal-date` (= bestätigte Ankunft + 60 min) raus. Der Inhalt dieses
# Pushes IST der finale Zustand — Phase „gelandet", echte Ankunftszeit —, und
# die Karte bleibt bis zur Frist sichtbar, statt in der Sekunde der Landung zu
# verschwinden. Zwei Pushes (erst Update, dann Ende) wären derselbe Inhalt
# doppelt und damit Apple-Throttle-Budget für nichts.
#
# WOHER DIE LANDUNG KOMMT — und was NICHT als Landung zählt:
# ausschließlich `airport_delay_obs` (gespeicherte Board-Beobachtung, EIN
# gebündelter Supabase-Read pro Sweep). KEIN LH-Call, kein FR24, kein bezahlter
# Fallback — dieser Sweep bleibt kostenlos.
#   · Der Ankunfts-Status muss TERMINAL sein („Gelandet"/„landed"/…, dieselbe
#     Liste wie `app._arr_status_terminal`). „Erwartet"/„Anflug" ist eine
#     Prognose.
#   · Es muss eine echte `esti`-Uhrzeit dastehen.
#   · MESSHÜRDE (`esti_changed_at`, Migration 20260802): eine Landung kann nicht
#     VOR der Landung aufgezeichnet worden sein. Boards frieren `esti` gern auf
#     dem Planwert ein und schalten nur den Status um — ein terminaler Status
#     ALLEIN beendet hier deshalb nichts. Altbestandszeilen ohne Stempel fallen
#     wie im Kalender-Lesepfad auf `updated_at` zurück.
#   · Eine Ankunft in der ZUKUNFT ist keine Landung.
#
# UND DER TURNAROUND? Ein Umlauf hat mehrere Legs; die Karte läuft über den
# Turnaround weiter. Beendet wird deshalb NUR, wenn im Activity-Fenster kein
# Leg mehr AUSSTEHT (kein Abflug in der Zukunft) und ausgerechnet das LETZTE
# Leg bestätigt gelandet ist. Genau darum bleibt das Ende hier im Sweep, der
# den ganzen Roster sieht — und nicht im MQTT-Fanout, der pro Ereignis nur ein
# einzelnes Leg kennt (dort ist `arrived` weiterhin völlig richtig „turnaround").
_LA_LANDED_HOLD_S = 60 * 60         # Owner-Staffelung: Ankunft + 60 min
_LA_LANDED_LOOKBACK_H = 20          # so weit zurück gilt eine Board-Landung
_LA_LANDED_OBS_CAP = 400            # Deckel für den Board-Read pro Sweep

# Fallback-Liste, falls app.py nicht erreichbar ist (isolierter Unit-Test).
# Die Wahrheit steht in `app._ARR_STATUS_TERMINAL` — hier NICHT erweitern.
_ARR_TERMINAL_FALLBACK = ('gelandet', 'landed', 'arrived', 'angekommen',
                          'at gate', 'on blocks', 'on-blocks', 'gepäck',
                          'gepaeck', 'baggage', 'diverted', 'umgeleitet')

# Toleranz zwischen behaupteter Ankunft und ihrem Schreib-Stempel (identisch zu
# `app._ARR_OBS_SETTLE_MIN`): Uhren laufen auseinander, ein paar Minuten sind
# kein Beleg für eine Fälschung.
_LA_OBS_SETTLE_MIN = 3


def _status_terminal(status):
    fn = _app_attr('_arr_status_terminal')
    if callable(fn):
        try:
            return bool(fn(status))
        except Exception:                               # pragma: no cover
            pass
    s = str(status or '').strip().lower()
    return bool(s) and any(t in s for t in _ARR_TERMINAL_FALLBACK)


def _flight_keys(sector):
    """Schreibweisen der Flugnummer, unter denen die Board-Zeile stehen kann
    ('LH 1457' → {'LH1457'}). Leere Menge, wenn der Sektor keine trägt."""
    raw = str((sector or {}).get('flight') or '').replace(' ', '').upper()
    keys = {raw} if raw else set()
    try:
        from blueprints.lh_mqtt import _norm_flight
        nf = _norm_flight(raw)
        if nf:
            keys.add(nf[0] + nf[1])
    except Exception:                                   # pragma: no cover
        pass
    return {k for k in keys if k}


def _closing_sector(sectors, now_utc):
    """Das Leg, dessen Landung die Karte beenden DÜRFTE — oder None.

    None heißt „die Karte darf auf keinen Fall wegen einer Landung enden":
    entweder liegt gar kein Leg im Activity-Fenster, oder es steht noch ein
    Abflug aus (Turnaround/Mehr-Leg-Umlauf).
    """
    plausible = _plausible_sectors(sectors, now_utc)
    if not plausible:
        return None
    try:
        from blueprints.lh_mqtt import _parse_iso_utc
    except Exception:                                   # pragma: no cover
        return None
    best, best_dep = None, None
    for s in plausible:
        dep = _parse_iso_utc(s.get('dep_iso'))
        if dep is None:                                 # pragma: no cover
            continue
        if dep > now_utc:
            return None                                 # ein Leg steht noch aus
        if best_dep is None or dep > best_dep:
            best, best_dep = s, dep
    return best


def _arrival_obs_rows(sectors, now_utc):
    """EIN gebündelter Board-Read für alle Kandidaten-Legs. Wirft nie; []
    heißt „keine Landung belegt" und damit: nichts beenden."""
    want_ap, want_fn = set(), set()
    for s in sectors or []:
        to = str((s or {}).get('to') or '').strip().upper()
        keys = _flight_keys(s)
        if len(to) == 3 and keys:
            want_ap.add(f'{to}#ARR')
            want_fn |= keys
    if not want_ap or not want_fn:
        return []
    client = _sb()
    if client is None:
        return []
    dates = [(now_utc.date() + timedelta(days=off)).isoformat()
             for off in (-1, 0, 1)]
    base = 'airport,flight,date,sched,esti,status,cancelled,updated_at'

    def _read(cols):
        return (client.table('airport_delay_obs').select(cols)
                .in_('airport', sorted(want_ap))
                .in_('flight', sorted(want_fn))
                .in_('date', dates)
                .limit(_LA_LANDED_OBS_CAP).execute()).data or []

    try:
        # SCHEMA-SAFE (Lehre fcm_token 01.08.): ohne die Spalte lieber ohne
        # Messhürde-Stempel lesen als gar nicht — der Altbestands-Zweig unten
        # (updated_at) trägt dann.
        return _read(base + ',esti_changed_at')
    except Exception:
        try:
            return _read(base)
        except Exception as exc:
            log.warning('[live-activity] sweep board read failed: %s',
                        type(exc).__name__)
            return []


def confirmed_arrival(obs_rows, sector, now_utc):
    """Bestätigte Landezeit dieses Legs (aware UTC) oder None. Pure.

    Die vier Hürden stehen im Kopfkommentar dieses Abschnitts. Im Zweifel
    None — eine Karte fälschlich zu beenden ist teurer, als sie 10 min zu lang
    stehen zu lassen.
    """
    to = str((sector or {}).get('to') or '').strip().upper()
    keys = _flight_keys(sector)
    if len(to) != 3 or not keys:
        return None
    try:
        from blueprints.lh_mqtt import _board_dt, _parse_iso_utc, _station_tz
    except Exception:                                   # pragma: no cover
        return None
    tz = _station_tz(to)
    if tz is None:
        # Ohne Ortszone wäre jede Board-Uhrzeit eine Rechnung ins Blaue
        # (Zeitzonen-Fehlerklasse). Dann lieber kein Ende.
        return None
    dep = _parse_iso_utc((sector or {}).get('dep_iso'))
    best = None
    for row in obs_rows or []:
        if not isinstance(row, dict):                   # pragma: no cover
            continue
        if str(row.get('airport') or '').upper() != f'{to}#ARR':
            continue
        if str(row.get('flight') or '').replace(' ', '').upper() not in keys:
            continue
        if row.get('cancelled'):
            continue
        if not _status_terminal(row.get('status')):
            continue                                    # Prognose, keine Messung
        arr = _board_dt(row.get('date'), row.get('esti'), tz)
        if arr is None:
            continue                                    # Status ohne Uhrzeit
        arr = arr.astimezone(timezone.utc)
        if arr > now_utc:
            continue                                    # „gelandet" in der Zukunft
        if (now_utc - arr) > timedelta(hours=_LA_LANDED_LOOKBACK_H):
            continue                                    # Vortags-Rotation
        if dep is not None and arr < dep:
            continue                                    # vor dem eigenen Abflug
        # MESSHÜRDE: der Stempel muss NACH der behaupteten Ankunft liegen.
        stamp = _parse_iso_utc(row.get('esti_changed_at')) \
            or _parse_iso_utc(row.get('updated_at'))
        if stamp is None:
            continue
        if stamp < arr - timedelta(minutes=_LA_OBS_SETTLE_MIN):
            continue                                    # eingefrorene Prognose
        if best is None or arr > best:
            best = arr
    return best


def _end_row(row, mark_time, reason, dismiss_after_s=0, kicker='BEENDET',
             sector=None, arrived=False):
    """EINE Zeile beenden: `event='end'` mit `dismissal-date`, danach die
    Registry-Zeile stilllegen. Die Zeile wird AUCH dann stillgelegt, wenn der
    APNs-Push scheitert — sonst pusht der Fanout weiter gegen eine Karte, die
    nachweislich nichts Richtiges mehr zeigt.

    `dismiss_after_s=0` (Default) = sofort weg: der Fall „nichts trägt mehr".
    `arrived=True` = der Abschluss NACH einer bestätigten Landung; dann trägt
    die Karte den echten Landezeitpunkt und verschwindet erst zur Frist.
    """
    state = {
        'stateVersion': 2,
        # BEWUSST ein bereits im Vertrag benutzter Phasen-Wert. Ein neuer String
        # („ended") wäre riskant: ist `phase` in Swift ein enum mit String-
        # rawValue, verwirft der Decoder ein unbekanntes ContentState KOMPLETT
        # und STILL — das Ende käme dann nie an. `turnaround` ist zugleich die
        # Phase, die der MQTT-Fanout für `arrived` setzt: gelandet, steht.
        'phase': 'turnaround',
        'kicker': kicker,
        # Echter, bekannter Zeitpunkt — keine erfundene Landezeit.
        'mainTime': mark_time,
        'generatedAt': datetime.now(timezone.utc),
    }
    if arrived:
        frm = str((sector or {}).get('from') or '').strip().upper() or None
        to = str((sector or {}).get('to') or '').strip().upper() or None
        fno = (sorted(_flight_keys(sector))[:1] or [None])[0]
        state.update({
            'fromIATA': frm, 'toIATA': to,
            'route': f'{frm}–{to}' if frm and to else None,
            'fromTZIdentifier': _airport_tz(frm),
            'toTZIdentifier': _airport_tz(to),
            'flightNo': fno, 'footLeading': fno,
            # Die gemessene Landung IST die Ankunftszeit dieser Karte.
            'estArr': mark_time,
            # Eine Landung belegt beide Enden. `arrConfirmed` ist genau das
            # Feld, an dem der Client seine 60-min-Frist (`landedGrace`)
            # festmacht — hier stimmen Server und Client überein.
            'depConfirmed': True, 'arrConfirmed': True,
            # 1.0 ist ein KONSTANTER Wert, kein Uhr-Ableitung — er darf den
            # Digest-Schutz nicht zeitabhängig machen (5-%-Quantisierung im
            # Fanout aus demselben Grund).
            'progress': 1.0,
        })
    normalized, problems = _normalize_content_state(state)
    if _fatal_problems(problems):                       # pragma: no cover
        log.warning('[live-activity] sweep end state invalid: %s', problems)
        return False
    ok = False
    try:
        res = _push_row(row, normalized, event='end',
                        dismiss_after_s=max(0, int(dismiss_after_s or 0)))
        ok = res.get('status') == 'sent'
    except Exception as exc:
        log.warning('[live-activity] sweep end push failed user_ref=%s: %s',
                    _token_ref(row.get('user_token')), type(exc).__name__)
    _rpc('end_live_activity', {
        'p_user_token': row.get('user_token'),
        'p_activity_id': row.get('activity_id'),
        'p_reason': reason})
    with _state_lock:
        _LAST_SENT.pop(row.get('id'), None)
    log.info('[live-activity] sweep ended user_ref=%s activity=%s reason=%s '
             'pushed=%s', _token_ref(row.get('user_token')),
             (row.get('activity_id') or '-')[:24], reason, ok)
    return True


def sweep_stale_live_activities(now_utc=None):
    """Beendet Live Activities ohne Grundlage. Returns Zähler-Dict. Wirft nie.

    KEIN LH-Call, kein FR24 — ausschließlich gespeicherte Roster-Zeiten und
    gespeicherte Board-Beobachtungen (`airport_delay_obs`).
    """
    counts = {'checked': 0, 'ended': 0, 'kept': 0, 'no_roster': 0,
              'hard_age': 0, 'landed': 0}
    if not _sweep_enabled():
        counts['disabled'] = True
        return counts
    now_utc = now_utc or datetime.now(timezone.utc)
    rows = _active_update_rows_all()
    start_rows = _active_start_rows_all()
    counts['checked'] = len(rows)
    # PUSH-TO-START AUS DER ECHTEN CLIENT-MARKE. Eine aktive Update-Zeile
    # gewinnt immer; ohne sie darf genau der gespeicherte iPhone-Snapshot die
    # Karte im gleichen 60-Minuten-Fenster wie der lokale Controller starten.
    active_users = {r.get('user_token') for r in rows if r.get('user_token')}
    for row in start_rows:
        user_token = row.get('user_token')
        state = row.get('last_content_state')
        if not user_token or user_token in active_users:
            continue
        if not _stored_state_wants_start(state, now_utc.timestamp()):
            continue
        if _start_cooldown_active(row, now_utc.timestamp()):
            continue
        fresh = dict(state)
        fresh['generatedAt'] = now_utc.timestamp() - _APPLE_EPOCH_OFFSET
        attrs, attr_problems = _normalize_attributes(
            _stored_start_attributes(fresh, now_utc))
        if _fatal_problems(attr_problems):
            continue
        nxt = _next_milestone_after(fresh, now_utc.timestamp())
        stale_after = (max(5 * 60, int(nxt - now_utc.timestamp()))
                       if nxt is not None else None)
        try:
            result = _push_row(row, fresh, event='start',
                               attributes=attrs, stale_after_s=stale_after)
        except Exception as exc:                       # pragma: no cover
            log.warning('[live-activity] scheduled start failed user_ref=%s: %s',
                        _token_ref(user_token), type(exc).__name__)
            continue
        if result.get('status') == 'sent':
            counts['started'] = counts.get('started', 0) + 1
            _store_last_state([row.get('id')], fresh)
    if not rows:
        return counts
    by_token = _roster_sectors_by_token({r.get('user_token') for r in rows},
                                        now_utc)
    # ERST alle Abschluss-Kandidaten sammeln, DANN EINEN Board-Read (statt
    # einem pro Karte — dieselbe Bündelungs-Regel wie beim Roster-Read).
    closing = {}
    for row in rows:
        try:
            sec = _closing_sector(by_token.get(row.get('user_token')), now_utc)
        except Exception:                               # pragma: no cover
            sec = None
        if sec is not None:
            closing[row.get('id')] = sec
    obs_rows = _arrival_obs_rows(closing.values(), now_utc) if closing else []
    for row in rows:
        try:
            sectors = by_token.get(row.get('user_token'))
            if sectors:
                # (1) BESTÄTIGTE LANDUNG schlägt jede Planzeit-Rechnung: finaler
                # Zustand mit echter Ankunftszeit, Karte verschwindet zur Frist.
                sec = closing.get(row.get('id'))
                arr_dt = (confirmed_arrival(obs_rows, sec, now_utc)
                          if sec is not None else None)
                if arr_dt is not None:
                    hold = _LA_LANDED_HOLD_S - (now_utc - arr_dt).total_seconds()
                    if _end_row(row, arr_dt, 'arrived_confirmed',
                                dismiss_after_s=hold, kicker='GELANDET',
                                sector=sec, arrived=True):
                        counts['ended'] += 1
                        counts['landed'] += 1
                    continue
                if _duty_still_plausible(sectors, now_utc):
                    if _refresh_row_at_milestone(row, now_utc):
                        counts['milestone_updates'] = \
                            counts.get('milestone_updates', 0) + 1
                    counts['kept'] += 1
                    continue
                if _end_row(row, _last_known_arrival(sectors) or
                            row.get('last_timestamp') or now_utc,
                            'stale_no_running_leg'):
                    counts['ended'] += 1
                continue
            # Kein Roster-Beleg: nur die Reißleine greift.
            counts['no_roster'] += 1
            age = _row_age_h(row, now_utc)
            if age is not None and age > _LA_SWEEP_HARD_MAX_AGE_H:
                if _end_row(row, row.get('last_timestamp') or now_utc,
                            'stale_max_age'):
                    counts['ended'] += 1
                    counts['hard_age'] += 1
            else:
                if _refresh_row_at_milestone(row, now_utc):
                    counts['milestone_updates'] = \
                        counts.get('milestone_updates', 0) + 1
                counts['kept'] += 1
        except Exception as exc:                        # pragma: no cover
            log.warning('[live-activity] sweep row failed: %s',
                        type(exc).__name__)
    if counts['ended']:
        log.info('[live-activity] sweep: %d von %d beendet (gelandet: %d, kein '
                 'laufendes Leg: %d, Reißleine: %d)', counts['ended'],
                 counts['checked'], counts['landed'],
                 counts['ended'] - counts['hard_age'] - counts['landed'],
                 counts['hard_age'])
    return counts


def kick_sweep():
    """Sweep im Hintergrund anstoßen, höchstens alle `_LA_SWEEP_MIN_GAP_S`.
    Gedacht für Aufrufer, die ohnehin regelmäßig vorbeikommen (der
    MQTT-Topic-Poll). Blockiert nie, wirft nie, liefert True wenn gestartet."""
    if not _sweep_enabled():
        return False
    now = time.time()
    with _sweep_lock:
        if _sweep_state['running']:
            return False
        if (now - _sweep_state['last']) < _LA_SWEEP_MIN_GAP_S:
            return False
        _sweep_state['running'] = True
        _sweep_state['last'] = now

    def _work():
        try:
            sweep_stale_live_activities()
        except Exception as exc:
            log.warning('[live-activity] sweep crashed: %s', type(exc).__name__)
        finally:
            with _sweep_lock:
                _sweep_state['running'] = False
            try:
                from app import _close_current_thread_supabase_client
                _close_current_thread_supabase_client()
            except Exception:
                pass

    try:
        threading.Thread(target=_work, daemon=True,
                         name='la-stale-sweep').start()
        return True
    except Exception as exc:
        # Ohne das bliebe das Flag für immer True und dieser Worker würde nie
        # wieder aufräumen (gleiche Falle wie in lh_mqtt._topics_kick_refresh).
        with _sweep_lock:
            _sweep_state['running'] = False
        log.warning('[live-activity] sweep thread start fail: %s',
                    type(exc).__name__)
        return False


@live_activity_bp.route('/api/internal/live-activity/sweep', methods=['POST'])
def internal_live_activity_sweep():
    """NUR intern (Cron/manuell). Beendet Live Activities ohne laufendes Leg.
    Läuft synchron und liefert die Zähler — als Beleg nach einem Deploy."""
    if not _secret_ok():
        return jsonify({'ok': False, 'error': 'forbidden'}), 403
    return jsonify({'ok': True, **sweep_stale_live_activities()})
