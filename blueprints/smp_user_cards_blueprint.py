# ═══════════════════════════════════════════════════════════════
#  SMP-User-Cards Blueprint — User-erstellte SMP-Flashcards (2026-08-01)
#
#  User bauen eigene Lernkarten (module/topic/front/back) in der iOS-App,
#  laden sie hoch. Der Owner prüft sie über eine Review-Queue. Freigegebene
#  Karten werden ANONYMISIERT an ALLE User als Community-Deck ausgeliefert
#  (niemals owner_token oder sonst eine Identität in einer Multi-User-
#  Antwort — Token=Credential-Regel, siehe user_search-Härtung 2026-08-01
#  in app.py: ein fremdes Token im Response-Body ist eine Account-
#  Übernahme, kein harmloses Metadatum).
#
#  Wiring in app.py:
#      from blueprints.smp_user_cards_blueprint import smp_user_cards_bp
#      app.register_blueprint(smp_user_cards_bp)
#
#  Endpunkte (KEIN Token im Pfad — das globale _bug004_token_auth_gate
#  matcht nur AT-…-Segmente in der URL, hier läuft die Bindung also
#  eigenständig über den Authorization-Bearer, exakt das user_search-Muster
#  in app.py:11195ff — "Diese Route lief bis heute völlig UNAUTHENTIFIZIERT
#  (ihr Pfad enthält kein AT-Segment, also greift das Gate nicht)"):
#      POST /api/ax/smp/user-cards          Bearer  Upsert eigener Karte
#      GET  /api/ax/smp/user-cards          Bearer  eigene Karten (alle Status)
#      GET  /api/ax/smp/community-cards     Bearer  nur approved+nicht-gelöscht,
#                                                    anonymisiert
#      GET  /api/ax/smp/review/pending      Admin   Review-Queue (pending)
#      POST /api/ax/smp/review/<id>         Admin   {"decision": approved|rejected}
#      PUT  /api/ax/smp/progress            Bearer  Geräte-Transfer-Sync: EIN
#                                                    JSON-Blob (Lernfortschritt)
#                                                    upserten (2026-08-02)
#      GET  /api/ax/smp/progress            Bearer  eigenen Fortschritt lesen
#
#  Admin-Gate: X-Admin-Token == RECOVERY_SECRET (bestehender Mechanismus,
#  identisch zu admin_support_list/ax_crew_hotels-Admin/ax_lh_quota in
#  app.py — es wurde bewusst KEIN neues AEROX_ADMIN_SECRET eingeführt, damit
#  es nur EINEN Admin-Secret-Pfad im Repo gibt).
#
#  Storage: Supabase SB primary (ax_smp_user_cards, Migration
#  supabase_migrations/20260801_smp_user_cards.sql). Kein Disk-Fallback —
#  User-Content ohne Sync-Garantie zu cachen wäre schlimmer als ein
#  ehrliches 503 (Owner-Regel „lieber keine Zeile als ein synthetisierter
#  Wert" gilt sinngemäß: lieber ein 503 als eine stille Karte, die nach dem
#  nächsten Cloud-Restart wieder weg ist).
#
#  /api/ax/smp/progress liegt in derselben (noch nicht angewendeten)
#  Migration, zweite Tabelle ax_smp_progress — EIN JSON-Blob pro
#  owner_token, damit ein Geräte-Wechsel (altes iPhone -> neues iPhone) den
#  SMP-Lernfortschritt nicht auf Null zurücksetzt. Gleiches Auth-/Storage-
#  Muster wie oben (Tri-State-Bearer, service-role-only, kein Disk-Fallback).
# ═══════════════════════════════════════════════════════════════

import html as _html_lib
import re
import uuid as _uuid
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify, current_app

smp_user_cards_bp = Blueprint('smp_user_cards', __name__)


