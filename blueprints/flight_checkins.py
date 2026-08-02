"""Für einen Flug einchecken — Flug-Ereignis-Pushes (Forum-Wunsch 2026-07-31).

Der Wunsch aus dem Forum, wörtlich: „Es wäre cool wenn man für einen Flug
,eincheckt' unter Crew Bordkarte und dann über diesen Flug Push
Benachrichtigungen erhält. Z.B. ist abgeflogen, gelandet bzw landet in 1h."

DREI MELDUNGEN, MEHR NICHT (Owner: „nicht nerven" — ein Push für abgelaufene
Verbindungen wurde am selben Tag ausdrücklich abgelehnt):
  1. abgeflogen      — NUR aus einem LH-MQTT-`departed`-Event.
  2. landet in ~1 h  — NUR gegen eine ECHTE geschätzte Ankunft, ausdrücklich
                       als Schätzung beschriftet.
  3. gelandet        — aus einem LH-MQTT-`arrived`-Event, hilfsweise aus einem
                       belegten Board-Landestatus NACH belegtem Abflug.
Jede Meldung höchstens EINMAL pro Abo (`sent`-Flags an der Zeile + wertbasierter
Outbox-idempotency_key als zweite Sicherung).

DIE WICHTIGSTE REGEL — EINE VERSTRICHENE PLANZEIT IST KEIN EREIGNIS.
Die Maschine kann am Gate stehen. Genau diese Unterscheidung wurde am
2026-07-31 auch in die Live Activity eingebaut (`depConfirmed`/`arrConfirmed`),
und genau daran scheiterte am selben Tag der eigene Flug des Owners: in
`aircraft_track` lagen `on_ground=True`-Positionen, sein Roster-Sektor hatte
trotzdem `status: null` / `est_arr_iso: null`. Es gab also weder eine Abflug-
noch eine Landebestätigung — und deshalb hätte hier auch KEIN Push gefeuert.
Das ist kein Defekt dieses Moduls, sondern sein Zweck: ohne Beleg gibt es
keine Meldung. Eine falsche Meldung an fremde Leute ist schlimmer als eine
fehlende.

KEIN ZWEITER PUSH-WEG: alles läuft über `app._push_notify_async` (Push-Outbox).
KEIN ZWEITER TAKT: der Sweep hängt am 300-s-Topics-Poll des MQTT-Daemons
(`lh_mqtt._kick_flight_checkin_sweep`), genau wie der Live-Activity-Sweep —
kein neuer Cron-Eintrag, kein neuer Thread pro Feature.
KEIN NEUER EINSTELLUNGS-SCHALTER: die drei Typen hängen an der BESTEHENDEN
Kategorie `roster_change` („Dienstplan-Änderungen"), unter der schon
`flight_update`/`inbound_departure`/`inbound_arrival` laufen.

DATENSCHUTZ: ein Check-in ist ein privates Abo. Es gibt keinen Endpoint, der
fremde Check-ins ausliefert. Niemand erfährt, dass jemand seinen Flug verfolgt.

WESSEN FLUG IST DAS? (Tibor 2026-08-02)
───────────────────────────────────────
Tibor checkte sich über die Crew-Bordkarte bei JULIENS Umlauf ein und bekam
später „Landet bald · LH455" — ohne jeden Hinweis, warum ihn dieser Flug
etwas angeht. Der Push braucht also den Kontext, aus dem der Check-in
entstanden ist.

Dafür gibt es `via_name`: der Rufname des Crew-Mitglieds, über dessen
Bordkarte eingecheckt wurde. Er wird vom Client mitgeschickt (er zeigt die
Karte ja gerade an) und NICHT vom Server erraten — es gibt keine Ableitung
„wer sonst könnte auf diesem Flug sein", weil ein geratener Name schlimmer
wäre als gar keiner. Fehlt er (alte App-Builds, Selbst-Check-in), lautet der
Titel „Dein Check-in · LH455".

Das ist KEIN Widerspruch zum Datenschutz-Absatz oben: `via_name` benennt
niemanden gegenüber Dritten. Der Wert wandert ausschließlich in die Pushes
GENAU DES NUTZERS, der den Namen ohnehin auf der Bordkarte vor sich hatte,
und in dessen eigene Abo-Liste. Es gibt weiterhin keinen Endpoint, der fremde
Check-ins ausliefert, und der Beobachtete erfährt weiterhin nichts.
"""
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, request

