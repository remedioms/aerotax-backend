# ═══════════════════════════════════════════════════════════════════════════
#  Kalender-Sweep — periodischer Nachlauf für gespeicherte iCal-Links
#  (NICHT-Lufthansa-Hosts, Messung 2026-07-29/30)
#
#  WARUM (gemessenes Problem):
#    Quelle              User    Median-Alter der Server-Kopie   >24 h alt
#    LH FlightOps         689    3,2 h                            0 %
#    Kalender-Link       1038    6,9 h                           31 %  (max 26 d)
#    nur über die App     182    77 h                            65 %
#  `app._maybe_refresh_calendar_feed` existiert und funktioniert (Drossel 6 h +
#  exponentielles Backoff für kaputte Links) — sie wird aber NUR anlassbezogen
#  gerufen, wenn zufällig jemand das Profil liest (Freundes-Roster, eigener
#  Briefing-Read). Wen niemand anschaut und wer die App nicht öffnet, dessen
#  Server-Kopie altert unbegrenzt. Es fehlte schlicht der periodische Anstoß —
#  genau den liefert dieser Blueprint. KEINE zweite Abruf-Logik: der Sweep
#  ruft dieselbe Funktion, die der anlassbezogene Pfad ruft (Drossel, Backoff,
#  FlightOps-Quellen-Pause, Kill-Switch gelten damit unverändert).
#
#  DIE HARTE EINSCHRÄNKUNG — Lufthansa bleibt draußen:
#    810 der 1038 Links zeigen auf `api.lufthansa.com` (myTime-Kalender-
#    Freigaben). LH hat ausdrücklich gewarnt, dass diese Freigaben
#    eingeschränkt werden könnten, wenn sie Rechenzentrums-Abrufe im großen
#    Stil sehen — dafür existiert der Kill-Switch AEROX_SERVER_ICAL_REFRESH,
#    und dafür holt die iOS-App die myTime-ICS seit 2026-07-21 selbst vom
#    Gerät des Users. Ein Sweep über myTime-Links würde die wichtigste
#    Datenquelle des Produkts gefährden. Er ist deshalb VERBOTEN.
#    ⇒ Dieser Lauf fasst AUSSCHLIESSLICH Nicht-LH-Hosts an. Gegen die
#      Prod-Daten gemessen (30.07., dieser Filter): 1183 gespeicherte Links →
#      212 erlaubt, 971 gesperrt. Erlaubt sind cube.aero 129+3 ·
#      schedule.swiss.com 42 · outlook.office365.com 16 · offblock.de 4 ·
#      flybase.eurowings.com 4 · apps.apple.com 4 · icloud-caldav 5 ·
#      ecrew.germanairways.com 2 · Rest 3.
#      Gesperrt: api.lufthansa.com 959 · api-test.lufthansa.com 2 ·
#      ebase2go.lufthansa.com 1 · www.ui-deref.de 4 · supr.sh 1.
#      Zusaetzlich: crewaccess.cms.discover.aero 4 — dieses Portal liefert
#      nachweislich Login-HTML statt iCal und verlangt den PDF-Import.
#
#  FILTER = AUSSCHLUSS-LISTE, keine Erlaubnis-Liste (`host_is_blocked`):
#    Eine Whitelist würde jeden NEUEN Anbieter still hinten runterfallen
#    lassen — der Sweep täte dann heimlich nichts mehr für ihn. Also: alles
#    ist erlaubt AUSSER dem, was auf `lufthansa.com` endet (inkl. Subdomains)
#    bzw. `lufthansa.com` als Label-Folge irgendwo im Hostnamen trägt
#    (Täusch-Hosts wie `api.lufthansa.com.evil.tld`). `notlufthansa.com` ist
#    KEIN LH-Host (Label-Grenzen, kein nackter `endswith`-String-Vergleich).
#    Unparsebare/schema-fremde URLs gelten als blockiert (fail-closed).
#
#    ZWEITE FILTER-STUFE — versteckte myTime-Ziele (Prod-Messung 30.07.):
#    4 User haben KEINEN LH-Host gespeichert, sondern einen Redirector:
#    `https://www.ui-deref.de/r/?to=https%3A%2F%2Fapi.lufthansa.com%2Fmytime…`.
#    Der Abruf folgt Redirects — ein reiner Host-Check hätte myTime also doch
#    aus dem Rechenzentrum gezogen. Deshalb wird zusätzlich die GANZE
#    (prozent-dekodierte) URL nach der Label-Folge `lufthansa.com` durchsucht.
#    Wo das Ziel prinzipiell unsichtbar ist (Link-Kürzer), gilt fail-closed:
#    diese Hosts stehen mit auf der Ausschluss-Liste (`_REDIRECTOR_HOSTS`).
#
#  OWNER-REGEL „User merken von Wartung nichts":
#    Der Sweep erzeugt KEINE eigene User-sichtbare Wirkung. Er ruft exakt den
#    Pfad, den ein Profil-Read heute schon auslöst; die bestehenden Push-Gates
#    (Whitelist + Gate 4 „echte Dienst-Substanz", Vergangenheits-Filter,
#    Flip-Flop-Hysterese) bleiben unangetastet — der Sweep hebelt sie nicht
#    aus und fügt keinen neuen Push-Typ hinzu. Er hält still Daten frisch.
#
#  BETRIEB: Endpoint (Cron → Poll-Container :8081, Auth wie poll-boards),
#    genau wie `/api/internal/flightops/refresh-all`. Ein Lauf läuft im
#    Daemon-Thread, nie zweimal parallel (Lock + running-Flag), mit Pause
#    zwischen zwei Usern; ein Fehler bei einem User bricht den Lauf nie ab.
#    Cron-Zeile für /etc/crontab auf Hetzner (alle 3 h um :41 — bewusst
#    versetzt zum :23-refresh-all-Fenster und zum :07-relogin-watch):
#      41 */3 * * * root PS=$(grep "^ADSB_POLL_SECRET=" /opt/aerox/env.list|cut -d= -f2-); curl -s -o /var/log/calendar-sweep.last -m 30 -X POST -H "X-Poll-Secret: $PS" http://127.0.0.1:8081/api/internal/calendar/sweep
#    Der 6-h-Drossel in _maybe_refresh_calendar_feed macht den 3-h-Takt
#    unschädlich: jeder zweite Lauf findet die meisten Feeds noch frisch und
#    stößt nichts an; die Feeds, deren 6 h gerade voll sind, werden dafür
#    zeitnah nachgezogen (statt bis zu 6 h zusätzlich zu warten).
#    Kontrolle: `docker logs aerotax-poll | grep calendar-sweep` bzw.
#    GET /api/internal/calendar/sweep-status.
#
#  LOGGING: `log = logging.getLogger('aerotax')` + log.info — dieselbe Ebene
#    und derselbe Logger wie `[flightops-refresh-all]`. Der fehlende
#    Root-Handler (Blindflug-Fund 2026-07-29, app.py) ist gefixt, INFO landet
#    seither in `docker logs`. Die Zusammenfassung (`done …`) wird ZUSÄTZLICH
#    auf WARNING geloggt, wenn der Lauf gar nichts anstoßen konnte — dann ist
#    sie auch bei einem erneut kaputten Log-Setup sichtbar (lastResort ≥
#    WARNING), ohne im Normalbetrieb Rauschen zu erzeugen.
# ═══════════════════════════════════════════════════════════════════════════

