"""Lufthansa Open API — Flight-Facts-Enrichment (Engine A, 2026-07-21).

Autoritative, KOSTENLOSE Flug-Fakten aus erster Hand der operierenden Airline
für die Lufthansa-Group-Carrier, die AeroX-Crews fliegen (LH/LX/OS/SN/EW/4Y/
EN/CL/WK). Liefert Soll-/Ist-Zeiten (lokal+UTC → exakter Offset), Gate,
Terminal, Delay, Status, Flugzeugtyp UND Registration.

Architektur (Owner „Master-Plan", free-first):
- Client-Credentials-Token (auto-refresh, thread-safe).
- Pro-(Flug,Datum,Route)-Cache — geteilt über ALLE Crew + Familien auf dem Flug
  (ein Discover-FRA-MBA = EIN Call für alle). Kurze TTL für heute (Ist-Zeiten
  frisch), lange für Vergangenheit/Zukunft (Plan ändert sich kaum).
- 5/sec-Throttle + Stunden-Budget-Wächter (Public-Plan 1.000/h) — Überschuss
  wird NICHT still verschluckt, sondern geloggt und übersprungen (Fallback auf
  die bestehenden Board/FR24-Tiers).
- Voll no-op wenn nicht konfiguriert (Env fehlt) → Deploy/Commit ist immer
  sicher; erst das Setzen der Env-Vars aktiviert die Anreicherung.

Secret NUR aus Env (LH_OPEN_API_KEY/SECRET, Fallback LH_KEY/SECRET) — nie im
Code/Log. Rückgabe von `lh_flight_facts` ist byte-shape-kompatibel mit
`_obs_rows_to_facts` (aerox_data_blueprint), damit der Merge trivial bleibt.
"""
import os
import re
import time
import json
import threading
import urllib.request
import urllib.parse
import urllib.error
import logging

from flask import Blueprint, jsonify

log = logging.getLogger('aerotax')
lh_open_bp = Blueprint('lh_open_bp', __name__)

_BASE = 'https://api.lufthansa.com/v1'
_KEY = (os.environ.get('LH_OPEN_API_KEY') or os.environ.get('LH_KEY') or '').strip()
_SECRET = (os.environ.get('LH_OPEN_API_SECRET') or os.environ.get('LH_SECRET') or '').strip()

# Lufthansa-Group OPERIERENDE Carrier (die AeroX-Crews fliegen). Verifiziert
# 2026-07-21, dass die Open API LH/LX/4Y liefert; OS/SN/EW/EN/CL/WK sind
# dieselbe Group. AZ (ITA Airways, Group seit 2025) verifiziert 2026-07-22:
# flightstatus liefert volle AZ-Daten inkl. Actuals (AZ1157) und der
# Notification-Broker streamt AZ-Events — Owner-Entscheid „ITA ist auch LH
# Group". Bewusst KEINE Nicht-Group-Carrier — schont das 1.000/h-Budget
# (Roster-Sektoren der Crew sind immer Group-Flüge). VL (Lufthansa City,
# AeroX-Synthetik-Prefix) ist KEIN eigener API-Carrier → ausgelassen.
_LH_GROUP = {'LH', 'LX', 'OS', 'SN', 'EW', '4Y', 'EN', 'CL', 'WK', 'AZ'}

# ── Token-Cache ──────────────────────────────────────────────────────────────
_tok_lock = threading.Lock()
_tok_val = None
_tok_exp = 0.0

# ── 5/sec-Throttle + Stunden-Budget ──────────────────────────────────────────
# ACHTUNG (live gelernt 2026-07-22, „Developer Over Rate" bis auf den OAuth-
# Endpoint): die 1.000/h-Quota gilt PRO KEY, dieser Zähler aber PRO PROZESS —
# und produktiv laufen 3 Backend-Gunicorn-Worker + 1 Poll-Worker (+ Daemon,
# der selbst kaum ruft). 900/h pro Prozess konnte also ~4× überziehen und
# legte den MQTT-Daemon (kein Token mehr) lahm. 220 × 4 Prozesse ≈ 880/h
# Aggregat-Obergrenze mit Marge.
_rate_lock = threading.Lock()
_last_call_ts = 0.0
# QPS-LEHRE (Owner 2026-07-24 „warum immer über dem Limit?"): auch das
# 5/sec-Limit gilt PRO KEY, dieser Throttle aber PRO PROZESS. 0.22s pro
# Prozess erlaubte mit 4 rufenden Prozessen (3 Gunicorn-Worker + Poll)
# ~18 Calls/sec Aggregat-Bursts → „Developer Over Rate"-403-Salven, obwohl
# die Stunden-Quote längst passte (61 Calls/h und trotzdem 40× 403).
# 0.9s ≈ 1,1/sec pro Prozess → ≤ ~4,4/sec Aggregat mit Marge.
_MIN_INTERVAL = 0.9
_HOUR_BUDGET = 220             # PRO PROZESS (s.o.) — Aggregat < 1.000/h
# 403-Cool-down: meldet LH „Over Rate", kurz komplett aussetzen (Fallback-
# Tiers übernehmen), statt weitere Budget-Slots in die 403-Wand zu feuern.
_rate_penalty_until = 0.0
_RATE_PENALTY_SEC = 60.0
_hour_window = 0               # aktuelle Stunde (epoch // 3600)
_hour_count = 0
_budget_warned_hour = -1

# ── Prozess-ÜBERGREIFENDER Stunden-Gate ─────────────────────────────────────
# WARUM (Owner 2026-07-27, drei Stunden über 1.000/h): `_HOUR_BUDGET` oben
# zählt pro Prozess. 3 Backend-Worker + 1 Poll-Worker × 220 = 880/h Decke, und
# weil die Prozesse voneinander nichts wissen, hilft ein Senken des Werts nur
# um den Preis, dass ein einzelner Prozess unnötig früh dichtmacht.
# Gemeinsame Wahrheit ist der Zähler `lhopen:<YYYYMMDDHH>` in ax_api_budget.
# Der Flusher schreibt ihn ohnehin alle 30 s und bekommt vom atomaren RPC den
# neuen GESAMTSTAND zurück — den übernehmen wir hier, ohne einen einzigen
# zusätzlichen Netz-Call.
# 850 statt 1.000: zwischen zwei Flushes liegen bis zu 30 s, in denen alle
# Prozesse zusammen (~4,4 Calls/s, s. _MIN_INTERVAL) noch überziehen können.
_GLOBAL_HOUR_BUDGET = 850
_global_lock = threading.Lock()
_global_hour = 0               # Stunde, auf die sich _global_count bezieht
_global_count = 0              # Gesamtstand ALLER Prozesse beim letzten Flush
_global_local_since = 0        # was DIESER Prozess seither noch drauflegte
_global_warned_hour = -1

# ── Prioritätsklassen (Owner 2026-07-30: „App meldet über dem Limit") ────────
# MESSUNG über 24 h (2026-07-29 18 UTC … 2026-07-30 17 UTC, `ax_api_budget`):
#     gesendet 19.140 · vom eigenen Gate ABGEWIESEN 29.562
# Der Key lief in JEDER der 24 Stunden am Deckel (562–910 gesendet/h). Gewollt
# pro Aufrufer (gesendet + abgewiesen):
#     obs_merge          20.439      warm_obs_overlay   19.064
#     warm_obs_merge      3.591      mqtt_leg_reg        3.929
#     mqtt_event          1.192      mqtt_inbound          541
#
# Der eigentliche Schaden war nicht die MENGE, sondern die REIHENFOLGE: alles
# zog aus demselben Topf, first come first served. Ergebnis in denselben 24 h:
# 505 abgewiesene `mqtt_event` + 504 `mqtt_inbound` — das sind Live-Activity-
# Updates, die auf dem Sperrbildschirm NICHT ankamen, während gleichzeitig
# 9.986 rein spekulative Vorrats-Warms durchgingen. Genau die Beschwerde.
#
# Jetzt hat jede Klasse ihre eigene Decke auf DENSELBEN Stundenzähler:
#     user  95 %  — was gerade jemand auf dem Schirm hat (obs_merge,
#                   mqtt_event/inbound, Detail-/uflight-Pfade)
#     bg    85 %  — halb spekulative Vorrats-Regs für den Inbound-Watch
#     warm  60 %  — reine Spekulation; verhungert ZUERST, nie umgekehrt
# Die Deckel gelten sowohl global (`_GLOBAL_HOUR_BUDGET`) als auch pro Prozess
# (`_HOUR_BUDGET`) — sonst könnte ein einzelner Worker seine 220 komplett mit
# Warms füllen, obwohl der Key als Ganzes noch Luft für User-Calls hätte.
_CLASS_USER = 'user'
_CLASS_BG = 'bg'
_CLASS_WARM = 'warm'
_CLASS_SHARE = {_CLASS_USER: 0.95, _CLASS_BG: 0.85, _CLASS_WARM: 0.60}
# Halb spekulativ: der MQTT-Daemon holt die Regs aller Topic-Legs auf Vorrat,
# damit der Inbound-Watch sie hat, wenn der User fragt. Wichtiger als ein Warm,
# unwichtiger als eine offene Karte.
_BG_CALLERS = {'mqtt_leg_reg'}
_class_warned = {}             # (klasse, stunde) → schon gewarnt