log = logging.getLogger('aerotax')
flight_checkins_bp = Blueprint('flight_checkins_bp', __name__)

# ── Fenster & Deckel ────────────────────────────────────────────────────────
# „landet in 1 h": der Sweep läuft frühestens alle 4 min, das Fenster muss
# also breiter sein als der Takt. Untergrenze 5 min, damit die Meldung nicht
# 90 s vor dem Aufsetzen noch „in etwa einer Stunde" behauptet.
ETA_LEAD_MAX_MIN = 60
ETA_LEAD_MIN_MIN = 5
# Nach belegtem Abflug frühestens so spät einem Board-„gelandet" glauben.
# (Grober Plausibilitäts-Boden; die feine Prüfung macht `leg_status_gate`.)
LANDED_MIN_BLOCK_MIN = 20
# Ein Abo lebt bis Abflugdatum + 2 Tage; danach räumt der Sweep es weg.
PRUNE_AFTER_DAYS = 2
# Deckel pro Sweep-Lauf: ein Sweep darf nie unbegrenzt Board-Abfragen machen.
SWEEP_ROW_CAP = 200
_SWEEP_MIN_GAP_S = 240

_sweep_lock = threading.Lock()
_sweep_state = {'running': False, 'last': 0.0}


def _sb():
    """Test-Seam: Supabase-Client oder None. Lazy-Import (Blueprint bleibt
    ohne app-Import ladbar)."""
    try:
        from app import sb, SB_AVAILABLE
        return sb if (SB_AVAILABLE and sb is not None) else None
    except Exception:
        return None


def _do_push(token, title, body, data=None, idempotency_key=None):
    """Test-Seam um die echte Push-Outbox (app._push_notify_async)."""
    from app import _push_notify_async
    return _push_notify_async(token, title, body, data=data,
                              idempotency_key=idempotency_key)


def _bearer_ok(path_token):
    """Test-Seam um app._request_bearer_matches (IDOR-Gate)."""
    try:
        from app import _request_bearer_matches
        return _request_bearer_matches(path_token)
    except Exception:
        return False


def _secret_ok():
    """Interner Sweep-Endpoint: gleiche Auth wie poll-boards."""
    import os as _os
    import hmac as _hmac
    secret = _os.environ.get('ADSB_POLL_SECRET', '').strip()
    if secret:
        provided = (request.headers.get('X-Poll-Secret') or '').strip()
        return bool(provided) and _hmac.compare_digest(provided, secret)
    return (request.remote_addr or '') in ('127.0.0.1', '::1')


# ── Reine Helfer (test-bar ohne Netz) ───────────────────────────────────────

def parse_iso_utc(s):
    """ISO-String → aware UTC-datetime oder None. Naiv = als UTC gelesen."""
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace('Z', '+00:00'))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)
    except Exception:
        return None


def norm_flight_no(flight_no):
    """'lh 0455' → 'LH455' oder None. Gleiche Normalisierung wie die
    MQTT-Topics (führende Nullen fallen weg), damit Abo und Event denselben
    Schlüssel benutzen."""
    try:
        from blueprints.lh_mqtt import _norm_flight
    except Exception:
        return None
    nf = _norm_flight(flight_no)
    return f'{nf[0]}{nf[1]}' if nf else None


def topic_date_for(dep_iso, dep_iata, fallback_date):
    """LOKALES Abflugdatum am Startflughafen ('YYYY-MM-DD') — der Schlüssel,
    auf den die Broker-Topics keyen.

    Der Server rechnet das SELBST aus dem Abflug-Instant plus Airport-TZ,
    statt dem Client-Datum zu vertrauen: weicht die TZ-Tabelle des Geräts
    von `airport_tz` ab, träge das Abo einen Schlüssel, den der Event-Fanout
    nie findet — der Nutzer bekäme stillschweigend nie eine Meldung. Ohne
    brauchbaren Instant oder ohne bekannte TZ bleibt das Client-Datum.
    """
    dep = parse_iso_utc(dep_iso)
    frm = (dep_iata or '').strip().upper()
    if dep is not None and len(frm) == 3:
        try:
            from airport_tz import AIRPORT_TZ
            from zoneinfo import ZoneInfo
            tz_name = AIRPORT_TZ.get(frm, (None, None))[1]
            if tz_name:
                return dep.astimezone(ZoneInfo(tz_name)).date().isoformat()
        except Exception:
            pass
    if dep is not None:
        return dep.date().isoformat()
    try:
        return datetime.fromisoformat(str(fallback_date)[:10]).date().isoformat()
    except Exception:
        return None


