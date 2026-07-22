"""LH Open API MQTT-Push-Notifications — Backend-Gehirn (Engine A2, 2026-07-22).

Der Akamai-MQTT-Broker der Lufthansa publiziert pro Flug Change-Events
(Gate-Änderung, neue Estimated-Zeiten, Departed/Arrived, Cancelled, Diverted)
OHNE Business-Daten — nur „es hat sich was geändert" + Link auf die
FlightStatus-Resource. Live verifiziert 2026-07-22: Topic-Shape
`prd/FlightUpdate/<carrier>/<carrier><nr>/<datum-lokal>`, Payload
`{"Update": {"Timestamp", "Message", "FlightNumber", "ScheduledFlightDate",
"ScheduledFlightTime"}, "Meta": {"Link": [...]}}`.

Arbeitsteilung (bewusst): der eigenständige Daemon-Prozess (`lh_mqtt_daemon.py`,
eigener Compose-Service) ist DUMM — er hält nur die MQTT-Verbindung, holt sich
hier die Topic-Liste und reicht empfangene Events hierher zurück. ALLE Logik
(welche Flüge, lokales Topic-Datum, User-Mapping, Push-Texte, LH-Fakten-
Refresh, Dedupe) lebt in diesem Blueprint — offline testbar, ein Deploy-Pfad.

Endpoints (Auth wie /api/internal/poll-boards: `X-Poll-Secret` ==
ADSB_POLL_SECRET; ohne gesetztes Secret nur localhost):
- GET  /api/internal/lh-mqtt/topics — Topic-Liste aus den Roster-Sektoren
  aller User (LH-Group, Abflug −4h…+48h; Topic-Datum = LOKALES Abflugdatum
  am Start-Airport via AIRPORT_TZ — der Broker keyt auf das operationelle
  Lokal-Datum, UTC-Datum kann daneben liegen).
- POST /api/internal/lh-mqtt/event — ein empfangenes Broker-Event: frische
  LH-Fakten ziehen (force, umgeht den 120s-Memo) und betroffene Crews pushen
  (Gate-Änderung / Verspätung ≥15 min / Annullierung / Umleitung). Dedupe über
  den Push-Outbox-idempotency_key (wertbasiert: gleiches Gate/gleiche
  Est-Zeit pusht nie doppelt, ECHTE Folge-Änderung schon).
- GET  /api/lh/mqtt/status — Diagnose (Zähler + letzte Events, pro Worker).

Push-Policy bewusst konservativ: Departed/Arrived/Est-Arrival wecken keine
Crew (sie sitzt selbst drin bzw. Inbound-Push existiert separat) — diese
Events refreshen nur die Fakten. Kein Event erfindet Daten: fehlt das neue
Gate in den Fakten, sagt der Push ehrlich „Details in der App".
"""
import re
import time
import threading
import logging
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, request

from blueprints.lh_open_api import is_lh_group, lh_flight_facts

log = logging.getLogger('aerotax')
lh_mqtt_bp = Blueprint('lh_mqtt_bp', __name__)

# Abflug-Fenster für Subscriptions: leicht in die Vergangenheit (laufende
# Flüge behalten ihr Topic bis zur Landung), 48h voraus (Gate-Änderungen
# kommen ohnehin erst kurz vorher, Cancellations auch mal früher).
_SUB_PAST_H = 4
_SUB_FUTURE_H = 48

# Inbound-Watch (Owner 22.07.: „was cool ist, wann der Inbound-Flieger
# abfliegt und ankommt — dann weiß man im Layover, ob es pünktlich ist"):
# Legs mit Abflug in diesem Fenster bekommen die Maschinen-Zubringer-Topics.
_INBOUND_DEP_WINDOW_H = 16

_TOPIC_RE = re.compile(r'^prd/FlightUpdate/([A-Z0-9]{2})/([A-Z0-9]{2})(\d{1,4})/'
                       r'(\d{4}-\d{2}-\d{2})$')
