# ═══════════════════════════════════════════════════════════════
#  Hotel-Rooms Blueprint — USP-1: "Room-Database" (Network-Effect Killer)
#
#  Crews tauschen taeglich in WhatsApp / Crew-Slack Tipps der Art
#  "Im Sheraton Frankfurt NIE Room 4xx — Autobahn-Seite, Lärm bis 02:00".
#  AeroX wird die Single-Source-of-Truth: jeder Tipp ist ein Report,
#  der upgevoted werden kann. Mehr Upvotes → höher gelistet → mehr Wert.
#
#  Wiring in app.py:
#      from blueprints.hotel_rooms_blueprint import hotel_rooms_bp
#      app.register_blueprint(hotel_rooms_bp)
#
#  Endpoints:
#      POST   /api/hotel-rooms/<token>/report
#      GET    /api/hotel-rooms/by-hotel?hotel_name=...&hotel_iata=FRA
#      GET    /api/hotel-rooms/by-iata?iata=FRA
#      POST   /api/hotel-rooms/<token>/upvote/<report_id>
#      DELETE /api/hotel-rooms/<token>/<report_id>
#
#  Privacy:
#    · `reported_by_token` wird gespeichert, aber NIEMALS im Listing
#      ausgegeben (PII-strip am Output).
#    · Upvotes sind pro (report_id, voter_token) eindeutig (DB-PK).
#    · Owner-Delete: nur wenn der reportende Token === request-Token.
#
#  Rate-Limit:
#    · 5 Reports pro Tag pro Token (via _token_rate_limited aus app.py)
#
#  Storage: SB primary + Disk fallback analog aircraft_health_blueprint.py
# ═══════════════════════════════════════════════════════════════

import json
import os
import re
import uuid
import threading
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify, current_app

hotel_rooms_bp = Blueprint('hotel_rooms', __name__)

# ── Supabase-Anbindung (lazy-resolve wie aircraft_health_blueprint) ──
try:
    from app import sb as _sb, SB_AVAILABLE as _SB_AVAILABLE
except ImportError:
    _sb = None
    _SB_AVAILABLE = False

try:
    from app import _token_rate_limited as _rl
except ImportError:
    _rl = None


def _sb_client():
    """Lazy re-resolve, damit init-Order zwischen app.py und Blueprint egal ist."""
    global _sb, _SB_AVAILABLE
    if _sb is not None and _SB_AVAILABLE:
        return _sb, True
    try:
        from app import sb as live_sb, SB_AVAILABLE as live_av
        _sb = live_sb
        _SB_AVAILABLE = bool(live_av)
        return _sb, _SB_AVAILABLE
    except ImportError:
        return None, False


def _rate_limited(token, endpoint, limit, window_sec):
    """Wrapper damit der Blueprint laeuft auch ohne app.py Helper."""
    if _rl is None:
        return False
    try:
        return bool(_rl(token, endpoint, limit, window_sec))
    except Exception:
        return False


# ── Disk-Fallback ───────────────────────────────────────────────
_USER_HISTORY_DIR = '_user_history_state'
_DISK_REPORTS = 'hotel_room_reports.json'
_DISK_UPVOTES = 'hotel_room_upvotes.json'
_DISK_LOCK = threading.Lock()


# ── Limits / Tunables ───────────────────────────────────────────
NOTE_MAX_LEN = 500
HOTEL_NAME_MAX = 120
LISTING_MAX = 50
RATING_MIN = 1
RATING_MAX = 5
ALLOWED_SIDES = {'street', 'courtyard', 'highway', 'runway', 'inner'}


# ─── Validation Helpers ────────────────────────────────────────

def _safe_token_fragment(token):
    if not token or not isinstance(token, str):
        return None
    safe = re.sub(r'[^A-Za-z0-9_-]', '', token)[:64]
    return safe or None


def _norm_iata(raw):
    if not raw or not isinstance(raw, str):
        return None
    s = re.sub(r'[^A-Z0-9]', '', raw.upper().strip())[:4]
    return s or None


def _norm_hotel_name(raw):
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip()[:HOTEL_NAME_MAX]
    return s or None


def _norm_rating(raw):
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return None
    if v < RATING_MIN or v > RATING_MAX:
        return None
    return v


def _norm_room_number(raw):
    if raw is None or raw == '':
        return None
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return None
    if v < 0 or v > 99999:
        return None
    return v


def _norm_side(raw):
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip().lower()
    return s if s in ALLOWED_SIDES else None


def _norm_note(raw):
    if raw is None:
        return ''
    if not isinstance(raw, str):
        return ''
    return raw.strip()[:NOTE_MAX_LEN]