def eta_one_hour_due(now_utc, est_arr_iso, departed_confirmed, already_sent):
    """Reine Entscheidung „jetzt ,landet in etwa einer Stunde' melden?".

    ALLE vier Bedingungen müssen erfüllt sein:
      · der Abflug ist BELEGT (sonst wäre die Restzeit auf eine Maschine
        gerechnet, die noch am Gate steht),
      · es gibt eine ECHTE geschätzte Ankunft — eine reine PLANZEIT reicht
        nicht (der Aufrufer gibt für sie schlicht None herein),
      · die Restzeit liegt im Fenster [5, 60] Minuten,
      · die Meldung ist noch nicht raus.
    """
    if already_sent or not departed_confirmed:
        return False
    est = parse_iso_utc(est_arr_iso)
    if est is None or now_utc is None:
        return False
    lead_min = (est - now_utc).total_seconds() / 60.0
    return ETA_LEAD_MIN_MIN <= lead_min <= ETA_LEAD_MAX_MIN


def landed_confirmed_by_board(status_bucket, now_utc, departed_at_iso):
    """Rückfall-Beleg für die Landung, wenn KEIN `arrived`-Event kam.

    Doppelt gegatet: der Abflug muss von uns belegt worden sein UND seither
    muss mindestens eine grobe Mindest-Blockzeit vergangen sein. Ein Board,
    das direkt nach dem Abflug schon „gelandet" behauptet (stale Vortags-
    Instanz derselben täglichen Flugnummer), kommt so nicht durch.
    """
    if status_bucket != 'landed':
        return False
    dep_at = parse_iso_utc(departed_at_iso)
    if dep_at is None or now_utc is None:
        return False
    return (now_utc - dep_at) >= timedelta(minutes=LANDED_MIN_BLOCK_MIN)


def clean_via_name(raw):
    """Rufname des Crew-Mitglieds, über dessen Bordkarte eingecheckt wurde —
    oder None. Streng, weil der Wert später in einen Push-Titel wandert.

    Genommen wird NUR das erste Wort (aus „Julien K." wird „Julien"): ein
    Nachname gehört nicht in einen Push, und „Julien K.s Flug" wäre kein
    deutscher Satz. Alles mit Ziffern, Steuerzeichen oder dem Trenner „·"
    fliegt raus — ein Token oder eine E-Mail hat hier nichts verloren.
    """
    s = ' '.join(str(raw or '').split())
    if not s:
        return None
    first = s.split(' ')[0].strip('.,;:!?')
    if not (2 <= len(first) <= 24):
        return None
    if any(c.isdigit() for c in first):
        return None
    if any(c in first for c in '·@/\\<>{}[]|"\''):
        return None
    if not any(c.isalpha() for c in first):
        return None
    return first


def possessive(name):
    """Deutscher Genitiv eines Rufnamens: „Julien" → „Juliens", aber
    „Lukas"/„Max"/„Fritz" → „Lukas'" (Zischlaut bekommt nur den Apostroph)."""
    if not name:
        return None
    return name + ("'" if name[-1].lower() in ('s', 'ß', 'x', 'z') else 's')


def context_title(flight_no, via_name=None):
    """Der Titel sagt zuerst, WESSEN Flug das ist (Tibor 2026-08-02).

    Mit bekanntem Rufnamen „Juliens Flug · LH455", sonst „Dein Check-in ·
    LH455" — nie ein geratener Name. Was passiert ist, steht vollständig im
    Body; die Zuordnung ist die Information, die im Titel gefehlt hat.
    """
    who = possessive(clean_via_name(via_name))
    lead = f'{who} Flug' if who else 'Dein Check-in'
    return f'{lead} · {flight_no}'


