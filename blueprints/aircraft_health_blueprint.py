# ═══════════════════════════════════════════════════════════════
#  Aircraft-Health Blueprint — Worker W-USP (Backend)
#
#  Crew-sourced Aircraft-Health-Reports: Crews dokumentieren tail-spezifische
#  Probleme (IFE row 24-30 broken, Galley freezer warm, Toilet vac intermittent),
#  das naechste Crew das auf derselben Tail-Reg fliegt sieht beim Boarding
#  "3 Berichte letzter 7 Tage".
#
#  Wiring in app.py:
#      from blueprints.aircraft_health_blueprint import aircraft_health_bp
#      app.register_blueprint(aircraft_health_bp)
#
#  Endpunkte:
#      POST /api/aircraft-health/<token>/report
#      GET  /api/aircraft-health/<tail_reg>/recent?days=7
#
#  Privacy-by-Design (identisch zum CrewGraph-Pattern):
#    · `reported_by_token` wird gespeichert, aber NIE an andere User
#      ausgegeben. Listing-Endpoint liefert nur description/system/severity/
#      created_at — kein Token, kein Name.
#    · Anonyme Listing — alle Crews die fuer die selbe Tail eingeloggt sind
#      sehen die gleiche Liste, ohne PII-Korrelation.
#    · severity ist enum (info/minor/major), kein freitext-severity.
#    · description ist text bis 280 chars, server kappt drueber.
#
#  Storage-Strategie:
#    · SB primary (`aircraft_health_reports`, Migration 20260601_aircraft_health.sql)
#    · Disk fallback unter _USER_HISTORY_DIR/aircraft_health.json (bei SB-down)
#    · Kein lazy-migrate (Daten sind global, nicht per-user — disk-fallback
#      ist ephemer)
# ═══════════════════════════════════════════════════════════════

import json
import os
import re
import uuid
import threading
from datetime import datetime, timezone, timedelta
from flask import Blueprint, request, jsonify, current_app

aircraft_health_bp = Blueprint('aircraft_health', __name__)

# ── Supabase-Anbindung (lazy-resolve wie crew_graph_blueprint) ──
try:
    from app import sb as _sb, SB_AVAILABLE as _SB_AVAILABLE
except ImportError:
    _sb = None
    _SB_AVAILABLE = False


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


# ── Disk-Fallback ───────────────────────────────────────────────
_USER_HISTORY_DIR = '_user_history_state'
_DISK_FALLBACK_NAME = 'aircraft_health.json'
_DISK_LOCK = threading.Lock()


# ── Limits / Tunables ───────────────────────────────────────────
DESCRIPTION_MAX_LEN = 280
DESCRIPTION_MIN_LEN = 6
TAIL_REG_MAX_LEN = 12
RECENT_MAX_DAYS = 30
RECENT_DEFAULT_DAYS = 7
LISTING_MAX = 100

ALLOWED_SYSTEMS = {'ife', 'galley', 'cabin', 'lavatory', 'avionics', 'other'}
ALLOWED_SEVERITY = {'info', 'minor', 'major'}
ALLOWED_STATUS = {'open', 'resolved'}


# ─── Validation Helpers ────────────────────────────────────────

def _safe_token_fragment(token):
    """Sanitiert ein Token. Erlaubt nur [A-Za-z0-9_-], 64 chars max."""
    if not token or not isinstance(token, str):
        return None
    safe = re.sub(r'[^A-Za-z0-9_-]', '', token)[:64]
    return safe or None


def _normalize_tail_reg(raw):
    """Tail-Reg-Normalisierung: uppercase, strip, only A-Z0-9-. Max 12 chars.

    Akzeptierte Beispiele: D-AIPB, N12345, G-EUAA, JA801A.
    Rejected: leerer String, > 12 chars, Sonderzeichen wie Unicode-Whitespace.
    """
    if not raw or not isinstance(raw, str):
        return None
    cleaned = re.sub(r'[^A-Z0-9\-]', '', raw.upper().strip())
    cleaned = cleaned[:TAIL_REG_MAX_LEN]
    if len(cleaned) < 3:
        return None
    return cleaned