# ─── Lazy app.py-Zugriff ────────────────────────────────────────
# _validate_token/_request_bearer_token/_recovery_pepper/_token_rate_limited
# werden erst weit unten in app.py definiert (nach dem Blueprint-
# Registrierungs-Loop) — ein `from app import ...` auf Modulebene würde beim
# Import also mit AttributeError sterben. Re-Import zur Request-Zeit (analog
# blueprints/flight_profile_blueprint.py:_app_attr) lädt die Attribute erst,
# wenn app.py komplett durchgelaufen ist.
def _app_attr(name, default=None):
    try:
        import app as _app_mod
        return getattr(_app_mod, name, default)
    except Exception:
        return default


def _sb_client():
    """Lazy re-resolve (thread-local SB-Proxy, analog hotel_rooms_blueprint)."""
    sb = _app_attr('sb')
    ok = bool(_app_attr('SB_AVAILABLE', False)) and sb is not None
    return sb, ok


def _log():
    try:
        return current_app.logger
    except RuntimeError:
        import logging
        return logging.getLogger('smp_user_cards')


# ─── Bearer-Auth (kein Pfad-Token — Bindung läuft rein über den Header) ──
def _authed_token():
    """Validiert den Authorization-Bearer gegen auth_users.

    Returns (token, None) wenn gültig, sonst (None, (response, status)).
    Tri-State-Semantik wie app.py:_validate_token — ein Store-Ausfall (503,
    Retry-After) wird NIE als "unauthorized" (401) beantwortet, damit ein
    transienter Supabase-Hickup keinen Client-Logout auslöst."""
    bearer_fn = _app_attr('_request_bearer_token')
    validate_fn = _app_attr('_validate_token')
    if not callable(bearer_fn) or not callable(validate_fn):
        # app.py noch nicht vollständig geladen (z.B. Unit-Test-Import) —
        # fail-closed statt fail-open.
        return None, (jsonify({'ok': False, 'error': 'auth_unavailable'}), 503)
    bearer = bearer_fn()
    if not bearer:
        return None, (jsonify({'ok': False, 'error': 'auth_required'}), 401)
    validation = validate_fn(bearer)
    state = getattr(validation.state, 'value', None)
    if state == 'unavailable':
        unavailable_fn = _app_attr('_auth_store_unavailable_response')
        if callable(unavailable_fn):
            return None, unavailable_fn()
        resp = jsonify({'ok': False, 'error': 'auth_store_unavailable'})
        resp.status_code = 503
        return None, resp
    if state != 'valid':
        return None, (jsonify({'ok': False, 'error': 'unauthorized'}), 401)
    return bearer, None


def _admin_ok():
    """X-Admin-Token == RECOVERY_SECRET, constant-time. Bestehender Mechanismus
    (app.py: admin_support_list, ax_crew_hotels-Admin, ax_lh_quota)."""
    import hmac as _hmac
    pepper_fn = _app_attr('_recovery_pepper')
    if not callable(pepper_fn):
        return False
    try:
        expected = pepper_fn()
    except Exception:
        return False
    got = request.headers.get('X-Admin-Token', '')
    return bool(expected and got and _hmac.compare_digest(got, expected))


def _rate_limited(token, endpoint, limit, window_sec):
    fn = _app_attr('_token_rate_limited')
    if not callable(fn):
        return False
    try:
        return bool(fn(token, endpoint, limit, window_sec))
    except Exception:
        return False


# ─── Validation / Sanitize ──────────────────────────────────────
FRONT_MAX = 300
BACK_MAX = 1200
TOPIC_MAX = 120
ALLOWED_MODULES = {'bwl', 'kommunikation', 'fuehren', 'servicemanagement'}
ALLOWED_DECISIONS = {'approved', 'rejected'}

_TAG_RE = re.compile(r'<[^>]+>')
_WS_RE = re.compile(r'\s+')


def _strip_html(s):
    """Tags raus, Entities dekodiert, Whitespace kollabiert. Reiner Text bleibt
    unangetastet (Zeilenumbrüche in Karteninhalten sind gewollt lesbar) —
    Absatzstruktur ist für Flashcard-Front/Back nicht relevant, anders als bei
    news_blueprint._html_to_paragraph_text."""
    if not s or not isinstance(s, str):
        return ''
    no_tags = _TAG_RE.sub(' ', s)
    unescaped = _html_lib.unescape(no_tags)
    return _WS_RE.sub(' ', unescaped).strip()