_FLIGHT_RE = re.compile(r'^([A-Z0-9]{2})(\d{1,4})[A-Z]?$')

# Topic-Listen-Memo (der Daemon fragt alle ~5 min; SB entsprechend selten
# belasten — der Voll-Fetch über alle User ist der teuerste Query hier)
_topics_lock = threading.Lock()
_topics_memo = {'ts': 0.0, 'topics': []}
_TOPICS_TTL_S = 240

# Diagnose (pro Gunicorn-Worker — Status zeigt die Sicht EINES Workers)
_stat_lock = threading.Lock()
_stats = {'events': 0, 'pushes': 0, 'last_events': []}


def _secret_ok():
    """Gleiche Auth wie poll-boards: Secret-Header, sonst nur localhost."""
    import os as _os
    import hmac as _hmac
    secret = _os.environ.get('ADSB_POLL_SECRET', '').strip()
    if secret:
        provided = (request.headers.get('X-Poll-Secret') or '').strip()
        return bool(provided) and _hmac.compare_digest(provided, secret)
    return (request.remote_addr or '') in ('127.0.0.1', '::1')


def _norm_flight(flight_no):
    """'LH 0400' → ('LH', '400') oder None. Führende Nullen fallen weg, weil
    die Broker-Topics unpadded sind (live gesehen: LH2015, LX1821, EW586)."""
    fn = (flight_no or '').replace(' ', '').upper().strip()
    m = _FLIGHT_RE.match(fn)
    if not m:
        return None
    num = m.group(2).lstrip('0')
    if not num:
        return None
    return m.group(1), num


def _parse_iso_utc(s):
    """ISO-String → aware UTC-datetime oder None. Naiv = als UTC gelesen
    (dep_iso der Roster-Sektoren ist UTC-gekeyt)."""
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace('Z', '+00:00'))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)
    except Exception:
        return None


def _sector_topic_dates(sector):
    """Kandidaten-Topic-Daten (ISO-Strings) eines Sektors. Der Broker keyt auf
    das LOKALE Abflugdatum; mit bekannter Airport-TZ ist das EIN Datum, ohne
    TZ konservativ Lokal-Fenster UTC±1 Tag."""
    dep = _parse_iso_utc(sector.get('dep_iso'))
    if dep is None:
        return []
    frm = (sector.get('from') or '').strip().upper()
    try:
        from airport_tz import AIRPORT_TZ
        from zoneinfo import ZoneInfo
        tz_name = AIRPORT_TZ.get(frm, (None, None))[1]
        if tz_name:
            return [dep.astimezone(ZoneInfo(tz_name)).date().isoformat()]
    except Exception:
        pass
    d = dep.date()
    return [(d + timedelta(days=off)).isoformat() for off in (-1, 0, 1)]


# Schlankes Select: NUR die Sektoren via jsonb-Pfad, nicht das ganze
# raw_event (Voll-Payload wäre ~4× größer — Egress).
_SECTOR_SELECT = 'token,datum,sectors:raw_event->ical_sectors'


def _sb():
    """Test-Seam: Supabase-Client oder None. Lazy-Import (Blueprint bleibt
    ohne app-Import ladbar)."""
    try:
        from app import sb, SB_AVAILABLE
        return sb if (SB_AVAILABLE and sb is not None) else None
    except Exception:
        return None


def _sector_rows(dates):
    """Alle Briefing-Rows der Daten — PAGINIERT. PostgREST kappt still bei
    1000 Rows (live gemessen 2026-07-22: 3682 Rows im 4-Tage-Fenster — ohne
    range() fehlten ~73% der User in Topics UND Push-Fanout). Wirft nie."""
    client = _sb()
    if client is None:
        return []
    out = []
    page = 1000
    try:
        for start in range(0, 40000, page):
            r = (client.table('user_ical_briefings')
                 .select(_SECTOR_SELECT)
                 .in_('datum', list(dates))
                 .range(start, start + page - 1).execute())
            rows = r.data or []
            out.extend(rows)
            if len(rows) < page:
                break
    except Exception as e:
        log.warning('[lh_mqtt] sector rows fail: %s', type(e).__name__)
    return out