def build_message(kind, flight_no, dep_iata, arr_iata, via_name=None):
    """(title, body) einer Meldung — oder None für unbekannte Arten.

    KEINE Uhrzeit im Text. Grund: eine Uhrzeit ohne ihren Zonen-Bezug ist die
    teuerste Fehlerklasse dieses Projekts (Zeitzonenregel), und ein Push-Text
    trägt keinen Platz für „Ortszeit <Stadt>". Die drei Aussagen sind
    zonen-invariant formuliert; die genaue Zeit steht in der App.

    KEIN UNSICHERHEITS-ANHANG (Owner 2026-08-02, wörtlich: „keine bestätigte
    landung kann weg was für ein blöder hinweis"). Der Satz „Geschätzt — keine
    bestätigte Landung." ist ersatzlos gestrichen. Er war doppelt überflüssig:
    „landet voraussichtlich in etwa einer Stunde" IST bereits die Schätzung,
    und die Meldung feuert ohnehin nur gegen eine ECHTE geschätzte Ankunft
    (`eta_one_hour_due`) — eine reine Planzeit erzeugt hier gar keinen Push.
    Ein Disclaimer, der das nochmal dementiert, macht die eigene Aussage nur
    unglaubwürdig.
    """
    frm = (dep_iata or '').strip().upper()
    to = (arr_iata or '').strip().upper()
    route = f'{frm}–{to}' if len(frm) == 3 and len(to) == 3 else None
    tail = f' · {route}' if route else ''
    title = context_title(flight_no, via_name)
    if kind == 'departed':
        return (title, f'{flight_no} ist gestartet{tail}.')
    if kind == 'eta_1h':
        where = f' in {to}' if len(to) == 3 else ''
        return (title,
                f'{flight_no} landet voraussichtlich in etwa einer Stunde'
                f'{where}.')
    if kind == 'arrived':
        where = f' in {to}' if len(to) == 3 else ''
        return (title, f'{flight_no} ist{where} gelandet.')
    return None


_PUSH_TYPE = {'departed': 'flight_departed',
              'eta_1h': 'flight_eta_1h',
              'arrived': 'flight_landed'}


# ── Supabase-Zugriff ────────────────────────────────────────────────────────

_SELECT_BASE = ('id,user_token,flight_no,flight_date,dep_iata,arr_iata,'
                'dep_iso,sent')
_SELECT = _SELECT_BASE + ',via_name'

# SCHEMA-SAFE (Lehre fcm_token, 01.08.2026): läuft die Migration irgendwo noch
# nicht oder hat PostgREST sein Schema-Cache noch nicht neu geladen, scheitert
# JEDER Read mit „column ... does not exist" — und die Meldungen wären still
# komplett tot. Beim ersten Fehlschlag fällt das Modul deshalb prozessweit auf
# die alte Projektion zurück (Verhalten = Stand vor dieser Änderung: Titel ohne
# Namen). Der Flag-Flip ist bewusst einseitig; ein Neustart nach der Migration
# holt die Spalte zurück.
_via_name_available = True


def _select_cols():
    return _SELECT if _via_name_available else _SELECT_BASE


def _note_missing_via_name(exc):
    """True, wenn dieser Fehler nach der fehlenden Spalte aussieht."""
    global _via_name_available
    txt = str(exc).lower()
    if 'via_name' in txt or 'pgrst204' in txt or '42703' in txt:
        if _via_name_available:
            log.warning('[fcheck] Spalte via_name fehlt — Titel ohne Namen '
                        '(Migration 20260802_flight_checkins_via_name.sql?)')
        _via_name_available = False
        return True
    return False


def _rows_for_flight(flight_no, dates):
    """Alle Abos für diesen Flug an einem dieser (lokalen) Daten."""
    client = _sb()
    if client is None or not flight_no:
        return []

    def _read():
        return (client.table('flight_checkins').select(_select_cols())
                .eq('flight_no', flight_no)
                .in_('flight_date', list(dates))
                .limit(500).execute()).data or []

    try:
        return _read()
    except Exception as e:
        if _note_missing_via_name(e):
            try:
                return _read()
            except Exception as e2:
                e = e2
        log.warning('[fcheck] rows_for_flight fail %s: %s', flight_no,
                    type(e).__name__)
        return []


def _mark_sent(row_id, sent, kind, now_utc):
    """`sent`-Flag setzen. Bei `departed` zusätzlich den Zeitpunkt merken —
    er ist der Boden für den Landungs-Rückfall."""
    client = _sb()
    if client is None:
        return
    patch = dict(sent or {})
    patch[kind] = True
    if kind == 'departed':
        patch.setdefault('departed_at', now_utc.isoformat())
    try:
        (client.table('flight_checkins')
         .update({'sent': patch, 'updated_at': now_utc.isoformat()})
         .eq('id', row_id).execute())
    except Exception as e:
        log.warning('[fcheck] mark_sent fail id=%s: %s', row_id,
                    type(e).__name__)