def _normalize_description(raw):
    """Strip + length-cap. Returns None bei leerer/ungueltiger Eingabe."""
    if not raw or not isinstance(raw, str):
        return None
    s = raw.strip()
    if len(s) < DESCRIPTION_MIN_LEN:
        return None
    return s[:DESCRIPTION_MAX_LEN]


# ─── Disk-Fallback ─────────────────────────────────────────────

def _disk_path():
    os.makedirs(_USER_HISTORY_DIR, exist_ok=True)
    return os.path.join(_USER_HISTORY_DIR, _DISK_FALLBACK_NAME)


def _disk_load():
    """Returns list[dict] der Disk-Reports (newest first nicht garantiert)."""
    p = _disk_path()
    try:
        with open(p) as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
            return []
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, OSError):
        return []


def _disk_save(rows):
    p = _disk_path()
    try:
        tmp = p + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(rows, f, ensure_ascii=False)
        os.replace(tmp, p)
        return True
    except OSError:
        return False


def _disk_append(report_row):
    """Append-mit-Lock fuer den Disk-Fallback."""
    with _DISK_LOCK:
        rows = _disk_load()
        rows.append(report_row)
        # Cap auf die letzten 2000 — sonst waechst die Disk-Datei unbegrenzt.
        if len(rows) > 2000:
            rows = rows[-2000:]
        return _disk_save(rows)


def _disk_recent(tail_reg, since_iso):
    """List recent reports for tail aus Disk-Fallback. since_iso = ISO-Timestamp."""
    rows = _disk_load()
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        if r.get('tail_reg') != tail_reg:
            continue
        created = r.get('created_at')
        if not isinstance(created, str):
            continue
        if created < since_iso:
            continue
        if r.get('status') and r['status'] not in ALLOWED_STATUS:
            continue
        out.append(r)
    out.sort(key=lambda x: x.get('created_at', ''), reverse=True)
    return out[:LISTING_MAX]


# ─── Supabase IO ───────────────────────────────────────────────

def _sb_insert(report_row):
    """Insert eines neuen Reports. Returns True bei Erfolg."""
    sb, ok = _sb_client()
    if not ok:
        return False
    try:
        sb.table('aircraft_health_reports').insert(report_row).execute()
        return True
    except Exception as e:
        current_app.logger.warning(
            f'[aircraft-health] sb_insert_fail err={type(e).__name__}: {str(e)[:120]}'
        )
        return False


def _sb_recent(tail_reg, since_iso):
    """List recent reports. None bei SB-down."""
    sb, ok = _sb_client()
    if not ok:
        return None
    try:
        r = (sb.table('aircraft_health_reports')
             .select('report_id,tail_reg,system,severity,description,created_at,status')
             .eq('tail_reg', tail_reg)
             .gte('created_at', since_iso)
             .order('created_at', desc=True)
             .limit(LISTING_MAX)
             .execute())
        return r.data or []
    except Exception as e:
        current_app.logger.warning(
            f'[aircraft-health] sb_recent_fail tail={tail_reg} '
            f'err={type(e).__name__}: {str(e)[:120]}'
        )
        return None


# ════════════════════════════════════════════════════════════════
#                          E N D P O I N T S
# ════════════════════════════════════════════════════════════════