def _caller_class(caller):
    """Aufrufer-Label → Prioritätsklasse. Pure. Unbekanntes gilt als `user` —
    ein neuer Aufrufer soll lieber zu VIEL Budget bekommen als still hinter
    Spekulation zurückzustehen (ein Fehler in dieser Richtung ist sichtbar,
    der andere nicht)."""
    c = re.sub(r'[^a-z0-9_]', '', str(caller or '').lower())
    if c.startswith('warm'):
        return _CLASS_WARM
    if c in _BG_CALLERS:
        return _CLASS_BG
    return _CLASS_USER


def _class_cap(total, caller):
    """Stunden-Decke DIESER Klasse auf einem Gesamtkontingent. Pure."""
    return max(1, int(total * _CLASS_SHARE.get(_caller_class(caller), 1.0)))


def note_global_budget(hour_key, total):
    """Vom Flusher aufgerufen, sobald der atomare RPC den echten Gesamtstand
    für `lhopen:<hour>` geliefert hat. Setzt den lokalen Seit-Flush-Zähler
    zurück — ab hier ist `total` die Wahrheit. Wirft nie."""
    global _global_hour, _global_count, _global_local_since
    try:
        h = int(hour_key)
        with _global_lock:
            if h < _global_hour:
                return                     # veralteter Flush, ignorieren
            if h > _global_hour:
                _global_hour = h
                _global_local_since = 0
            _global_count = max(0, int(total))
            _global_local_since = 0
    except Exception:
        pass


def _global_budget_check(now, caller=None):
    """False, wenn der GANZE Key das Stundenkontingent DIESER KLASSE
    ausgeschöpft hat. Schätzung = letzter bekannter Gesamtstand + was dieser
    Prozess seit dem Flush selbst verbraucht hat. Prüft NUR — gebucht wird in
    `_global_budget_commit`, erst wenn der Call auch wirklich rausgeht.
    Fail-OPEN: ohne Supabase-Stand (frischer Prozess, RPC-Fallback) bleibt der
    Prozess-Gate `_HOUR_BUDGET` die Bremse — lieber die alte Decke als
    LH-Enrichment komplett aus.

    `caller=None` = volle Decke (Alt-Verhalten, u. a. für Tests)."""
    global _global_hour, _global_count, _global_local_since, _global_warned_hour
    hour = int(time.strftime('%Y%m%d%H', time.gmtime(now)))
    cap = (_GLOBAL_HOUR_BUDGET if caller is None
           else _class_cap(_GLOBAL_HOUR_BUDGET, caller))
    with _global_lock:
        if hour != _global_hour:
            _global_hour = hour
            _global_count = 0
            _global_local_since = 0
            _class_warned.clear()
            return True
        est = _global_count + _global_local_since
        if est >= cap:
            cls = _caller_class(caller) if caller is not None else 'all'
            if _class_warned.get(cls) != hour:
                log.warning('[lh_open] GLOBALE Stunden-Decke der Klasse %s '
                            'erreicht (%d von %d, geschätzt %d über alle '
                            'Prozesse) — Fallback-Tiers', cls, cap,
                            _GLOBAL_HOUR_BUDGET, est)
                _class_warned[cls] = hour
                _global_warned_hour = hour
            return False
        return True


def warm_gate_open(now=None):
    """True, solange die WARM-Klasse global noch Luft hat. Für den Warm-Pfad,
    der so schon VOR dem Einreihen abbrechen kann, statt einen Thread zu
    starten, der garantiert an derselben Wand endet (die 12.669 abgewiesenen
    Warms in 24 h waren selbst Last und Log-Rauschen). Wirft nie."""
    try:
        return _global_budget_check(time.time() if now is None else now,
                                    caller='warm')
    except Exception:
        return True


def _global_budget_commit():
    """Einen tatsächlich abgesetzten Call auf den globalen Schätzer buchen."""
    global _global_local_since
    with _global_lock:
        _global_local_since += 1

# ── Fakten-Cache ─────────────────────────────────────────────────────────────
_facts_lock = threading.Lock()
_facts_memo = {}               # key -> (expires_at, facts_dict)
_FACTS_MAX = 12000


def lh_open_configured():
    """True nur wenn Key+Secret via Env gesetzt sind (sonst voll no-op)."""
    return bool(_KEY and _SECRET)


def is_lh_group(flight_no):
    """True wenn die Flugnummer ein LH-Group-Operating-Carrier ist (2-stelliger
    IATA-Prefix, inkl. ziffern-führend wie 4Y). Robuster Prefix-Parse."""
    fn = (flight_no or '').replace(' ', '').upper().strip()
    if len(fn) < 3:
        return False
    # 2-Zeichen-Carrier + Ziffern: 'LH400', '4Y136', 'EW8'.
    pfx = fn[:2]
    return pfx in _LH_GROUP and fn[2:3].isdigit()


_tok_fail_until = 0.0