def _push_for_row(row, kind, now_utc):
    """Eine Meldung für EIN Abo. Gibt True zurück, wenn wirklich gepusht
    wurde. Setzt das `sent`-Flag NUR nach erfolgreichem Enqueue — ein
    Fehlschlag darf die Meldung nicht für immer verschlucken."""
    sent = row.get('sent') or {}
    if sent.get(kind):
        return False
    built = build_message(kind, row.get('flight_no'), row.get('dep_iata'),
                          row.get('arr_iata'), via_name=row.get('via_name'))
    if not built:
        return False
    title, body = built
    tok = row.get('user_token')
    if not tok:
        return False
    key = (f"fcheck:{row.get('flight_no')}:{row.get('flight_date')}:"
           f'{kind}:{tok}')
    try:
        _do_push(tok, title, body,
                 data={'type': _PUSH_TYPE[kind],
                       'flight': row.get('flight_no'),
                       'date': str(row.get('flight_date')),
                       'kind': kind},
                 idempotency_key=key)
    except Exception as e:
        log.warning('[fcheck] push fail %s %s: %s', row.get('flight_no'),
                    kind, type(e).__name__)
        return False
    _mark_sent(row.get('id'), sent, kind, now_utc)
    return True


# ── Ereignis-Kante: LH-MQTT ─────────────────────────────────────────────────

def notify_flight_event(kind, flight_disp, topic_date, facts=None,
                        now_utc=None):
    """Aus `lh_mqtt.lh_mqtt_event` gerufen, wenn der Broker ein `departed`-
    oder `arrived`-Event für diesen Flug liefert. Das IST der Beleg — ein
    Zeitablauf wäre keiner. Gibt die Anzahl gepushter Abos zurück.

    ⚠️ Nur diese beiden Arten. `est_dep`/`gate`/`schedule` sind für die
    Check-in-Meldungen bewusst stumm: wer einen fremden Flug verfolgt, will
    Start und Landung wissen, nicht jede Gate-Nummer (Owner: „nicht nerven").
    """
    if kind not in ('departed', 'arrived'):
        return 0
    now_utc = now_utc or datetime.now(timezone.utc)
    flight_no = norm_flight_no(flight_disp)
    if not flight_no or not topic_date:
        return 0
    # ±1 Tag lesen wie der Roster-Fanout, aber NUR das exakte Topic-Datum
    # matchen: eine tägliche Flugnummer hat an jedem Tag ein eigenes Abo, und
    # ein Nachbartag-Treffer wäre eine Meldung über einen fremden Flug.
    try:
        base = datetime.fromisoformat(str(topic_date)[:10]).date()
        dates = [(base + timedelta(days=o)).isoformat() for o in (-1, 0, 1)]
    except Exception:
        dates = [str(topic_date)]
    want = str(topic_date)[:10]
    rows = [r for r in _rows_for_flight(flight_no, dates)
            if str(r.get('flight_date'))[:10] == want]
    if not rows:
        return 0
    facts = facts or {}
    pushed = 0
    for row in rows:
        # Fehlt der Route-Teil im Abo, ergänzen die Fakten ihn — aber sie
        # ERSETZEN nichts, was der Nutzer beim Einchecken schon gesehen hat.
        merged = dict(row)
        for src, dst in (('dep_iata', 'dep_iata'), ('arr_iata', 'arr_iata')):
            if not merged.get(dst) and facts.get(src):
                merged[dst] = facts.get(src)
        if _push_for_row(merged, kind, now_utc):
            pushed += 1
    if pushed:
        log.info('[fcheck] event %s %s %s: %d Meldungen', flight_no, want,
                 kind, pushed)
    return pushed


def checkin_topics(now_utc):
    """MQTT-Topics der eingecheckten Flüge (Menge). Ohne diese Zeilen bekäme
    ein Abo auf einen Flug, der in KEINEM Roster steht, nie ein Broker-Event
    — und damit stillschweigend nie eine Meldung."""
    client = _sb()
    if client is None:
        return set()
    dates = [(now_utc.date() + timedelta(days=o)).isoformat()
             for o in (-1, 0, 1, 2)]
    try:
        r = (client.table('flight_checkins')
             .select('flight_no,flight_date')
             .in_('flight_date', dates).limit(2000).execute())
        rows = r.data or []
    except Exception as e:
        log.warning('[fcheck] topics fail: %s', type(e).__name__)
        return set()
    try:
        from blueprints.lh_mqtt import _norm_flight
        from blueprints.lh_open_api import is_lh_group
    except Exception:
        return set()
    out = set()
    for row in rows:
        nf = _norm_flight(row.get('flight_no'))
        if not nf or not is_lh_group(nf[0] + nf[1]):
            continue
        d = str(row.get('flight_date'))[:10]
        if not d:
            continue
        out.add(f'prd/FlightUpdate/{nf[0]}/{nf[0]}{nf[1]}/{d}')
    return out