def _norm_year(raw):
    if raw is None or raw == '':
        return None
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return None
    if v < 1900 or v > 2100:
        return None
    return v


# ─── Disk-Fallback ─────────────────────────────────────────────

def _disk_path(name):
    os.makedirs(_USER_HISTORY_DIR, exist_ok=True)
    return os.path.join(_USER_HISTORY_DIR, name)


def _disk_load(name):
    try:
        with open(_disk_path(name)) as f:
            d = json.load(f)
            return d if isinstance(d, list) else []
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, OSError):
        return []


def _disk_save(name, rows):
    p = _disk_path(name)
    try:
        tmp = p + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(rows, f, ensure_ascii=False)
        os.replace(tmp, p)
        return True
    except OSError:
        return False


# ─── Supabase IO ───────────────────────────────────────────────

def _sb_insert_report(row):
    sb, ok = _sb_client()
    if not ok:
        return False
    try:
        sb.table('hotel_room_reports').insert(row).execute()
        return True
    except Exception as e:
        current_app.logger.warning(
            f'[hotel-rooms] sb_insert_fail err={type(e).__name__}: {str(e)[:120]}'
        )
        return False


def _sb_list_by_hotel(hotel_name, hotel_iata):
    sb, ok = _sb_client()
    if not ok:
        return None
    try:
        q = (sb.table('hotel_room_reports')
             .select('id,hotel_name,hotel_iata,room_number_low,room_number_high,'
                     'side,noise_rating,view_rating,comfort_rating,'
                     'overall_rating,breakfast_rating,fitness_rating,note,'
                     'renovated_year,upvote_count,created_at')
             .eq('deleted', False)
             .eq('hotel_name', hotel_name))
        if hotel_iata:
            q = q.eq('hotel_iata', hotel_iata)
        r = q.order('upvote_count', desc=True).limit(LISTING_MAX).execute()
        return r.data or []
    except Exception as e:
        current_app.logger.warning(
            f'[hotel-rooms] sb_list_hotel_fail err={type(e).__name__}: {str(e)[:120]}'
        )
        return None


def _sb_list_by_iata(iata):
    sb, ok = _sb_client()
    if not ok:
        return None
    try:
        r = (sb.table('hotel_room_reports')
             .select('id,hotel_name,hotel_iata,room_number_low,room_number_high,'
                     'side,noise_rating,view_rating,comfort_rating,'
                     'overall_rating,breakfast_rating,fitness_rating,note,'
                     'renovated_year,upvote_count,created_at')
             .eq('deleted', False)
             .eq('hotel_iata', iata)
             .order('upvote_count', desc=True)
             .limit(LISTING_MAX)
             .execute())
        return r.data or []
    except Exception as e:
        current_app.logger.warning(
            f'[hotel-rooms] sb_list_iata_fail err={type(e).__name__}: {str(e)[:120]}'
        )
        return None


def _sb_list_for_summary(iata):
    """Alle nicht-geloeschten Reports am IATA (fuer Hotel-Level-Aggregation).

    Nicht upvote-sortiert/LISTING_MAX-gekappt wie das Listing — wir brauchen die
    volle Stichprobe pro Hotel, sonst verzerren gekappte Reihen die Durchschnitte.
    """
    sb, ok = _sb_client()
    if not ok:
        return None
    try:
        r = (sb.table('hotel_room_reports')
             .select('hotel_name,comfort_rating,overall_rating,'
                     'breakfast_rating,fitness_rating')
             .eq('deleted', False)
             .eq('hotel_iata', iata)
             .limit(5000)
             .execute())
        return r.data or []
    except Exception as e:
        current_app.logger.warning(
            f'[hotel-rooms] sb_summary_fail err={type(e).__name__}: {str(e)[:120]}'
        )
        return None


def _sb_upvote_insert(report_id, voter_token):
    """Idempotent: bei doppelter PK gibt SB einen Fehler — wir wandeln in False."""
    sb, ok = _sb_client()
    if not ok:
        return False, False  # (success, was_new)
    try:
        sb.table('hotel_room_upvotes').insert({
            'report_id': report_id,
            'voter_token': voter_token,
        }).execute()
        # Atomic increment via RPC waere ideal; rein-additiv reicht aber:
        # wir lesen + schreiben mit best-effort. Bei race-condition kann der
        # counter um 1 abweichen — Cosmetic, nicht funktional kritisch.
        cur = (sb.table('hotel_room_reports')
               .select('upvote_count')
               .eq('id', report_id)
               .limit(1)
               .execute())
        old = (cur.data or [{}])[0].get('upvote_count', 0) or 0
        sb.table('hotel_room_reports').update({
            'upvote_count': old + 1,
        }).eq('id', report_id).execute()
        return True, True
    except Exception as e:
        # Duplicate PK → already voted; ist OK.
        msg = str(e).lower()
        if 'duplicate' in msg or '23505' in msg or 'conflict' in msg:
            return True, False
        current_app.logger.warning(
            f'[hotel-rooms] sb_upvote_fail err={type(e).__name__}: {str(e)[:120]}'
        )
        return False, False