def _rows_for_flight(dates, carrier, num):
    """Nur die Rows, deren Sektoren GENAU diesen Flug tragen — jsonb-
    Containment serverseitig (Bruchteil des Voll-Fetches; Live-Format ist
    kompakt 'LH501', Space-/Padding-Varianten als Belt&Braces). Fallback bei
    Query-Fehler: paginierter Voll-Fetch."""
    client = _sb()
    if client is None:
        return []
    variants = [f'{carrier}{num}', f'{carrier} {num}']
    if len(num) < 4:
        variants.append(f'{carrier}{num.zfill(4)}')
    out, seen_tok_datum = [], set()
    ok = False
    for v in variants:
        try:
            r = (client.table('user_ical_briefings')
                 .select(_SECTOR_SELECT)
                 .in_('datum', list(dates))
                 .filter('raw_event->ical_sectors', 'cs',
                         f'[{{"flight":"{v}"}}]')
                 .execute())
            ok = True
            for row in (r.data or []):
                k = (row.get('token'), row.get('datum'))
                if k not in seen_tok_datum:
                    seen_tok_datum.add(k)
                    out.append(row)
        except Exception as e:
            log.warning('[lh_mqtt] flight rows cs fail %s: %s', v,
                        type(e).__name__)
    if not ok:
        return _sector_rows(dates)
    return out


def _rows_from_station(dates, station):
    """Nur Rows, deren Sektoren an dieser Station STARTEN (jsonb-Containment)
    — für den Inbound-Watch am Ankunfts-Airport eines Events. Fallback:
    paginierter Voll-Fetch."""
    client = _sb()
    if client is None:
        return []
    try:
        r = (client.table('user_ical_briefings')
             .select(_SECTOR_SELECT)
             .in_('datum', list(dates))
             .filter('raw_event->ical_sectors', 'cs',
                     f'[{{"from":"{station}"}}]')
             .execute())
        return r.data or []
    except Exception as e:
        log.warning('[lh_mqtt] station rows cs fail %s: %s', station,
                    type(e).__name__)
        return _sector_rows(dates)


def _iter_sectors(rows):
    """(token, sector_dict) über alle Briefing-Rows (neue schlanke 'sectors'-
    Shape, legacy raw_event.ical_sectors als Fallback)."""
    for row in rows or []:
        secs = row.get('sectors')
        if not isinstance(secs, list):
            raw = row.get('raw_event') or {}
            secs = raw.get('ical_sectors') if isinstance(raw, dict) else None
        if not isinstance(secs, list):
            continue
        tok = row.get('token')
        for s in secs:
            if isinstance(s, dict):
                yield tok, s


def topics_for_rows(rows, now_utc):
    """Pure: Briefing-Rows → sortierte Topic-Liste (dedupliziert über User —
    ein Discover-Flug mit 8 AeroX-Crews = EIN Topic)."""
    topics = set()
    lo = now_utc - timedelta(hours=_SUB_PAST_H)
    hi = now_utc + timedelta(hours=_SUB_FUTURE_H)
    for _tok, s in _iter_sectors(rows):
        nf = _norm_flight(s.get('flight'))
        if not nf or not is_lh_group(nf[0] + nf[1]):
            continue
        dep = _parse_iso_utc(s.get('dep_iso'))
        if dep is None or not (lo <= dep <= hi):
            continue
        for d in _sector_topic_dates(s):
            topics.add(f'prd/FlightUpdate/{nf[0]}/{nf[0]}{nf[1]}/{d}')
    return sorted(topics)


# ── Inbound-Watch: Maschinen-Zubringer eines Roster-Legs ─────────────────────