@aircraft_health_bp.route('/api/aircraft-health/<token>/report', methods=['POST'])
def aircraft_health_post(token):
    """Schreibt einen neuen Report fuer eine Tail-Reg.

    Body:
        {
          "tail_reg": "D-AIPB",
          "system": "ife",                # one of ALLOWED_SYSTEMS
          "severity": "minor",            # one of ALLOWED_SEVERITY
          "description": "IFE row 24-30 broken, others ok"
        }

    Response 200:
        {"ok": true, "report_id": "<uuid>"}

    Response 4xx bei Validation-Fehler.
    """
    safe_tok = _safe_token_fragment(token)
    if not safe_tok:
        return jsonify({'ok': False, 'error': 'Ungueltiges Token.'}), 400

    body = request.get_json(silent=True) or {}
    tail_reg = _normalize_tail_reg(body.get('tail_reg'))
    if not tail_reg:
        return jsonify({'ok': False, 'error': 'tail_reg fehlt oder ungueltig.'}), 400

    system = (body.get('system') or '').strip().lower()
    if system not in ALLOWED_SYSTEMS:
        return jsonify({
            'ok': False,
            'error': f'system muss eines von {sorted(ALLOWED_SYSTEMS)} sein.'
        }), 400

    severity = (body.get('severity') or '').strip().lower()
    if severity not in ALLOWED_SEVERITY:
        return jsonify({
            'ok': False,
            'error': f'severity muss eines von {sorted(ALLOWED_SEVERITY)} sein.'
        }), 400

    description = _normalize_description(body.get('description'))
    if not description:
        return jsonify({
            'ok': False,
            'error': f'description fehlt oder zu kurz (min {DESCRIPTION_MIN_LEN} chars).'
        }), 400

    report_id = str(uuid.uuid4())
    now_iso = datetime.now(timezone.utc).isoformat()

    row = {
        'report_id': report_id,
        'tail_reg': tail_reg,
        'system': system,
        'severity': severity,
        'description': description,
        'reported_by_token': safe_tok,
        'created_at': now_iso,
        'status': 'open',
    }

    sb_ok = _sb_insert(row)
    # Disk-fallback: immer als Sicherung schreiben, auch wenn SB ok.
    # Bei einer mehrfachen Replica-Setup waere das nur fuer den lokalen
    # Worker — fuer single-pod Render-Setup ist das ein zweiter "Beleg".
    _disk_append(row)

    if not sb_ok:
        current_app.logger.warning(
            f'[aircraft-health] report_sb_down tail={tail_reg} '
            f'tok={safe_tok[:8]} id={report_id[:8]}'
        )

    current_app.logger.info(
        f'[aircraft-health] report_in tail={tail_reg} sys={system} '
        f'sev={severity} tok={safe_tok[:8]} id={report_id[:8]} sb_ok={sb_ok}'
    )

    return jsonify({'ok': True, 'report_id': report_id})


@aircraft_health_bp.route('/api/aircraft-health/<tail_reg>/recent', methods=['GET'])
def aircraft_health_recent(tail_reg):
    """Liste der Reports fuer diese Tail in den letzten N Tagen.

    Query:
        days=<int>   default 7, max 30

    Response 200:
        {
          "ok": true,
          "tail_reg": "...",
          "days": 7,
          "count": N,
          "reports": [
            {"report_id": "...", "tail_reg": "...", "system": "ife",
             "severity": "minor", "description": "...", "created_at": "ISO",
             "status": "open"}
          ]
        }

    Privacy: KEIN reported_by_token im Output.
    """
    norm_tail = _normalize_tail_reg(tail_reg)
    if not norm_tail:
        return jsonify({'ok': False, 'error': 'tail_reg ungueltig.'}), 400

    try:
        days = int(request.args.get('days', RECENT_DEFAULT_DAYS))
    except (TypeError, ValueError):
        days = RECENT_DEFAULT_DAYS
    days = max(1, min(RECENT_MAX_DAYS, days))

    since = datetime.now(timezone.utc) - timedelta(days=days)
    since_iso = since.isoformat()

    rows = _sb_recent(norm_tail, since_iso)
    if rows is None:
        # SB down → fallback auf Disk.
        rows = _disk_recent(norm_tail, since_iso)
        current_app.logger.info(
            f'[aircraft-health] recent_disk_fallback tail={norm_tail} n={len(rows)}'
        )

    # Defensive PII-Strip — falls die Liste irgendwann mal ein 'reported_by_token'
    # mitliefert (z.B. nach einem Schema-Drift), wird es hier rausgeschmissen.
    cleaned = []
    for r in rows or []:
        cleaned.append({
            'report_id': r.get('report_id'),
            'tail_reg': r.get('tail_reg'),
            'system': r.get('system'),
            'severity': r.get('severity'),
            'description': r.get('description'),
            'created_at': r.get('created_at'),
            'status': r.get('status') or 'open',
        })

    return jsonify({
        'ok': True,
        'tail_reg': norm_tail,
        'days': days,
        'count': len(cleaned),
        'reports': cleaned,
    })