# ── Fakten: nur kostenlose, vorhandene Quellen ──────────────────────────────

def _facts_for(row):
    """Belegte Fakten zu einem Abo — `est_arr` (ECHTE Schätzung, nie die
    Planzeit) und der Board-Status-Bucket.

    Reihenfolge: LH-Fakten-CACHE (`cached_only=True`, kostet keinen LH-Call —
    der Key hat eine Stunden-Quote und eine 403-Strafe) → Board-Merge
    (`free_only=True`, keine bezahlten Fallbacks). Findet keine Quelle etwas,
    kommt {} zurück — und damit keine Meldung.
    """
    out = {'est_arr': None, 'bucket': None}
    flight_no = row.get('flight_no')
    date = str(row.get('flight_date'))[:10]
    dep = (row.get('dep_iata') or '').strip().upper() or None
    arr = (row.get('arr_iata') or '').strip().upper() or None
    if not flight_no or not date:
        return out
    try:
        from blueprints.lh_open_api import lh_flight_facts
        f = lh_flight_facts(flight_no, date, dep, arr, cached_only=True,
                            caller='flight_checkin') or {}
        if f.get('est_arr'):
            out['est_arr'] = f.get('est_arr')
    except Exception:
        pass
    try:
        from app import _flight_obs_merged, _flight_status_bucket
        obs = _flight_obs_merged(flight_no, date=date, dep_iata=dep,
                                 arr_iata=arr, free_only=True) or {}
        if not out['est_arr'] and obs.get('est_arr_iso'):
            out['est_arr'] = obs.get('est_arr_iso')
        out['bucket'] = _flight_status_bucket(obs.get('status'))
    except Exception:
        pass
    return out


# ── Sweep: „landet in 1 h", Landungs-Rückfall, Aufräumen ────────────────────

def sweep(now_utc=None):
    """Ein Durchlauf. Gibt einen Zähler-Dict zurück (Diagnose/Test).

    Der Sweep ist absichtlich das SCHWÄCHERE der beiden Werkzeuge: er
    ANNONCIERT nie einen Abflug. Ein Board-Status, der auf „airborne"
    springt, schaltet hier nur still die Folge-Meldungen frei — die Aussage
    „ist abgeflogen" bleibt dem MQTT-Event vorbehalten. Grund: der
    Board-Anreicherungs-Pfad ist am 2026-07-31 nachweislich löchrig
    (Roster-Sektor ohne `status`/`est_arr_iso` trotz `on_ground`-Positionen im
    `aircraft_track`); auf so einer Quelle darf keine Behauptung stehen.
    """
    now_utc = now_utc or datetime.now(timezone.utc)
    client = _sb()
    stats = {'rows': 0, 'eta': 0, 'landed': 0, 'pruned': 0}
    if client is None:
        return stats
    dates = [(now_utc.date() + timedelta(days=o)).isoformat()
             for o in (-1, 0, 1)]
    def _read():
        return (client.table('flight_checkins').select(_select_cols())
                .in_('flight_date', dates)
                .limit(SWEEP_ROW_CAP).execute()).data or []

    try:
        rows = _read()
    except Exception as e:
        rows = []
        retried = False
        if _note_missing_via_name(e):
            try:
                rows = _read()
                retried = True
            except Exception as e2:
                e = e2
        if not retried:
            log.warning('[fcheck] sweep read fail: %s', type(e).__name__)
    stats['rows'] = len(rows)
    for row in rows:
        sent = row.get('sent') or {}
        if sent.get('arrived'):
            continue
        need_eta = not sent.get('eta_1h')
        need_landed = bool(sent.get('departed'))
        if not need_eta and not need_landed:
            continue
        facts = _facts_for(row)
        departed = bool(sent.get('departed')) or facts.get('bucket') in (
            'airborne', 'landed')
        if departed and not sent.get('departed'):
            # STILL freischalten, nicht annoncieren (s. Docstring).
            _mark_sent(row.get('id'), sent, 'departed', now_utc)
            sent = dict(sent)
            sent['departed'] = True
            sent['departed_at'] = now_utc.isoformat()
            row['sent'] = sent
        if eta_one_hour_due(now_utc, facts.get('est_arr'), departed,
                            sent.get('eta_1h')):
            if _push_for_row(row, 'eta_1h', now_utc):
                stats['eta'] += 1
                sent = dict(sent)
                sent['eta_1h'] = True
                row['sent'] = sent
        if landed_confirmed_by_board(facts.get('bucket'), now_utc,
                                     sent.get('departed_at')):
            if _push_for_row(row, 'arrived', now_utc):
                stats['landed'] += 1
    # Aufräumen: ein Abo überlebt seinen Flugtag um zwei Tage, dann ist es
    # Datenmüll (und ein Abo, das nie endet, wäre eine stille Dauer-Beobachtung).
    cutoff = (now_utc.date() - timedelta(days=PRUNE_AFTER_DAYS)).isoformat()
    try:
        d = (client.table('flight_checkins').delete()
             .lt('flight_date', cutoff).execute())
        stats['pruned'] = len(d.data or [])
    except Exception as e:
        log.warning('[fcheck] prune fail: %s', type(e).__name__)
    if stats['eta'] or stats['landed'] or stats['pruned']:
        log.info('[fcheck] sweep rows=%d eta=%d landed=%d pruned=%d',
                 stats['rows'], stats['eta'], stats['landed'],
                 stats['pruned'])
    return stats