def _valid_uuid(raw):
    if not raw or not isinstance(raw, str):
        return None
    try:
        return str(_uuid.UUID(raw))
    except (ValueError, AttributeError, TypeError):
        return None


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


# ─── Output-Shapes (PII-strip) ──────────────────────────────────
def _own_card_view(row):
    """Eigene Karte — alle Felder AUSSER owner_token."""
    return {
        'id': row.get('id'),
        'module': row.get('module'),
        'topic': row.get('topic'),
        'front': row.get('front'),
        'back': row.get('back'),
        'status': row.get('status'),
        'deleted': bool(row.get('deleted')),
        'created_at': row.get('created_at'),
        'updated_at': row.get('updated_at'),
    }


def _community_card_view(row):
    """Community-Karte — NUR id, module, topic, front, back. Niemals
    owner_token/status/Zeitstempel (Token=Credential-Regel + keine unnötige
    Identitäts-Fläche)."""
    return {
        'id': row.get('id'),
        'module': row.get('module'),
        'topic': row.get('topic'),
        'front': row.get('front'),
        'back': row.get('back'),
    }


def _review_card_view(row):
    """Review-Queue für den Owner — bewusst OHNE owner_token: die
    Freigabe-Entscheidung braucht nur den Inhalt, nicht wer ihn geschrieben
    hat (Prinzip minimaler Exposition, auch gegenüber dem eigenen Admin-Tool)."""
    return {
        'id': row.get('id'),
        'module': row.get('module'),
        'topic': row.get('topic'),
        'front': row.get('front'),
        'back': row.get('back'),
        'created_at': row.get('created_at'),
    }


# ─── POST /api/ax/smp/user-cards — Upsert ───────────────────────
@smp_user_cards_bp.route('/api/ax/smp/user-cards', methods=['POST'])
def smp_upsert_user_card():
    token, err = _authed_token()
    if err:
        return err

    if _rate_limited(token, 'smp_user_card_write', limit=60, window_sec=3600):
        return jsonify({'ok': False, 'error': 'rate_limited'}), 429

    sb, sb_ok = _sb_client()
    if not sb_ok:
        return jsonify({'ok': False, 'error': 'storage_unavailable'}), 503

    body = request.get_json(silent=True) or {}
    card_id = _valid_uuid(str(body.get('id') or ''))
    if not card_id:
        return jsonify({'ok': False, 'error': 'invalid_id'}), 400

    module = (body.get('module') or '').strip().lower()
    if module not in ALLOWED_MODULES:
        return jsonify({'ok': False, 'error': 'invalid_module'}), 400

    front = _strip_html(body.get('front') or '')
    back = _strip_html(body.get('back') or '')
    if not front or len(front) > FRONT_MAX:
        return jsonify({'ok': False, 'error': 'invalid_front'}), 400
    if not back or len(back) > BACK_MAX:
        return jsonify({'ok': False, 'error': 'invalid_back'}), 400

    topic_raw = body.get('topic')
    topic = _strip_html(topic_raw)[:TOPIC_MAX] if topic_raw else None

    deleted_provided = 'deleted' in body
    deleted = bool(body.get('deleted')) if deleted_provided else None

    now = _now_iso()
    try:
        existing = (sb.table('ax_smp_user_cards')
                    .select('id,owner_token,status,front,back,deleted')
                    .eq('id', card_id)
                    .limit(1)
                    .execute())
        rows = existing.data or []
        if rows:
            row = rows[0]
            if row.get('owner_token') != token:
                # Niemals "existiert, gehört aber jemand anderem" leaken —
                # exakt das hotel_rooms-Muster (_sb_owner_delete).
                return jsonify({'ok': False, 'error': 'not_found'}), 404
            update_fields = {
                'module': module,
                'topic': topic,
                'front': front,
                'back': back,
                'updated_at': now,
            }
            if deleted_provided:
                update_fields['deleted'] = deleted
            # Inhaltsänderung an einer bereits freigegebenen Karte geht zurück
            # in die Review-Queue — sonst könnte ein User nach Freigabe den
            # Text unbemerkt gegen etwas anderes tauschen, das nie geprüft
            # wurde ("approved" bliebe stehen, obwohl der Inhalt neu ist).
            if (row.get('status') == 'approved'
                    and (front != row.get('front') or back != row.get('back'))):
                update_fields['status'] = 'pending'
            (sb.table('ax_smp_user_cards')
             .update(update_fields)
             .eq('id', card_id)
             .eq('owner_token', token)
             .execute())
        else:
            insert_row = {
                'id': card_id,
                'owner_token': token,
                'module': module,
                'topic': topic,
                'front': front,
                'back': back,
                'status': 'pending',
                'deleted': bool(deleted) if deleted_provided else False,
                'created_at': now,
                'updated_at': now,
            }
            sb.table('ax_smp_user_cards').insert(insert_row).execute()
    except Exception as e:
        _log().warning(
            f'[smp-user-cards] upsert_fail err={type(e).__name__}: {str(e)[:160]}'
        )
        return jsonify({'ok': False, 'error': 'storage_error'}), 503

    return jsonify({'ok': True, 'id': card_id})