def _sb_owner_delete(report_id, token):
    sb, ok = _sb_client()
    if not ok:
        return None  # SB-down → 503
    try:
        cur = (sb.table('hotel_room_reports')
               .select('id,reported_by_token,deleted')
               .eq('id', report_id)
               .limit(1)
               .execute())
        rows = cur.data or []
        if not rows:
            return 404
        row = rows[0]
        if row.get('reported_by_token') != token:
            return 404  # niemals "exists but not yours" leaken
        if row.get('deleted'):
            return 200  # already gone, idempotent
        sb.table('hotel_room_reports').update({
            'deleted': True,
        }).eq('id', report_id).execute()
        return 200
    except Exception as e:
        current_app.logger.warning(
            f'[hotel-rooms] sb_delete_fail err={type(e).__name__}: {str(e)[:120]}'
        )
        return 503


# ─── Output PII-strip ──────────────────────────────────────────

def _clean_report(r):
    """Strip jegliche PII bevor Server -> Client geht."""
    return {
        'id': r.get('id'),
        'hotel_name': r.get('hotel_name'),
        'hotel_iata': r.get('hotel_iata'),
        'room_number_low': r.get('room_number_low'),
        'room_number_high': r.get('room_number_high'),
        'side': r.get('side'),
        'noise_rating': r.get('noise_rating'),
        'view_rating': r.get('view_rating'),
        'comfort_rating': r.get('comfort_rating'),
        'overall_rating': r.get('overall_rating'),
        'breakfast_rating': r.get('breakfast_rating'),
        'fitness_rating': r.get('fitness_rating'),
        'note': r.get('note'),
        'renovated_year': r.get('renovated_year'),
        'upvote_count': r.get('upvote_count') or 0,
        'created_at': r.get('created_at'),
    }


# ════════════════════════════════════════════════════════════════
#                          E N D P O I N T S
# ════════════════════════════════════════════════════════════════

@hotel_rooms_bp.route('/api/hotel-rooms/<token>/report', methods=['POST'])
def hotel_rooms_post(token):
    safe_tok = _safe_token_fragment(token)
    if not safe_tok:
        return jsonify({'ok': False, 'error': 'Ungueltiges Token.'}), 400

    if _rate_limited(safe_tok, 'hotel_room_report', limit=5, window_sec=86400):
        return jsonify({
            'ok': False,
            'error': 'Tageslimit erreicht (5 Reports/Tag).'
        }), 429

    body = request.get_json(silent=True) or {}
    hotel_name = _norm_hotel_name(body.get('hotel_name'))
    if not hotel_name:
        return jsonify({'ok': False, 'error': 'hotel_name fehlt.'}), 400

    hotel_iata = _norm_iata(body.get('hotel_iata'))  # optional

    room_low = _norm_room_number(body.get('room_number_low'))
    room_high = _norm_room_number(body.get('room_number_high'))
    side = _norm_side(body.get('side'))
    noise = _norm_rating(body.get('noise_rating'))
    view = _norm_rating(body.get('view_rating'))
    comfort = _norm_rating(body.get('comfort_rating'))
    overall = _norm_rating(body.get('overall_rating'))
    breakfast = _norm_rating(body.get('breakfast_rating'))
    fitness = _norm_rating(body.get('fitness_rating'))
    note = _norm_note(body.get('note'))
    renovated = _norm_year(body.get('renovated_year'))

    # Mindest-Inhalt: mindestens 1 Rating oder note >= 6 chars oder room-range
    has_signal = (noise or view or comfort or overall or breakfast or fitness or
                  (note and len(note) >= 6) or
                  (room_low is not None))
    if not has_signal:
        return jsonify({
            'ok': False,
            'error': 'Mindestens Room-Number, Rating oder Notiz erforderlich.'
        }), 400

    report_id = str(uuid.uuid4())
    now_iso = datetime.now(timezone.utc).isoformat()

    row = {
        'id': report_id,
        'reported_by_token': safe_tok,
        'hotel_name': hotel_name,
        'hotel_iata': hotel_iata,
        'room_number_low': room_low,
        'room_number_high': room_high,
        'side': side,
        'noise_rating': noise,
        'view_rating': view,
        'comfort_rating': comfort,
        'overall_rating': overall,
        'breakfast_rating': breakfast,
        'fitness_rating': fitness,
        'note': note,
        'renovated_year': renovated,
        'upvote_count': 0,
        'deleted': False,
        'created_at': now_iso,
    }

    sb_ok = _sb_insert_report(row)
    # Disk-fallback als zweiter "Beleg" (idempotent append).
    with _DISK_LOCK:
        rows = _disk_load(_DISK_REPORTS)
        rows.append(row)
        if len(rows) > 5000:
            rows = rows[-5000:]
        _disk_save(_DISK_REPORTS, rows)

    current_app.logger.info(
        f'[hotel-rooms] report_in hotel={hotel_name[:30]} iata={hotel_iata} '
        f'tok={safe_tok[:8]} id={report_id[:8]} sb_ok={sb_ok}'
    )

    return jsonify({'ok': True, 'report': _clean_report(row)})


