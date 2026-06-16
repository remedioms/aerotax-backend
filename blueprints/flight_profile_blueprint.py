# ═══════════════════════════════════════════════════════════════
#  Flight-Profile Blueprint  (Stage 2 + 3)
#
#  Stage 2 — Selbst-bauende Flug-DB (KOSTENLOS, kein bezahltes API):
#    Jedes Mal wenn ein Client eine Flugnummer öffnet und dabei eine LIVE
#    ADS-B-Maschine (reg/type) + die geplante Route (adsbdb) gesehen hat,
#    meldet er eine Beobachtung. Über die Zeit entsteht eine eigene
#    Flugzeug-/Routen-Historie für genau die Flüge die die Nutzer fliegen.
#      POST /api/flight/<callsign>/observe   body {date, reg?, type?, dep?, arr?}
#      GET  /api/flight/<callsign>/history   → typische Maschine + zuletzt gesehen
#
#  Stage 3 — Crew-Ebene:
#      GET  /api/flight/<callsign>/crew/<token>
#        → wie viele aus dem eigenen Friend-Netzwerk diese Flugnummer im Roster
#          haben (kommende Tage), mit Namen + Datum. Nutzt vorhandene Daten
#          (Friends + roster_snapshot), kostenlos.
#
#  Wiring in app.py: ('blueprints.flight_profile_blueprint', 'flight_profile_bp')
# ═══════════════════════════════════════════════════════════════

import os
import re
import json
import logging
import datetime as _dt
from collections import Counter
from flask import Blueprint, request, jsonify, current_app

flight_profile_bp = Blueprint('flight_profile', __name__)

_MAX_HISTORY = 20


def _app_attr(name, default=None):
    try:
        import app as _app_mod
        return getattr(_app_mod, name, default)
    except Exception:
        return default


def _log():
    try:
        return current_app.logger
    except RuntimeError:
        return logging.getLogger('flight_profile')


def _get_sb():
    return _app_attr('SB_AVAILABLE', False), _app_attr('sb', None)


def _safe_callsign(cs):
    if not cs or not isinstance(cs, str):
        return None
    s = re.sub(r'[^A-Za-z0-9]', '', cs).upper()[:10]
    return s or None


def _history_dir():
    d = _app_attr('_USER_HISTORY_DIR', '_user_history_state')
    os.makedirs(d, exist_ok=True)
    return d


def _disk_path(callsign):
    return os.path.join(_history_dir(), f'flight_obs_{callsign}.json')


def _atomic_write_json(path, data):
    fn = _app_attr('_atomic_write_json')
    if callable(fn):
        try:
            return fn(path, data)
        except Exception:
            pass
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


# ── Stage 2: Observe + History ──────────────────────────────────────────────
@flight_profile_bp.route('/api/flight/<callsign>/observe', methods=['POST'])
def observe_flight(callsign):
    """Client meldet eine Live-Beobachtung (eine Zeile pro callsign+Tag)."""
    cs = _safe_callsign(callsign)
    if not cs:
        return jsonify({'ok': False, 'error': 'invalid_callsign'}), 400
    body = request.get_json(silent=True) or {}
    date = (body.get('date') or _dt.datetime.now(_dt.timezone.utc).date().isoformat()).strip()[:10]
    reg = (body.get('reg') or '').strip().upper()[:12] or None
    type_code = (body.get('type') or '').strip().upper()[:8] or None
    dep = (body.get('dep') or '').strip().upper()[:4] or None
    arr = (body.get('arr') or '').strip().upper()[:4] or None
    # Mindestens eine sinnvolle Info nötig, sonst keine leere Zeile schreiben.
    if not (reg or type_code or dep or arr):
        return jsonify({'ok': True, 'skipped': True})
    now = _dt.datetime.now(_dt.timezone.utc).isoformat()
    row = {'callsign': cs, 'obs_date': date, 'reg': reg, 'type_code': type_code,
           'dep': dep, 'arr': arr, 'last_seen': now}
    sb_avail, sb = _get_sb()
    if sb_avail and sb is not None:
        try:
            sb.table('flight_observations').upsert(
                {**row, 'first_seen': now}, on_conflict='callsign,obs_date').execute()
            return jsonify({'ok': True})
        except Exception as e:
            _log().info(f'[flight-obs] sb_skip {type(e).__name__}')
    # Disk-Fallback: Liste pro callsign, dedupe nach obs_date.
    try:
        p = _disk_path(cs)
        rows = []
        if os.path.exists(p):
            with open(p) as f:
                rows = json.load(f) or []
        rows = [r for r in rows if r.get('obs_date') != date]
        rows.append({**row, 'first_seen': now})
        rows = rows[-60:]
        _atomic_write_json(p, rows)
    except Exception as e:
        _log().warning(f'[flight-obs] disk_fail {e}')
    return jsonify({'ok': True})