import os
import re
import time
import logging
import threading
from datetime import datetime
from urllib.parse import urlsplit, unquote

from flask import Blueprint, jsonify, request

log = logging.getLogger('aerotax')

calendar_sweep_bp = Blueprint('calendar_sweep', __name__)

# ── Ausschluss-Liste ────────────────────────────────────────────────────────
# Lufthansa (myTime) plus bekannte Nicht-Feed-Portale. Swiss
# (schedule.swiss.com) und Eurowings (flybase.eurowings.com) sind eigene
# Portale mit echten Freigaben und bleiben erlaubt. Discover CrewAccess ist
# dagegen Login-HTML; der produktive Import antwortet dort korrekt mit
# ``discover_needs_pdf`` und darf vom Sweep nicht immer wieder angestossen
# werden.
BLOCKED_HOST_SUFFIXES = ('lufthansa.com', 'crewaccess.cms.discover.aero')

# Redirect-/Kürzungsdienste: das Ziel steht NICHT in der URL, der Abruf folgt
# aber Redirects — es könnte also myTime sein. Da wir das nicht sehen können,
# gilt fail-closed. Gemessen 30.07.: ui-deref (4 User, Ziel nachweislich
# api.lufthansa.com — den fängt schon die URL-Prüfung) und supr.sh (1 User,
# Ziel unsichtbar). Diese 5 User holen ihren Plan weiter über die App; der
# Preis ist minimal gegenüber dem Risiko für 810 myTime-Freigaben.
_REDIRECTOR_HOSTS = ('ui-deref.de', 'supr.sh')