@hotel_rooms_bp.route('/api/hotel-rooms/by-hotel', methods=['GET'])
def hotel_rooms_by_hotel():
    hotel_name = _norm_hotel_name(request.args.get('hotel_name'))
    if not hotel_name:
        return jsonify({'ok': False, 'error': 'hotel_name fehlt.'}), 400
    hotel_iata = _norm_iata(request.args.get('hotel_iata'))

    rows = _sb_list_by_hotel(hotel_name, hotel_iata)
    if rows is None:
        # Disk-fallback
        all_rows = _disk_load(_DISK_REPORTS)
        rows = [r for r in all_rows
                if not r.get('deleted')
                and r.get('hotel_name') == hotel_name
                and (not hotel_iata or r.get('hotel_iata') == hotel_iata)]
        rows.sort(key=lambda x: x.get('upvote_count', 0), reverse=True)
        rows = rows[:LISTING_MAX]

    return jsonify({
        'ok': True,
        'hotel_name': hotel_name,
        'hotel_iata': hotel_iata,
        'count': len(rows),
        'reports': [_clean_report(r) for r in rows],
    })


@hotel_rooms_bp.route('/api/hotel-rooms/by-iata', methods=['GET'])
def hotel_rooms_by_iata():
    iata = _norm_iata(request.args.get('iata'))
    if not iata:
        return jsonify({'ok': False, 'error': 'iata fehlt oder ungueltig.'}), 400

    rows = _sb_list_by_iata(iata)
    if rows is None:
        all_rows = _disk_load(_DISK_REPORTS)
        rows = [r for r in all_rows
                if not r.get('deleted') and r.get('hotel_iata') == iata]
        rows.sort(key=lambda x: x.get('upvote_count', 0), reverse=True)
        rows = rows[:LISTING_MAX]

    return jsonify({
        'ok': True,
        'iata': iata,
        'count': len(rows),
        'reports': [_clean_report(r) for r in rows],
    })


def _avg_or_none(values):
    """Mittelwert auf 1 Dezimale gerundet, oder None wenn keine Werte.

    Ehrlich: kein erfundener 0-Wert wenn fuer eine Sub-Dimension nichts vorliegt.
    """
    vals = [v for v in values if isinstance(v, (int, float)) and v is not None]
    if not vals:
        return None
    return round(sum(vals) / len(vals), 1)


def _aggregate_hotels(rows):
    """Aggregiert flache Report-Rows zu Hotel-Level-Sub-Ratings.

    Pro distinct hotel_name: report_count + avg_overall / avg_room (=comfort) /
    avg_breakfast / avg_fitness. Sortiert nach report_count desc.
    """
    buckets = {}
    for r in rows:
        name = r.get('hotel_name')
        if not name:
            continue
        b = buckets.setdefault(name, {
            'overall': [], 'room': [], 'breakfast': [], 'fitness': [],
        })
        if r.get('overall_rating') is not None:
            b['overall'].append(r['overall_rating'])
        if r.get('comfort_rating') is not None:
            b['room'].append(r['comfort_rating'])
        if r.get('breakfast_rating') is not None:
            b['breakfast'].append(r['breakfast_rating'])
        if r.get('fitness_rating') is not None:
            b['fitness'].append(r['fitness_rating'])

    out = []
    for name, b in buckets.items():
        # report_count = Anzahl Reports am Hotel (auch ohne Sub-Rating).
        count = sum(1 for r in rows if r.get('hotel_name') == name)
        out.append({
            'hotel_name': name,
            'report_count': count,
            'avg_overall': _avg_or_none(b['overall']),
            'avg_room': _avg_or_none(b['room']),
            'avg_breakfast': _avg_or_none(b['breakfast']),
            'avg_fitness': _avg_or_none(b['fitness']),
        })
    out.sort(key=lambda x: x['report_count'], reverse=True)
    return out


