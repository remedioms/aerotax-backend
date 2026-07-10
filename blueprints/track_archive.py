"""
Track-VERDICHTUNG (Permanenz-Plan (c), docs/data-permanence-plan.md).

aircraft_track wächst ~1 M Breadcrumbs/Tag und wird nach TRACK_RETENTION_DAYS
gelöscht — die EINZIGE Stelle, an der wir Daten endgültig verlieren. Dieses
Modul verdichtet jeden Flug VOR dem Prune zu einer kompakten Polyline
(Douglas-Peucker, ≤80 Punkte) und archiviert sie dauerhaft in
`flight_tracks_archive` (PK (reg, service_date, flight), Upsert = idempotent).

Aufbau (bewusst Flask-frei — der Endpoint lebt in aerox_data_blueprint.py):
  • pure Funktionen (unit-testbar, keine DB): `douglas_peucker_indices`,
    `simplify_track`, `split_legs`, `compact_leg`
  • Orchestrator `run_compact(sb, mark_get, mark_set, …)` — arbeitet
    Tag-für-Tag von der Watermark ('trackarch:until' im ax_api_budget-KV)
    bis heute−(RETENTION−2); pro Tag Reg-Cursor über das Archiv selbst
    (max bereits archiviertes reg), damit ein abgebrochener Lauf beim
    nächsten Mal exakt weitermacht. Batch-Limit ~200 Legs pro Lauf.

Leg-Erkennung = dasselbe Muster wie `_flown_track_db` im flown-track-Endpoint:
Split bei Zeitlücke > 45 min; zusätzlich beendet `on_ground=true` das Leg
(Boden-Breadcrumbs gehören zu keinem Leg — kein Geister-Flieger im Archiv).
"""
import itertools
import math
import time

# ── Konstanten (Plan (c), Schritt 2+3) ───────────────────────────────────────
GAP_SEC = 45 * 60          # Leg-Split bei Lücke > 45 min (wie _flown_track_db)
MAX_POINTS = 80            # Ziel-Punktzahl nach Douglas-Peucker
START_EPS_DEG = 0.005      # Start-Epsilon ~500 m; adaptiv verdoppeln
MIN_LEG_POINTS = 5         # kürzere Fragmente sind Rausch, kein Leg
DAY_SEC = 86400


def _iso_to_epoch(s):
    """ISO-8601 (mit +00:00 oder Z) → Epoch-Sekunden (int) | None."""
    if not s:
        return None
    try:
        from datetime import datetime, timezone
        return int(datetime.fromisoformat(str(s).replace('Z', '+00:00'))
                   .astimezone(timezone.utc).timestamp())
    except Exception:
        return None


def _epoch_to_iso(e):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(int(e)))


def _day_floor(epoch):
    """Epoch → Epoch der UTC-Tagesgrenze (00:00Z)."""
    return int(epoch) - (int(epoch) % DAY_SEC)


# ── Douglas-Peucker (pure, unit-getestet) ────────────────────────────────────