# Ops-Notausgang ohne Redeploy: weitere Hosts sperren (Komma-Liste), falls ein
# zweiter Anbieter je dieselbe Warnung ausspricht.
_EXTRA_BLOCK_ENV = 'AEROX_SWEEP_BLOCK_HOSTS'

# Unsichtbare Zeichen, die beim Kopieren aus Portal-Apps in Links landen
# (gespiegelt von app._sanitize_feed_url — der Host-Filter muss dieselbe URL
# sehen wie der spätere Abruf, sonst filtert er an der Realität vorbei).
_URL_INVISIBLE = frozenset(
    '​‌‍‎‏'      # Zero-Width, LRM/RLM
    '‪‫‬‭‮'      # BiDi-Embedding/Override
    '⁠﻿'                        # Word-Joiner, BOM
)


def _clean_url(raw):
    """Whitespace + unsichtbare Zeichen raus (wie app._sanitize_feed_url)."""
    if not raw:
        return ''
    return ''.join(ch for ch in str(raw)
                   if not ch.isspace() and ch not in _URL_INVISIBLE)


def feed_host(url):
    """PURE: Hostname einer Feed-URL in Kleinschrift, '' wenn nicht sauber
    bestimmbar. Behandelt `webcal://` / `webcals://` wie `https://` (derselbe
    Feed, anderes Scheme — so speichert iOS den myTime-Link), entfernt
    userinfo (`user:pw@host`), Port, IPv6-Klammern und den Wurzel-Punkt.

    Alles Unklare ⇒ '' ⇒ blockiert: kein Scheme (`api.lufthansa.com/x`),
    fremdes Scheme (`file:`, `ftp:`), Backslash im netloc (Browser und
    urllib lesen `https://api.lufthansa.com\\@evil.tld` unterschiedlich)."""
    u = _clean_url(url)
    if not u:
        return ''
    low = u.lower()
    if low.startswith('webcal://'):
        u = 'https://' + u[len('webcal://'):]
    elif low.startswith('webcals://'):
        u = 'https://' + u[len('webcals://'):]
    try:
        parts = urlsplit(u)
    except Exception:
        return ''
    if (parts.scheme or '').lower() not in ('http', 'https'):
        return ''
    netloc = parts.netloc or ''
    if '\\' in netloc:
        return ''
    if '@' in netloc:
        netloc = netloc.rsplit('@', 1)[1]
    if netloc.startswith('['):                      # IPv6-Literal
        end = netloc.find(']')
        host = netloc[1:end] if end > 0 else ''
    else:
        host = netloc.split(':', 1)[0]
    host = host.strip().strip('.').lower()
    # Ein Host besteht aus Labels — alles andere ist kaputt und fliegt raus.
    if not host or any(c in host for c in ' /?#'):
        return ''
    return host