@hotel_rooms_bp.route('/api/hotel-rooms/<iata>/summary', methods=['GET'])
def hotel_rooms_summary(iata):
    norm = _norm_iata(iata)
    if not norm:
        return jsonify({'ok': False, 'error': 'iata fehlt oder ungueltig.'}), 400

    rows = _sb_list_for_summary(norm)
    if rows is None:
        # Disk-fallback: volle nicht-geloeschte Stichprobe am IATA.
        all_rows = _disk_load(_DISK_REPORTS)
        rows = [r for r in all_rows
                if not r.get('deleted') and r.get('hotel_iata') == norm]

    hotels = _aggregate_hotels(rows)
    return jsonify({
        'ok': True,
        'iata': norm,
        'count': len(hotels),
        'hotels': hotels,
    })


@hotel_rooms_bp.route('/api/hotel-rooms/<token>/upvote/<report_id>', methods=['POST'])
def hotel_rooms_upvote(token, report_id):
    safe_tok = _safe_token_fragment(token)
    if not safe_tok:
        return jsonify({'ok': False, 'error': 'Ungueltiges Token.'}), 400
    safe_id = _safe_token_fragment(report_id)
    if not safe_id:
        return jsonify({'ok': False, 'error': 'Ungueltige Report-ID.'}), 400

    if _rate_limited(safe_tok, 'hotel_room_upvote', limit=60, window_sec=3600):
        return jsonify({'ok': False, 'error': 'Zu viele Upvotes.'}), 429

    ok, was_new = _sb_upvote_insert(safe_id, safe_tok)
    if not ok:
        # Disk-fallback: best-effort dedupe
        with _DISK_LOCK:
            ups = _disk_load(_DISK_UPVOTES)
            existing = {(u.get('report_id'), u.get('voter_token')) for u in ups}
            if (safe_id, safe_tok) not in existing:
                ups.append({
                    'report_id': safe_id,
                    'voter_token': safe_tok,
                    'created_at': datetime.now(timezone.utc).isoformat(),
                })
                if len(ups) > 20000:
                    ups = ups[-20000:]
                _disk_save(_DISK_UPVOTES, ups)
                was_new = True
            # Mirror counter on disk reports
            rows = _disk_load(_DISK_REPORTS)
            for r in rows:
                if r.get('id') == safe_id and was_new:
                    r['upvote_count'] = (r.get('upvote_count') or 0) + 1
                    break
            _disk_save(_DISK_REPORTS, rows)

    current_app.logger.info(
        f'[hotel-rooms] upvote tok={safe_tok[:8]} id={safe_id[:8]} new={was_new}'
    )
    return jsonify({'ok': True, 'new_vote': was_new})


@hotel_rooms_bp.route('/api/hotel-rooms/<token>/<report_id>', methods=['DELETE'])
def hotel_rooms_delete(token, report_id):
    safe_tok = _safe_token_fragment(token)
    if not safe_tok:
        return jsonify({'ok': False, 'error': 'Ungueltiges Token.'}), 400
    safe_id = _safe_token_fragment(report_id)
    if not safe_id:
        return jsonify({'ok': False, 'error': 'Ungueltige Report-ID.'}), 400

    status = _sb_owner_delete(safe_id, safe_tok)
    if status == 404:
        return jsonify({'ok': False, 'error': 'Nicht gefunden.'}), 404
    if status == 503 or status is None:
        # SB down → versuch Disk-soft-delete als best-effort
        deleted = False
        with _DISK_LOCK:
            rows = _disk_load(_DISK_REPORTS)
            for r in rows:
                if (r.get('id') == safe_id
                        and r.get('reported_by_token') == safe_tok
                        and not r.get('deleted')):
                    r['deleted'] = True
                    deleted = True
                    break
            if deleted:
                _disk_save(_DISK_REPORTS, rows)
        if deleted:
            return jsonify({'ok': True})
        return jsonify({'ok': False, 'error': 'Speicher nicht verfuegbar.'}), 503

    current_app.logger.info(
        f'[hotel-rooms] delete tok={safe_tok[:8]} id={safe_id[:8]}'
    )
    return jsonify({'ok': True})
