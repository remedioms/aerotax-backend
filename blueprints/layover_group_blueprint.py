"""
Layover-Group-Meta — geteilter Layover-Plan + Meetup-Umfragen pro Gruppe.

Bisher hielt die iOS-App den gemeinsamen Layover-Plan (Ort/Hotel/Treffpunkt/
Datum/Notizen), die Meetup-Umfragen und den angepinnten Hinweis NUR lokal pro
Gerät (LayoverGroupMetaStore → OfflineCache). Dieser Blueprint speichert das
geteilte Meta-Blob server-seitig, sodass alle Gruppen-Mitglieder denselben Plan
+ dieselben Umfragen sehen.

Muster (wie dm_messages / crew_at_destination): Supabase als Primärspeicher,
Disk als Fallback bei SB-down. Plan + Polls werden als opaque JSON-Blob
durchgereicht (die iOS-Codable-Form ist die Wahrheit) — NUR der Vote-Endpoint
versteht die Poll-Struktur, um Token-basiert (cross-device-korrekt) abzustimmen.

Endpoints (token = aufrufendes Crew-Mitglied, group_id = Gruppen-ID):
  GET  /api/layover-group/<token>/meta/<group_id>          → {ok, meta}
  PUT  /api/layover-group/<token>/meta/<group_id>          → Plan/Polls/Pin upsert
  POST /api/layover-group/<token>/meta/<group_id>/vote     → Token zu Option toggeln

Vote ist atomar + merge-fähig: der Server hält pro Option eine Token-Liste
(`voter_tokens`). Ein PUT (Struktur-Änderung: Poll anlegen/löschen/schließen)
ÜBERNIMMT die bestehenden Server-Stimmen, damit keine Stimmen verloren gehen.
"""
import os
import json
import time
import re
import tempfile
from flask import Blueprint, request, jsonify

layover_group_bp = Blueprint('layover_group', __name__)

_SB_TABLE = 'layover_group_meta'
# Cloud Run: nur /tmp ist zuverlässig beschreibbar → Disk-Fallback dorthin, sonst
# schlägt makedirs auf dem read-only Container-FS fehl und der Fallback ist tot.
_DISK_DIR = os.environ.get('AEROTAX_STATE_DIR') or '/tmp/aerotax_state'


# ── Lazy-Bindings an die App (SB-Client + Bearer-Gate) ──
def _app():
    """Liefert (sb, SB_AVAILABLE, request_bearer_matches) lazy aus app.py.
    Lazy, weil der Blueprint beim Import von app.py registriert wird — ein
    Top-Level-Import würde zirkulär."""
    try:
        import app as _a
        return getattr(_a, 'sb', None), bool(getattr(_a, 'SB_AVAILABLE', False)), \
            getattr(_a, '_request_bearer_matches', None)
    except Exception:
        return None, False, None


def _safe_id(s):
    return re.sub(r'[^A-Za-z0-9_:.\-]', '', s or '')[:128]


# ── Disk-Fallback (atomar) ──
def _disk_path(group_id):
    d = os.path.join(_DISK_DIR, 'layover_group_meta')
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        return None
    return os.path.join(d, f'{_safe_id(group_id)}.json')


def _disk_load(group_id):
    p = _disk_path(group_id)
    if not p or not os.path.exists(p):
        return None
    try:
        with open(p, 'r') as f:
            return json.load(f)
    except Exception:
        return None