def _blocked_suffixes():
    extra = []
    for part in (os.environ.get(_EXTRA_BLOCK_ENV) or '').split(','):
        part = part.strip().strip('.').lower()
        if part:
            extra.append(part)
    return tuple(BLOCKED_HOST_SUFFIXES) + _REDIRECTOR_HOSTS + tuple(extra)


def host_is_blocked(host, suffixes=None):
    """PURE: fällt `host` unter die Ausschluss-Liste?

    Label-genauer Vergleich statt `endswith` auf dem rohen String:
      · `lufthansa.com`, `api.lufthansa.com`, `a.b.lufthansa.com` → True
      · `notlufthansa.com`, `mylufthansa.com`, `lufthansa.company` → False
        (die Label-FOLGE `lufthansa` + `com` kommt dort NICHT vor — genau der
        Fehler, den ein nackter `endswith('lufthansa.com')` machen würde)
      · `api.lufthansa.com.evil.tld`, `lufthansa.com.mirror.example` → True
        (Täusch-Hosts: die LH-Labels stecken drin; so etwas fassen wir
        grundsätzlich nicht an — fail-closed statt schlau sein)
    Leerer/unbekannter Host → True (fail-closed)."""
    h = (host or '').strip().strip('.').lower()
    if not h:
        return True
    labels = h.split('.')
    for suf in (suffixes if suffixes is not None else _blocked_suffixes()):
        s = [p for p in str(suf).strip().strip('.').lower().split('.') if p]
        if not s:
            continue
        n = len(s)
        for i in range(len(labels) - n + 1):
            if labels[i:i + n] == s:
                return True
    return False


def url_hides_blocked_host(url, suffixes=None):
    """PURE: steckt ein gesperrter Host IRGENDWO in der URL (Redirect-/Deref-
    Links)? Gemessen 30.07.: 4 User haben
    `https://www.ui-deref.de/r/?to=https%3A%2F%2Fapi.lufthansa.com%2Fmytime…`
    gespeichert — Host harmlos, Ziel myTime. Der Abruf folgt Redirects, also
    zählt die ganze URL, prozent-dekodiert (zweifach, gegen Doppelkodierung).

    Label-genau wie `host_is_blocked`: `notlufthansa.com` im Pfad triggert
    nicht, `…?to=https://api.lufthansa.com/…` schon."""
    raw = _clean_url(url).lower()
    if not raw:
        return False
    try:
        decoded = unquote(unquote(raw))
    except Exception:
        decoded = raw
    for suf in (suffixes if suffixes is not None else _blocked_suffixes()):
        s = str(suf).strip().strip('.').lower()
        if not s:
            continue
        pat = r'(?<![a-z0-9-])' + re.escape(s) + r'(?![a-z0-9-])'
        if re.search(pat, decoded) or re.search(pat, raw):
            return True
    return False


def sweep_allows_url(url):
    """PURE: darf der Sweep diese Feed-URL anfassen? (Ausschluss-Liste;
    unbekannte/neue Anbieter sind erlaubt, kaputte URLs nicht.) Zwei Stufen:
    Host UND versteckter Redirect-Ziel-Host."""
    if host_is_blocked(feed_host(url)):
        return False
    return not url_hides_blocked_host(url)


# ── Lauf-Zustand ────────────────────────────────────────────────────────────
_sweep_lock = threading.Lock()
_sweep_state = {'running': False, 'drain': False, 'last': None,
                'started_at': 0.0}
_sweep_thread = [None]