def kick_sweep():
    """Sweep im Hintergrund anstoßen, höchstens alle `_SWEEP_MIN_GAP_S`.
    Wirft nie. Der Aufrufer (MQTT-Topics-Poll) wartet NIE auf das Ergebnis —
    dasselbe Muster wie `live_activity.kick_sweep`."""
    now = time.time()
    with _sweep_lock:
        if _sweep_state['running']:
            return False
        if (now - _sweep_state['last']) < _SWEEP_MIN_GAP_S:
            return False
        _sweep_state['running'] = True
        _sweep_state['last'] = now

    def _work():
        try:
            sweep()
        except Exception as e:
            log.warning('[fcheck] sweep fail: %s: %s', type(e).__name__,
                        str(e)[:160])
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
                         name='fcheck-sweep').start()
        return True
    except Exception as e:
        with _sweep_lock:
            _sweep_state['running'] = False
        log.warning('[fcheck] sweep thread start fail: %s', type(e).__name__)
        return False


# ── Endpoints ───────────────────────────────────────────────────────────────

@flight_checkins_bp.route('/api/flight/checkin/<token>', methods=['POST'])
def flight_checkin(token):
    """Für einen Flug einchecken. Bearer-Match Pflicht (sonst könnte ein
    Fremder Abos in deinem Namen anlegen und dich zumüllen)."""
    if not _bearer_ok(token):
        return jsonify({'ok': False, 'error': 'forbidden'}), 403
    body = request.get_json(silent=True) or {}
    flight_no = norm_flight_no(body.get('flight'))
    if not flight_no:
        return jsonify({'ok': False, 'error': 'bad_flight'}), 400
    dep = (body.get('dep') or '').strip().upper()[:3] or None
    arr = (body.get('arr') or '').strip().upper()[:3] or None
    date = topic_date_for(body.get('dep_iso'), dep, body.get('date'))
    if not date:
        return jsonify({'ok': False, 'error': 'bad_date'}), 400
    client = _sb()
    if client is None:
        return jsonify({'ok': False, 'error': 'unavailable'}), 503
    now_iso = datetime.now(timezone.utc).isoformat()
    # Der Abflug-Instant wird NUR gespeichert, wenn er parsebar ist — ein
    # kaputter String würde den Upsert killen und das Einchecken scheitern
    # lassen, obwohl das Datum längst feststeht.
    dep_instant = parse_iso_utc(body.get('dep_iso'))
    row = {'user_token': token, 'flight_no': flight_no, 'flight_date': date,
           'dep_iata': dep, 'arr_iata': arr,
           'dep_iso': dep_instant.isoformat() if dep_instant else None,
           'updated_at': now_iso}
    # ÜBER WESSEN BORDKARTE? Der Client kennt den Namen (er zeigt die Karte
    # gerade an) — der Server rät ihn NIE. Fehlt/verunglückt er, bleibt es beim
    # neutralen „Dein Check-in"-Titel; ein Check-in scheitert daran nie.
    via = clean_via_name(body.get('via_name') or body.get('via'))

    def _store(with_via):
        payload = dict(row)
        if with_via:
            payload['via_name'] = via
        (client.table('flight_checkins')
         .upsert(payload, on_conflict='user_token,flight_no,flight_date')
         .execute())

    try:
        _store(bool(via) and _via_name_available)
    except Exception as e:
        ok = False
        if via and _note_missing_via_name(e):
            # Migration noch nicht überall durch: lieber ein Abo ohne Namen als
            # gar keins (Lehre fcm_token, 01.08.2026).
            try:
                _store(False)
                ok = True
            except Exception as e2:
                e = e2
        if not ok:
            log.warning('[fcheck] checkin fail tok=%s: %s', token[:8],
                        type(e).__name__)
            return jsonify({'ok': False, 'error': 'store_failed'}), 503
    return jsonify({'ok': True, 'flight': flight_no, 'date': date,
                    'via_name': via})