def _token():
    """Client-Credentials-Access-Token, gecacht bis kurz vor Ablauf. None bei
    Fehler (Aufrufer fällt dann still auf die anderen Tiers zurück).
    NEGATIV-CACHE 90s (Ausfall 22.07. abends: OAuth-403 → JEDER LH-Aufruf
    machte einen eigenen blockierenden Token-Roundtrip, 67 Fails/5 min)."""
    global _tok_val, _tok_exp, _tok_fail_until
    if not lh_open_configured():
        return None
    now = time.time()
    with _tok_lock:
        if _tok_val and now < _tok_exp:
            return _tok_val
        if now < _tok_fail_until:
            return None
    body = urllib.parse.urlencode({
        'client_id': _KEY, 'client_secret': _SECRET,
        'grant_type': 'client_credentials'}).encode()
    req = urllib.request.Request(
        _BASE + '/oauth/token', data=body,
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
    except Exception as e:
        log.warning('[lh_open] token fail: %s', type(e).__name__)
        with _tok_lock:
            _tok_fail_until = time.time() + 90
        return None


def _budget_ok(caller=None):
    """5/sec-Throttle + Stunden-Budget DIESER KLASSE (s. `_CLASS_SHARE`).
    False (mit Log) wenn die Decke erschöpft ist — kein stiller Cap.
    INCIDENT-LEHRE 2026-07-22 (Backend unresponsive, sogar /api/health 30s):
    der Spacing-Sleep lief UNTERM _rate_lock — jeder wartende Worker-Thread
    hing zusätzlich im Lock des Vordermanns. Jetzt: Slot unterm Lock
    reservieren, schlafen DANACH."""
    global _last_call_ts, _hour_window, _hour_count, _budget_warned_hour
    now = time.time()
    if now < _rate_penalty_until:
        return False               # Over-Rate-Cool-down aktiv → Fallback-Tiers
    if not _global_budget_check(now, caller=caller):
        return False               # Klassen-Decke des Keys erreicht
    hour = int(now // 3600)
    proc_cap = _class_cap(_HOUR_BUDGET, caller)
    wait = 0.0
    with _rate_lock:
        if hour != _hour_window:
            _hour_window = hour
            _hour_count = 0
        if _hour_count >= proc_cap:
            if _budget_warned_hour != hour:
                log.warning('[lh_open] Prozess-Decke %d (Klasse %s von %d) '
                            'erreicht — überspringe LH-Enrichment bis zur '
                            'nächsten Stunde (Fallback aktiv)',
                            proc_cap, _caller_class(caller), _HOUR_BUDGET)
                _budget_warned_hour = hour
            return False
        wait = _MIN_INTERVAL - (now - _last_call_ts)
        _last_call_ts = now + max(0.0, wait)   # Slot reservieren
        _hour_count += 1
    _global_budget_commit()        # erst buchen, wenn der Slot wirklich steht
    if wait > 0:
        time.sleep(wait)
    return True


# ── Prozess-übergreifender LH-Zähler (Owner 2026-07-26: „erst messen") ──────
# WARUM: `_HOUR_BUDGET`/`_hour_count` oben zählen PRO PROZESS — produktiv laufen
# 3 Backend-Worker (:8080) + 1 Poll-Worker (:8081) + der Pfad des MQTT-Daemons,
# und ein datei-basierter Zähler ginge nicht (die Container teilen KEIN Volume).
# Die LH-Quota gilt aber PRO KEY. Gemeinsame Wahrheit = ax_api_budget.
#
# SCHLÜSSEL-LAYOUT — Stunde VOR dem Aufrufer:
#     lhopen:<YYYYMMDDHH>            lhopen:<YYYYMMDDHH>:<caller>
# Damit ist die Stunden-Abfrage ein PRÄFIX (`lhopen:2026072621%`) und kann den
# Primärschlüssel-Index nutzen; mit dem Aufrufer vorn wäre nur ein
# `%:<stunde>`-Suffix-Match möglich → Seq-Scan über eine ewig wachsende Tabelle.
_budget_buf_lock = threading.Lock()
_budget_buf = {}
_BUDGET_FLUSH_S = 30.0
_budget_thread = None


def _budget_writes_allowed():
    """Kein Zähler-Schreiben aus der Test-Suite — sonst verfälscht ein lokaler
    pytest-Lauf (mit echten Supabase-Creds in der Env) genau die Zahlen, die
    dieser Zähler beweisen soll."""
    return not os.environ.get('PYTEST_CURRENT_TEST')


def budget_flush():
    """Gepufferte Zähler nach Supabase schreiben. Wirft nie.

    NUR aus dem eigenen Flusher-Thread aufrufen (oder aus Tests). Der
    Supabase-Client hat 120 s postgrest-Timeout und der Retry-Transport kann
    das auf ein Vielfaches strecken — im Request-/MQTT-/Cron-Thread wäre das
    ein Aufhänger in einem MESS-Pfad. Fehlgeschlagene Einheiten wandern zurück
    in den Puffer, gehen also nicht schon beim ersten Netzruckler verloren."""
    with _budget_buf_lock:
        if not _budget_buf:
            return 0
        pending = dict(_budget_buf)
        _budget_buf.clear()
    done = 0
    try:
        from blueprints.aerox_data_blueprint import _budget_key_inc
        for k, u in list(pending.items()):
            total = _budget_key_inc(k, u)
            # Der bare Stunden-Key (ohne :<caller>) ist die gemeinsame Wahrheit
            # für den prozess-übergreifenden Gate — der RPC liefert hier den
            # Stand ALLER Prozesse zurück, sonst weiß niemand voneinander.
            if total is not None and k.startswith('lhopen:'):
                parts = k.split(':')
                if len(parts) == 2:
                    note_global_budget(parts[1], total)
            pending.pop(k, None)
            done += 1
    except Exception:
        pass
    if pending:                       # Rest zurücklegen statt verwerfen
        with _budget_buf_lock:
            for k, u in pending.items():
                _budget_buf[k] = _budget_buf.get(k, 0) + u
    return done


def _budget_thread_start():
    """Einen (1) Daemon-Thread pro Prozess, der den Puffer wegschreibt.
    Bewusst EIN langlebiger Thread: `supabase_threadlocal` hält pro Thread
    einen eigenen httpx-Client und schließt ihn beim Thread-Ende NICHT — ein
    Flush aus kurzlebigen `lh-warm`-Threads würde also Sockets lecken."""
    global _budget_thread
    if _budget_thread is not None or not _budget_writes_allowed():
        return

    def _run():
        while True:
            time.sleep(_BUDGET_FLUSH_S)
            try:
                budget_flush()
            except Exception:
                pass

    _budget_thread = threading.Thread(target=_run, daemon=True,
                                      name='lh-budget-flush')
    _budget_thread.start()


def budget_inc(key_prefix, caller=None, units=1):
    """Einen LH-Call buchen — reine In-Memory-Addition, KEIN Netz.
    Der Flusher-Thread schreibt höchstens alle 30 s. Wirft nie."""
    try:
        h = time.strftime('%Y%m%d%H', time.gmtime())
        lbl = re.sub(r'[^a-z0-9_]', '', str(caller or 'unknown').lower())[:32]
        with _budget_buf_lock:
            for k in (f'{key_prefix}:{h}', f'{key_prefix}:{h}:{lbl or "unknown"}'):
                _budget_buf[k] = _budget_buf.get(k, 0) + max(1, int(units))
        _budget_thread_start()
    except Exception:
        pass


def budget_inc_key(key, units=1):
    """Wie budget_inc, aber mit VOLLSTÄNDIG vorgegebenem Schlüssel — ohne die
    automatische `:<YYYYMMDDHH>`-Anhängung und ohne Aufrufer-Label.

    Gebraucht seit dem lhfo-TAGES-Deckel (2026-07-28): der FlightOps-Key hat
    neben dem Stundenlimit ein TAGESkontingent, der Zähler dafür heißt
    `lhfoD:<YYYYMMDD>` und passt damit nicht in das stundenbasierte
    Schlüssel-Layout von budget_inc. Gleicher Puffer, gleicher Flusher, gleiche
    `_budget_key_used`-Lesbarkeit — nur der Schlüssel kommt vom Aufrufer.
    Wirft nie."""
    try:
        k = str(key or '').strip()
        if not k:
            return
        with _budget_buf_lock:
            _budget_buf[k] = _budget_buf.get(k, 0) + max(1, int(units))
        _budget_thread_start()
    except Exception:
        pass


# ── „leer" ≠ „unbekannt" ────────────────────────────────────────────────────
# Ein leeres Ergebnis hat DREI sehr verschiedene Ursachen: (a) LH sagt sauber
# „diesen Flug gibt es an dem Tag nicht" (HTTP 404), (b) der Call wurde vom
# eigenen Throttle abgewiesen, (c) LH war kaputt (403/503/Timeout). Nur (a) ist
# eine ANTWORT und darf negativ gecacht werden; (b) und (c) sind „wir wissen es
# nicht" und würden als Negativ-Cache einen Ausfall verlängern.
# MESSUNG 2026-07-27: in der Stichprobe waren alle Reg-Fehlschläge HTTP-503 —
# die landeten bisher 30 min lang als „hat keine Reg" im Memo.
# Thread-local, weil Gunicorn hier mit --threads 8 fährt.
_call_state = threading.local()


def last_call_answered():
    """True, wenn der letzte LH-GET dieses Threads eine ECHTE Antwort bekam
    (HTTP 200 oder 404). False bei Throttle-Abweisung, 403/5xx, Netzfehler —
    dann ist ein leeres Ergebnis kein Fakt, sondern eine Lücke."""
    return bool(getattr(_call_state, 'answered', False))


def last_call_denied():
    """True, wenn der letzte LH-Versuch dieses Threads am EIGENEN Throttle
    scheiterte (Stunden-Budget erschöpft oder Over-Rate-Cool-down) — der Call
    ging also nie raus.

    WARUM getrennt von `last_call_answered` (Messung 27.07.): ein geschlossenes
    Gate gilt für JEDEN weiteren Call dieser Stunde, ein LH-503 nur für diesen
    einen Flug. Batch-Aufrufer (`lh_mqtt._legs_regs`) können damit nach dem
    ersten „Gate zu" abbrechen, statt die restlichen ~320 Legs gegen dieselbe
    Wand zu feuern — genau der Sturm, der `lhopen_denied` auf 3.901/h trieb."""
    return bool(getattr(_call_state, 'denied', False))


def _note_answer(answered, denied=False):
    """Zustand des letzten Versuchs setzen. Auch Memo-Treffer sind Antworten —
    ohne das hier läse ein Aufrufer nach einem Cache-Hit den Zustand eines
    ganz anderen, längst vergangenen GETs desselben Threads."""
    _call_state.answered = bool(answered)
    _call_state.denied = bool(denied)


def _get(path, caller=None):
    """Authentifizierter GET → dict oder None. Wirft nie."""
    global _rate_penalty_until
    _note_answer(False)
    tok = _token()
    if not tok:
        return None
    if not _budget_ok(caller):
        # ABGEWIESEN mitzählen (sonst ist der Zähler blind für genau die Frage
        # des Owners): `_budget_ok` deckelt bei _HOUR_BUDGET pro Prozess, ein
        # reiner Gesendet-Zähler könnte also NIE über 220×Prozesse steigen.
        # Erst „gewollt = gesendet + abgewiesen" zeigt den echten Bedarf.
        # Label = GRUND + AUFRUFER: der reine Grund sagte zwar „3.901 mal am
        # Stunden-Budget abgewiesen", aber nicht von wem — und genau das war
        # die Frage („endlich sehen was da alles zieht"). Der ':'-Separator
        # bleibt dem Key-Schema vorbehalten, darum '_' (budget_inc strippt ':'
        # ohnehin aus dem Label).
        reason = ('rate_penalty' if time.time() < _rate_penalty_until
                  else 'hour_budget')
        budget_inc('lhopen_denied', f'{reason}_{caller or "unknown"}')
        _note_answer(False, denied=True)
        return None
    budget_inc('lhopen', caller)
    req = urllib.request.Request(
        _BASE + path,
        headers={'Authorization': 'Bearer ' + tok,
                 'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read().decode('utf-8'))
        _note_answer(True)
        return d
    except urllib.error.HTTPError as e:
        if e.code == 403:
            # „Developer Over Rate": kurzer Voll-Stopp statt 403-Salve —
            # jeder weitere Call verbrennt nur Budget gegen dieselbe Wand.
            _rate_penalty_until = time.time() + _RATE_PENALTY_SEC
        if e.code == 404:            # 404 = Flug an dem Tag nicht geflogen (normal)
            _note_answer(True)
        else:
            log.warning('[lh_open] GET %s -> HTTP %s', path.split('?')[0], e.code)
        return None
    except Exception as e:
        log.warning('[lh_open] GET %s -> %s', path.split('?')[0], type(e).__name__)
        return None


def _offset_iso(local_str, utc_str):
    """('2026-07-21T10:55', '2026-07-21T08:55Z') → '2026-07-21T10:55:00+02:00'.
    Offset exakt aus lokal−UTC (auf Minuten). Fällt auf naives Lokal-ISO zurück,
    wenn UTC fehlt (Consumer datieren es dann stations-lokal)."""
    if not local_str:
        return None
    loc = local_str.strip()
    if len(loc) == 16:               # 'YYYY-MM-DDTHH:MM'
        loc_full = loc + ':00'
    else:
        loc_full = loc
    if not utc_str:
        return loc_full
    try:
        from datetime import datetime as _dt
        lu = _dt.strptime(loc[:16], '%Y-%m-%dT%H:%M')
        uu = _dt.strptime(utc_str.strip().rstrip('Z')[:16], '%Y-%m-%dT%H:%M')
        delta_min = round((lu - uu).total_seconds() / 60.0)
        sign = '+' if delta_min >= 0 else '-'
        delta_min = abs(delta_min)
        return f'{loc_full}{sign}{delta_min // 60:02d}:{delta_min % 60:02d}'
    except Exception:
        return loc_full


def _side_times(side):
    """LH-Departure/Arrival-Block → (sched_iso, est_iso). Ist bevorzugt Actual,
    sonst Estimated. Alles Station-Offset-ISO."""
    def _pick(kind):
        b = side.get(kind) or {}
        return b.get('DateTime')
    sched = _offset_iso(_pick('ScheduledTimeLocal'),
                        (side.get('ScheduledTimeUTC') or {}).get('DateTime'))
    # Ist: Actual gewinnt, sonst Estimated.
    a_loc = _pick('ActualTimeLocal') or _pick('EstimatedTimeLocal')
    a_utc = ((side.get('ActualTimeUTC') or {}).get('DateTime')
             or (side.get('EstimatedTimeUTC') or {}).get('DateTime'))
    est = _offset_iso(a_loc, a_utc) if a_loc else None
    return sched, est


def _delay_min(sched_iso, est_iso):
    """Delay in Minuten aus zwei Offset-ISO-Strings (oder None)."""
    if not sched_iso or not est_iso:
        return None
    try:
        from datetime import datetime as _dt
        s = _dt.fromisoformat(sched_iso)
        e = _dt.fromisoformat(est_iso)
        return int(round((e - s).total_seconds() / 60.0))
    except Exception:
        return None


def _leg_to_facts(leg):
    """Ein FlightStatus-Leg → Fakten-Dict (Shape wie _obs_rows_to_facts)."""
    dep = leg.get('Departure') or {}
    arr = leg.get('Arrival') or {}
    facts = {}
    sd, ed = _side_times(dep)
    sa, ea = _side_times(arr)
    if sd:
        facts['sched_dep'] = sd
    if ed:
        facts['est_dep'] = ed
    if sa:
        facts['sched_arr'] = sa
    if ea:
        facts['est_arr'] = ea
    dm = _delay_min(sd, ed)
    if dm is not None:
        facts['dep_delay_min'] = dm
    am = _delay_min(sa, ea)
    if am is not None:
        facts['arr_delay_min'] = am
    # Gate/Terminal
    dt = dep.get('Terminal') or {}
    if dt.get('Name'):
        facts['terminal'] = str(dt['Name'])
    if dt.get('Gate'):
        facts['gate'] = str(dt['Gate'])
    at = arr.get('Terminal') or {}
    if at.get('Name'):
        facts['arr_terminal'] = str(at['Name'])
    if at.get('Gate'):
        facts['arr_gate'] = str(at['Gate'])
    # Equipment: Typ + Registration (autoritativ, frisch)
    eq = leg.get('Equipment') or {}
    if eq.get('AircraftCode'):
        facts['type'] = str(eq['AircraftCode'])
    if eq.get('AircraftRegistration'):
        # LH gibt Regs ohne Bindestrich ('DAIKP') — normalisieren auf 'D-AIKP'
        facts['reg'] = _norm_reg(str(eq['AircraftRegistration']))
    # Status / Cancelled
    fs = leg.get('FlightStatus') or {}
    code = (fs.get('Code') or '').upper()
    definition = fs.get('Definition')
    if code in ('CD', 'CX', 'DX'):
        facts['cancelled'] = True
    if definition and definition.lower() != 'no status':
        facts['dep_status'] = definition
    # Route
    if dep.get('AirportCode'):
        facts['dep_iata'] = dep['AirportCode']
    if arr.get('AirportCode'):
        facts['arr_iata'] = arr['AirportCode']
    return facts


# ── Fakten-TTL nach Abflugnähe (Owner 2026-07-27, „Key wieder überm Limit") ──
# GEMESSEN, nicht geschätzt: nach dem Reg-Cache-Fix war die obs_*-Familie
# (obs_merge/obs_overlay + deren warm_-Zwillinge) der nächstgrößte Verbraucher
# des Open-API-Keys — 397/h in der Stunde 08 UTC, 620/h in der Stunde 07.
# Ursache ist die FLACHE 120-s-TTL für alles, was „heute" ist. Die 120 s sind
# nur für EIN Fenster nötig (Gate/Ist-Zeiten; ein Gate wechselt ~40 min vor
# Abflug) — der Roster-Warmer rechnet aber alle 30 min bis zu 500 Flüge von
# HEUTE UND MORGEN vor, und deren Abflug liegt meist Stunden weg. Dort kostete
# jeder Lauf denselben Flug erneut, ohne dass sich ein einziges Feld geändert
# hätte.
# Drei Stufen statt einer, alles rein deterministisch aus den Fakten:
_TTL_OPS = 120                 # Betriebsfenster: unverändert frisch
# 45 min ist KEINE runde Zahl, sondern eine Antwort auf den Takt des
# Roster-Warmers (`aerox_data_blueprint`, poll-tick alle 30 min, bis zu 500
# Flüge pro Lauf): eine TTL UNTER diesem Takt heißt, dass jeder Lauf jeden
# Flug erneut kauft — genau das machte die obs_*-Familie zum grössten
# Verbraucher, nachdem der Reg-Pfad entlastet war (gemessen 10 UTC:
# warm_obs_overlay 321 Calls in 20 min). Gefährlich lang wird das nie: die
# TTL ist unten hart auf den Beginn des Betriebsfensters gedeckelt, ein Flug
# kurz vor seinem Fenster bekommt also weiterhin nur die Rest-Sekunden.
_TTL_PLAN = 45 * 60            # heute, aber noch weit vor Abflug (nur Plan)
_TTL_DONE = 30 * 60            # heute, aber lange gelandet (Ist-Zeiten final)
_TTL_OTHER_DAY = 6 * 3600      # anderer Tag als heute (unverändert)
_OPS_LEAD_S = 2 * 3600         # ab hier zählt ein Flug als „operativ"
# BEWUSST GROSSZÜGIG (2 h statt 30 min): `est_arr` ist mal Actual, mal
# Estimated — die Fakten sagen nicht, welches von beiden. Eine zu optimistische
# Schätzung dürfte den Flug sonst als „fertig" einfrieren, während er noch in
# der Luft ist. Erst 2 h nach der letzten bekannten Ankunftszeit ist das
# ausgeschlossen; bis dahin bleibt alles beim alten 120-s-Verhalten.
_OPS_TAIL_S = 2 * 3600

# ── FINALE Fakten (Owner 2026-07-30, „Fakten fehlen stellenweise") ──────────
# Ein gelandeter Flug ändert sich NIE WIEDER: Ist-Abflug, Ist-Ankunft, Gate,
# Terminal, Reg, Muster stehen fest. Trotzdem lief bisher jede abgelaufene TTL
# in einen NEUEN Kauf — `_TTL_DONE` 30 min bzw. `_TTL_OTHER_DAY` 6 h, mal 4
# Prozesse ohne gemeinsames Gedächtnis, plus jedes Deploy als Voll-Reset.
# MESSUNG: im Warm-Fenster (heute−1 … heute+2) liegen über ALLE 1.419 Roster
# zusammen nur 1.860 verschiedene LH-Group-Legs — davon 454 am Vortag, also zu
# 100 % gelandet. Dem stehen 48.702 gewollte Calls in 24 h gegenüber: jedes
# Leg wurde im Schnitt ~26× am Tag gekauft.
# Ab jetzt: einmal gekauft, 14 Tage gültig — im Prozess UND im geteilten Cache.
_TTL_FINAL = 14 * 24 * 3600
# 6 h nach der letzten bekannten Ankunftszeit ist auch ein „Estimated", das sich
# noch verschoben hätte, sicher überholt (der 2-h-`_OPS_TAIL_S` deckelt nur die
# 120-s-Frische ab, er ist bewusst KEIN Endgültigkeits-Beweis).
_FINAL_AFTER_S = 6 * 3600
# ABKÜRZUNG mit Beweis: sagt LH selbst „Flight Landed"/„Arrived" UND die
# Ankunftszeit liegt in der Vergangenheit, ist nichts mehr offen — dann braucht
# es die 6-h-Sicherheitsmarge nicht. Keine Heuristik, sondern die Aussage der
# operierenden Airline über ihren eigenen Flug.
_FINAL_STATUS_HINTS = ('landed', 'arrived')


def _facts_final(facts, now=None):
    """True, wenn diese Fakten sich NIE WIEDER ändern können. Pure.

    Drei Wege, alle beweisbar aus den Fakten selbst:
      1. LH sagt „Landed"/„Arrived" und die Ankunftszeit ist vorbei.
      2. Die letzte bekannte Zeit (Ankunft, ersatzweise Abflug) liegt mehr als
         `_FINAL_AFTER_S` zurück UND der Abflug ebenfalls — beides ist nötig,
         sonst friert ein verspäteter Flug ein (Soll-Ankunft vorbei, Abflug
         noch in der Zukunft), oder ein Langstreckenflug gilt sechs Stunden
         nach dem Start schon als gelandet.
      3. LH sagt „annulliert" und der Abflugtag ist vorbei — ein gestrichener
         Flug wird nicht nachträglich wieder gestrichen.
    Ohne Zeiten: False (dann bleibt es bei der alten, kurzen TTL — lieber
    einmal zu oft fragen als einen falschen Wert 14 Tage festschreiben)."""
    if not isinstance(facts, dict) or not facts:
        return False
    now = time.time() if now is None else now
    dep = _facts_dt(facts.get('est_dep') or facts.get('sched_dep'))
    arr = _facts_dt(facts.get('est_arr') or facts.get('sched_arr'))
    status = str(facts.get('dep_status') or '').lower()
    if arr is not None and now > arr.timestamp():
        if any(h in status for h in _FINAL_STATUS_HINTS):
            return True
    if facts.get('cancelled') and dep is not None and now > dep.timestamp():
        return True
    ref_end = arr or dep
    if ref_end is None:
        return False
    return (now > ref_end.timestamp() + _FINAL_AFTER_S
            and (dep is None or now > dep.timestamp() + _FINAL_AFTER_S))


def _facts_dt(val):
    """Offset-ISO aus den Fakten → aware datetime, sonst None. Ein NAIVER
    String (kein UTC in der LH-Antwort, s. `_offset_iso`) ist bewusst None:
    ohne Zone ist er nicht mit „jetzt" vergleichbar, und eine falsche Annahme
    wäre hier eine falsche TTL."""
    if not val:
        return None
    try:
        from datetime import datetime as _dt
        d = _dt.fromisoformat(str(val))
        return d if d.tzinfo is not None else None
    except Exception:
        return None


def _facts_ttl(date_str, facts, now=None):
    """Wie lange die Fakten eines Flugs gültig bleiben (Sekunden). Pure.

    Sobald Zeiten bekannt sind, entscheidet die UHR — nicht der Kalendertag:
      • vor dem Betriebsfenster (> 2 h vor Abflug) → bis zu 20 min (heute) bzw.
        6 h (anderer Tag), aber NIE über den Beginn des Fensters hinaus; der
        erste Read danach holt garantiert frische Gate-/Ist-Daten
      • nach dem Betriebsfenster (> 2 h nach Abflug UND Ankunft) → 30 min
        (heute) bzw. 6 h (anderer Tag)
      • im Fenster → 120 s (unverändert)
    Ohne Zeiten bleibt es beim alten, rein datumsbasierten Verhalten.

    Dass der Kalendertag NICHT mehr allein entscheidet, schliesst nebenbei das
    Mitternachts-Loch: ein Red-Eye mit Servicetag „gestern", der um 01:00 UTC
    in der Luft ist, bekam vorher 6 h TTL — jetzt die 120 s des Fensters.
    Und ein Flug, dessen SOLL-Ankunft vorbei ist, dessen Abflug sich aber
    verspätet hat, gilt erst als fertig, wenn AUCH der Abflug lange vorbei ist
    (adversarialer Review 27.07.: sonst hätte ein verspäteter Flug 30 min lang
    ein veraltetes Gate gezeigt)."""
    now = time.time() if now is None else now
    other_day = (date_str or '') != time.strftime('%Y-%m-%d', time.gmtime(now))
    if not isinstance(facts, dict) or not facts:
        return _TTL_OTHER_DAY if other_day else _TTL_OPS
    # FINAL schlägt alles: ein gelandeter Flug wird nie wieder nachgefragt.
    if _facts_final(facts, now):
        return _TTL_FINAL
    dep = _facts_dt(facts.get('est_dep') or facts.get('sched_dep'))
    arr = _facts_dt(facts.get('est_arr') or facts.get('sched_arr'))
    if dep is None and arr is None:
        return _TTL_OTHER_DAY if other_day else _TTL_OPS
    if dep is not None:
        lead = dep.timestamp() - _OPS_LEAD_S - now
        # `max(_TTL_OPS, …)`: kurz VOR dem Betriebsfenster ist der Rest-Abstand
        # winzig — ohne den Boden käme dort eine TTL von Sekunden heraus, also
        # MEHR Calls als vorher. Die Überschreitung ins Fenster hinein ist
        # höchstens so gross wie die Fenster-TTL selbst.
        if lead > 0:
            cap = _TTL_OTHER_DAY if other_day else _TTL_PLAN
            return max(_TTL_OPS, int(min(cap, lead)))
    # „Fertig" heisst: der SPÄTESTE bekannte Zeitpunkt liegt lange zurück —
    # und der Abflug auch. Beides ist nötig: nur die Ankunft zu prüfen fror
    # verspätete Flüge ein (Soll-Ankunft vorbei, Abflug noch in der Zukunft),
    # nur den Abflug zu prüfen hätte einen Langstreckenflug schon zwei Stunden
    # nach dem Start für fertig erklärt.
    ref_end = arr or dep
    if (ref_end is not None and now > ref_end.timestamp() + _OPS_TAIL_S
            and (dep is None or now > dep.timestamp() + _OPS_TAIL_S)):
        return _TTL_OTHER_DAY if other_day else _TTL_DONE
    return _TTL_OPS


def _norm_reg(reg):
    """'DAIKP' → 'D-AIKP' (heuristisch, verbreitete Präfixe). Unbekanntes bleibt
    unverändert. Nur kosmetisch — der Wert wird als tail durchgereicht."""
    r = (reg or '').upper().replace('-', '').strip()
    if not r:
        return reg
    for p in ('D', 'HB', 'OE', 'OO', '9H', 'I', 'G', 'F', 'EI', 'LX'):
        if r.startswith(p) and len(r) > len(p):
            return p + '-' + r[len(p):]
    return reg


# ── GETEILTER Fakten-Cache über alle Prozesse (Owner 2026-07-30) ────────────
# WARUM DAS PROZESS-MEMO ALLEIN NICHT REICHT — dieselbe Lehre wie beim
# Reg-Cache des MQTT-Daemons (27.07.), nur eine Ebene höher:
#   1. FRAGMENTIERUNG: 3 Backend-Worker + 1 Poll-Worker + MQTT-Daemon pflegen
#      je ihr eigenes `_facts_memo` — derselbe Flug wird bis zu 5× gekauft.
#   2. FLÜCHTIGKEIT: jedes Deploy und jeder gunicorn-Recycle löscht das Memo
#      komplett; der nächste Roster-Durchlauf kauft ALLES neu. Heute liefen
#      zwei Deploys — und die Stunden danach standen wieder am Deckel.
# Persistenz-Ebene ist `ax_paid_call_cache` (provider='lhopen') — dieselbe
# Tabelle, dieselbe Semantik, KEINE neue Tabelle und kein Migrations-Schritt.
#   lhfacts:<fn>:<date>:<dep>:<arr>   result={'facts':…,'answered':…}
#   lhwarm:<fn>:<date>:<dep>:<arr>    negative_until = Warm-Sperre
# LÜCKEN (503, Throttle-Abweisung) werden NIE als Fakten geschrieben — sonst
# gälte ein LH-Ausfall prozessübergreifend als Antwort.
_SHARED_PROVIDER = 'lhopen'
_SHARED_MIN_TTL = 60           # unter einer Minute lohnt der Roundtrip nicht


def _shared_sb():
    """Supabase-Client für den geteilten Cache oder None. Test-Seam.
    In der Test-Suite hart None (ein lokaler pytest-Lauf mit echten Creds
    dürfte NIE in den Produktions-Cache schreiben) — Tests setzen diese
    Funktion gezielt auf einen Fake."""
    if os.environ.get('PYTEST_CURRENT_TEST'):
        return None
    try:
        from app import sb, SB_AVAILABLE
        return sb if (SB_AVAILABLE and sb is not None) else None
    except Exception:
        return None


def _shared_key(kind, fn, d, dep, arr):
    return f'{kind}:{fn}:{d}:{dep or ""}:{arr or ""}'


def _shared_until(val, now_dt):
    """Rest-Sekunden bis `val` (ISO) oder 0. Zeitstempel wird GEPARST, nicht
    als String verglichen — lexikografisch stimmt nur, solange PostgREST exakt
    dieses Format liefert; ein 'Z' statt '+00:00' würde den Cache still
    komplett entwerten (Lehre aus `lh_mqtt._ts_after`)."""
    if not val:
        return 0
    try:
        from datetime import datetime as _dt, timezone as _tz
        d = _dt.fromisoformat(str(val).replace('Z', '+00:00'))
        if d.tzinfo is None:
            d = d.replace(tzinfo=_tz.utc)
        return max(0, int((d - now_dt).total_seconds()))
    except Exception:
        return 0


def _shared_read(fn, d, dep, arr, with_warm=False):
    """EIN Query für Fakten UND (optional) die Warm-Sperre.
    → {'facts': dict|None, 'answered': bool, 'ttl': int, 'warm_block': int}
    Wirft nie; bei jedem Problem einfach ein Miss (dann greift LH)."""
    out = {'facts': None, 'answered': False, 'ttl': 0, 'warm_block': 0}
    client = _shared_sb()
    if client is None:
        return out
    keys = [_shared_key('lhfacts', fn, d, dep, arr)]
    if with_warm:
        keys.append(_shared_key('lhwarm', fn, d, dep, arr))
    try:
        from datetime import datetime as _dt, timezone as _tz
        now_dt = _dt.now(_tz.utc)
        r = (client.table('ax_paid_call_cache')
             .select('call_key,result,result_until,negative_until')
             .in_('call_key', keys).execute())
        for row in (getattr(r, 'data', None) or []):
            k = str(row.get('call_key') or '')
            if k.startswith('lhfacts:'):
                left = _shared_until(row.get('result_until'), now_dt)
                res = row.get('result')
                if left > 0 and isinstance(res, dict):
                    out['facts'] = dict(res.get('facts') or {})
                    out['answered'] = bool(res.get('answered'))
                    out['ttl'] = left
            elif k.startswith('lhwarm:'):
                out['warm_block'] = _shared_until(row.get('negative_until'),
                                                  now_dt)
    except Exception as e:
        log.warning('[lh_open] shared-cache read: %s', type(e).__name__)
    return out


def _shared_write(rows):
    """Upsert in den geteilten Cache. Wirft nie."""
    client = _shared_sb()
    if client is None or not rows:
        return
    try:
        client.table('ax_paid_call_cache').upsert(
            rows, on_conflict='call_key').execute()
    except Exception as e:
        log.warning('[lh_open] shared-cache write: %s', type(e).__name__)


def _shared_write_facts(keys, facts, answered, ttl):
    """Fakten (inkl. Alias-Key) prozessübergreifend ablegen. `keys` sind
    (fn,date,dep,arr)-Tupel. Nur ANTWORTEN, nie Lücken."""
    if not answered or ttl < _SHARED_MIN_TTL:
        return
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    now_dt = _dt.now(_tz.utc)
    until = (now_dt + _td(seconds=int(ttl))).isoformat()
    _shared_write([{
        'call_key': _shared_key('lhfacts', *k),
        'provider': _SHARED_PROVIDER,
        'result': {'facts': dict(facts or {}), 'answered': True},
        'result_until': until,
        'negative_reason': None,
        'negative_until': None,
        'updated_at': now_dt.isoformat(),
    } for k in keys])


def _shared_write_warm_block(fn, d, dep, arr, ttl, reason):
    """Warm-Sperre für DIESEN Flug setzen — der einzige Zweck ist, dass ein
    frisch gestarteter Prozess (Deploy!) nicht sofort wieder gegen dieselbe
    Wand läuft. Wird NUR nach einem erfolglosen Warm geschrieben; ein
    erfolgreicher Warm sperrt sich über den Fakten-Eintrag selbst."""
    if ttl <= 0:
        return
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    now_dt = _dt.now(_tz.utc)
    _shared_write([{
        'call_key': _shared_key('lhwarm', fn, d, dep, arr),
        'provider': _SHARED_PROVIDER,
        'result': None,
        'result_until': None,
        'negative_reason': str(reason or 'warm')[:32],
        'negative_until': (now_dt + _td(seconds=int(ttl))).isoformat(),
        'updated_at': now_dt.isoformat(),
    }])


# ── Hintergrund-Warmup (Incident-Fix 2026-07-22, umgebaut 2026-07-30) ───────
# Fan-out-Consumer (cached_only=True) blockieren NIE auf LH-HTTP: Memo-Miss
# → {} sofort zurück + EIN dedupliziertes Warmup; der nächste Read innerhalb
# der TTL trifft dann den Memo.
#
# NEU: fester Worker-POOL statt Thread-pro-Warm. Zwei Gründe, beide gemessen:
#   • `supabase_threadlocal` hält pro Thread einen eigenen httpx-Client und
#     schließt ihn beim Thread-Ende NICHT (steht so im Docstring von
#     `_budget_thread_start`). Der geteilte Cache oben wird aus dem Warm-Pfad
#     gelesen — mit kurzlebigen `lh-warm`-Threads würde das Sockets lecken.
#   • Warms sind durch `_MIN_INTERVAL` ohnehin serialisiert; acht parallele
#     Threads haben nur den 0,9-s-Slot vor den User-Calls weggeschnappt.
_warm_lock = threading.Lock()
_warm_inflight = set()
_WARM_WORKERS = 2
_WARM_QUEUE_MAX = 256
_warm_q = None
_warm_started = False
# Sobald die Warm-Klasse global dicht ist, gilt das für den REST DER STUNDE —
# jeder weitere Versuch wäre garantiert eine Abweisung. Prozess-lokal reicht:
# alle Prozesse stellen das binnen Sekunden unabhängig fest.
_warm_gate_closed_hour = -1
# „Ein Flug pro Stunde" für SPEKULATIVE Warms (Owner-Vorgabe). Bewusst NICHT
# für Flüge im Betriebsfenster: deren 120-s-TTL ist der Grund, warum Gate- und
# Ist-Zeiten auf offenen Karten überhaupt frisch sind — die zu drosseln wäre
# genau die sichtbare Verschlechterung, die es nicht geben darf.
_warm_hour_ledger = {}
_WARM_LEDGER_MAX = 8000

# HORIZONT: die Aufrufer gaten bereits auf heute−1 … heute+2 (app.py
# `_lh_fill_obs_merged`). Ein engeres Fenster (die vorgeschlagenen −2 h…+36 h)
# würde die Roster-Ansicht für übermorgen ohne LH-Sollzeiten lassen = sichtbare
# Verschlechterung. Deshalb hier nur die Enden abschneiden, die NIEMAND ansieht:
# ein Flug, der über 8 h weg ist (und trotzdem nicht als final erkannt wurde),
# und alles jenseits von 60 h.
_WARM_TAIL_S = 8 * 3600
_WARM_LEAD_S = 60 * 3600


def _warm_horizon_ok(d, now=None):
    """Datums-Horizont für spekulative Warms: heute−1 … heute+2 (UTC). Pure."""
    now = time.time() if now is None else now
    try:
        from datetime import datetime as _dt
        dd = _dt.strptime(str(d)[:10], '%Y-%m-%d').date()
        t0 = _dt.utcfromtimestamp(now).date()
        return -1 <= (dd - t0).days <= 2
    except Exception:
        return False


def _warm_time_horizon_ok(facts, now=None):
    """Zeit-Horizont, sobald wir Zeiten KENNEN: nichts wärmen, was über 8 h
    vorbei oder über 60 h entfernt ist. Ohne Zeiten immer True. Pure."""
    if not isinstance(facts, dict) or not facts:
        return True
    now = time.time() if now is None else now
    dep = _facts_dt(facts.get('est_dep') or facts.get('sched_dep'))
    arr = _facts_dt(facts.get('est_arr') or facts.get('sched_arr'))
    ref = arr or dep
    if ref is None:
        return True
    if now > ref.timestamp() + _WARM_TAIL_S:
        return False
    ref_start = dep or arr
    return not (ref_start.timestamp() - now > _WARM_LEAD_S)


def _warm_skip(reason):
    """Nicht abgesetzte Warms sichtbar machen — aber in EIGENER Familie.
    `lhopen_denied` muss weiter genau eines heißen: „am Gate abgewiesen".
    Ein Skip ist das Gegenteil, nämlich eine Frage, die gar nicht erst
    gestellt wurde."""
    try:
        budget_inc('lhopen_skip', reason)
    except Exception:
        pass


def _warm_ledger_ok(key, now):
    """False, wenn dieser Flug in DIESER Stunde schon einmal spekulativ
    gewärmt wurde. Bucht den Versuch gleich mit."""
    hour = int(now // 3600)
    with _warm_lock:
        if _warm_hour_ledger.get(key) == hour:
            return False
        if len(_warm_hour_ledger) > _WARM_LEDGER_MAX:
            _warm_hour_ledger.clear()
        _warm_hour_ledger[key] = hour
        return True


def _warm_workers_start():
    global _warm_q, _warm_started
    if _warm_started:
        return
    with _warm_lock:
        if _warm_started:
            return
        import queue as _queue
        _warm_q = _queue.Queue(maxsize=_WARM_QUEUE_MAX)
        _warm_started = True
    for i in range(_WARM_WORKERS):
        threading.Thread(target=_warm_loop, daemon=True,
                         name=f'lh-warm-{i}').start()


def _warm_loop():
    while True:
        try:
            item = _warm_q.get()
        except Exception:
            return
        try:
            _warm_one(*item)
        except Exception:
            pass
        finally:
            with _warm_lock:
                _warm_inflight.discard(item[:4])


def _warm_one(fn, d, dep, arr, caller):
    """EIN Warm im Pool-Thread: geteilter Cache → LH. Wirft nie."""
    global _warm_gate_closed_hour
    now = time.time()
    shared = _shared_read(fn, d, dep, arr, with_warm=True)
    if shared['warm_block'] > 0:
        _warm_skip('shared_block')
        return
    if shared['facts'] is not None and shared['ttl'] > 0:
        # Ein anderer Prozess (oder derselbe vor dem Deploy) hat das schon
        # bezahlt — nur noch ins Prozess-Memo heben, KEIN LH-Call.
        with _facts_lock:
            _facts_memo[(fn, d, dep, arr)] = (now + shared['ttl'],
                                              dict(shared['facts']),
                                              shared['answered'])
        _warm_skip('shared_hit')
        return
    if not warm_gate_open(now):
        _warm_gate_closed_hour = int(now // 3600)
        _warm_skip('gate_closed')
        return
    lh_flight_facts(fn, d, dep, arr, caller=caller, shared_read=False)
    if last_call_denied():
        # Gate ist zu → für den Rest der Stunde gar nicht erst antreten, und
        # den Flug prozessübergreifend sperren, damit ein Deploy in dieser
        # Stunde nicht sofort dieselbe Salve neu startet.
        _warm_gate_closed_hour = int(time.time() // 3600)
        _shared_write_warm_block(fn, d, dep, arr,
                                 _seconds_to_next_hour(), 'budget')
    elif not last_call_answered():
        # LH kaputt (503/Timeout) — kurz sperren, aber nicht die ganze Stunde.
        _shared_write_warm_block(fn, d, dep, arr, 600, 'upstream')


def _seconds_to_next_hour(now=None):
    now = time.time() if now is None else now
    return int(3600 - (now % 3600)) + 5


def _warm_async(fn, d, dep, arr, caller=None):
    """Spekulativen Warm einreihen — oder begründet verwerfen. Wirft nie.

    Reihenfolge der Filter ist Absicht: die billigsten zuerst, damit ein
    geschlossenes Gate keinen Supabase-Read und keinen Thread mehr kostet."""
    key = (fn, d, dep, arr)
    now = time.time()
    lbl = f'warm_{caller}' if caller else 'warm'
    if not _warm_horizon_ok(d, now):
        _warm_skip('horizon')
        return
    if _warm_gate_closed_hour == int(now // 3600):
        _warm_skip('gate_closed')
        return
    # Was wir schon WISSEN, wird nicht nachgekauft: finale Fakten nie, und
    # nichts ausserhalb des Zeit-Horizonts.
    with _facts_lock:
        hit = _facts_memo.get(key)
    known = dict(hit[1]) if hit else None
    in_ops = False
    if known:
        if _facts_final(known, now):
            _warm_skip('final')
            return
        if not _warm_time_horizon_ok(known, now):
            _warm_skip('time_horizon')
            return
        in_ops = _facts_ttl(d, known, now) <= _TTL_OPS
    # Ein Flug pro Stunde — ausser er steht im Betriebsfenster (dort ist die
    # Frische das Produkt, s. Kommentar bei `_warm_hour_ledger`).
    if not in_ops and not _warm_ledger_ok(key, now):
        _warm_skip('dedup_hour')
        return
    _warm_workers_start()
    with _warm_lock:
        if key in _warm_inflight:
            return
        _warm_inflight.add(key)
    try:
        _warm_q.put_nowait((fn, d, dep, arr, lbl))
    except Exception:
        with _warm_lock:
            _warm_inflight.discard(key)
        _warm_skip('queue_full')


def lh_flight_facts(flight_no, date, dep_iata=None, arr_iata=None, force=False,
                    cached_only=False, caller=None, shared_read=True):
    """Autoritative LH-Group-Flug-Fakten (Shape wie _obs_rows_to_facts) oder {}.
    Gecacht pro (flight,date,dep,arr). No-op wenn nicht konfiguriert / kein
    LH-Group-Flug. Wirft nie. force=True überspringt den Memo-READ (schreibt
    ihn aber neu) — für den MQTT-Push-Pfad, der per Definition frischer als
    die TTL ist (Broker sagt „hat sich GERADE geändert"). cached_only=True:
    NIE blockieren — Memo-Hit liefert, Miss triggert nur ein Hintergrund-
    Warmup und gibt {} (Pflicht für Fan-out-Pfade wie _flight_obs_merged;
    Incident 2026-07-22: blockierende LH-Calls + Throttle-Sleeps im Request-
    Pfad legten alle Gunicorn-Worker lahm).
    shared_read=False überspringt NUR den Read aus dem prozessübergreifenden
    Cache (der Warm-Pfad hat ihn eine Zeile vorher schon gemacht) — geschrieben
    wird IMMER, sonst bezahlte ein Warm einen Call, von dem kein zweiter
    Prozess je profitiert."""
    fn = (flight_no or '').replace(' ', '').upper().strip()
    d = ((date or '').strip()[:10])
    if not fn or not d or not lh_open_configured() or not is_lh_group(fn):
        # Auch das ist eine Aussage über DIESEN Aufruf — sonst liest der
        # Aufrufer danach den Zustand eines fremden, früheren GETs.
        _note_answer(False)
        return {}
    dep = (dep_iata or '').upper().strip() or None
    arr = (arr_iata or '').upper().strip() or None
    key = (fn, d, dep, arr)
    now = time.time()
    if not force:
        with _facts_lock:
            hit = _facts_memo.get(key)
            if hit and now < hit[0]:
                # Ein Memo-Treffer trägt seine EIGENE Antwort-Eigenschaft mit
                # (3. Tupel-Element). Ohne die läse ein Aufrufer wie
                # `_fetch_leg_reg` den Zustand eines völlig anderen, längst
                # vergangenen GETs desselben Threads — und ein pauschales
                # `True` wäre noch schlimmer: ein aus einer LÜCKE (503,
                # Throttle-Abweisung) gecachtes `{}` würde damit zur
                # autoritativen Aussage „dieser Flug hat keine Reg" und landete
                # über `_legs_regs` sogar im GETEILTEN Cache
                # (adversarialer Review 27.07.).
                _note_answer(hit[2] if len(hit) > 2 else bool(hit[1]))
                return dict(hit[1])
    if cached_only:
        _warm_async(fn, d, dep, arr, caller)
        _note_answer(False)          # „noch nicht bekannt", kein Fakt
        return {}

    # GETEILTER Cache vor dem Netz: was ein anderer Prozess (oder derselbe vor
    # dem letzten Deploy) schon bezahlt hat, wird nicht zweimal gekauft. Das
    # ist der Pfad, der die 26-fache Wiederholung pro Leg auflöst. Der Read
    # kostet ~40 ms gegen 0,9 s Throttle-Spacing + LH-RTT — und vor allem
    # KEINEN Quota-Slot. `force` (MQTT-Push: „hat sich GERADE geändert")
    # überspringt ihn bewusst.
    if shared_read and not force:
        _sh = _shared_read(fn, d, dep, arr)
        if _sh['facts'] is not None and _sh['ttl'] > 0:
            with _facts_lock:
                _facts_memo[key] = (now + _sh['ttl'], dict(_sh['facts']),
                                    _sh['answered'])
            _note_answer(_sh['answered'])
            return dict(_sh['facts'])

    data = _get(f'/operations/flightstatus/{urllib.parse.quote(fn)}/{d}',
                caller=caller)
    facts = {}
    try:
        legs = (((data or {}).get('FlightStatusResource') or {})
                .get('Flights') or {}).get('Flight')
        if isinstance(legs, dict):
            legs = [legs]
        legs = legs or []
        chosen = None
        for lg in legs:
            lf = (lg.get('Departure') or {}).get('AirportCode')
            lt = (lg.get('Arrival') or {}).get('AirportCode')
            if dep and arr:
                if lf == dep and lt == arr:
                    chosen = lg
                    break
            elif dep:
                if lf == dep:
                    chosen = lg
                    break
            else:
                # MEHR-LEG-GUARD (Tail-Audit 22.07.): ohne dep/arr bei einem
                # Multi-Sektor-Flug (4Y136 FRA-MBA-JRO) wäre „erstes Leg"
                # geraten — falsche Reg/Zeiten für Flugnummer-only-Caller.
                # Ein-Leg-Flüge (der Normalfall) bleiben unverändert.
                chosen = lg if len(legs) == 1 else None
                break
        if chosen is not None:
            facts = _leg_to_facts(chosen)
    except Exception as e:
        log.warning('[lh_open] parse %s/%s: %s', fn, d, type(e).__name__)
        facts = {}

    # TTL nach Abflugnähe (s. `_facts_ttl`): im Betriebsfenster kurz
    # (Gate-Wechsel/Ist-Zeiten), weit davor bzw. lange danach länger.
    # LEER OHNE ANTWORT ist eine LÜCKE, kein Fakt (Throttle-Abweisung, 503) —
    # die darf NIE mit einer langen TTL zementiert werden, sonst gilt der
    # Ausfall stundenlang weiter (adversarialer Review 27.07.).
    answered = last_call_answered()
    ttl = _facts_ttl(d, facts, now) if (facts or answered) else _TTL_OPS
    # CACHE-FRAGMENTIERUNG (2026-07-26): der MQTT-Push-Pfad ruft ohne dep/arr
    # (Key (fn,date,None,None)), die Roster-/Detail-Pfade IMMER mit
    # (fn,date,dep,arr). Derselbe Flug belegte dadurch zwei Einträge und der
    # frisch bezahlte Push-Call wärmte den Key nicht, den die App danach liest.
    # Der Alias-Write kostet nichts und macht jeden Call doppelt nutzbar.
    _alias = None
    if (dep is None or arr is None) and isinstance(facts, dict):
        _fd = (facts.get('dep_iata') or '').upper().strip() or None
        _fa = (facts.get('arr_iata') or '').upper().strip() or None
        if _fd and _fa and (_fd, _fa) != (dep, arr):
            _alias = (fn, d, _fd, _fa)
    with _facts_lock:
        _facts_memo[key] = (now + ttl, dict(facts), answered)
        if _alias is not None:
            _facts_memo[_alias] = (now + ttl, dict(facts), answered)
        if len(_facts_memo) > _FACTS_MAX:
            items = sorted(_facts_memo.items(), key=lambda kv: kv[1][0])
            for k, _v in items[:len(items) // 4 or 1]:
                _facts_memo.pop(k, None)
    # …und dieselbe Antwort mit derselben TTL prozessübergreifend ablegen.
    # Dieselbe TTL ist Pflicht: wäre der geteilte Cache je nach Prozess länger
    # oder kürzer gültig als das lokale Memo, hinge die Frische der Fakten
    # davon ab, welcher Worker die Anfrage bekommt (Lehre aus
    # `lh_mqtt._reg_cache_write`).
    _shared_write_facts([key] + ([_alias] if _alias is not None else []),
                        facts, answered, ttl)
    return facts


@lh_open_bp.route('/api/lh/flight/<flight>/<date>', methods=['GET'])
def lh_flight_debug(flight, date):
    """Diagnose: die autoritativen LH-Fakten für einen Flug (kein Secret, keine
    PII). Zeigt configured/is_group-Status, damit man die Verdrahtung prüfen
    kann. Optional ?dep=FRA&arr=MBA für die Leg-Wahl."""
    from flask import request
    dep = request.args.get('dep')
    arr = request.args.get('arr')
    return jsonify({
        'configured': lh_open_configured(),
        'is_lh_group': is_lh_group(flight),
        'flight': flight, 'date': date,
        'facts': lh_flight_facts(flight, date, dep, arr, caller='debug'),
    })
