# ═══════════════════════════════════════════════════════════════
#  Poll-Scheduler — adaptiver Takt für /api/internal/poll-boards (?tier=auto)
#
#  Owner-Ziel (2026-07-09): „Boards öfter pollen, aber smart statt pauschal."
#  Der Hetzner-Cron feuert poll-boards künftig JEDE Minute mit ?tier=auto;
#  DIESER Scheduler entscheidet pro Airport, ob er im aktuellen Tick dran ist.
#
#  Takt-Matrix (Minuten zwischen zwei Polls desselben Airports):
#    · 1   Hub-Event: FRA/MUC im Event-Fenster (s.u.) — der Cron feuert eh
#          minütlich, die Hubs bekommen dann JEDEN Tick
#    · 3   Event-Fenster: ±45 min um sched_dep/sched_arr eines NACHGEFRAGTEN
#          Flugs an diesem Airport (Roster-Leg eines Users)
#    · 5   nachgefragt: Airport ist dep/arr eines Roster-Legs in now±3h,
#          oder FRA/MUC (immer nachgefragt)
#    · 10  Default — entspricht exakt dem heutigen 10-min-Cron
#    · 30  Nacht (0–5 Uhr Airport-LOKALZEIT) — übersteuert 5/10, aber NICHT
#          das Event-Fenster (Red-Eye eines Users braucht trotzdem Daten)
#    · SKIP Quiet-Gate: KEINE geplante Bewegung in [now−1h, now+2h] (eigene
#          airport_delay_obs-Scheds des Tages, ~15-min-Memo) → Airport wird
#          komplett geskippt; Re-Check spätestens nach 60 min (Sicherheits-
#          netz). Event-Fenster/Roster-Events übersteuern IMMER; kalte
#          Airports (noch keine Rows heute) werden NIE geskippt (Henne-Ei).
#          FRA-Nachtflugverbot 23–5 → stundenlanger Skip ist dort GEWOLLT.
#
#  Demand-Quelle = user_ical_briefings.raw_event->ical_sectors über ALLE User
#  (EIN Supabase-Query pro ~10 min, siehe get_demand) — bewusst NICHT
#  get_briefings pro User (das würde je Token den Kalender-Feed refreshen).
#  Freunde-/Watch-Flüge fließen NICHT ein: es gibt dafür keinen billigen
#  Sammel-Query (das Watch-Set lebt pro Device/In-Memory; family_shares
#  bräuchte N Roster-Reads). Deren Airports sind praktisch immer durch die
#  Roster-Legs aller User + FRA/MUC abgedeckt — dokumentierter Trade-off.
#
#  ZUSTAND = In-Process-Memos (last-poll pro Airport, Demand-Memo, Row-Hashes).
#  Das ist bewusst OK: es gibt genau EINEN Poll-Container (Hetzner-Cron → ein
#  Backend). Laufen dort mehrere gunicorn-Worker, hat jeder Worker sein eigenes
#  Memo → der effektive Takt kann bis Worker-Anzahl-fach über der Matrix liegen;
#  das deckelt nur die Request-Rate nach oben, die SB-Writes deckelt zusätzlich
#  Write-on-change (obs_write_needed). Restart → leere Memos → ein voller Tick
#  bzw. normale Writes (defensiv, nie Datenverlust).
# ═══════════════════════════════════════════════════════════════

import hashlib
import time
from datetime import datetime, timedelta, timezone

# ── Takt-Matrix (Minuten) ─────────────────────────────────────────────────────
TICK_HUB_EVENT_MIN = 1
TICK_EVENT_MIN = 3
TICK_DEMAND_MIN = 5
TICK_DEFAULT_MIN = 10
TICK_NIGHT_MIN = 30

EVENT_WINDOW_MIN = 45        # ±45 min um sched_dep/sched_arr
DEMAND_HORIZON_MIN = 180     # Roster-Legs in now±3h erzeugen Nachfrage
NIGHT_START_H, NIGHT_END_H = 0, 5   # [0, 5) Uhr Airport-Lokalzeit

# FRA + MUC sind IMMER nachgefragt (Brief) — unabhängig vom Roster-Stand.
ALWAYS_DEMAND = frozenset({'FRA', 'MUC'})
# … und bekommen im Event-Fenster den 1-min-Takt (jeder Cron-Tick).
HUB_1MIN = frozenset({'FRA', 'MUC'})