# Schonender Takt: ~216 Kandidaten × 3 s ≈ 11 min pro Lauf. Der Abstand ist
# bewusst großzügig — _maybe_refresh_calendar_feed feuert den Re-Import
# fire-and-forget in einem eigenen Thread (Self-Call auf den Import-Endpoint,
# Timeout 30 s), der Gap ist also zugleich die Bremse gegen viele parallele
# Importe auf dem Web-Container. Per Env nachregelbar.
_SWEEP_GAP_S = float(os.environ.get('AEROX_CALENDAR_SWEEP_GAP_S') or 3.0)
_SWEEP_MAX_USERS = int(os.environ.get('AEROX_CALENDAR_SWEEP_MAX_USERS') or 600)
_SWEEP_PAGE = 500
_SWEEP_MAX_PAGES = 20
# Ab wann ein „laufender" Sweep als hängend gilt (Selbstheilung, s.u.).
_SWEEP_STUCK_S = 2 * 3600

# Self-Call-Ziel für den Re-Import (der Sweep läuft im Poll-Container, der
# Import-Endpoint lebt im Web-Container → öffentlicher Host). Host-Form ohne
# Scheme ist erlaubt; _maybe_refresh_calendar_feed setzt https:// davor.
_SELF_BASE_DEFAULT = 'api.aerosteuer.de'


def _self_base_url():
    raw = (os.environ.get('AEROX_SELF_BASE_URL') or _SELF_BASE_DEFAULT).strip()
    raw = raw.rstrip('/')
    if raw.startswith('http://'):
        raw = raw[len('http://'):]
    return raw if raw.startswith('https://') else f'https://{raw}'


def _internal_secret_ok():
    """Gleiche Auth wie /api/internal/poll-boards bzw. flightops/refresh-all."""
    import hmac as _hmac
    secret = os.environ.get('ADSB_POLL_SECRET', '').strip()
    if secret:
        provided = (request.headers.get('X-Poll-Secret') or '').strip()
        return bool(provided) and _hmac.compare_digest(provided, secret)
    return (request.remote_addr or '') in ('127.0.0.1', '::1')


# SCHMALER SELECT: `metadata` enthält NICHT nur die Feed-URL, sondern auch die
# geparsten Events des Kalenders (und die FlightOps-Tokens). Ein
# `select('token,metadata')` über ~1900 Zeilen zöge alle 2–3 h Megabytes durch
# den Prozess. PostgREST kann JSON-Pfade direkt selektieren (auf Prod geprüft:
# 500 Zeilen in 0,23 s) — Fallback auf das breite Select, falls die Syntax mal
# nicht greift, damit der Sweep nie ganz ausfällt.
_SELECT_NARROW = ('token,feed_url:metadata->calendar_feed->>url,'
                  'feed_url_enc:metadata->calendar_feed->>url_enc,'
                  'feed_imported_at:metadata->calendar_feed->>imported_at,'
                  'feed2_url:metadata->calendar_feed_2->>url,'
                  'feed2_url_enc:metadata->calendar_feed_2->>url_enc')
_SELECT_WIDE = 'token,metadata'


def _feed_url(obj):
    if not isinstance(obj, dict):
        return ''
    raw = (obj.get('url') or '').strip()
    if raw:
        return raw
    try:
        import app as _app
        return _app._calendar_feed_decrypt_value(obj.get('url_enc'), 'url')
    except Exception:
        return ''


def _row_to_candidate(row):
    """Eine SB-Zeile → Kandidat | None. Versteht BEIDE Select-Formen: schmal
    (feed_url/feed_imported_at/feed2_url) und breit (metadata-Dict)."""
    token = (row.get('token') or '').strip()
    if not token:
        return None
    if 'metadata' in row:
        md = row.get('metadata') if isinstance(row.get('metadata'), dict) else {}
        feed = md.get('calendar_feed')
        url = _feed_url(feed)
        url_2 = _feed_url(md.get('calendar_feed_2'))
        age_h = _feed_age_h(feed)
    else:
        url = _feed_url({'url': row.get('feed_url'),
                         'url_enc': row.get('feed_url_enc')})
        url_2 = _feed_url({'url': row.get('feed2_url'),
                           'url_enc': row.get('feed2_url_enc')})
        age_h = _feed_age_h({'imported_at': row.get('feed_imported_at') or ''})
    if not url:
        return None
    return {'token': token, 'url': url, 'url_2': url_2, 'age_h': age_h}