# ─── GET /api/ax/smp/user-cards — eigene Karten ─────────────────
@smp_user_cards_bp.route('/api/ax/smp/user-cards', methods=['GET'])
def smp_list_user_cards():
    token, err = _authed_token()
    if err:
        return err

    sb, sb_ok = _sb_client()
    if not sb_ok:
        return jsonify({'ok': False, 'error': 'storage_unavailable'}), 503

    try:
        out = []
        offset = 0
        page = 1000
        while True:
            r = (sb.table('ax_smp_user_cards')
                 .select('id,module,topic,front,back,status,deleted,'
                         'created_at,updated_at')
                 .eq('owner_token', token)
                 .order('created_at', desc=True)
                 .range(offset, offset + page - 1)
                 .execute())
            rows = r.data or []
            out.extend(_own_card_view(row) for row in rows)
            if len(rows) < page:
                break
            offset += page
    except Exception as e:
        _log().warning(
            f'[smp-user-cards] list_own_fail err={type(e).__name__}: {str(e)[:160]}'
        )
        return jsonify({'ok': False, 'error': 'storage_error'}), 503

    return jsonify({'ok': True, 'cards': out, 'count': len(out)})


# ─── GET /api/ax/smp/community-cards — anonymisiertes Deck ──────
@smp_user_cards_bp.route('/api/ax/smp/community-cards', methods=['GET'])
def smp_list_community_cards():
    _token, err = _authed_token()
    if err:
        return err

    sb, sb_ok = _sb_client()
    if not sb_ok:
        return jsonify({'ok': False, 'error': 'storage_unavailable'}), 503

    try:
        out = []
        offset = 0
        page = 1000
        while True:
            # PostgREST-Default-LIMIT ist 1000 — ohne explizites .range() würde
            # ein wachsendes Community-Deck ab Karte 1001 unsichtbar (Repo-
            # Lehre aus dem PostgREST-LIMIT-1000-Vorfall). Der Loop paginiert
            # explizit statt sich auf einen impliziten Default zu verlassen.
            r = (sb.table('ax_smp_user_cards')
                 .select('id,module,topic,front,back')
                 .eq('status', 'approved')
                 .eq('deleted', False)
                 .order('created_at', desc=True)
                 .range(offset, offset + page - 1)
                 .execute())
            rows = r.data or []
            out.extend(_community_card_view(row) for row in rows)
            if len(rows) < page:
                break
            offset += page
    except Exception as e:
        _log().warning(
            f'[smp-user-cards] list_community_fail err={type(e).__name__}: {str(e)[:160]}'
        )
        return jsonify({'ok': False, 'error': 'storage_error'}), 503

    return jsonify({'ok': True, 'cards': out, 'count': len(out)})