def _perp_dist(p, a, b):
    """Senkrechter Abstand Punkt p zum SEGMENT a→b, alles (lat, lon) in Grad.
    Bewusst planare Näherung im Grad-Raum (Plan: „Douglas-Peucker auf
    (lat, lon)", Epsilon in Grad) — für Formtreue reicht das völlig."""
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def douglas_peucker_indices(pts, eps):
    """Douglas-Peucker über (lat, lon)-Tupel → SORTIERTE Index-Liste der
    behaltenen Punkte. Iterativ (expliziter Stack) statt rekursiv — 4000-Punkte-
    Trails würden sonst im Degenerat-Fall die Rekursionstiefe sprengen.
    Endpunkte bleiben IMMER erhalten."""
    n = len(pts)
    if n <= 2:
        return list(range(n))
    keep = [False] * n
    keep[0] = keep[n - 1] = True
    stack = [(0, n - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        dmax, imax = -1.0, -1
        a, b = pts[i], pts[j]
        for k in range(i + 1, j):
            d = _perp_dist(pts[k], a, b)
            if d > dmax:
                dmax, imax = d, k
        if dmax > eps and imax > 0:
            keep[imax] = True
            stack.append((i, imax))
            stack.append((imax, j))
    return [i for i in range(n) if keep[i]]


def simplify_track(pts, max_points=MAX_POINTS, start_eps=START_EPS_DEG):
    """Adaptive Verdichtung: Epsilon verdoppeln, bis ≤ max_points übrig sind
    (Plan (c) Schritt 3). Fallback: gleichmäßiges Downsampling mit erhaltenen
    Endpunkten (garantiert die Obergrenze auch bei pathologischen Zickzacks).
    → sortierte Index-Liste in die Eingabe."""
    n = len(pts)
    if n <= max_points:
        return list(range(n))
    eps = float(start_eps)
    idx = douglas_peucker_indices(pts, eps)
    for _ in range(16):
        if len(idx) <= max_points:
            return idx
        eps *= 2.0
        idx = douglas_peucker_indices(pts, eps)
    if len(idx) <= max_points:
        return idx
    # Harter Fallback: uniform aus den DP-Überlebenden samplen, Enden behalten.
    step = (len(idx) - 1) / float(max_points - 1)
    out = sorted({idx[int(round(k * step))] for k in range(max_points)})
    if out[0] != idx[0]:
        out.insert(0, idx[0])
    if out[-1] != idx[-1]:
        out.append(idx[-1])
    return out[:max_points]


# ── Gap-/Boden-Gruppierung (pure, unit-getestet) ─────────────────────────────

def split_legs(rows, gap_sec=GAP_SEC):
    """Breadcrumb-Rows EINER reg (aufsteigend nach ts sortiert) → Liste von
    Legs (Row-Listen). Split bei Zeitlücke > gap_sec (wie _flown_track_db)
    ODER on_ground=true — der Boden-Punkt selbst gehört zu KEINEM Leg.
    Rows: Dicts mit 'ts' (Epoch|None) und optional 'on_ground'."""
    legs, cur, prev = [], [], None
    for r in rows:
        ts = r.get('ts')
        if r.get('on_ground'):
            if cur:
                legs.append(cur)
                cur = []
            if ts is not None:
                prev = ts
            continue
        if cur and prev is not None and ts is not None and ts - prev > gap_sec:
            legs.append(cur)
            cur = []
        cur.append(r)
        if ts is not None:
            prev = ts
    if cur:
        legs.append(cur)
    return legs


def _mode(values):
    """Häufigster nicht-leerer Wert | None."""
    counts = {}
    for v in values:
        if v:
            counts[v] = counts.get(v, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]


def compact_leg(reg, rows, max_points=MAX_POINTS):
    """EIN Leg (Rows einer reg, ts-sortiert, mit lat/lon/ts) → Archiv-Row-Dict
    für flight_tracks_archive, oder None (zu kurz / unbrauchbar).
    points-Format wie im Plan: [[epoch, lat, lon, alt_ft, gs_kt], …],
    lat/lon auf 4 Dezimalen (~11 m) gerundet."""
    pts = [r for r in rows
           if r.get('lat') is not None and r.get('lon') is not None
           and r.get('ts') is not None]
    if len(pts) < MIN_LEG_POINTS:
        return None
    latlon = [(float(r['lat']), float(r['lon'])) for r in pts]
    idx = simplify_track(latlon, max_points=max_points)
    kept = [pts[i] for i in idx]
    service_date = time.strftime('%Y-%m-%d', time.gmtime(int(pts[0]['ts'])))
    flight = _mode([(r.get('flight') or '').strip().upper() for r in pts])
    dep = _mode([(r.get('origin') or '').strip().upper() for r in pts])
    arr = _mode([(r.get('dest') or '').strip().upper() for r in pts])
    if not flight:
        # PK-Spalte darf nicht leer kollidieren: Stadt-Paar als Fallback-Key,
        # damit zwei route-bekannte Legs ohne Flugnummer nicht verschmelzen.
        flight = ('%s-%s' % (dep, arr)) if (dep and arr) else ''

    def _i(v):
        try:
            return int(round(float(v)))
        except (TypeError, ValueError):
            return None

    points = [[int(r['ts']),
               round(float(r['lat']), 4), round(float(r['lon']), 4),
               _i(r.get('alt_ft')), _i(r.get('gs_kt'))] for r in kept]
    return {'reg': reg, 'service_date': service_date, 'flight': flight,
            'dep': dep, 'arr': arr, 'points': points, 'pt_count': len(points)}


# ── Orchestrator (DB, best-effort) ───────────────────────────────────────────

def _compact_day(sb, day_lo_epoch, legs_budget, page_size, max_rows, max_points):
    """Einen UTC-Tag verdichten. Reg-Cursor = höchstes bereits archiviertes reg
    dieses service_date (Regs werden aufsteigend verarbeitet) → abgebrochene/
    truncierte Läufe machen beim nächsten Aufruf exakt dort weiter.
    → {'legs': n, 'rows': n, 'complete': bool}"""
    # Mitternachts-Legs (Review-Fix): Rohpunkte mit Ueberhang holen — GAP_SEC
    # davor (erkennt Fortsetzungen des Vortags) und +12h danach (Langstrecke
    # ueber 00:00Z bleibt EIN Leg). Zugeordnet wird ein Leg dem Tag seines
    # ERSTEN Punkts; Fremd-Tage-Legs werden unten uebersprungen (der Vortags-
    # Lauf hat sie mit SEINEM Ueberhang bereits vollstaendig archiviert).
    day_lo = _epoch_to_iso(day_lo_epoch - GAP_SEC)
    day_hi = _epoch_to_iso(day_lo_epoch + DAY_SEC + 12 * 3600)
    service_date = time.strftime('%Y-%m-%d', time.gmtime(day_lo_epoch))
    cursor = None
    try:
        cur_rows = (sb.table('flight_tracks_archive').select('reg')
                    .eq('service_date', service_date)
                    .order('reg', desc=True).limit(1).execute()).data or []
        cursor = (cur_rows[0].get('reg') or None) if cur_rows else None
    except Exception:
        cursor = None

    # Roh-Breadcrumbs des Tages seitenweise holen (reg-aufsteigend, ab Cursor).
    rows, off, exhausted = [], 0, False
    while off < max_rows:
        q = (sb.table('aircraft_track')
             .select('reg,flight,origin,dest,lat,lon,alt_ft,gs_kt,seen_ts,on_ground')
             .gte('seen_ts', day_lo).lt('seen_ts', day_hi))
        if cursor:
            q = q.gt('reg', cursor)
        page = (q.order('reg').order('seen_ts')
                .range(off, off + page_size - 1).execute()).data or []
        rows.extend(page)
        off += page_size
        if len(page) < page_size:
            exhausted = True
            break
    if not rows:
        return {'legs': 0, 'rows': 0, 'complete': True}
    if not exhausted:
        # Letzte Reg-Gruppe ist evtl. angeschnitten → verwerfen, kommt im
        # nächsten Lauf vollständig dran. Außer sie ist die EINZIGE Gruppe
        # (eine reg > max_rows Punkte/Tag gibt es real nicht — Endlos-Guard).
        last_reg = rows[-1].get('reg')
        trimmed = [r for r in rows if r.get('reg') != last_reg]
        if trimmed:
            rows = trimmed

    n_rows = len(rows)
    for r in rows:
        r['ts'] = _iso_to_epoch(r.get('seen_ts'))

    out, truncated = [], False
    for reg, group in itertools.groupby(rows, key=lambda r: r.get('reg') or ''):
        if len(out) >= legs_budget:
            truncated = True
            break
        if not reg:
            continue
        g = sorted(group, key=lambda r: r['ts'] if r['ts'] is not None else 0)
        by_key = {}
        for leg in split_legs(g):
            first_ts = leg[0].get('ts') or 0
            if not (day_lo_epoch <= first_ts < day_lo_epoch + DAY_SEC):
                continue    # gehoert dem Vortag/Folgetag (s. Fenster-Kommentar)
            row = compact_leg(reg, leg, max_points=max_points)
            if row is None:
                continue
            k = (row['service_date'], row['flight'])
            old = by_key.get(k)
            # PK-Kollision im selben Lauf (z.B. zwei flug-lose Fragmente):
            # das längere Leg gewinnt — konservativ, nie beide verlieren wollen
            # wäre ein PK-Redesign (Owner-Entscheid im Plan: (reg,date,flight)).
            if old is None or row['pt_count'] > old['pt_count']:
                by_key[k] = row
        out.extend(by_key.values())    # eine reg immer KOMPLETT (Cursor-Invariante)

    if out:
        sb.table('flight_tracks_archive').upsert(
            out, on_conflict='reg,service_date,flight').execute()
    return {'legs': len(out), 'rows': n_rows,
            'complete': bool(exhausted and not truncated)}


def run_compact(sb, mark_get, mark_set, now=None, retention_days=10,
                max_legs=4000, page_size=4000, max_rows=200000,
                max_points=MAX_POINTS, max_days=30):
    """Verdichtungs-Lauf: Tage von der Watermark bis heute−(retention−2)
    verarbeiten (nur VOLLE Tage — die Watermark bleibt tag-aligned), Watermark
    nach jedem komplett archivierten Tag anheben. mark_get/mark_set = Epoch-
    Watermark-KV ('trackarch:until'); track-prune liest sie als Obergrenze.
    → Stats-Dict (nie werfen lassen — Caller fängt Exceptions)."""
    now = float(now if now is not None else time.time())
    hi_bound = _day_floor(now - (retention_days - 2) * DAY_SEC)
    start = 0
    try:
        start = int(mark_get() or 0)
    except Exception:
        start = 0
    if not start:
        rows = (sb.table('aircraft_track').select('seen_ts')
                .order('seen_ts').limit(1).execute()).data or []
        start = _iso_to_epoch(rows[0].get('seen_ts')) if rows else None
        if not start:
            return {'days_done': 0, 'legs_archived': 0, 'rows_read': 0,
                    'archived_until': None, 'note': 'no_rows'}

    day_lo = _day_floor(start)
    legs_budget = max(1, int(max_legs))
    days_done = legs_total = rows_total = 0
    complete_until = start
    while day_lo + DAY_SEC <= hi_bound and legs_budget > 0 and days_done < max_days:
        res = _compact_day(sb, day_lo, legs_budget, page_size, max_rows, max_points)
        legs_total += res['legs']
        rows_total += res['rows']
        legs_budget -= res['legs']
        if not res['complete']:
            break               # Budget/Seiten-Limit — nächster Lauf macht weiter
        complete_until = day_lo + DAY_SEC
        try:
            mark_set(complete_until)
        except Exception:
            pass
        day_lo += DAY_SEC
        days_done += 1
    return {'days_done': days_done, 'legs_archived': legs_total,
            'rows_read': rows_total,
            'archived_until': _epoch_to_iso(complete_until),
            'hi_bound': _epoch_to_iso(hi_bound)}
