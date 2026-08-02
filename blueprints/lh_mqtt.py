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
  aller User (LH-Group; Abflug bis +48h voraus, Abo läuft bis ANKUNFT +1h —
  s. `_SUB_ARR_GRACE_H`, das Fenster hing bis 2026-07-31 am Abflug und warf
  Langstrecken mitten im Flug ab; Topic-Datum = LOKALES Abflugdatum am
  Start-Airport via AIRPORT_TZ — der Broker keyt auf das operationelle
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

# ── Abo-Fenster: es hängt an der ANKUNFT, nicht am Abflug ───────────────────
#
# ⚠️ HIER LAG DIE WURZEL DER BESCHWERDE „arrival time was wrong the whole time"
# (Audit 2026-07-31, read-only, Zahlen aus ax_api_budget + 52-Event-Mitschnitt).
#
# Bis heute stand hier ein reines ABFLUG-Fenster: `dep ∈ [now−4h, now+48h]`.
# Der Kommentar behauptete „laufende Flüge behalten ihr Topic bis zur Landung" —
# das galt nur für Kurzstrecke. Jeder Flug mit mehr als 4 h Blockzeit verlor
# sein Abo MITTEN IM FLUG:
#   · 24,7 % aller Legs (355 von 1440) betroffen
#   · Median 5,6 h blind, Maximum 10,3 h blind — jeweils VOR der Landung
#   · empirisch: 11 von 11 beobachteten `arrived`-Events waren ≤2,2-h-Flüge;
#     LH433 (8,5 h) lieferte in der ganzen Messung 0 Events
# Genau deshalb kam auf Langstrecke nie ein est_arr/arrived an, die Live
# Activity zählte bis zur beim Abflug eingefrorenen Zeit herunter, und der
# Rückblick sah nie die echte Landung.
#
# Neu: das Fenster endet ARR+1h (echte `arr_iso` des Sektors). Kein bekanntes
# arr ⇒ konservative Max-Blockannahme (`_SUB_BLOCK_FALLBACK_H`) statt einer
# kurzen Annahme — lieber ein Topic zu lang halten als wieder mitten im Flug
# abmelden. Das Ergebnis ist eine echte OBERMENGE des alten Fensters: der
# Boden `dep + _SUB_PAST_H` bleibt als Untergrenze stehen, das Abo kann also
# nie KÜRZER werden als es heute schon ist.
#
# Topic-Zahl: das Fenster verlängert im Schnitt um Stunden. Der Audit fand
# kein beobachtetes LH-Limit (40er-Chunks fehlerfrei), trotzdem wird die Zahl
# in `_topics_compute` geloggt — wer sie nicht misst, merkt eine Decke erst,
# wenn Abos still fehlen.
_SUB_PAST_H = 4                # Untergrenze: nie kürzer als das alte Fenster
_SUB_FUTURE_H = 48             # Vorlauf (Gate/Cancel kommen früher als der Flug)
_SUB_ARR_GRACE_H = 1           # nach der geplanten Ankunft noch zuhören
# Kein `arr_iso` im Sektor: der längste LH-Group-Umlauf liegt deutlich unter
# 16 h Block — diese Annahme ist bewusst zu GROSSZÜGIG, weil ein zu kurzes
# Fenster genau der Fehler ist, den diese Änderung behebt.
_SUB_BLOCK_FALLBACK_H = 16
# Obergrenze gegen kaputte/verrutschte arr-Zeiten (Datumssprung im iCal, arr
# im nächsten Jahr): ein einzelner Mülleintrag darf kein Dauer-Abo erzeugen.
_SUB_BLOCK_MAX_H = 20

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

# ── Warum hier mehr steht als ein Memo (Messung 27.07.: 16,7 s Schnitt, 37 s max)
#
# Die Rechnung ist teuer, weil `inbound_topics_for_rows` → `_legs_regs` für
# jedes noch unbekannte Leg EINEN blockierenden HTTP-Call gegen die LH-Open-API
# macht — sequentiell, 10 s Timeout pro Call. Drei Fehler kamen zusammen:
#
#   1. Das Memo lief mit 240 s ab, der Daemon fragt aber alle 300 s
#      (lh_mqtt_daemon.py `_REFRESH_S`). Das Memo war also bei JEDEM Poll
#      schon tot — es hat nie getroffen.
#   2. Der Lock deckte nur Lesen und Schreiben ab, nicht die Rechnung dazwischen.
#      Kalte Aufrufer rechneten deshalb parallel dieselbe Rechnung.
#   3. Der Daemon bricht nach 30 s ab und wiederholt nach 20 s (`refresh_ok`
#      false). Jeder Wiederholer traf wieder auf ein kaltes Memo — dreimal
#      dieselbe Rechnung, live beobachtet.
#
# Antwort auf alle drei: der Aufrufer bekommt IMMER sofort den letzten
# Schnappschuss (stale-while-revalidate), die Erneuerung läuft im Hintergrund,
# und es rechnet immer nur EINER (`_topics_build_lock` = Single-Flight). Die
# allererste Rechnung eines Prozesses hat keinen Schnappschuss — die bekommt
# ein Zeitbudget, damit sie garantiert unter den 30 s des Daemons bleibt.
_topics_build_lock = threading.Lock()
_topics_state = {'refreshing': False}
_TOPICS_BUILD_BUDGET_S = 18.0
_TOPICS_RETRY_AFTER_S = 20.0

# Obergrenze für „alt, aber wir liefern es trotzdem sofort aus".
#
# Wichtig, weil produktiv DREI Gunicorn-Worker laufen und der Daemon alle 300 s
# fragt: ein einzelner Worker wird im Schnitt nur alle ~900 s getroffen. Würde
# jeder Treffer bedingungslos den alten Stand ausliefern, wäre die Topic-Liste
# im Mittel eine Viertelstunde alt — schlechter als vorher —, und die drei
# Worker würden auseinanderlaufen. Da der Daemon sich von allem abmeldet, was
# in der Antwort fehlt, führte das zu flatternden Abos.
#
# Deshalb: bis _TOPICS_MAX_STALE_S alt sofort ausliefern und im Hintergrund
# erneuern; darüber hinaus wird gerechnet — unter Budget und Single-Flight,
# also weiterhin garantiert unter dem 30-s-Timeout des Daemons.
_TOPICS_MAX_STALE_S = 600

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
# raw_event (Voll-Payload wäre ~4× größer — Egress). `updated_at` kam
# 2026-07-30 für die Frische-Schranke dazu (s. _drop_stale_rows).
_SECTOR_SELECT = 'token,datum,updated_at,sectors:raw_event->ical_sectors'

# ── FRISCHE-SCHRANKE (Birgit Münch, 2026-07-30) ─────────────────────────────
# Der Fanout wählt seine Empfänger ALLEIN danach, ob ein Flug in den
# gespeicherten Sektoren steht. Wie alt diese Zeile ist, spielte keine Rolle —
# und genau daran hing Birgits Beschwerde: Sie bekam einen Flug aus der Reserve,
# ihr Umlauf wurde gestrichen, ihr Handy-Kalender war korrekt — aber unsere
# Server-Kopie stand seit vier Tagen still. Also lief die Meldungs-Kette weiter
# für Flüge, aus denen sie längst rausgenommen war.
#
# Die Aussage des Pushes ist „du bist auf diesem Flug". Der einzige Beleg dafür
# IST diese Zeile. Hat sie seit Tagen niemand bestätigt, ist der Beleg wertlos —
# dann lieber schweigen als etwas Falsches behaupten (Owner-Regel: keine
# erfundenen Werte).
#
# Gemessen am 2026-07-30 über die Roster-Tage der nächsten drei Tage:
#   · LH-FlightOps-User:  max 9,2 h alt  → 0 Zeilen betroffen
#   · Kalender-Link-User: 17 % älter als 72 h
#   · Nur-Gerät-User:     51 % älter als 72 h (Server kann sie NICHT nachladen —
#     der Hintergrund-Task liest bewusst kein EventKit, weil iOS dabei
#     systemweite Passwortdialoge auslösen kann)
#
# ── KORREKTUR NOCH AM SELBEN TAG (Owner): ALTER ALLEIN IST KEIN BELEG ───────
# Erste Fassung schaltete jede Zeile >72 h stumm. Live gemessen waren das
# **650 von 5005** bzw. **797 von 5007** Empfängern pro Fanout-Ereignis —
# 13–16 %. Owner dazu: „älter als 3 Tage ist oft Ruhezeiten, gehen 5 Tage,
# Urlaub etc. … es muss eher einen Weg geben, diesen zu aktualisieren statt
# zu stummen."
#
# Er hat recht, und die Logik war schief: Ein Plan, den seit fünf Tagen
# niemand angefasst hat, ist meistens nicht FALSCH — er ist UNVERÄNDERT, weil
# der Mensch frei hatte. Wer danach fliegt, verlöre seine Verspätungsmeldung
# für nichts.
#
# Der Belegwert einer alten Zeile hängt nicht am Alter, sondern daran, ob wir
# sie überhaupt hätten auffrischen KÖNNEN:
#   · LH-Grant vorhanden        → der Refresher holt den Plan alle paar Stunden;
#                                 ist die Zeile trotzdem alt, hat LH sie eben
#                                 nicht geändert. Beleg gültig.
#   · Kalender-Link vorhanden   → wird serverseitig bzw. vom Gerät nachgeladen.
#                                 Beleg gültig.
#   · WEDER NOCH                → der Server kann diesen Plan gar nicht
#                                 nachladen (der Hintergrund-Task liest bewusst
#                                 kein EventKit — iOS kann dabei systemweite
#                                 Passwortdialoge auslösen). Hier, und NUR
#                                 hier, heißt alt tatsächlich „wir wissen es
#                                 nicht". Genau Birgits Lage: kein Link, keine
#                                 LH-Verbindung, Kopie vier Tage alt.
# Deshalb greift die Schranke jetzt ausschließlich bei Nutzern OHNE
# nachladbare Quelle (gemessen 182 von 2404).
_STALE_ROW_MAX_AGE_H = 72

# Kurzes Memo für die Quellen-Abfrage: ein Fanout-Ereignis prüft dieselben
# Tokens mehrfach, und Ereignisse kommen in Wellen. TTL bewusst klein — wer
# sich neu verbindet, soll nicht minutenlang als quellenlos gelten.
_SRC_MEMO_TTL_S = 300
_src_memo_lock = threading.Lock()
_src_memo = {}


def _tokens_without_refreshable_source(tokens):
    """Teilmenge der Tokens OHNE nachladbare Roster-Quelle (kein lebender
    LH-Grant, kein Kalender-Link). EIN gebündelter Read je Block, Ergebnis
    kurz gememot. Wirft nie — im Fehlerfall die LEERE Menge, damit die
    Schranke fail-open bleibt und niemand fälschlich verstummt."""
    want = {t for t in (tokens or []) if t}
    if not want:
        return set()
    out, ask = set(), []
    now = time.time()
    with _src_memo_lock:
        for t in want:
            hit = _src_memo.get(t)
            if hit and (now - hit[0]) < _SRC_MEMO_TTL_S:
                if hit[1]:
                    out.add(t)
            else:
                ask.append(t)
    if not ask:
        return out
    client = _sb()
    if client is None:
        return set()                      # fail-open
    try:
        for i in range(0, len(ask), 60):
            chunk = ask[i:i + 60]
            r = (client.table('user_profiles')
                 .select('token,metadata')
                 .in_('token', chunk).execute())
            seen = {}
            for row in (r.data or []):
                if not isinstance(row, dict):
                    continue
                md = row.get('metadata') or {}
                fo = md.get('flightops_tokens') or {}
                cf = md.get('calendar_feed') or {}
                has_lh = bool(fo.get('access')) and not fo.get('needs_relogin')
                has_url = str(cf.get('url') or '').startswith(
                    ('http://', 'https://', 'webcal://', 'webcals://'))
                seen[row.get('token')] = not (has_lh or has_url)
            with _src_memo_lock:
                for t in chunk:
                    # Kein Profil gefunden ⇒ NICHT als quellenlos werten
                    # (fail-open); nur ein belegtes „weder noch" zählt.
                    val = bool(seen.get(t, False))
                    _src_memo[t] = (now, val)
                    if val:
                        out.add(t)
        if len(_src_memo) > 8000:
            with _src_memo_lock:
                _src_memo.clear()
        return out
    except Exception as e:
        log.warning('[lh_mqtt] source lookup fail: %s', type(e).__name__)
        return set()                      # fail-open


def _drop_stale_rows(rows):
    """Zeilen aussortieren, die seit >72 h niemand bestätigt hat UND deren
    Nutzer gar keine nachladbare Quelle hat. Fehlt `updated_at`, ist es
    unparsebar oder ist die Quellen-Abfrage gestört, bleibt die Zeile DRIN
    (fail-open — lieber ein Push zu viel als eine echte Änderung
    verschluckt). Wirft nie."""
    try:
        from datetime import datetime as _dt, timezone as _tz
        now = _dt.now(_tz.utc)

        def _too_old(row):
            ts = (row or {}).get('updated_at')
            if not ts:
                return False               # kein Stempel → fail-open
            try:
                when = _dt.fromisoformat(str(ts).replace('Z', '+00:00'))
                if when.tzinfo is None:
                    when = when.replace(tzinfo=_tz.utc)
                return ((now - when).total_seconds() / 3600.0
                        > _STALE_ROW_MAX_AGE_H)
            except Exception:
                return False               # unparsebar → fail-open

        rows = rows or []
        old = [r for r in rows if _too_old(r)]
        if not old:
            return rows
        # ERST JETZT die Quellen-Frage stellen — nur für die alten Zeilen, und
        # gebündelt. Im Normalfall (alles frisch) kostet die Schranke keinen
        # einzigen zusätzlichen Read.
        blind = _tokens_without_refreshable_source({r.get('token') for r in old})
        if not blind:
            return rows
        out = [r for r in rows
               if not (_too_old(r) and r.get('token') in blind)]
        dropped = len(rows) - len(out)
        if dropped:
            log.info('[lh_mqtt] stale rows dropped: %d von %d '
                     '(alt: %d, davon ohne nachladbare Quelle: %d)',
                     dropped, len(rows), len(old), dropped)
        return out
    except Exception:
        return rows or []


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
    return _drop_stale_rows(out)


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
        return _sector_rows(dates)         # filtert selbst
    return _drop_stale_rows(out)


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
        return _drop_stale_rows(r.data or [])
    except Exception as e:
        log.warning('[lh_mqtt] station rows cs fail %s: %s', station,
                    type(e).__name__)
        return _sector_rows(dates)         # filtert selbst


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


def sector_sub_end(sector, dep):
    """Ende des Abo-Fensters EINES Sektors (aware UTC). Pure.

    Reihenfolge der Quellen: echte `arr_iso` des Sektors → `est_arr`, falls die
    Zeile eine geschätzte Ankunft trägt → konservative Blockannahme. Eine
    unbrauchbare Ankunft (fehlt, nicht parsebar, nicht NACH dem Abflug, oder
    absurd weit weg) fällt auf die Annahme zurück; sie darf das Fenster NIE
    verkürzen.

    Der Boden `dep + _SUB_PAST_H` garantiert, dass das neue Fenster jedes alte
    enthält — diese Änderung kann kein Abo verlieren, nur welche dazugewinnen.
    """
    arr = None
    for key in ('arr_iso', 'est_arr', 'arr_est'):
        arr = _parse_iso_utc(sector.get(key))
        if arr is not None:
            break
    if arr is None or arr <= dep:
        arr = dep + timedelta(hours=_SUB_BLOCK_FALLBACK_H)
    elif (arr - dep) > timedelta(hours=_SUB_BLOCK_MAX_H):
        arr = dep + timedelta(hours=_SUB_BLOCK_MAX_H)
    return max(arr + timedelta(hours=_SUB_ARR_GRACE_H),
               dep + timedelta(hours=_SUB_PAST_H))


def _sector_block_min(sector):
    """Blockzeit eines Sektors in Minuten (int) oder None. Pure, wirft nie.
    Nur aus ECHTEN Zeiten des Sektors — es wird nichts geschätzt."""
    if not isinstance(sector, dict):
        return None
    dep = _parse_iso_utc(sector.get('dep_iso'))
    arr = _parse_iso_utc(sector.get('arr_iso'))
    if dep is None or arr is None or arr <= dep:
        return None
    return int((arr - dep).total_seconds() // 60)


def topics_for_rows(rows, now_utc):
    """Pure: Briefing-Rows → sortierte Topic-Liste (dedupliziert über User —
    ein Discover-Flug mit 8 AeroX-Crews = EIN Topic).

    Das Fenster endet an der ANKUNFT (+1 h), nicht am Abflug — s. den Block bei
    `_SUB_ARR_GRACE_H`. Vorne begrenzt weiterhin der Abflug (+48 h Vorlauf)."""
    topics = set()
    hi = now_utc + timedelta(hours=_SUB_FUTURE_H)
    for _tok, s in _iter_sectors(rows):
        nf = _norm_flight(s.get('flight'))
        if not nf or not is_lh_group(nf[0] + nf[1]):
            continue
        dep = _parse_iso_utc(s.get('dep_iso'))
        if dep is None or dep > hi:
            continue
        if sector_sub_end(s, dep) < now_utc:
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


# ── Reg-Cache: Prozess-Memo → Supabase → LH ─────────────────────────────────
# Reg-Memo entkoppelt vom 120s-Facts-Memo: Maschinen-Zuteilung ändert sich
# intraday kaum — 3h TTL hält den LH-Budget-Verbrauch der Topics-Rechnung klein.
#
# WARUM DAS MEMO ALLEIN NICHT REICHTE (Owner 2026-07-27, drei Stunden über
# 1.000/h; `mqtt_leg_reg` war mit 73 % der mit Abstand größte Verbraucher):
# ein `dict` im Prozess wird gleich ZWEIMAL entwertet —
#   1. FRAGMENTIERUNG: der Daemon pollt /topics alle 300 s, gunicorn verteilt
#      round-robin auf 3 Worker. Jeder pflegt sein eigenes Memo, derselbe Flug
#      wird also dreifach bei LH gekauft.
#   2. RECYCLING (der eigentliche Verstärker): bei ~24.600 req/h und
#      `--max-requests 5000` recycelt jeder Worker etwa alle 37 min — gemessen
#      8 Boots in 82 min. Jeder Neustart löscht das Memo, der nächste
#      Topics-Poll kauft ALLE ~342 Legs erneut. Rechnung: 5,9 Recycles/h ×
#      342 = ~2.000 Calls/h gewollt; genau das zeigte `lhopen_denied`.
# MESSUNG, die die naheliegende Alternative ausschließt: LH liefert die Reg
# auch 12–16 h vor Abflug (Stichprobe 32/32) — das Fenster zu kürzen wäre also
# reiner Feature-Verlust am Inbound-Watch, keine Ersparnis. Der Bedarf ist
# nicht zu hoch, der Cache war zu flüchtig.
#
# Persistenz-Ebene ist `ax_paid_call_cache` (provider='lhopen'): call_key-PK,
# result jsonb, result_until/negative_until — exakt die gebrauchte Semantik,
# inkl. opportunistischem Prune. KEINE neue Tabelle, kein Migrations-Schritt.
# Der Name der Tabelle sagt „paid"; sie ist faktisch der generische
# API-Call-Cache (Spalte `provider` ist genau dafür da).
_reg_lock = threading.Lock()
_reg_memo = {}
_REG_TTL_S = 3 * 3600          # Rückfall, wenn die Abflugzeit unbekannt ist
# Transportfehler/Throttle-Abweisung sind KEIN „hat keine Reg" — nur kurz
# zurückhalten, damit ein LH-503 nicht 30 min als Fakt gilt (in der Stichprobe
# vom 27.07. waren ALLE Fehlschläge 503er).
# 600 s statt 120 s (Messung 27.07.): der Daemon gleicht die Topic-Liste alle
# 300 s ab (LH_MQTT_REFRESH_S). Eine Sperre UNTER dem Poll-Intervall heißt, dass
# jeder Poll alle ~320 Legs erneut versucht — bei geschlossenem Gate ergab das
# 3.901 abgewiesene Versuche/h, ohne dass je eine Reg dabei herauskam.
_REG_UNKNOWN_TTL_S = 600

# ── R8 (Audit 2026-07-31): die WIEDERHOLUNG war teurer als die Events ───────
#
# Gemessen: 3.789 `mqtt_leg_reg`-Calls/Tag — das DREIFACHE dessen, was die
# MQTT-Events selbst kosten. Ursache war eine FLACHE Negativ-TTL: 1800 s über
# ein 17-h-Beobachtungsfenster (Abflug −16 h … +1 h) sind bis zu 34
# Wiederholungen PRO LEG, und zwar für Legs, bei denen LH gerade „ich habe
# keine Maschine für diesen Flug" gesagt hat.
#
# Warum das so verschwenderisch ist: die Reg ist keine flüchtige Größe. Die
# Messung vom 27.07. (Stichprobe 32/32) zeigt, dass LH sie meist schon 12–16 h
# vor Abflug liefert — der Positiv-Pfad ist also längst gestaffelt (`_reg_ttl`).
# Bleibt sie aus, taucht sie erfahrungsgemäß erst zum BOARDING-Fenster auf, wenn
# die Maschine am Board erscheint. Alles dazwischen ist reines Nachfragen ins
# Leere.
#
# Neue Staffelung — vor dem Boarding LANG, im Boarding-Fenster KURZ:
#   · Abflug > 3 h weg  → genau bis zum Boarding-Fenster schlafen (max 14 h)
#   · Abflug ≤ 3 h weg  → alle 2,5 h nachsehen (hier erscheint die Reg wirklich)
#   · nach dem Abflug   → 3 h; das Fenster endet ohnehin bei Abflug +1 h
# Worst case über die vollen 17 h: Versuch beim Eintritt (T−16 h), bei T−3 h und
# bei T−0,5 h — also DREI statt bis zu 34 (`test_hoechstens_drei_reg_versuche_
# pro_leg_und_tag` nagelt das fest). Die 2,5 h sind mit Absicht nicht kürzer:
# sie legen den LETZTEN Versuch auf eine halbe Stunde vor Abflug — näher am
# „Reg erscheint am Board"-Moment als jede der 34 alten Wiederholungen, und
# gleichzeitig so, dass kein vierter Versuch mehr ins Fenster fällt.
_REG_NEG_BOARDING_LEAD_S = 3 * 3600
_REG_NEG_BOARDING_TTL_S = 150 * 60
_REG_NEG_FAR_MAX_S = 14 * 3600
_REG_NEG_FAR_MIN_S = 1800
_REG_NEG_AFTER_DEP_S = 3 * 3600
# Ohne bekannte Abflugzeit lässt sich nicht staffeln. 2 h statt der alten
# 1800 s: auch ungestaffelt war die halbe Stunde für eine Größe, die sich über
# Stunden nicht ändert, viel zu kurz.
_REG_NEG_TTL_S = 2 * 3600
# Gleiche Logik für die „wir wissen es nicht"-Sperre (LH-Fehler/Gate zu): nahe
# am Abflug muss sich das schnell erholen, weit weg darf es ruhen. Der Nah-Wert
# bleibt bei 600 s — er MUSS über dem 300-s-Topic-Poll liegen.
_REG_UNKNOWN_FAR_TTL_S = 3600


def _reg_neg_ttl(dep_utc, now_ts):
    """TTL eines BELEGTEN „hat keine Reg" (Sekunden). Pure. `dep_utc`
    unbekannt → flacher Rückfall."""
    if dep_utc is None:
        return _REG_NEG_TTL_S
    try:
        lead = dep_utc.timestamp() - now_ts
    except Exception:
        return _REG_NEG_TTL_S
    if lead <= 0:
        return _REG_NEG_AFTER_DEP_S
    if lead > _REG_NEG_BOARDING_LEAD_S:
        return int(max(_REG_NEG_FAR_MIN_S,
                       min(_REG_NEG_FAR_MAX_S,
                           lead - _REG_NEG_BOARDING_LEAD_S)))
    return _REG_NEG_BOARDING_TTL_S


def _reg_unknown_ttl(dep_utc, now_ts):
    """TTL einer LÜCKE (LH-Fehler, eigener Throttle). Pure. Nahe am Abflug
    kurz (schnelle Erholung), weit weg lang (nichts zu gewinnen)."""
    if dep_utc is None:
        return _REG_UNKNOWN_TTL_S
    try:
        lead = dep_utc.timestamp() - now_ts
    except Exception:
        return _REG_UNKNOWN_TTL_S
    return (_REG_UNKNOWN_FAR_TTL_S if lead > _REG_NEG_BOARDING_LEAD_S
            else _REG_UNKNOWN_TTL_S)
_REG_CACHE_PROVIDER = 'lhopen'
_REG_KEY_CHUNK = 80            # PostgREST-URL-Länge: in_() nicht überdehnen

# ── Wie lange eine BEKANNTE Reg gilt (Owner 2026-07-27) ─────────────────────
# Die Maschinenzuteilung eines konkreten Flugs an einem konkreten Datum ist
# faktisch unveränderlich; nur ein Tailswap kippt sie, und der ist in der Regel
# bis ~90 min vor Abflug entschieden. Die alte FLACHE 3-h-TTL bezahlte das mit
# ~5,7 Käufen pro Leg über seine 17 h im Beobachtungsfenster (Abflug −1 h …
# +16 h) — bei ~320 Legs also ~107 Calls/h reine Wiederholung.
# Neue Politik: EINMAL früh holen, die TTL so setzen, dass sie exakt zum
# Gegencheck ~90 min vor Abflug abläuft, und danach als final behandeln.
# Ergebnis: 2 Käufe pro Leg statt 5,7 — und der letzte davon liegt NÄHER am
# Abflug als bisher, die Aussage wird also nicht schlechter, sondern besser.
_REG_RECHECK_LEAD_S = 90 * 60
_REG_FINAL_TTL_S = 12 * 3600


def _reg_ttl(dep_utc, now_ts):
    """TTL einer bekannten Reg (Sekunden). Pure. `dep_utc` unbekannt → die
    alte flache TTL (kein Verhaltenswechsel für Aufrufer ohne Abflugzeit)."""
    if dep_utc is None:
        return _REG_TTL_S
    try:
        lead = dep_utc.timestamp() - now_ts
    except Exception:
        return _REG_TTL_S
    if lead > _REG_RECHECK_LEAD_S:
        # Läuft genau dann ab, wenn der Gegencheck fällig ist (min. 5 min, damit
        # ein Leg knapp über der Schwelle keine Mikro-TTL bekommt).
        return int(max(300, min(_REG_FINAL_TTL_S, lead - _REG_RECHECK_LEAD_S)))
    return _REG_FINAL_TTL_S


def _reg_cache_key(flight_disp, date, dep, arr):
    return f'lhreg:{flight_disp}:{date}:{dep or ""}:{arr or ""}'


def _reg_memo_get(key, now):
    with _reg_lock:
        hit = _reg_memo.get(key)
        if hit and now < hit[0]:
            return True, hit[1]
    return False, None


def _reg_memo_put(key, reg, ttl, now):
    with _reg_lock:
        _reg_memo[key] = (now + ttl, reg)
        if len(_reg_memo) > 3000:
            items = sorted(_reg_memo.items(), key=lambda kv: kv[1][0])
            for k, _v in items[:len(items) // 4 or 1]:
                _reg_memo.pop(k, None)


def _reg_cache_read(cache_keys):
    """Batch-Read aus dem geteilten Cache → {call_key: reg-or-None} für die
    Einträge, die noch gültig sind. Ein Query je 80 Keys statt eines RPCs pro
    Key. Wirft nie — bei SB-Problemen einfach leer (dann greift LH)."""
    client = _sb()
    if client is None or not cache_keys:
        return {}
    keys = sorted(set(cache_keys))
    out = {}
    now = datetime.now(timezone.utc)
    for i in range(0, len(keys), _REG_KEY_CHUNK):
        chunk = keys[i:i + _REG_KEY_CHUNK]
        try:
            r = (client.table('ax_paid_call_cache')
                 .select('call_key,result,result_until,negative_until')
                 .in_('call_key', chunk).execute())
        except Exception as e:
            log.warning('[lh_mqtt] reg-cache read fail: %s', type(e).__name__)
            continue
        for row in (r.data or []):
            # Zeitstempel PARSEN, nicht als String vergleichen: lexikografisch
            # stimmt nur, solange PostgREST exakt dieses Format liefert — ein
            # 'Z' statt '+00:00' würde den Cache still komplett entwerten.
            if _ts_after(row.get('result_until'), now):
                out[row.get('call_key')] = (row.get('result') or {}).get('reg')
            elif _ts_after(row.get('negative_until'), now):
                out[row.get('call_key')] = None
    return out


def _ts_after(val, now):
    """True, wenn der ISO-Zeitstempel `val` noch in der Zukunft liegt.
    Unbekanntes/kaputtes Format → False (Cache-Miss, nie ein falscher Hit)."""
    if not val:
        return False
    try:
        d = datetime.fromisoformat(str(val).replace('Z', '+00:00'))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d > now
    except Exception:
        return False


def _reg_cache_write(items):
    """items = [(call_key, reg_or_None, ttl_s)] → Upsert in den geteilten Cache.
    Wirft nie. Ein negativer Eintrag (reg=None) wird bewusst MITgeschrieben:
    „dieser Flug hat bei LH keine Reg" ist eine Antwort und spart die nächsten
    Aufrufe. Nicht geschrieben werden Lücken (LH kaputt) — die kommen hier
    gar nicht erst an.

    Die TTL kommt vom Aufrufer (`_reg_ttl` bzw. `_reg_neg_ttl`, beide abflugnah
    gestaffelt) und muss dieselbe sein wie im Prozess-Memo — sonst wäre der
    geteilte Cache je nach Prozess mal kürzer, mal länger gültig als der lokale.
    Das galt bis 2026-07-31 NUR für positive Einträge: `negative_until` rechnete
    mit einer eigenen Konstanten und ignorierte die übergebene TTL. Damit lief
    die neue Negativ-Staffelung (R8) am geteilten Cache vorbei — der lokale
    Prozess hätte 14 h geschwiegen, der geteilte Cache nach 30 min wieder
    freigegeben."""
    client = _sb()
    if client is None or not items:
        return
    now = datetime.now(timezone.utc)
    rows = []
    for key, reg, ttl in items:
        pos = reg is not None
        rows.append({
            'call_key': key,
            'provider': _REG_CACHE_PROVIDER,
            'result': {'reg': reg} if pos else None,
            'result_until': (now + timedelta(seconds=int(ttl))).isoformat()
                            if pos else None,
            'negative_reason': None if pos else 'no_reg',
            'negative_until': None if pos else
                              (now + timedelta(seconds=int(ttl))).isoformat(),
            'updated_at': now.isoformat(),
        })
    try:
        client.table('ax_paid_call_cache').upsert(
            rows, on_conflict='call_key').execute()
    except Exception as e:
        log.warning('[lh_mqtt] reg-cache write fail: %s', type(e).__name__)


def _fetch_leg_reg(flight_disp, date, dep, arr):
    """EIN LH-Lookup. Returns (reg, answered) — `answered=False` heißt „wir
    wissen es nicht" (Throttle-Abweisung oder LH-Fehler) und darf NICHT als
    Negativ-Ergebnis gecacht werden."""
    facts = lh_flight_facts(flight_disp, date, dep, arr,
                            caller='mqtt_leg_reg') or {}
    reg = facts.get('reg')
    if reg:
        return reg, True
    try:
        from blueprints.lh_open_api import last_call_answered
        return None, bool(last_call_answered())
    except Exception:
        return None, False


def _cached_leg_reg(flight_disp, date, dep, arr, dep_utc=None):
    """Reg EINES Legs über alle drei Ebenen. Für Einzelabfragen
    (`_push_inbound`); die Topics-Rechnung nutzt `_legs_regs` (Batch)."""
    leg = (flight_disp, date, dep, arr)
    return _legs_regs([leg],
                      dep_times={leg: dep_utc} if dep_utc else None).get(leg)


def _legs_regs(legs, dep_times=None, deadline=None):
    """Regs für viele Legs: Prozess-Memo → geteilter Cache (EIN Batch-Read) →
    LH nur für den Rest. Returns {(flight,date,dep,arr): reg-or-None}.

    `deadline` (monotone time.time()-Marke, optional): ab hier keine weiteren
    LH-Calls mehr. Jeder Call ist ein blockierender HTTP-Request mit 10 s
    Timeout, und die Schleife ist sequenziell — ohne Bremse konnte ein kalter
    Aufbau beliebig lang laufen (gemessen 37 s, während der MQTT-Daemon nach
    30 s längst aufgegeben hatte). Übriggebliebene Legs gelten als „Lücke,
    kein Fakt" — dieselbe kurze, NICHT geteilte TTL wie bei einer
    unbeantworteten Anfrage, damit der nächste Lauf sie sofort neu versucht.

    Der Batch ist der Punkt: vorher stand pro Leg ein potenzieller LH-Call, und
    ein frisch recycelter Worker feuerte alle auf einmal.

    `dep_times` = {leg: aware datetime} — optional, steuert die TTL einer
    gefundenen Reg (s. `_reg_ttl`). Fehlt der Eintrag, gilt die alte flache
    3-h-TTL."""
    now = time.time()
    dep_times = dep_times or {}
    out = {}
    todo = []
    # DEDUPE ist Pflicht, nicht Kosmetik: ein Flug mit 8 AeroX-Crews steht 8×
    # in `rows`. Ohne das hier wäre jeder Doppelte ein eigener LH-Call, weil
    # das Memo erst NACH dem Sammeln gefüllt wird.
    for leg in dict.fromkeys(legs):
        key = _reg_cache_key(*leg)
        found, val = _reg_memo_get(key, now)
        if found:
            out[leg] = val
        else:
            todo.append((leg, key))
    if not todo:
        return out

    shared = _reg_cache_read([k for _leg, k in todo])
    misses = []
    for leg, key in todo:
        if key in shared:
            reg = shared[key]
            out[leg] = reg
            _reg_memo_put(key, reg,
                          _reg_ttl(dep_times.get(leg), now) if reg
                          else _reg_neg_ttl(dep_times.get(leg), now), now)
        else:
            misses.append((leg, key))

    fresh = []
    gate_shut = False
    budget_hit = 0
    for leg, key in misses:
        if not gate_shut and deadline and time.time() >= deadline:
            gate_shut = True
        if gate_shut and deadline:
            budget_hit += 1     # wie viele Legs das Budget wirklich gekostet hat
        if gate_shut:
            # GATE ZU: der eigene Throttle hat schon abgewiesen, das gilt für
            # jeden weiteren Call dieser Stunde. Die restlichen Legs gar nicht
            # erst versuchen — ohne diesen Abbruch feuerte JEDER Topic-Poll
            # alle ~320 Legs gegen dieselbe Wand (gemessen 3.901 abgewiesene
            # Versuche/h, kein einziger davon konnte je eine Reg liefern).
            out[leg] = None
            _reg_memo_put(key, None, _reg_unknown_ttl(dep_times.get(leg), now),
                          now)
            continue
        reg, answered = _fetch_leg_reg(*leg)
        out[leg] = reg
        if not answered:
            # Lücke, kein Fakt: nur kurz zurückhalten, NICHT teilen.
            _reg_memo_put(key, None, _reg_unknown_ttl(dep_times.get(leg), now),
                          now)
            if _leg_reg_gate_shut():
                gate_shut = True
            continue
        ttl = (_reg_ttl(dep_times.get(leg), now) if reg
               else _reg_neg_ttl(dep_times.get(leg), now))
        _reg_memo_put(key, reg, ttl, now)
        fresh.append((key, reg, ttl))
    if fresh:
        _reg_cache_write(fresh)
    if budget_hit:
        log.info('[lh_mqtt] leg-reg Zeitbudget erreicht — %d von %d Legs auf '
                 'den naechsten Lauf vertagt', budget_hit, len(misses))
    return out


def _leg_reg_gate_shut():
    """True, wenn der letzte LH-Versuch am EIGENEN Throttle scheiterte (nicht
    an LH). Eigene Funktion, damit Tests sie ersetzen können. Wirft nie."""
    try:
        from blueprints.lh_open_api import last_call_denied
        return bool(last_call_denied())
    except Exception:
        return False


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


def inbound_topics_for_rows(rows, now_utc, deadline=None):
    """Topics der Maschinen-Zubringer: pro Leg (Abflug −1h…+16h) die LH-
    autoritative Reg holen, am Abflug-Airport die letzte ARR-Board-Ankunft
    dieser Reg finden → deren Flug subscriben (Topic-Datum = Board-Service-
    Datum, plus Vortag für Langstrecken-Zubringer, die lokal am Vortag
    starteten)."""
    lo = now_utc - timedelta(hours=1)
    hi = now_utc + timedelta(hours=_INBOUND_DEP_WINDOW_H)
    # ERST alle Kandidaten sammeln, DANN die Regs in EINEM Rutsch auflösen —
    # per-Leg-Auflösung in dieser Schleife war der Pfad, über den ein frisch
    # recycelter Worker ~342 LH-Calls am Stück abfeuerte (s. _legs_regs).
    cands = []                       # (frm, dep, roster_tail_or_None, leg_key)
    need = []
    dep_times = {}                   # leg_key -> Abflug (steuert die Reg-TTL)
    for _tok, s in _iter_sectors(rows):
        nf = _norm_flight(s.get('flight'))
        if not nf or not is_lh_group(nf[0] + nf[1]):
            continue
        dep = _parse_iso_utc(s.get('dep_iso'))
        frm = (s.get('from') or '').strip().upper()
        if dep is None or len(frm) != 3 or not (lo <= dep <= hi):
            continue
        tail = _sector_tail(s)
        leg_key = None
        if not tail:
            leg_key = (nf[0] + nf[1], dep.date().isoformat(), frm,
                       (s.get('to') or '').strip().upper() or None)
            need.append(leg_key)
            # Frühester bekannter Abflug gewinnt: derselbe Leg-Key kann aus
            # mehreren Roster-Zeilen kommen, und eine zu SPÄTE Annahme würde
            # die Reg über den Gegencheck hinaus festhalten.
            prev = dep_times.get(leg_key)
            if prev is None or dep < prev:
                dep_times[leg_key] = dep
        cands.append((frm, dep, tail, leg_key))
    regs = (_legs_regs(need, dep_times=dep_times, deadline=deadline)
            if need else {})
    legs = []
    for frm, dep, tail, leg_key in cands:
        reg = tail or (regs.get(leg_key) if leg_key else None)
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


def checkin_topics_for(now_utc):
    """Topics der eingecheckten Flüge (Test-Seam um
    `flight_checkins.checkin_topics`). Leere Menge, wenn das Modul fehlt."""
    from blueprints.flight_checkins import checkin_topics
    return checkin_topics(now_utc)


def _topics_snapshot():
    """(ts, topics) des letzten Schnappschusses. ts=0.0 ⇒ noch keiner da."""
    with _topics_lock:
        return _topics_memo['ts'], list(_topics_memo['topics'])


def _topics_compute(budget_s=None):
    """Die eigentliche Rechnung. Returns (topics, vollstaendig).

    `budget_s` begrenzt den LH-Call-Anteil, damit ein kalter Aufbau nicht in
    den 30-s-Timeout des Daemons läuft. Das Budget wird VOR dem ersten teuren
    Schritt gesetzt, nicht erst vor dem Zubringer-Teil — der paginierte
    Roster-Fetch gehört mit unter die Schranke.

    `vollstaendig=False` heisst: es fehlen Zubringer-Topics, weil das Budget
    griff oder der Roster-Fetch scheiterte. Der Aufrufer darf so ein Ergebnis
    NICHT als vollen Stand ablegen — s. _topics_build_and_store."""
    deadline = (time.time() + budget_s) if budget_s else None
    now_utc = datetime.now(timezone.utc)
    dates = [(now_utc.date() + timedelta(days=off)).isoformat()
             for off in (-1, 0, 1, 2)]
    rows = _sector_rows(dates)
    if not rows:
        # _sector_rows schluckt Fehler und liefert dann []. Ein leerer
        # Roster-Fetch ist nicht von „niemand fliegt" zu unterscheiden — und
        # „niemand fliegt" gibt es bei 200+ verbundenen Crews nicht. Als
        # unvollständig behandeln, sonst würde ein Supabase-Aussetzer eine
        # leere Topic-Liste als gültigen Stand ablegen.
        return [], False
    roster = set(topics_for_rows(rows, now_utc))
    tset = set(roster)
    # EINGECHECKTE FLÜGE (2026-07-31): wer sich auf der Crew-Bordkarte für
    # einen Flug anmeldet, verfolgt oft einen Flug, der in seinem EIGENEN
    # Roster gar nicht steht. Ohne dieses Abo käme für so einen Flug nie ein
    # Broker-Event — der Nutzer hätte eingecheckt und bekäme stillschweigend
    # nie eine Meldung. Fehlschlag ist unkritisch: dann bleibt es beim
    # Roster-Stand (der die Bordkarten-Fälle ohnehin abdeckt), es geht kein
    # bestehendes Abo verloren.
    try:
        tset |= checkin_topics_for(now_utc)
    except Exception as e:
        log.warning('[lh_mqtt] checkin topics fail: %s', type(e).__name__)
    complete = True
    inbound = set()
    try:
        inbound = inbound_topics_for_rows(rows, now_utc, deadline=deadline)
        tset |= inbound
        if deadline is not None and time.time() >= deadline:
            complete = False
    except Exception as e:
        log.warning('[lh_mqtt] inbound topics fail: %s', type(e).__name__)
        complete = False
    # ZAHL MITSCHREIBEN (2026-07-31): das Abo-Fenster hängt seit heute an der
    # Ankunft statt am Abflug und ist damit im Schnitt Stunden länger. Ein
    # beobachtetes LH-Limit gibt es nicht (40er-Chunks liefen fehlerfrei) —
    # aber eine Decke, die niemand misst, merkt man erst daran, dass Abos
    # still fehlen. Diese Zeile ist der Vorher/Nachher-Beleg.
    log.info('[lh_mqtt] topics gerechnet: %d Roster + %d Zubringer = %d '
             '(rows=%d, vollstaendig=%s)', len(roster),
             len(inbound - roster), len(tset), len(rows), complete)
    return sorted(tset), complete


def _topics_build_and_store(budget_s=None):
    """Rechnet und legt den Schnappschuss ab. Nur EINER rechnet gleichzeitig;
    wer wartet und danach einen frischen Schnappschuss vorfindet, nimmt den.

    Ein UNVOLLSTÄNDIGES Ergebnis wird mit dem bisherigen Stand vereinigt statt
    ihn zu ersetzen. Grund: der Daemon rechnet `unsub = aktuell − ziel` und
    meldet sich von allem ab, was in der Antwort fehlt (lh_mqtt_daemon.py).
    Eine gekürzte Liste als vollen Stand auszuliefern hiesse also, laufende
    Flug-Abos zu kündigen — bei QoS 0 ohne Nachlieferung ein echtes Loch in
    den Pushes. Vor dieser Änderung lief eine überlange Rechnung in den
    Timeout; der Daemon behielt dann seine alten Topics. Dieses fail-safe-
    Verhalten muss erhalten bleiben.

    Ausserdem bekommt ein unvollständiger Stand einen ALTEN Zeitstempel: er
    gilt sofort als erneuerungsbedürftig, statt _TOPICS_TTL_S lang als frisch
    durchzugehen."""
    with _topics_build_lock:
        ts, topics = _topics_snapshot()
        if ts and (time.time() - ts) < _TOPICS_TTL_S:
            return topics, True          # jemand anders war schneller
        fresh, complete = _topics_compute(budget_s=budget_s)
        if complete:
            merged = fresh
            stamp = time.time()
        else:
            merged = sorted(set(fresh) | set(topics))
            # sofort wieder erneuerungsfähig, aber nicht „nie dagewesen"
            stamp = time.time() - _TOPICS_TTL_S + _TOPICS_RETRY_AFTER_S
            log.warning('[lh_mqtt] unvollstaendiger Topic-Aufbau (%d neu, %d '
                        'behalten) — Stand wird vereinigt, nicht ersetzt',
                        len(fresh), len(merged) - len(fresh))
        with _topics_lock:
            _topics_memo['ts'] = stamp
            _topics_memo['topics'] = merged
        return merged, False


def _topics_kick_refresh():
    """Hintergrund-Erneuerung anstossen. Immer höchstens eine gleichzeitig."""
    with _topics_lock:
        if _topics_state['refreshing']:
            return
        _topics_state['refreshing'] = True

    def _work():
        try:
            _topics_build_and_store()
        except Exception as e:
            log.warning('[lh_mqtt] topics refresh fail: %s: %s',
                        type(e).__name__, str(e)[:160])
        finally:
            with _topics_lock:
                _topics_state['refreshing'] = False
            try:
                from app import _close_current_thread_supabase_client
                _close_current_thread_supabase_client()
            except Exception:
                pass

    try:
        threading.Thread(target=_work, daemon=True,
                         name='lhmqtt-topics-refresh').start()
    except Exception as e:
        # Ohne das bliebe das Flag bei einem fehlgeschlagenen Thread-Start für
        # immer True — dieser Worker würde nie wieder erneuern und bis in alle
        # Ewigkeit denselben alten Stand ausliefern.
        with _topics_lock:
            _topics_state['refreshing'] = False
        log.warning('[lh_mqtt] topics refresh thread start fail: %s',
                    type(e).__name__)


def _kick_live_activity_sweep():
    """Den Live-Activity-Stale-Sweep (R2b) mitlaufen lassen. Wirft nie.

    WARUM HIER: der Sweep braucht einen verlässlichen Takt, und den gibt es an
    dieser Stelle schon — der MQTT-Daemon fragt diesen Endpoint alle 300 s.
    Damit kostet das zeitbasierte Ende der Live Activities KEINE neue
    Infrastruktur, keinen neuen Cron-Eintrag auf dem Host und keinen LH-Call.
    Der Sweep selbst deckelt sich (`_LA_SWEEP_MIN_GAP_S`) und läuft im
    Hintergrund — dieser Request wartet nie auf ihn."""
    try:
        from blueprints.live_activity import kick_sweep
        kick_sweep()
    except Exception as e:
        log.warning('[lh_mqtt] la sweep kick fail: %s', type(e).__name__)


def _kick_flight_checkin_sweep():
    """Den Check-in-Sweep („landet in etwa einer Stunde", Aufräumen)
    mitlaufen lassen. Wirft nie.

    GLEICHER GRUND WIE OBEN: es gibt an dieser Stelle bereits einen
    verlässlichen 300-s-Takt. Ein eigener Cron-Eintrag oder ein eigener
    Dauer-Thread pro Feature wäre Infrastruktur, die niemand überwacht. Der
    Sweep deckelt sich selbst (`_SWEEP_MIN_GAP_S`) und läuft im Hintergrund —
    dieser Request wartet nie auf ihn."""
    try:
        from blueprints.flight_checkins import kick_sweep as _fc_kick
        _fc_kick()
    except Exception as e:
        log.warning('[lh_mqtt] checkin sweep kick fail: %s', type(e).__name__)


@lh_mqtt_bp.route('/api/internal/lh-mqtt/topics', methods=['GET'])
def lh_mqtt_topics():
    if not _secret_ok():
        return jsonify({'ok': False, 'error': 'forbidden'}), 403
    _kick_live_activity_sweep()
    _kick_flight_checkin_sweep()
    ts, topics = _topics_snapshot()
    age = time.time() - ts if ts else None
    if ts and age < _TOPICS_TTL_S:
        return jsonify({'ok': True, 'topics': topics, 'count': len(topics),
                        'memo': True, 'age_s': int(age)})
    if ts and age < _TOPICS_MAX_STALE_S:
        # Schnappschuss ist alt, aber brauchbar: sofort ausliefern, im
        # Hintergrund erneuern. Der Daemon wartet nie auf die Rechnung.
        _topics_kick_refresh()
        return jsonify({'ok': True, 'topics': topics, 'count': len(topics),
                        'memo': 'stale', 'age_s': int(age)})
    # Kaltstart oder zu alt zum Ausliefern → jetzt rechnen, aber unter Budget
    # und Single-Flight (parallele Aufrufer warten auf denselben Lauf statt
    # jeder seine eigene LH-Call-Kette abzufeuern).
    topics, coalesced = _topics_build_and_store(budget_s=_TOPICS_BUILD_BUDGET_S)
    return jsonify({'ok': True, 'topics': topics, 'count': len(topics),
                    'memo': 'coalesced' if coalesced else False,
                    'age_s': int(age) if ts else None})


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


# Force-Drossel für est_arr-Fakten-Refreshes: ACARS-ETA-Updates können auf
# Langstrecke minütlich ticken; jeder Force = echte LH-Open-API-Calls (Key
# hat Stunden-Quote + 403-Penalty). Pro Flug+Datum max. 1 Force / 10 min —
# dazwischen läuft lh_flight_facts ungeforced (Memo-TTL-frisch reicht dann).
_FACTS_FORCE_MIN_GAP_S = 600
_facts_force_last = {}


def _facts_force_ok(flight_disp, topic_date, now=None):
    now = now if now is not None else time.time()
    key = f'{flight_disp}:{topic_date}'
    last = _facts_force_last.get(key, 0)
    if (now - last) < _FACTS_FORCE_MIN_GAP_S:
        return False
    # Memo klein halten (ein Tag Flugbetrieb ≈ wenige hundert Keys).
    if len(_facts_force_last) > 2000:
        _facts_force_last.clear()
    _facts_force_last[key] = now
    return True


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


def _push_inbound(kind, event_flight, topic_date, facts=None,
                  excluded_tokens=None):
    """Departed/Arrived/Est-Dep eines (subscribten) Flugs: die Crews finden,
    deren NÄCHSTES Leg am Ankunfts-Airport mit GENAU dieser Maschine geplant
    ist, und ihnen den Zubringer-Status pushen — im Layover weiß man so, ob
    der eigene Abflug pünktlich wird (est_dep = Frühwarnung noch VOR dem
    Zubringer-Start, ab 15 min). Guard gegen die Früh-Rotation derselben
    Maschine: der Event-Flug muss der BESTE (letzte) Board-Inbound vor dem
    Leg sein; ohne Board-Daten (Outstation) zählt der Maschinen-Match.

    `excluded_tokens` enthält die Crew des Event-Flugs selbst. Bei einem
    Durchlauf bleibt dieselbe Besatzung auf der Maschine und hat anschließend
    ebenfalls ein Leg ab dem Ankunftsort. Ohne diesen Ausschluss wurde sie als
    wartende Crew fehlklassifiziert und bekam nach ihrer eigenen Landung
    „Dein Flieger ist gelandet" (Tibor + Kollegin, BGO, 02.08.2026)."""
    facts = facts or lh_flight_facts(event_flight, topic_date, force=True,
                                     caller='mqtt_inbound') or {}
    if kind == 'est_dep':
        d = facts.get('dep_delay_min')
        if not isinstance(d, int) or d < 15 or not facts.get('est_dep'):
            return 0
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
    seen = set(excluded_tokens or ())
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
            (s.get('to') or '').strip().upper() or None, dep_utc=dep)
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
            key = f'lhflup:inb:{event_flight}:{topic_date}:{kind}:{tok}'
        elif kind == 'est_dep':
            est_dep = _hhmm(facts.get('est_dep'))
            dep_delay = facts.get('dep_delay_min')
            title = f'Dein Flieger verspätet sich · {user_flight}'
            body = f'{reg} ({event_flight}) startet'
            if origin:
                body += f' in {origin}'
            body += f' erst {est_dep} (+{dep_delay} min)'
            if est_arr:
                body += f' — Ankunft in {arr} ca. {est_arr}'
            body += '.'
            ptype = 'inbound_delay'
            # wert-basiert: neue Est-Zeit = neuer Push, gleiche nie doppelt
            key = (f'lhflup:inb:{event_flight}:{topic_date}:estdep:'
                   f'{est_dep}:{tok}')
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
    # FAKTEN-REFRESH auch für departed/est_arr/arrived (Owner 2026-07-28
    # „arrival time was wrong the whole time"): vorher lief der Force-Refresh
    # NUR für gate/est_dep/cancelled/diverted — beim Abflug-Event war `facts`
    # leer und der Live-Activity-Fanout fiel auf die ROSTER-SOLL-Ankunft
    # (`sector.arr_iso`) zurück; est_arr-Events wurden ganz verworfen. Damit
    # zeigte die Lockscreen-Karte den ganzen Flug die Plan-Ankunft und der
    # Rückblick nie die echte Landung. est_arr kann während eines Langstrecken-
    # flugs oft ticken → pro Flug gedrosselt forcen (LH-Open-API-Key-Schonung,
    # vgl. 403-Penalty-Kette 24.07.); departed/arrived sind Einzel-Events.
    facts_kinds = ('gate', 'est_dep', 'cancelled', 'diverted',
                   'departed', 'est_arr', 'arrived')
    if kind in facts_kinds and affected:
        s0 = affected[0][1]
        force = kind != 'est_arr' or _facts_force_ok(flight_disp, topic_date)
        facts = lh_flight_facts(flight_disp, topic_date,
                                (s0.get('from') or '').strip().upper() or None,
                                (s0.get('to') or '').strip().upper() or None,
                                force=force, caller='mqtt_event') or {}
    if kind in ('gate', 'est_dep', 'cancelled', 'diverted') and affected:
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
    # LIVE-ACTIVITY-FANOUT (P7-Verdrahtung 2026-07-27, s. Push-Notifications.md
    # „Noch nicht verdrahtet"): aktualisiert die Lockscreen-Karte der WIRKLICH
    # betroffenen Crews. `push_for_affected` gate't selbst (leeres `affected`
    # oder nicht anzeige-relevante Event-Art → 0) — hier gilt also „nur wenn
    # jemand betroffen ist". BEWUSST GETRENNT vom Inbound-Watch unten: der
    # abonniert absichtlich auch Zubringer-Maschinen, die in KEINEM Roster
    # stehen (`affected` dort per Definition leer) — dort darf ein
    # Betroffenheits-Gate nie eingebaut werden, und Live-Activities gibt es
    # dort nicht. Zähler getrennt von `pushed` (Alert-Pushes), damit die
    # Event-Statistik vergleichbar bleibt.
    la_sent = 0
    if affected:
        try:
            from blueprints.live_activity import push_for_affected
            la_sent = push_for_affected(affected, kind, flight_disp,
                                        topic_date, facts=facts or None)
        except Exception as e:
            log.warning('[lh_mqtt] live-activity fanout fail %s: %s',
                        flight_disp, type(e).__name__)
    if kind in ('departed', 'arrived'):
        # CHECK-IN-MELDUNGEN (Forum-Wunsch 2026-07-31): wer sich auf der
        # Crew-Bordkarte für genau diesen Flug angemeldet hat, bekommt jetzt
        # „abgeflogen" bzw. „gelandet". DIESES EVENT IST DER BELEG — eine
        # verstrichene Planzeit wäre keiner (die Maschine kann am Gate
        # stehen). Bewusst UNABHÄNGIG von `affected`: eingecheckt hat
        # typischerweise jemand, der selbst NICHT auf dem Flug sitzt.
        try:
            from blueprints.flight_checkins import notify_flight_event
            pushed += notify_flight_event(kind, flight_disp, topic_date,
                                          facts=facts or None)
        except Exception as e:
            log.warning('[lh_mqtt] checkin push fail %s: %s', flight_disp,
                        type(e).__name__)
    if kind in ('departed', 'arrived', 'est_dep'):
        # Inbound-Watch: diese Maschine ist der Zubringer für wen? est_dep
        # zusätzlich zur Direkt-Crew — der Zubringer eines ANDEREN Legs kann
        # sich schon VOR seinem Abflug verspäten (Layover-Frühwarnung).
        try:
            pushed += _push_inbound(
                kind, flight_disp, topic_date, facts=facts or None,
                excluded_tokens={tok for tok, _sector in affected})
        except Exception as e:
            log.warning('[lh_mqtt] inbound push fail %s: %s', flight_disp,
                        type(e).__name__)

    _record_event(topic, kind, len(affected), pushed)
    # ── R9-Teilfix (Audit 2026-07-31): Events hinterlassen SPUREN ────────────
    # Bis heute stand hier keine einzige Zeitangabe. Konsequenz laut Audit:
    #   · die LATENZ des Push-Kanals (Broker-Timestamp → unsere Verarbeitung)
    #     war schlicht unmessbar — `event_ts` schließt diese Lücke.
    #   · die LANGSTRECKEN-ABDECKUNG war unmessbar. Genau daran scheiterte die
    #     empirische Schließung des LH433-Falls (8,5 h Block, 0 Events): man
    #     konnte nicht zeigen, dass NIE ein Event für einen langen Flug kam,
    #     weil kein Log die Blockzeit trug. Mit `block_min` ist der R1-Beweis
    #     trivial — tauchen `arrived`-Events mit block_min > 240 auf, wirkt das
    #     neue Abo-Fenster.
    # `block_min` kommt aus dem betroffenen Roster-Sektor (dep_iso→arr_iso),
    # nicht aus einer Schätzung; ohne Sektor oder ohne Zeiten bleibt es leer.
    block_min = _sector_block_min(affected[0][1] if affected else None)
    log.info('[lh_mqtt] event %s kind=%s users=%d pushed=%d la=%d '
             'event_ts=%s block_min=%s', topic, kind, len(affected), pushed,
             la_sent, ev_ts or '-',
             block_min if block_min is not None else '-')
    return jsonify({'ok': True, 'kind': kind, 'users': len(affected),
                    'pushed': pushed, 'la_sent': la_sent})


@lh_mqtt_bp.route('/api/lh/mqtt/status', methods=['GET'])
def lh_mqtt_status():
    """Diagnose (kein Secret, keine PII — nur Flug-Events/Zähler). Achtung:
    zeigt die Sicht EINES Gunicorn-Workers; Events landen beim Worker, der den
    POST des Daemons zog."""
    with _stat_lock:
        return jsonify({'ok': True, 'events': _stats['events'],
                        'pushes': _stats['pushes'],
                        'last_events': list(_stats['last_events'])})