# ─── GET /api/ax/smp/review/pending — Owner-Review-Queue ────────
@smp_user_cards_bp.route('/api/ax/smp/review/pending', methods=['GET'])
def smp_review_pending():
    if not _admin_ok():
        return jsonify({'ok': False, 'error': 'forbidden'}), 403

    sb, sb_ok = _sb_client()
    if not sb_ok:
        return jsonify({'ok': False, 'error': 'storage_unavailable'}), 503

    try:
        out = []
        offset = 0
        page = 1000
        while True:
            r = (sb.table('ax_smp_user_cards')
                 .select('id,module,topic,front,back,created_at')
                 .eq('status', 'pending')
                 .eq('deleted', False)
                 .order('created_at', desc=False)
                 .range(offset, offset + page - 1)
                 .execute())
            rows = r.data or []
            out.extend(_review_card_view(row) for row in rows)
            if len(rows) < page:
                break
            offset += page
    except Exception as e:
        _log().warning(
            f'[smp-user-cards] review_pending_fail err={type(e).__name__}: {str(e)[:160]}'
        )
        return jsonify({'ok': False, 'error': 'storage_error'}), 503

    return jsonify({'ok': True, 'cards': out, 'count': len(out)})


# ─── POST /api/ax/smp/review/<id> — Freigabe/Ablehnung ──────────
@smp_user_cards_bp.route('/api/ax/smp/review/<card_id>', methods=['POST'])
def smp_review_decide(card_id):
    if not _admin_ok():
        return jsonify({'ok': False, 'error': 'forbidden'}), 403

    cid = _valid_uuid(card_id)
    if not cid:
        return jsonify({'ok': False, 'error': 'invalid_id'}), 400

    body = request.get_json(silent=True) or {}
    decision = (body.get('decision') or '').strip().lower()
    if decision not in ALLOWED_DECISIONS:
        return jsonify({'ok': False, 'error': 'invalid_decision'}), 400

    sb, sb_ok = _sb_client()
    if not sb_ok:
        return jsonify({'ok': False, 'error': 'storage_unavailable'}), 503

    try:
        existing = (sb.table('ax_smp_user_cards')
                    .select('id')
                    .eq('id', cid)
                    .limit(1)
                    .execute())
        if not (existing.data or []):
            return jsonify({'ok': False, 'error': 'not_found'}), 404
        (sb.table('ax_smp_user_cards')
         .update({'status': decision, 'updated_at': _now_iso()})
         .eq('id', cid)
         .execute())
    except Exception as e:
        _log().warning(
            f'[smp-user-cards] review_decide_fail err={type(e).__name__}: {str(e)[:160]}'
        )
        return jsonify({'ok': False, 'error': 'storage_error'}), 503

    return jsonify({'ok': True, 'id': cid, 'status': decision})


# ═══════════════════════════════════════════════════════════════
#  Geräte-Transfer-Sync: SMP-Lernfortschritt (2026-08-02)
#
#  EIN JSON-Blob pro User (iOS führt den kompletten Lernstand lokal; dieser
#  Endpoint ist nur die Kopie fürs nächste Gerät). Storage: ax_smp_progress
#  (zweite Tabelle in derselben — noch nicht angewendeten — Migration
#  supabase_migrations/20260801_smp_user_cards.sql). Gleiches Auth-Muster
#  wie oben (_authed_token, Tri-State), service-role-only.
# ═══════════════════════════════════════════════════════════════

# 256 KB Cap. DoS-Lehre des Repos (app.py:post_telemetry_diagnostics /
# _MK_MAX_BODY_BYTES): der Größen-Check muss VOR dem JSON-Parse laufen,
# sonst kostet ein riesiger Body schon CPU/Speicher, bevor er verworfen wird.
_PROGRESS_MAX_BODY_BYTES = 256 * 1024