def _feed_age_h(obj):
    """Alter der gespeicherten Kopie in Stunden (None wenn unbekannt).
    NUR für die Log-Zusammenfassung — die Refresh-Entscheidung selbst trifft
    weiter allein _maybe_refresh_calendar_feed."""
    if not isinstance(obj, dict):
        return None
    raw = (obj.get('imported_at') or '').strip()
    if not raw:
        return None
    try:
        return (datetime.now() - datetime.fromisoformat(raw)).total_seconds() / 3600.0
    except Exception:
        return None


def calendar_feed_candidates(limit=None):
    """Alle User mit ERLAUBTEM (= Nicht-LH-)Kalender-Link →
    `(liste, stats)`; Listen-Elemente sind {token, url, url_2, age_h},
    stats = {'rows', 'skipped_host'}.

    Quelle: Supabase user_profiles.metadata (calendar_feed liegt im
    metadata-jsonb, nicht in einer Spalte). PostgREST kappt still bei 1000
    Zeilen → paginiert lesen. Leer ohne SB (Dev).

    Der Host-Filter läuft SCHON HIER, vor dem Deckel: gemessen sind ~950 der
    ~1170 gespeicherten Links myTime-Links — ein Deckel VOR dem Filter würde
    sonst von LH-Zeilen aufgefressen und ließe echte Kandidaten still liegen
    (genau die Klasse Fehler, die dieser Sweep beheben soll).
    Ältester Stand zuerst (die 26-Tage-Fälle sollen zuerst drankommen).
    Wirft nie."""
    limit = limit or _SWEEP_MAX_USERS
    out = []
    rows_seen = 0
    skipped_host = 0
    try:
        import app as _app
        if not getattr(_app, 'SB_AVAILABLE', False) or _app.sb is None:
            return [], {'rows': 0, 'skipped_host': 0}
        def _page(select_expr, page):
            return (_app.sb.table('user_profiles').select(select_expr)
                    .filter('metadata->calendar_feed', 'not.is', 'null')
                    .range(page * _SWEEP_PAGE,
                           page * _SWEEP_PAGE + _SWEEP_PAGE - 1).execute())

        select_expr = _SELECT_NARROW
        page = 0
        while page < _SWEEP_MAX_PAGES:
            try:
                r = _page(select_expr, page)
            except Exception as e:
                if select_expr is _SELECT_WIDE:
                    raise
                log.warning('[calendar-sweep] schmaler select failte (%s) — '
                            'fallback auf metadata', type(e).__name__)
                select_expr = _SELECT_WIDE
                r = _page(select_expr, page)
            rows = r.data or []
            for row in rows:
                cand = _row_to_candidate(row)
                if not cand:
                    continue
                rows_seen += 1
                if not _candidate_allowed(cand):
                    skipped_host += 1
                    continue
                out.append(cand)
            if len(rows) < _SWEEP_PAGE:
                break
            page += 1
    except Exception as e:
        log.warning('[calendar-sweep] kandidaten-read fehlgeschlagen: %s: %s',
                    type(e).__name__, str(e)[:160])
    # Ältester zuerst; unbekanntes Alter gilt als sehr alt (nie importiert).
    out.sort(key=lambda c: (c['age_h'] if c['age_h'] is not None else 1e9),
             reverse=True)
    return out[:limit], {'rows': rows_seen, 'skipped_host': skipped_host}


def _candidate_allowed(cand):
    """Beide gespeicherten Links prüfen: der Import-Endpoint zieht ohne
    expliziten `url_2` IMMER auch den gespeicherten Zweit-Link nach. Ein
    User mit LH-Link in Slot 2 wäre über den Zweit-Link-Fallback also doch
    ein myTime-Abruf — deshalb fliegt er komplett raus."""
    if not sweep_allows_url(cand.get('url')):
        return False
    u2 = cand.get('url_2')
    return (not u2) or sweep_allows_url(u2)