def _disk_save(group_id, blob):
    p = _disk_path(group_id)
    if not p:
        return False
    try:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), suffix='.tmp')
        with os.fdopen(fd, 'w') as f:
            json.dump(blob, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
        return True
    except Exception:
        return False


# ── Persistenz (SB primär, Disk-Fallback) ──
_DEFAULT = {'plan': {}, 'polls': [], 'pinned_note': '', 'updated_at': 0}


def _load(group_id):
    sb, ok, _ = _app()
    if ok and sb is not None:
        try:
            r = (sb.table(_SB_TABLE).select('*')
                 .eq('group_id', group_id).limit(1).execute())
            rows = r.data or []
            if rows:
                row = rows[0]
                return {
                    'plan': row.get('plan') or {},
                    'polls': row.get('polls') or [],
                    'pinned_note': row.get('pinned_note') or '',
                    'updated_at': float(row.get('updated_at') or 0),
                }
        except Exception as e:
            print(f'[layover_group] SB_LOAD_FAIL gid={group_id}: {type(e).__name__}: {str(e)[:200]}', flush=True)
    disk = _disk_load(group_id)
    if disk is not None:
        return {
            'plan': disk.get('plan') or {},
            'polls': disk.get('polls') or [],
            'pinned_note': disk.get('pinned_note') or '',
            'updated_at': float(disk.get('updated_at') or 0),
        }
    return dict(_DEFAULT)


def _save(group_id, blob):
    blob = {
        'group_id': group_id,
        'plan': blob.get('plan') or {},
        'polls': blob.get('polls') or [],
        'pinned_note': blob.get('pinned_note') or '',
        'updated_at': float(blob.get('updated_at') or time.time()),
    }
    sb, ok, _ = _app()
    sb_ok = False
    if ok and sb is not None:
        try:
            sb.table(_SB_TABLE).upsert(blob, on_conflict='group_id').execute()
            sb_ok = True
        except Exception as e:
            print(f'[layover_group] SB_SAVE_FAIL gid={group_id}: {type(e).__name__}: {str(e)[:200]}', flush=True)
            sb_ok = False
    # Disk immer als Read-Cache/Fallback mitschreiben.
    _disk_save(group_id, blob)
    return sb_ok


def _auth_ok(token):
    """Bearer muss == path-token sein (wie die Crew-Chat-PII-Endpoints). Die
    Gruppen-ID selbst ist die Capability (nur per Invite-Code/QR bekannt) — das
    deckt auch per-Code beigetretene Mitglieder ab, die NICHT in der Owner-
    Mitgliederliste stehen (Channel-offen, identisch zum Gruppen-Chat)."""
    _, _, matches = _app()
    if matches is None:
        return True  # Gate nicht verfügbar (lokal/Test) → nicht hart blocken
    try:
        return bool(matches(token))
    except Exception:
        return False


# ── Poll-Merge: Server-Stimmen bei Struktur-PUT bewahren ──
def _merge_poll_votes(incoming_polls, stored_polls):
    """Übernimmt für jede (poll_id, option_id), die schon existiert, die
    Server-`voter_tokens` — damit ein Struktur-PUT (Frage/Optionen/closed)
    KEINE Stimmen verliert. Neue Polls/Optionen behalten ihre (leeren) Listen."""
    by_poll = {}
    for p in (stored_polls or []):
        if isinstance(p, dict) and p.get('id'):
            opts = {}
            for o in (p.get('options') or []):
                if isinstance(o, dict) and o.get('id'):
                    opts[o['id']] = list(o.get('voter_tokens') or [])
            by_poll[p['id']] = opts
    out = []
    for p in (incoming_polls or []):
        if not isinstance(p, dict):
            continue
        p = dict(p)
        # myVote ist pro-Gerät lokal → server-seitig nie speichern.
        p.pop('myVote', None)
        stored_opts = by_poll.get(p.get('id'), {})
        new_opts = []
        for o in (p.get('options') or []):
            if not isinstance(o, dict):
                continue
            o = dict(o)
            o['voter_tokens'] = stored_opts.get(o.get('id'), list(o.get('voter_tokens') or []))
            o.pop('localVotes', None)
            new_opts.append(o)
        p['options'] = new_opts
        out.append(p)
    return out


@layover_group_bp.route('/api/layover-group/<token>/meta/<group_id>', methods=['GET'])
def get_layover_group_meta(token, group_id):
    if not _auth_ok(token):
        return jsonify({'ok': False, 'error': 'unauthorized'}), 401
    gid = _safe_id(group_id)
    if not gid:
        return jsonify({'ok': False, 'error': 'bad_group'}), 400
    meta = _load(gid)
    return jsonify({'ok': True, 'meta': meta})


@layover_group_bp.route('/api/layover-group/<token>/meta/<group_id>', methods=['PUT'])
def put_layover_group_meta(token, group_id):
    if not _auth_ok(token):
        return jsonify({'ok': False, 'error': 'unauthorized'}), 401
    gid = _safe_id(group_id)
    if not gid:
        return jsonify({'ok': False, 'error': 'bad_group'}), 400
    body = request.get_json(silent=True) or {}
    stored = _load(gid)

    # Plan + Pin: Last-Write-Wins per ARRIVAL (wer zuletzt PUTtet, gewinnt). Kein
    # Client-Timestamp-Gate — das wäre anfällig für Clock-Skew (ein Gerät mit
    # leicht nachgehender Uhr würde sonst legitime Updates abgelehnt bekommen).
    # Plan-Edits sind selten + menschengetrieben → Arrival-Order ist robust genug.
    # Nur ÜBERMITTELTE Felder überschreiben — ein reiner Poll-PUT lässt Plan/Pin stehen.
    plan = body['plan'] if isinstance(body.get('plan'), dict) else stored['plan']
    pinned = str(body['pinned_note']) if 'pinned_note' in body else stored['pinned_note']

    # Polls: Struktur aus dem Client, Stimmen vom Server bewahren.
    polls = stored['polls']
    if 'polls' in body and isinstance(body.get('polls'), list):
        polls = _merge_poll_votes(body['polls'], stored['polls'])

    blob = {'plan': plan, 'polls': polls, 'pinned_note': pinned,
            'updated_at': time.time()}
    _save(gid, blob)
    return jsonify({'ok': True, 'meta': blob})


@layover_group_bp.route('/api/layover-group/<token>/meta/<group_id>/vote', methods=['POST'])
def vote_layover_group_poll(token, group_id):
    if not _auth_ok(token):
        return jsonify({'ok': False, 'error': 'unauthorized'}), 401
    gid = _safe_id(group_id)
    if not gid:
        return jsonify({'ok': False, 'error': 'bad_group'}), 400
    body = request.get_json(silent=True) or {}
    poll_id = body.get('poll_id') or ''
    option_id = body.get('option_id') or ''   # leer = Stimme zurücknehmen
    voter = _safe_id(token)

    stored = _load(gid)
    polls = stored['polls'] or []
    changed = False
    for p in polls:
        if not isinstance(p, dict) or p.get('id') != poll_id:
            continue
        for o in (p.get('options') or []):
            if not isinstance(o, dict):
                continue
            toks = [t for t in (o.get('voter_tokens') or []) if t != voter]
            # Token genau zur gewählten Option hinzufügen (Single-Choice).
            if option_id and o.get('id') == option_id:
                toks.append(voter)
            o['voter_tokens'] = toks
        changed = True
        break
    if changed:
        stored['polls'] = polls
        stored['updated_at'] = time.time()
        _save(gid, stored)
    return jsonify({'ok': True, 'meta': stored})