def _parse_client_updated_at(raw):
    """Validiert das vom Client gesendete ISO-Datum, BEVOR es an Supabase
    geht — ein kaputter String soll ein sauberes 400 geben, nicht ein
    storage_error, das wie ein Supabase-Ausfall aussieht. Der Wert selbst
    wird unverändert gespeichert (Client führt seine eigene Versions-Uhr)."""
    if not raw or not isinstance(raw, str):
        return None
    candidate = raw.strip()
    if not candidate:
        return None
    probe = candidate[:-1] + '+00:00' if candidate.endswith('Z') else candidate
    try:
        datetime.fromisoformat(probe)
    except (ValueError, TypeError):
        return None
    return candidate


# ─── PUT /api/ax/smp/progress — Upsert des Lernfortschritt-Blobs ────
@smp_user_cards_bp.route('/api/ax/smp/progress', methods=['PUT'])
def smp_put_progress():
    token, err = _authed_token()
    if err:
        return err

    # Größen-Cap ZUERST: Content-Length wenn gesetzt, sonst gelesene Bytes —
    # vor jedem JSON-Parse (siehe Modul-Docstring oben).
    if (request.content_length or 0) > _PROGRESS_MAX_BODY_BYTES:
        return jsonify({'ok': False, 'error': 'payload_too_large'}), 413
    raw = request.get_data(cache=True) or b''
    if len(raw) > _PROGRESS_MAX_BODY_BYTES:
        return jsonify({'ok': False, 'error': 'payload_too_large'}), 413

    if _rate_limited(token, 'smp_progress_write', limit=30, window_sec=3600):
        return jsonify({'ok': False, 'error': 'rate_limited'}), 429

    sb, sb_ok = _sb_client()
    if not sb_ok:
        return jsonify({'ok': False, 'error': 'storage_unavailable'}), 503

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({'ok': False, 'error': 'body_must_be_object'}), 400

    blob = body.get('blob')
    if blob is None:
        return jsonify({'ok': False, 'error': 'invalid_blob'}), 400

    client_updated_at = _parse_client_updated_at(body.get('updated_at'))
    if not client_updated_at:
        return jsonify({'ok': False, 'error': 'invalid_updated_at'}), 400

    try:
        (sb.table('ax_smp_progress')
         .upsert({
             'owner_token': token,
             'blob': blob,
             'updated_at': client_updated_at,
             'server_updated_at': _now_iso(),
         }, on_conflict='owner_token')
         .execute())
    except Exception as e:
        _log().warning(
            f'[smp-progress] upsert_fail err={type(e).__name__}: {str(e)[:160]}'
        )
        return jsonify({'ok': False, 'error': 'storage_error'}), 503

    return jsonify({'ok': True, 'updated_at': client_updated_at})


# ─── GET /api/ax/smp/progress — eigener Lernfortschritt ─────────────
@smp_user_cards_bp.route('/api/ax/smp/progress', methods=['GET'])
def smp_get_progress():
    token, err = _authed_token()
    if err:
        return err

    sb, sb_ok = _sb_client()
    if not sb_ok:
        return jsonify({'ok': False, 'error': 'storage_unavailable'}), 503

    try:
        r = (sb.table('ax_smp_progress')
             .select('blob,updated_at')
             .eq('owner_token', token)
             .limit(1)
             .execute())
        rows = r.data or []
    except Exception as e:
        _log().warning(
            f'[smp-progress] get_fail err={type(e).__name__}: {str(e)[:160]}'
        )
        return jsonify({'ok': False, 'error': 'storage_error'}), 503

    if not rows:
        # Kein echtes 404 — device-transfer-sync ist ein Normalfall ohne
        # Vorgeschichte (frisches Gerät, noch kein Fortschritt hochgeladen).
        return jsonify({'ok': True, 'blob': None, 'updated_at': None})

    row = rows[0]
    return jsonify({
        'ok': True,
        'blob': row.get('blob'),
        'updated_at': row.get('updated_at'),
    })