# Quiet-Gate: Bewegungs-Fenster [now−back, now+fwd] Airport-LOKALZEIT.
QUIET_BACK_MIN = 60
QUIET_FWD_MIN = 120
QUIET_RECHECK_MIN = 60       # Sicherheitsnetz: spätestens dann trotz quiet pollen

# Cron-Jitter-Toleranz: der Cron feuert minütlich, aber nie sekundengenau.
# Ohne Toleranz würde ein 3-min-Intervall bei 179.x s Abstand auf 4 min
# rutschen — 30 s Toleranz hält die Matrix-Takte stabil. Für den 1-min-Takt
# ist die Schwelle damit 60−30 = 30 s: jeder Minuten-Tick pollt (auch bei
# 55-s-Jitter), aber zwei Aufrufe in derselben Minute (<30 s) doppel-pollen
# NICHT.
_JITTER_TOLERANCE_S = 30


def parse_iso_utc(s):
    """Toleranter ISO-Parser → aware-UTC-datetime | None. dep_iso/arr_iso aus
    ical_sectors sind echt-UTC ('Z' oder Offset); naive Strings werden als UTC
    gelesen (gleiche Konvention wie der Roster-Import)."""
    if not s or not isinstance(s, str):
        return None
    try:
        dt = datetime.fromisoformat(s.strip().replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ── PURE Takt-Regeln ──────────────────────────────────────────────────────────

def poll_interval_min(demanded, in_event_window, local_hour, is_hub=False):
    """PURE: Minuten-Intervall für einen Airport in diesem Tick.
    Präzedenz: Event-Fenster schlägt ALLES (auch Nacht — der nachgefragte Flug
    findet ja gerade statt), Hubs (FRA/MUC) dann mit 1 min = jeder Cron-Tick;
    Nacht übersteuert danach Demand/Default auf 30."""
    if in_event_window:
        return TICK_HUB_EVENT_MIN if is_hub else TICK_EVENT_MIN
    if NIGHT_START_H <= local_hour < NIGHT_END_H:
        return TICK_NIGHT_MIN
    if demanded:
        return TICK_DEMAND_MIN
    return TICK_DEFAULT_MIN


def in_event_window(now_utc, event_times, window_min=EVENT_WINDOW_MIN):
    """PURE: liegt `now_utc` in ±window_min um eine der Event-Zeiten?"""
    if not event_times:
        return False
    w = timedelta(minutes=window_min)
    return any(t is not None and abs(now_utc - t) <= w for t in event_times)


def demand_from_sectors(sectors, now_utc, horizon_min=DEMAND_HORIZON_MIN):
    """PURE: aus Roster-Sektoren (ical_sectors-Form: flight/from/to/dep_iso/
    arr_iso) → (demanded: set[IATA], events: dict[IATA → [utc-datetime]]).
    Ein Leg zählt, wenn dep ODER arr in now±horizon liegt; dann sind BEIDE
    Seiten nachgefragt (wir brauchen Abflug- UND Ankunfts-Board). Event-Zeiten:
    sched_dep am from-Airport, sched_arr am to-Airport."""
    demanded = set()
    events = {}
    h = timedelta(minutes=horizon_min)
    for s in (sectors or []):
        if not isinstance(s, dict):
            continue
        dep = parse_iso_utc(s.get('dep_iso'))
        arr = parse_iso_utc(s.get('arr_iso'))
        relevant = ((dep is not None and abs(dep - now_utc) <= h)
                    or (arr is not None and abs(arr - now_utc) <= h))
        if not relevant:
            continue
        frm = (s.get('from') or '').strip().upper()
        to = (s.get('to') or '').strip().upper()
        if len(frm) == 3:
            demanded.add(frm)
            if dep is not None:
                events.setdefault(frm, []).append(dep)
        if len(to) == 3:
            demanded.add(to)
            if arr is not None:
                events.setdefault(to, []).append(arr)
    return demanded, events


def airports_due(airports, now_utc, demanded, events, last_poll, local_hour_of,
                 quiet=frozenset()):
    """PURE (bis auf das injizierte `local_hour_of(iata) → 0..23`): welche
    Airports sind DIESEN Tick fällig? `last_poll` = dict[IATA → unix-ts des
    letzten tatsächlichen Polls]; fehlender Eintrag (Restart) → sofort fällig.
    `quiet` = Airports ohne geplante Bewegung (Quiet-Gate) → komplett skippen;
    Event-Fenster übersteuert das immer (Belt&Braces — Event-Airports landen
    via exclude normalerweise gar nicht erst im quiet-Set)."""
    now_ts = now_utc.timestamp()
    due = []
    for ap in airports:
        in_ev = in_event_window(now_utc, events.get(ap))
        if ap in quiet and not in_ev:
            continue
        is_demanded = ap in demanded or ap in ALWAYS_DEMAND
        try:
            lh = int(local_hour_of(ap))
        except Exception:
            lh = 12  # unbekannte TZ → konservativ „Tag" (nie fälschlich 30 min)
        iv = poll_interval_min(is_demanded, in_ev, lh, is_hub=ap in HUB_1MIN)
        lp = last_poll.get(ap)
        # Floor auf die Jitter-Toleranz hält den 1-min-Takt robust: jeder
        # ~60-s-Cron-Tick pollt, gleiche-Minute-Doppelaufrufe (<30 s) nicht.
        threshold_s = max(iv * 60 - _JITTER_TOLERANCE_S, _JITTER_TOLERANCE_S)
        if lp is None or (now_ts - lp) >= threshold_s:
            due.append(ap)
    return due


# ── Demand-Memo (impure Hülle, EIN SB-Query pro TTL) ─────────────────────────

_DEMAND_TTL_S = 600  # ~10 min — Roster ändern sich selten, ±3h-Fenster ist grob
_DEMAND_MEMO = {'ts': 0.0, 'demanded': frozenset(ALWAYS_DEMAND), 'events': {}}


def _fetch_sector_rows(sb, dates):
    """Query über alle User: datum ∈ {gestern, heute, morgen} (UTC-Keying
    des Roster-Imports; das ±3h-Fenster kann Mitternacht kreuzen).
    PAGINIERT: PostgREST kappt still bei 1000 Rows — live gemessen 2026-07-22
    waren es 3682 Rows im Fenster, d.h. ohne range() fehlte der Demand der
    meisten User (Airports wurden fälschlich als „ohne Bedarf" behandelt)."""
    out = []
    page = 1000
    for start in range(0, 40000, page):
        r = (sb.table('user_ical_briefings')
             .select('datum,raw_event')
             .in_('datum', dates)
             .range(start, start + page - 1).execute())
        rows = r.data or []
        out.extend(rows)
        if len(rows) < page:
            break
    return out


def get_demand(sb, now_utc):
    """(demanded, events) mit ~10-min-Memo. sb=None/SB-Fehler → letzter Stand
    weiterverwenden (mind. FRA/MUC); ts wird auch bei Fehler gesetzt, damit ein
    SB-Ausfall nicht jede Minute einen neuen Query hämmert."""
    now_mono = time.time()
    if now_mono - _DEMAND_MEMO['ts'] < _DEMAND_TTL_S:
        return _DEMAND_MEMO['demanded'], _DEMAND_MEMO['events']
    demanded = set(ALWAYS_DEMAND)
    events = {}
    if sb is not None:
        try:
            dates = [(now_utc.date() + timedelta(days=d)).isoformat()
                     for d in (-1, 0, 1)]
            sectors = []
            for row in _fetch_sector_rows(sb, dates):
                raw = row.get('raw_event') or {}
                secs = raw.get('ical_sectors') if isinstance(raw, dict) else None
                if isinstance(secs, list):
                    sectors.extend(secs)
            d2, events = demand_from_sectors(sectors, now_utc)
            demanded |= d2
        except Exception:
            # SB down/Schema fehlt → alter Stand (falls vorhanden) für eine
            # weitere TTL; FRA/MUC bleiben so oder so nachgefragt.
            demanded = set(_DEMAND_MEMO['demanded']) | set(ALWAYS_DEMAND)
            events = _DEMAND_MEMO['events']
    _DEMAND_MEMO['ts'] = now_mono
    _DEMAND_MEMO['demanded'] = frozenset(demanded)
    _DEMAND_MEMO['events'] = events
    return _DEMAND_MEMO['demanded'], _DEMAND_MEMO['events']


# ── Quiet-Gate: keine geplante Bewegung → Airport komplett skippen ────────────
# Owner: „viele Airports machen zu — stundenlang muss nichts gescrapped werden."
# Billigster Pfad: die EIGENEN airport_delay_obs-Scheds des Tages (dep-Seite +
# '#ARR'-Seite) per limit(1)-Existenz-Query, gecacht im ~15-min-Memo pro
# Airport — das Gate selbst erzeugt so praktisch keine Query-Last.

_QUIET_TTL_S = 900        # ~15-min-Memo pro Airport (Probe-Ergebnis)
_QUIET_MEMO = {}          # IATA → {'ts': unix, 'quiet': bool}
_QUIET_SKIP_SINCE = {}    # IATA → unix-ts, seit wann das Gate durchgehend skippt


def movement_window_segments(local_now, back_min=QUIET_BACK_MIN,
                             fwd_min=QUIET_FWD_MIN):
    """PURE: Bewegungs-Fenster [now−back, now+fwd] (naive Airport-LOKALZEIT,
    gleiche Basis wie die sched-Strings der Boards) als (datum, von, bis)-
    Segmente in 'HH:MM' — lexikographisch = chronologisch, also direkt als
    gte/lte-Filter nutzbar. Kreuzt das Fenster Mitternacht → zwei Segmente."""
    lo = local_now - timedelta(minutes=back_min)
    hi = local_now + timedelta(minutes=fwd_min)
    if lo.date() == hi.date():
        return [(lo.date().isoformat(), lo.strftime('%H:%M'),
                 hi.strftime('%H:%M'))]
    return [(lo.date().isoformat(), lo.strftime('%H:%M'), '23:59'),
            (hi.date().isoformat(), '00:00', hi.strftime('%H:%M'))]


def roster_active_airports(events, now_utc, back_min=QUIET_BACK_MIN,
                           fwd_min=QUIET_FWD_MIN):
    """PURE: Airports mit Roster-Event (sched_dep/arr eines NACHGEFRAGTEN Flugs)
    im Bewegungs-Fenster — die übersteuern das Quiet-Gate IMMER (Owner-Regel b)
    und decken auch Flüge ab, die noch auf keinem Board stehen (Outstation)."""
    back = timedelta(minutes=back_min)
    fwd = timedelta(minutes=fwd_min)
    return {ap for ap, ts in (events or {}).items()
            if any(t is not None and (now_utc - back) <= t <= (now_utc + fwd)
                   for t in ts)}


def _probe_quiet(sb, ap, local_now):
    """IMPURE: 1–3 winzige limit(1)-Queries gegen airport_delay_obs (dep- und
    '#ARR'-Seite). True = quiet: es EXISTIEREN Rows des Tages (Airport ist
    „warm"), aber KEINE sched im Bewegungs-Fenster. Kalt (gar keine Rows
    heute) → False, sonst würde ein nie gescrapter Airport verhungern
    (Henne-Ei: erst der Poll erzeugt die Rows, die das Gate liest)."""
    keys = [ap, ap + '#ARR']
    for d, lo, hi in movement_window_segments(local_now):
        r = (sb.table('airport_delay_obs').select('sched')
             .in_('airport', keys).eq('date', d)
             .gte('sched', lo).lte('sched', hi).limit(1).execute())
        if r.data:
            return False  # geplante Bewegung im Fenster → nicht quiet
    r = (sb.table('airport_delay_obs').select('sched')
         .in_('airport', keys).eq('date', local_now.date().isoformat())
         .limit(1).execute())
    return bool(r.data)


def get_quiet_airports(sb, airports, local_now_of, exclude=frozenset()):
    """Quiet-Set dieses Ticks mit ~15-min-Memo pro Airport. sb=None → leeres
    Set (ohne Datenquelle NIE skippen). `exclude` (Event-/Roster-aktive
    Airports) wird gar nicht erst geprobed. Sicherheitsnetz gegen Fahrplan-
    Lücken: nach 60 min durchgehendem Skip wird EIN Poll erlaubt (der
    refresht die obs-Rows, die das Gate liest); rückt die nächste bekannte
    Bewegung <2h heran, öffnet der 15-min-Re-Probe das Gate ohnehin früher."""
    if sb is None:
        return set()
    quiet = set()
    now_mono = time.time()
    for ap in airports:
        if ap in exclude:
            _QUIET_SKIP_SINCE.pop(ap, None)
            continue
        memo = _QUIET_MEMO.get(ap)
        if memo is None or now_mono - memo['ts'] >= _QUIET_TTL_S:
            try:
                q = _probe_quiet(sb, ap, local_now_of(ap))
            except Exception:
                q = False  # SB-Zicken/TZ-Fehler → nie fälschlich aushungern
            memo = {'ts': now_mono, 'quiet': q}
            _QUIET_MEMO[ap] = memo
        if not memo['quiet']:
            _QUIET_SKIP_SINCE.pop(ap, None)
            continue
        since = _QUIET_SKIP_SINCE.setdefault(ap, now_mono)
        if now_mono - since >= QUIET_RECHECK_MIN * 60:
            _QUIET_SKIP_SINCE[ap] = now_mono  # EIN Poll erlaubt, Zähler neu
            continue
        quiet.add(ap)
    return quiet


# ── Per-Airport last-poll (In-Process, ein Poll-Container — s. Kopfkommentar) ─

_LAST_POLL = {}  # IATA → unix-ts des letzten Polls DIESES Prozesses


def select_due_airports(airports, sb, local_now_of, now_utc=None):
    """Haupteinstieg für den Endpoint: fällige Airports dieses Ticks bestimmen
    UND als gepollt markieren. `local_now_of(iata)` → naive Airport-Lokalzeit
    als VOLLE datetime (Stunde für die Nacht-Regel, Datum+Zeit fürs Quiet-
    Gate). Rückgabe (due, diag) — diag fürs Response-JSON."""
    now_utc = now_utc or datetime.now(timezone.utc)
    demanded, events = get_demand(sb, now_utc)
    # Quiet-Gate: Event-/Roster-aktive Airports gar nicht erst proben.
    active = roster_active_airports(events, now_utc)
    quiet = get_quiet_airports(sb, airports, local_now_of, exclude=active)
    due = airports_due(airports, now_utc, demanded, events, _LAST_POLL,
                       lambda ap: local_now_of(ap).hour, quiet=quiet)
    ts = now_utc.timestamp()
    for ap in due:
        _LAST_POLL[ap] = ts
    diag = {
        'demanded': sorted(demanded),
        'event_airports': sorted(events.keys()),
        'quiet': sorted(quiet),
        'due_count': len(due),
        'skipped_count': len(airports) - len(due),
    }
    return due, diag


# ── Write-on-change: Content-Hash-Memo für airport_delay_obs-Rows ────────────
# Der höhere Board-Takt (bis 3 min statt 10) darf die Supabase-Writes nicht
# linear multiplizieren. Pro Row-Key (date, airport, flight, sched) merken wir
# den Hash der OPERATIVEN Felder des letzten ERFOLGREICHEN Writes — unverändert
# → Write skippen (Row steht schon exakt so in SB). Leeres Memo (Restart) →
# alles gilt als geändert → normal schreiben (defensiv, nie Datenverlust).

_OBS_HASH_MEMO = {}
_OBS_HASH_MAX = 60000  # ~alle EU-Boards × Tage; drüber → clear (defensiv=writes)
_OBS_OPERATIVE_FIELDS = ('max_delay_min', 'cancelled', 'status', 'dest_iata',
                         'dest_name', 'gate', 'terminal', 'airline', 'esti',
                         'reg', 'type_code', 'source')


def obs_row_key(payload):
    return (payload.get('date'), payload.get('airport'),
            payload.get('flight'), payload.get('sched'))


def obs_content_hash(payload):
    """PURE: stabiler Hash über die operativen Felder (updated_at zählt NICHT —
    sonst wäre jede Row immer „geändert")."""
    parts = '|'.join(repr(payload.get(k)) for k in _OBS_OPERATIVE_FIELDS)
    return hashlib.sha1(parts.encode('utf-8')).hexdigest()


def obs_write_needed(payload):
    """True = Row-Inhalt weicht vom letzten erfolgreichen Write ab (oder ist
    unbekannt, z.B. nach Restart) → schreiben."""
    return _OBS_HASH_MEMO.get(obs_row_key(payload)) != obs_content_hash(payload)


def obs_mark_written(payload):
    """NACH erfolgreichem SB-Write rufen — ein gescheiterter Write darf nie als
    erledigt gelten (sonst ginge die Row bis zur nächsten Änderung verloren)."""
    if len(_OBS_HASH_MEMO) > _OBS_HASH_MAX:
        _OBS_HASH_MEMO.clear()
    _OBS_HASH_MEMO[obs_row_key(payload)] = obs_content_hash(payload)