@flight_profile_bp.route('/api/flight/<callsign>/history', methods=['GET'])
def flight_history(callsign):
    """Aggregierte Historie: typische Maschine + zuletzt gesehene Tage/Tails."""
    cs = _safe_callsign(callsign)
    if not cs:
        return jsonify({'ok': False, 'error': 'invalid_callsign'}), 400
    rows = []
    sb_avail, sb = _get_sb()
    if sb_avail and sb is not None:
        try:
            r = (sb.table('flight_observations').select('*')
                 .eq('callsign', cs).order('obs_date', desc=True)
                 .limit(_MAX_HISTORY).execute())
            rows = r.data or []
        except Exception as e:
            _log().info(f'[flight-hist] sb_skip {type(e).__name__}')
    if not rows:
        p = _disk_path(cs)
        if os.path.exists(p):
            try:
                with open(p) as f:
                    rows = sorted(json.load(f) or [],
                                  key=lambda x: x.get('obs_date') or '', reverse=True)[:_MAX_HISTORY]
            except Exception:
                rows = []
    if not rows:
        return jsonify({'ok': True, 'count': 0, 'typical_type': None,
                        'recent': [], 'regs': []})
    types = Counter(r.get('type_code') for r in rows if r.get('type_code'))
    regs = []
    seen_reg = set()
    for r in rows:
        rg = r.get('reg')
        if rg and rg not in seen_reg:
            seen_reg.add(rg)
            regs.append(rg)
    recent = [{'date': r.get('obs_date'), 'reg': r.get('reg'),
               'type': r.get('type_code')} for r in rows][:10]
    return jsonify({
        'ok': True,
        'count': len(rows),
        'typical_type': (types.most_common(1)[0][0] if types else None),
        'regs': regs[:8],
        'recent': recent,
    })


# ── Stage 3: Crew an Bord (Friend-Netzwerk) ─────────────────────────────────
@flight_profile_bp.route('/api/flight/<callsign>/crew/<token>', methods=['GET'])
def flight_crew(callsign, token):
    """Wer aus meinem Friend-Netzwerk hat diese Flugnummer im Roster (kommend)?
    Kostenlos: nutzt _friends_load + roster_snapshot. Matcht die Flugnummer im
    marker/routing der Roster-Tage."""
    cs = _safe_callsign(callsign)
    if not cs:
        return jsonify({'ok': False, 'error': 'invalid_callsign'}), 400
    # IATA-Variante (LH976) zusätzlich matchen — Roster nutzt oft IATA-Nummern.
    num = re.sub(r'^[A-Z]{3}', '', cs)            # DLH976 → 976
    friends_fn = _app_attr('_friends_load')
    profile_fn = _app_attr('_profile_load')
    snap_fn = _app_attr('_roster_snapshot_read')
    if not callable(friends_fn):
        return jsonify({'ok': True, 'crew': [], 'count': 0})
    try:
        friends = (friends_fn(token) or {}).get('friends') or []
    except Exception:
        friends = []
    today = _dt.date.today()
    out = []
    times = []
    time_re = re.compile(r'(\d{1,2}:\d{2})')
    for ft in friends:
        try:
            prof = (profile_fn(ft) or {}).get('profile', {}) if callable(profile_fn) else {}
            if prof.get('share_roster') is False:
                continue
            tage = (snap_fn(ft) or {}).get('tage') if callable(snap_fn) else None
            tage = tage or []
            for day in tage:
                d = (day.get('datum') or '')[:10]
                if not d:
                    continue
                try:
                    if _dt.date.fromisoformat(d) < today:
                        continue
                except Exception:
                    continue
                marker = day.get('marker') or ''
                hay = f"{marker} {day.get('routing') or ''}".upper()
                if num and (cs in hay or f"LH{num}" in hay or f" {num} " in f" {hay} "):
                    # Erste Uhrzeit im Marker ≈ Report/Abflugzeit des Tages.
                    m = time_re.search(marker)
                    t = m.group(1) if m else None
                    if t:
                        times.append(t)
                    out.append({'name': prof.get('name') or 'Crew', 'date': d, 'time': t})
                    break
        except Exception:
            continue
    out.sort(key=lambda x: x.get('date') or '')
    # Typische Zeit = häufigste beobachtete Marker-Startzeit (Crew-Plan, kostenlos).
    typical_time = None
    if times:
        typical_time = Counter(times).most_common(1)[0][0]
    return jsonify({'ok': True, 'crew': out[:10], 'count': len(out),
                    'typical_time': typical_time})