@flight_checkins_bp.route('/api/flight/checkout/<token>', methods=['POST'])
def flight_checkout(token):
    """Auschecken. Der Ausstieg muss IMMER funktionieren — deshalb toleriert
    er auch ein abweichendes Datum (Flug + Nutzer reichen), damit ein
    Datums-Kantenfall niemanden in einem Abo festhält."""
    if not _bearer_ok(token):
        return jsonify({'ok': False, 'error': 'forbidden'}), 403
    body = request.get_json(silent=True) or {}
    flight_no = norm_flight_no(body.get('flight'))
    if not flight_no:
        return jsonify({'ok': False, 'error': 'bad_flight'}), 400
    client = _sb()
    if client is None:
        return jsonify({'ok': False, 'error': 'unavailable'}), 503
    q = (client.table('flight_checkins').delete()
         .eq('user_token', token).eq('flight_no', flight_no))
    date = topic_date_for(body.get('dep_iso'), body.get('dep'),
                          body.get('date'))
    try:
        if date:
            q = q.eq('flight_date', date)
        q.execute()
    except Exception as e:
        log.warning('[fcheck] checkout fail tok=%s: %s', token[:8],
                    type(e).__name__)
        return jsonify({'ok': False, 'error': 'store_failed'}), 503
    return jsonify({'ok': True, 'flight': flight_no})


@flight_checkins_bp.route('/api/flight/checkins/<token>', methods=['GET'])
def flight_checkins_list(token):
    """Die EIGENEN offenen Abos (Gerätewechsel/Neuinstallation). Es gibt
    bewusst keinen Weg, fremde Check-ins zu lesen."""
    if not _bearer_ok(token):
        return jsonify({'ok': False, 'error': 'forbidden'}), 403
    client = _sb()
    if client is None:
        return jsonify({'ok': True, 'checkins': []})
    now_utc = datetime.now(timezone.utc)
    dates = [(now_utc.date() + timedelta(days=o)).isoformat()
             for o in (-1, 0, 1, 2)]
    def _read():
        cols = 'flight_no,flight_date,dep_iata,arr_iata'
        if _via_name_available:
            cols += ',via_name'
        return (client.table('flight_checkins').select(cols)
                .eq('user_token', token).in_('flight_date', dates)
                .limit(200).execute()).data or []

    try:
        rows = _read()
    except Exception as e:
        rows = []
        retried = False
        if _note_missing_via_name(e):
            try:
                rows = _read()
                retried = True
            except Exception as e2:
                e = e2
        if not retried:
            log.warning('[fcheck] list fail tok=%s: %s', token[:8],
                        type(e).__name__)
    # `via_name` ist additiv — alte Builds ignorieren das Feld. Ausgeliefert
    # wird es NUR an den Eigentümer des Abos (Bearer == Pfad-Token).
    return jsonify({'ok': True, 'checkins': [
        {'flight': r.get('flight_no'),
         'date': str(r.get('flight_date'))[:10],
         'dep': r.get('dep_iata'), 'arr': r.get('arr_iata'),
         'via_name': r.get('via_name')}
        for r in rows]})


@flight_checkins_bp.route('/api/internal/flight-checkin/sweep',
                          methods=['POST'])
def flight_checkin_sweep_endpoint():
    """Zweiter Takt-Eingang (Host-Cron), falls der MQTT-Daemon mal steht."""
    if not _secret_ok():
        return jsonify({'ok': False, 'error': 'forbidden'}), 403
    return jsonify({'ok': True, **sweep()})