def _sector_tail(s):
    """Roster-Tail eines Sektors (gleiche Key-Kaskade wie crew_live_state).
    Meist LEER — Tails werden nur in API-Antworten enriched, nicht in Supabase
    zurückgeschrieben; dann greift die LH-autoritative Reg (_cached_leg_reg)."""
    for k in ('tail', 'reg', 'ac_reg', 'registration', 'aircraft_reg'):
        v = s.get(k)
        if v:
            return str(v)
    return None


# Reg-Memo entkoppelt vom 120s-Facts-Memo: Maschinen-Zuteilung ändert sich
# selten — 45 min TTL hält den LH-Budget-Verbrauch der Topics-Rechnung klein.
_reg_lock = threading.Lock()
_reg_memo = {}
_REG_TTL_S = 2700
_REG_NEG_TTL_S = 600


def _cached_leg_reg(flight_disp, date, dep, arr):
    key = (flight_disp, date, dep, arr)
    now = time.time()
    with _reg_lock:
        hit = _reg_memo.get(key)
        if hit and now < hit[0]:
            return hit[1]
    reg = (lh_flight_facts(flight_disp, date, dep, arr) or {}).get('reg')
    with _reg_lock:
        _reg_memo[key] = (now + (_REG_TTL_S if reg else _REG_NEG_TTL_S), reg)
        if len(_reg_memo) > 3000:
            items = sorted(_reg_memo.items(), key=lambda kv: kv[1][0])
            for k, _v in items[:len(items) // 4 or 1]:
                _reg_memo.pop(k, None)
    return reg


def _station_tz(iata):
    try:
        from airport_tz import AIRPORT_TZ
        from zoneinfo import ZoneInfo
        name = AIRPORT_TZ.get((iata or '').upper(), (None, None))[1]
        return ZoneInfo(name) if name else None
    except Exception:
        return None


def _board_dt(date_str, val, tz):
    """Board-Zeit ('14:30' | ISO) + Service-Datum → aware datetime (Board-
    Zeiten sind stations-lokal) oder None."""
    if not val or not date_str:
        return None
    v = str(val).strip()
    try:
        if 'T' in v:
            d = datetime.fromisoformat(v.replace('Z', '+00:00'))
            if d.tzinfo is None:
                d = d.replace(tzinfo=tz)
            return d
        hh, mm = v[:5].split(':')
        base = datetime.fromisoformat(date_str[:10])
        return base.replace(hour=int(hh), minute=int(mm), tzinfo=tz)
    except Exception:
        return None


def _arr_board_rows(stations, regs, dates):
    """ARR-Board-Rows (airport='<Station>#ARR') für die Reg-Kandidaten —
    EIN Batch-Query, Reg-Varianten mit/ohne Bindestrich. Wirft nie."""
    client = _sb()
    if client is None or not stations or not regs:
        return []
    from blueprints.lh_open_api import _norm_reg
    variants = set()
    for r in regs:
        rn = str(r).replace('-', '').upper()
        variants.add(rn)
        variants.add(_norm_reg(rn))
    try:
        r = (client.table('airport_delay_obs')
             .select('airport,flight,reg,sched,esti,date')
             .in_('airport', [f'{s}#ARR' for s in sorted(stations)])
             .in_('date', sorted(dates))
             .in_('reg', sorted(variants)).execute())
        return r.data or []
    except Exception as e:
        log.warning('[lh_mqtt] arr board rows fail: %s', type(e).__name__)
        return []


def _best_inbound_for_leg(arr_rows, frm, reg, dep_utc):
    """Die LETZTE Board-Ankunft dieser Maschine vor dem Leg-Abflug — das ist
    der Zubringer. Ohne Airport-TZ keine Aussage (lieber kein Inbound als der
    falsche aus der Morgen-Rotation)."""
    tz = _station_tz(frm)
    if tz is None or dep_utc is None:
        return None
    dep_local = dep_utc.astimezone(tz)
    rn = (reg or '').replace('-', '').upper()
    best, best_dt = None, None
    for row in arr_rows or []:
        if (row.get('airport') or '') != f'{frm}#ARR':
            continue
        if str(row.get('reg') or '').replace('-', '').upper() != rn:
            continue
        dt = _board_dt(row.get('date'), row.get('esti') or row.get('sched'), tz)
        if dt is None:
            continue
        if not (dep_local - timedelta(hours=12) <= dt
                <= dep_local + timedelta(minutes=45)):
            continue
        if best_dt is None or dt > best_dt:
            best, best_dt = row, dt
    return best


def inbound_topics_for_rows(rows, now_utc):
    """Topics der Maschinen-Zubringer: pro Leg (Abflug −1h…+16h) die LH-
    autoritative Reg holen, am Abflug-Airport die letzte ARR-Board-Ankunft
    dieser Reg finden → deren Flug subscriben (Topic-Datum = Board-Service-
    Datum, plus Vortag für Langstrecken-Zubringer, die lokal am Vortag
    starteten)."""
    lo = now_utc - timedelta(hours=1)
    hi = now_utc + timedelta(hours=_INBOUND_DEP_WINDOW_H)
    legs = []
    for _tok, s in _iter_sectors(rows):
        nf = _norm_flight(s.get('flight'))
        if not nf or not is_lh_group(nf[0] + nf[1]):
            continue
        dep = _parse_iso_utc(s.get('dep_iso'))
        frm = (s.get('from') or '').strip().upper()
        if dep is None or len(frm) != 3 or not (lo <= dep <= hi):
            continue
        reg = _sector_tail(s) or _cached_leg_reg(
            nf[0] + nf[1], dep.date().isoformat(), frm,
            (s.get('to') or '').strip().upper() or None)
        if reg:
            legs.append((frm, str(reg).replace('-', '').upper(), dep))
    if not legs:
        return set()
    dates = {(now_utc.date() + timedelta(days=o)).isoformat()
             for o in (-1, 0, 1)}
    obs = _arr_board_rows({f for f, _r, _d in legs},
                          {r for _f, r, _d in legs}, dates)
    topics = set()
    for frm, reg, dep in legs:
        row = _best_inbound_for_leg(obs, frm, reg, dep)
        if not row:
            continue
        nf = _norm_flight(row.get('flight'))
        try:
            base = datetime.fromisoformat(str(row.get('date'))[:10]).date()
        except Exception:
            continue
        if not nf:
            continue
        for off in (0, -1):
            d = (base + timedelta(days=off)).isoformat()
            topics.add(f'prd/FlightUpdate/{nf[0]}/{nf[0]}{nf[1]}/{d}')
    return topics


@lh_mqtt_bp.route('/api/internal/lh-mqtt/topics', methods=['GET'])
def lh_mqtt_topics():
    if not _secret_ok():
        return jsonify({'ok': False, 'error': 'forbidden'}), 403
    now = time.time()
    with _topics_lock:
        if now - _topics_memo['ts'] < _TOPICS_TTL_S:
            return jsonify({'ok': True, 'topics': _topics_memo['topics'],
                            'count': len(_topics_memo['topics']), 'memo': True})
    now_utc = datetime.now(timezone.utc)
    dates = [(now_utc.date() + timedelta(days=off)).isoformat()
             for off in (-1, 0, 1, 2)]
    rows = _sector_rows(dates)
    tset = set(topics_for_rows(rows, now_utc))
    try:
        tset |= inbound_topics_for_rows(rows, now_utc)
    except Exception as e:
        log.warning('[lh_mqtt] inbound topics fail: %s', type(e).__name__)
    topics = sorted(tset)
    with _topics_lock:
        _topics_memo['ts'] = now
        _topics_memo['topics'] = topics
    return jsonify({'ok': True, 'topics': topics, 'count': len(topics)})


# ── Event-Verarbeitung ───────────────────────────────────────────────────────

# Broker-„Message"-Freitext → Event-Art. (Die FLUP-Codes aus der Doku kommen
# im Live-Payload nicht mit — der Text ist die verlässliche Quelle.)
_KIND_PATTERNS = [
    ('gate', 'gate'),
    ('estimated departure', 'est_dep'),
    ('estimated arrival', 'est_arr'),
    ('departed', 'departed'),
    ('arrived', 'arrived'),
    ('cancel', 'cancelled'),
    ('divert', 'diverted'),
    ('reinstat', 'reinstated'),
    ('rerout', 'rerouted'),
    ('schedule', 'schedule'),
]


def classify_message(message):
    m = (message or '').lower()
    for needle, kind in _KIND_PATTERNS:
        if needle in m:
            return kind
    return 'other'


def _hhmm(iso_str):
    """'2026-07-22T17:45:00+02:00' → '17:45' (station-lokal, wie geliefert)."""
    try:
        return str(iso_str)[11:16]
    except Exception:
        return None


def _do_push(token, title, body, data=None, idempotency_key=None):
    """Test-Seam um die echte Push-Outbox (app._push_notify_async)."""
    from app import _push_notify_async
    return _push_notify_async(token, title, body, data=data,
                              idempotency_key=idempotency_key)


def _users_for_flight(rows, carrier, num, topic_date):
    """[(token, sector)] aller User, deren Roster genau diesen Flug an diesem
    (lokalen) Topic-Datum trägt."""
    out = []
    seen = set()
    for tok, s in _iter_sectors(rows):
        if not tok or tok in seen:
            continue
        nf = _norm_flight(s.get('flight'))
        if nf != (carrier, num):
            continue
        if topic_date not in _sector_topic_dates(s):
            continue
        seen.add(tok)
        out.append((tok, s))
    return out


def _build_push(kind, flight_disp, topic_date, facts, sector):
    """(title, body, idempotency_suffix) oder None wenn dieses Event keinen
    Push verdient. Kein erfundenes Datum: fehlende Fakten → ehrliche Texte."""
    frm = (sector.get('from') or '').strip().upper()
    to = (sector.get('to') or '').strip().upper()
    route = f'{frm}–{to}' if frm and to else None
    try:
        nice_date = datetime.fromisoformat(topic_date).strftime('%d.%m.')
    except Exception:
        nice_date = topic_date

    if kind == 'est_dep':
        delay = facts.get('dep_delay_min')
        est = _hhmm(facts.get('est_dep'))
        sched = _hhmm(facts.get('sched_dep'))
        if not isinstance(delay, int) or delay < 15 or not est:
            return None
        body = f'{route or flight_disp} am {nice_date}: Abflug {est}'
        if sched:
            body += f' statt {sched}'
        body += f' (+{delay} min).'
        return (f'Verspätung · {flight_disp}', body, f'estdep:{est}')

    if kind == 'cancelled':
        body = (f'{route or flight_disp} am {nice_date} wurde annulliert. '
                'Bitte Dienstplan prüfen.')
        return (f'Flug annulliert · {flight_disp}', body, 'cancelled')

    if kind == 'diverted':
        body = (f'{route or flight_disp} am {nice_date} wird umgeleitet — '
                'Details in der App.')
        return (f'Umleitung · {flight_disp}', body, 'diverted')

    return None


def _push_inbound(kind, event_flight, topic_date):
    """Departed/Arrived eines (subscribten) Flugs: die Crews finden, deren
    NÄCHSTES Leg am Ankunfts-Airport mit GENAU dieser Maschine geplant ist,
    und ihnen den Zubringer-Status pushen — im Layover weiß man so, ob der
    eigene Abflug pünktlich wird. Guard gegen die Früh-Rotation derselben
    Maschine: der Event-Flug muss der BESTE (letzte) Board-Inbound vor dem
    Leg sein; ohne Board-Daten (Outstation) zählt der Maschinen-Match."""
    facts = lh_flight_facts(event_flight, topic_date, force=True) or {}
    reg = facts.get('reg')
    arr = (facts.get('arr_iata') or '').strip().upper()
    if not reg or len(arr) != 3:
        return 0
    rn = str(reg).replace('-', '').upper()
    now_utc = datetime.now(timezone.utc)
    dates = [(now_utc.date() + timedelta(days=o)).isoformat()
             for o in (-1, 0, 1, 2)]
    rows = _rows_from_station(dates[:3], arr)
    est_arr = _hhmm(facts.get('est_arr') or facts.get('sched_arr'))
    origin = facts.get('dep_iata')
    delay = facts.get('arr_delay_min')
    delay_txt = (f' ({delay:+d} min)'
                 if isinstance(delay, int) and abs(delay) >= 5 else '')
    obs = None
    pushed = 0
    seen = set()
    for tok, s in _iter_sectors(rows):
        if not tok or tok in seen:
            continue
        frm = (s.get('from') or '').strip().upper()
        if frm != arr:
            continue
        dep = _parse_iso_utc(s.get('dep_iso'))
        if dep is None or not (now_utc - timedelta(hours=1) <= dep
                               <= now_utc + timedelta(
                                   hours=_INBOUND_DEP_WINDOW_H)):
            continue
        nf = _norm_flight(s.get('flight'))
        if not nf or not is_lh_group(nf[0] + nf[1]):
            continue
        user_flight = nf[0] + nf[1]
        leg_reg = _sector_tail(s) or _cached_leg_reg(
            user_flight, dep.date().isoformat(), frm,
            (s.get('to') or '').strip().upper() or None)
        if not leg_reg or str(leg_reg).replace('-', '').upper() != rn:
            continue
        if obs is None:
            obs = _arr_board_rows({arr}, {rn}, set(dates[:3]))
        best = _best_inbound_for_leg(obs, arr, rn, dep)
        if best is not None:
            bn = _norm_flight(best.get('flight'))
            if bn and (bn[0] + bn[1]) != event_flight:
                continue  # Event ist eine frühere Rotation der Maschine
        tz = _station_tz(frm)
        dep_local = dep.astimezone(tz).strftime('%H:%M') if tz else None
        if kind == 'departed':
            title = f'Dein Flieger ist gestartet · {user_flight}'
            body = f'{reg} kommt als {event_flight}'
            if origin:
                body += f' aus {origin}'
            if est_arr:
                body += f' — Ankunft in {arr} ca. {est_arr}'
            body += f'{delay_txt}.'
            ptype = 'inbound_departure'
        else:
            title = f'Dein Flieger ist gelandet · {user_flight}'
            body = f'{reg} ist in {arr} gelandet{delay_txt}'
            if dep_local:
                body += f' — dein {user_flight} geht um {dep_local}'
            body += '.'
            ptype = 'inbound_arrival'
        key = f'lhflup:inb:{event_flight}:{topic_date}:{kind}:{tok}'
        try:
            _do_push(tok, title, body,
                     data={'type': ptype, 'flight': user_flight,
                           'date': dep.date().isoformat(),
                           'inbound_flight': event_flight, 'reg': str(reg),
                           'kind': kind},
                     idempotency_key=key)
            pushed += 1
            seen.add(tok)
        except Exception as e:
            log.warning('[lh_mqtt] inbound push fail %s: %s', user_flight,
                        type(e).__name__)
    return pushed


def _record_event(topic, kind, users, pushed):
    with _stat_lock:
        _stats['events'] += 1
        _stats['pushes'] += pushed
        _stats['last_events'].append({
            'ts': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'topic': topic, 'kind': kind, 'users': users, 'pushed': pushed})
        del _stats['last_events'][:-50]


@lh_mqtt_bp.route('/api/internal/lh-mqtt/event', methods=['POST'])
def lh_mqtt_event():
    if not _secret_ok():
        return jsonify({'ok': False, 'error': 'forbidden'}), 403
    body = request.get_json(silent=True) or {}
    topic = (body.get('topic') or '').strip()
    payload = body.get('payload') or {}
    m = _TOPIC_RE.match(topic)
    if not m or not isinstance(payload, dict):
        return jsonify({'ok': False, 'error': 'bad_event'}), 400
    carrier, carrier2, num_raw, topic_date = m.groups()
    num = num_raw.lstrip('0') or num_raw
    flight_disp = f'{carrier}{num}'
    upd = payload.get('Update') or {}
    kind = classify_message(upd.get('Message'))
    ev_ts = str(upd.get('Timestamp') or '')

    # Betroffene Crews (UTC-Datum-Keying des Rosters kann neben dem lokalen
    # Topic-Datum liegen → ±1 Tag lesen, exakt matcht _users_for_flight).
    base = None
    try:
        base = datetime.fromisoformat(topic_date).date()
    except Exception:
        pass
    dates = ([(base + timedelta(days=off)).isoformat() for off in (-1, 0, 1)]
             if base else [topic_date])
    rows = _rows_for_flight(dates, carrier, num)
    affected = _users_for_flight(rows, carrier, num, topic_date)

    # Frische LH-Fakten (force umgeht den Memo — der Sinn des Push-Kanals ist
    # ja gerade: Fakten JETZT, nicht nach TTL). Gate-Events refreshen NUR die
    # Fakten (Owner 22.07.: „Gate ist egal" — kein Push, aber die App zeigt
    # so das frische Gate). Leg-Wahl über den ersten betroffenen Sektor.
    facts = {}
    pushed = 0
    if kind in ('gate', 'est_dep', 'cancelled', 'diverted') and affected:
        s0 = affected[0][1]
        facts = lh_flight_facts(flight_disp, topic_date,
                                (s0.get('from') or '').strip().upper() or None,
                                (s0.get('to') or '').strip().upper() or None,
                                force=True) or {}
        for tok, sector in affected:
            built = _build_push(kind, flight_disp, topic_date, facts, sector)
            if not built:
                break  # wert-basiert für alle gleich (z.B. Delay < 15 min)
            title, text, suffix = built
            key = f'lhflup:{flight_disp}:{topic_date}:{suffix}:{tok}'
            try:
                _do_push(tok, title, text,
                         data={'type': 'flight_update', 'flight': flight_disp,
                               'date': topic_date, 'kind': kind,
                               'event_ts': ev_ts},
                         idempotency_key=key)
                pushed += 1
            except Exception as e:
                log.warning('[lh_mqtt] push fail %s: %s', flight_disp,
                            type(e).__name__)
    elif kind in ('departed', 'arrived'):
        # Inbound-Watch: diese Maschine ist der Zubringer für wen?
        try:
            pushed = _push_inbound(kind, flight_disp, topic_date)
        except Exception as e:
            log.warning('[lh_mqtt] inbound push fail %s: %s', flight_disp,
                        type(e).__name__)

    _record_event(topic, kind, len(affected), pushed)
    log.info('[lh_mqtt] event %s kind=%s users=%d pushed=%d', topic, kind,
             len(affected), pushed)
    return jsonify({'ok': True, 'kind': kind, 'users': len(affected),
                    'pushed': pushed})


@lh_mqtt_bp.route('/api/lh/mqtt/status', methods=['GET'])
def lh_mqtt_status():
    """Diagnose (kein Secret, keine PII — nur Flug-Events/Zähler). Achtung:
    zeigt die Sicht EINES Gunicorn-Workers; Events landen beim Worker, der den
    POST des Daemons zog."""
    with _stat_lock:
        return jsonify({'ok': True, 'events': _stats['events'],
                        'pushes': _stats['pushes'],
                        'last_events': list(_stats['last_events'])})