def _sweep_work(cands, base_url, stats=None):
    """Der eigentliche Lauf (Daemon-Thread). Wirft nie, bricht nie ab.
    `stats` ist die Bilanz aus der Kandidaten-Auswahl (rows/skipped_host) —
    der Host-Filter unten ist die zweite Verteidigungslinie (jeder Aufruf-Weg
    in diese Funktion ist gefiltert, auch ein direkter)."""
    stats = stats or {}
    checked = handed = errors = stale24 = 0
    skipped_host = int(stats.get('skipped_host') or 0)
    try:
        import app as _app
        for cand in cands:
            if _sweep_state.get('drain'):
                log.info('[calendar-sweep] drain angefordert — Abbruch nach '
                         '%d/%d', checked, len(cands))
                break
            checked += 1
            try:
                if not _candidate_allowed(cand):
                    skipped_host += 1
                    continue
                # EIN Aufrufer, EINE Abruf-Logik: Drossel (6 h), Backoff für
                # kaputte Links, FlightOps-Quellen-Pause und der Kill-Switch
                # stecken alle hier drin.
                _app._maybe_refresh_calendar_feed(cand['token'],
                                                  base_url=base_url)
                handed += 1
                if (cand.get('age_h') or 0) >= 24:
                    stale24 += 1
            except Exception as e:
                errors += 1
                log.warning('[calendar-sweep] tok=%s %s: %s',
                            (cand.get('token') or '')[:8], type(e).__name__,
                            str(e)[:120])
            if _SWEEP_GAP_S > 0:
                time.sleep(_SWEEP_GAP_S)
    except Exception as e:      # z.B. app-Import kaputt — Lauf endet sauber
        log.warning('[calendar-sweep] lauf-abbruch %s: %s',
                    type(e).__name__, str(e)[:160])
    finally:
        summary = {'ts': time.time(), 'rows': int(stats.get('rows') or 0),
                   'candidates': len(cands),
                   'checked': checked, 'skipped_host': skipped_host,
                   'handed': handed, 'stale24': stale24, 'errors': errors,
                   'took_s': round(time.time() - (_sweep_state.get('started_at')
                                                  or time.time()), 1)}
        with _sweep_lock:
            _sweep_state['running'] = False
            _sweep_state['last'] = summary
        line = ('[calendar-sweep] done links=%d candidates=%d checked=%d '
                'skipped_host=%d handed=%d stale>24h=%d errors=%d took=%ss')
        args = (summary['rows'], summary['candidates'], checked, skipped_host,
                handed, stale24, errors, summary['took_s'])
        log.info(line, *args)
        if handed == 0 and len(cands) > 0:
            # Ein Lauf ohne einen einzigen Anstoß ist auffällig (alles LH?
            # alles kaputt?) — auf WARNING, damit die Zeile auch bei einem
            # erneut kaputten INFO-Handler in `docker logs` ankommt.
            log.warning(line, *args)


def _start_sweep(cands, base_url, stats=None):
    """Startet den Thread. running-Flag + started_at setzt der Aufrufer."""
    th = threading.Thread(target=_sweep_work, args=(cands, base_url, stats),
                          daemon=True, name='calendar-sweep')
    _sweep_thread[0] = th
    th.start()
    return th


def _sweep_exit_drain():
    """Worker-Recycle/SIGTERM: keine neuen Anstöße mehr. Kein Join nötig —
    der Sweep hält keinen fremden Zustand (anders als die FlightOps-Rotation),
    ein Abbruch zwischen zwei Usern ist folgenlos. Wirft nie."""
    try:
        _sweep_state['drain'] = True
    except Exception:
        pass


import atexit as _atexit
_atexit.register(_sweep_exit_drain)


@calendar_sweep_bp.route('/api/internal/calendar/sweep', methods=['POST'])
def calendar_sweep():
    """Cron-Einstieg. Auth wie poll-boards (X-Poll-Secret / localhost).
    Antwortet sofort, der Lauf arbeitet im Hintergrund."""
    if not _internal_secret_ok():
        return jsonify({'ok': False, 'error': 'forbidden'}), 403
    try:
        import app as _app
        if not _app._server_ical_refresh_enabled():
            # Kill-Switch aus ⇒ der SERVER zieht gar nichts (Geräte-Abruf).
            log.info('[calendar-sweep] skip — AEROX_SERVER_ICAL_REFRESH=0')
            return jsonify({'ok': True, 'skipped': 'server_ical_refresh_off',
                            'last': _sweep_state['last']})
    except Exception as e:
        log.warning('[calendar-sweep] kill-switch-check fehlgeschlagen: %s',
                    type(e).__name__)
        return jsonify({'ok': False, 'error': 'app_unavailable'}), 503
    with _sweep_lock:
        if _sweep_state['running']:
            # SELBSTHEILUNG: ein Lauf dauert ~11 min. Steht running noch nach
            # _SWEEP_STUCK_S und lebt der Thread nicht mehr (harter
            # Worker-Kill mitten im Lauf), wäre der Sweep sonst FÜR IMMER
            # blockiert und niemand merkte es.
            th = _sweep_thread[0]
            alive = (th is None) or bool(getattr(th, 'is_alive',
                                                 lambda: True)())
            age = time.time() - (_sweep_state.get('started_at') or 0)
            if alive and age < _SWEEP_STUCK_S:
                return jsonify({'ok': True, 'already_running': True,
                                'last': _sweep_state['last']})
            log.warning('[calendar-sweep] hängender Lauf (alive=%s age=%ds) — '
                        'starte neu', alive, int(age))
        _sweep_state['running'] = True
        _sweep_state['drain'] = False
        _sweep_state['started_at'] = time.time()
    try:
        cands, stats = calendar_feed_candidates()
    except Exception as e:
        log.warning('[calendar-sweep] kandidaten-auswahl fehlgeschlagen: %s',
                    type(e).__name__)
        cands, stats = [], {}
    if not cands:
        with _sweep_lock:
            _sweep_state['running'] = False
        log.info('[calendar-sweep] nichts zu tun links=%d skipped_host=%d',
                 int(stats.get('rows') or 0), int(stats.get('skipped_host') or 0))
        return jsonify({'ok': True, 'candidates': 0,
                        'skipped_host': int(stats.get('skipped_host') or 0),
                        'last': _sweep_state['last']})
    log.info('[calendar-sweep] start links=%d candidates=%d skipped_host=%d '
             'gap=%.1fs', int(stats.get('rows') or 0), len(cands),
             int(stats.get('skipped_host') or 0), _SWEEP_GAP_S)
    try:
        _start_sweep(cands, _self_base_url(), stats)
    except Exception as e:
        with _sweep_lock:
            _sweep_state['running'] = False      # nie blockiert zurücklassen
        log.warning('[calendar-sweep] thread-start fehlgeschlagen: %s',
                    type(e).__name__)
        return jsonify({'ok': False, 'error': 'start_failed'}), 500
    return jsonify({'ok': True, 'started': True, 'candidates': len(cands),
                    'skipped_host': int(stats.get('skipped_host') or 0),
                    'last': _sweep_state['last']})


@calendar_sweep_bp.route('/api/internal/calendar/sweep-status', methods=['GET'])
def calendar_sweep_status():
    """Sichtbarkeit statt Schätzen: was tat der letzte Lauf?"""
    if not _internal_secret_ok():
        return jsonify({'ok': False, 'error': 'forbidden'}), 403
    return jsonify({'ok': True, 'running': bool(_sweep_state['running']),
                    'drain': bool(_sweep_state['drain']),
                    'gap_s': _SWEEP_GAP_S, 'max_users': _SWEEP_MAX_USERS,
                    'last': _sweep_state['last']})
