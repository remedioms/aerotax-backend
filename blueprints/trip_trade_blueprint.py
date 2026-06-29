# ═══════════════════════════════════════════════════════════════
#  Trip-Trade Board Blueprint (Worker P6b, 2026-06-01)
#
#  Open-Time / Swap / Pickup-Marketplace für Crew-Touren.
#  iOS-Client: /Users/miguelschumann/Desktop/aeris-ios/AeroTax/AeroTax/TripTrade/
#
#  Wiring in app.py:
#      from blueprints.trip_trade_blueprint import trip_trade_bp
#      app.register_blueprint(trip_trade_bp)
#
#  Endpunkte:
#      POST   /api/trade/<token>/post                       — Trade-Post anlegen (3/day)
#      GET    /api/trade/board                              — Gefiltertes Board listen
#      GET    /api/trade/<token>/my-posts                   — Eigene Posts (any status)
#      DELETE /api/trade/<token>/post/<post_id>             — Soft-Delete (nur Author)
#      POST   /api/trade/<token>/express-interest/<post_id> — Interesse anmelden
#      GET    /api/trade/<token>/incoming-interests         — Interessen auf eigene Posts
#      POST   /api/trade/<token>/post/<post_id>/close       — Post schließen
#
#  Persistenz:
#      · Supabase primär (Tabellen `trade_posts`, `trade_interests`).
#      · Disk-Fallback bei SB-down:
#          - trade_posts:    {_USER_HISTORY_DIR}/trade_posts.json (global, list of dicts)
#          - trade_interests: {_USER_HISTORY_DIR}/trade_interests_{token}.json
#            (eine Datei pro Author-Token, enthält Interessen die FÜR DIESEN
#             Author angemeldet wurden — d.h. die "incoming" Liste).
#      · Lazy-Migrate: bei jedem Disk-Write tracken wir nichts extra — sobald
#        SB wieder oben ist, schreibt die nächste Operation dorthin. Live-Posts
#        die in der SB-Down-Phase entstanden sind, bleiben auf Disk bis ein
#        explizites Re-Sync-Tool sie hochzieht (kein automatisches "drift catch-up"
#        weil das in der Praxis selten ist und Konflikte produzieren würde).
#
#  Self-Trade-Prevention:
#      express-interest auf einen eigenen Post → 400, kein DB-Write.
#      Author-only delete: DELETE auf fremden Post → 404, kein Soft-Delete.
#
#  Rate-Limit:
#      POST /api/trade/<token>/post: 3 Posts pro Tag pro Token. Versucht
#      `_token_rate_limited` aus app.py zu importieren — falls nicht möglich,
#      fallback auf lokales Counter-Dict mit gleicher Semantik (sliding window).
# ═══════════════════════════════════════════════════════════════

import json
import os
import re
import time
import threading
import uuid
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify, current_app

trip_trade_bp = Blueprint('trip_trade', __name__)


# ─── Lazy-Imports aus app.py (mit graceful Fallback) ──────────────────
#
# Wir importieren NICHT auf Modul-Ebene weil das in Tests / lokalem Dev ohne
# vollständige app.py-Boot-Sequenz scheitern würde (Stripe-Keys etc.). Stattdessen
# lazy-resolve via getattr im jeweiligen Helper.

def _get_app_module():
    """Holt das app-Modul (mit den globalen Helpern), oder None."""
    try:
        import app as _app_module  # noqa: F401
        return _app_module
    except Exception:
        return None


def _get_sb():
    """Returns (sb_client, sb_available_bool). Bei Importfehler (sb_None, False)."""
    m = _get_app_module()
    if m is None:
        return None, False
    return getattr(m, 'sb', None), bool(getattr(m, 'SB_AVAILABLE', False))


def _get_user_history_dir():
    """Pfad für Disk-Fallback. Default '_user_history_state' falls app nicht ladbar."""
    m = _get_app_module()
    if m is not None:
        d = getattr(m, '_USER_HISTORY_DIR', None)
        if d:
            return d
    return '_user_history_state'


# ─── Rate-Limit Helper (try import _token_rate_limited, sonst lokal) ──

_LOCAL_RATE_LOCK = threading.Lock()
_LOCAL_RATE_BUCKETS = {}


def _rate_limited(token, endpoint, limit, window_sec):
    """True wenn das Token-Limit erreicht ist. Versucht app._token_rate_limited
    zu benutzen; fallback auf lokales sliding-window Bucket-Dict."""
    if not token:
        return False
    m = _get_app_module()
    if m is not None:
        fn = getattr(m, '_token_rate_limited', None)
        if callable(fn):
            try:
                return bool(fn(token, endpoint, limit, window_sec))
            except Exception:
                # Fall-through zu lokalem Bucket — niemals silently zulassen
                pass
    now = time.time()
    cutoff = now - window_sec
    key = f'tok:{token}:{endpoint}'
    with _LOCAL_RATE_LOCK:
        bucket = _LOCAL_RATE_BUCKETS.setdefault(key, [])
        bucket[:] = [t for t in bucket if t > cutoff]
        if len(bucket) >= limit:
            return True
        bucket.append(now)
        # Cleanup wenn dict zu groß
        if len(_LOCAL_RATE_BUCKETS) > 5000:
            for k in list(_LOCAL_RATE_BUCKETS.keys())[:2500]:
                _LOCAL_RATE_BUCKETS.pop(k, None)
        return False


# ─── Push-Notification Helper (best effort) ───────────────────────────

def _push(token, title, body, data=None):
    """Best-Effort Push an `token`. Stillschweigend OK wenn nicht verfügbar."""
    m = _get_app_module()
    if m is None:
        return False
    fn = getattr(m, '_send_push_notification', None)
    if not callable(fn):
        return False
    try:
        return bool(fn(token, title, body, data))
    except Exception:
        return False


# ─── Disk-Paths ───────────────────────────────────────────────────────

def _trade_posts_disk_path():
    d = _get_user_history_dir()
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, 'trade_posts.json')


def _trade_interests_disk_path(author_token):
    """Disk-Path für die Liste der INCOMING Interessen eines Author-Tokens.
    Wir speichern Interessen pro Empfangs-Author, weil das die häufigste
    Read-Query ist (/incoming-interests). Bei express-interest wird sowohl
    diese Datei aktualisiert als auch ein flacher Lookup über alle posts
    möglich (für interest_count im Board)."""
    d = _get_user_history_dir()
    os.makedirs(d, exist_ok=True)
    safe = re.sub(r'[^A-Za-z0-9_-]', '', author_token or '')[:64]
    if not safe:
        return None
    return os.path.join(d, f'trade_interests_{safe}.json')


def _load_disk_posts():
    """Lädt globale Posts-Liste vom Disk. [] bei FileNotFound/JSON-Fehler."""
    p = _trade_posts_disk_path()
    try:
        with open(p) as f:
            data = json.load(f) or []
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    except Exception:
        return []


def _save_disk_posts(posts):
    """Schreibt globale Posts-Liste. Atomarer rewrite via tmp+rename
    NICHT nötig hier — best effort, bei Crash haben wir SB als Wahrheit."""
    p = _trade_posts_disk_path()
    try:
        with open(p, 'w') as f:
            json.dump(posts[-10000:], f, ensure_ascii=False, default=str)
        return True
    except Exception as e:
        try:
            current_app.logger.error(
                f'[trade] disk_save_posts_fail err={type(e).__name__}: {str(e)[:200]}'
            )
        except Exception:
            pass
        return False


def _load_disk_interests(author_token):
    p = _trade_interests_disk_path(author_token)
    if not p:
        return []
    try:
        with open(p) as f:
            data = json.load(f) or []
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    except Exception:
        return []


def _save_disk_interests(author_token, interests):
    p = _trade_interests_disk_path(author_token)
    if not p:
        return False
    try:
        with open(p, 'w') as f:
            json.dump(interests[-5000:], f, ensure_ascii=False, default=str)
        return True
    except Exception:
        return False


# ─── Persistence-Layer: trade_posts ───────────────────────────────────

def _sb_insert_post(row):
    """Inserts row in SB. True/False.

    DURABILITÄT-FALLBACK (2026-06-29): Das `aircraft`-Feld ist neu. Falls die
    Migration (add column aircraft) im Ziel-Projekt noch NICHT eingespielt ist,
    würde der Insert mit der unbekannten Spalte scheitern und der Post NUR auf
    der ephemeren Disk landen. Deshalb: bei einem Insert-Fehler EINMAL ohne das
    optionale `aircraft`-Feld erneut versuchen, bevor wir aufgeben. So bleibt
    der Post in SB durabel; nur das Muster fehlt bis die Migration läuft."""
    sb, available = _get_sb()
    if not available or sb is None:
        return False
    try:
        sb.table('trade_posts').insert(row).execute()
        return True
    except Exception as e:
        # Retry ohne optionale Neu-Spalte(n), wenn vorhanden.
        if isinstance(row, dict) and row.get('aircraft') is not None:
            slim = {k: v for k, v in row.items() if k != 'aircraft'}
            try:
                sb.table('trade_posts').insert(slim).execute()
                try:
                    current_app.logger.warning(
                        '[trade] sb_insert_post retried without aircraft column '
                        '(migration pending?)'
                    )
                except Exception:
                    pass
                return True
            except Exception:
                pass
        try:
            current_app.logger.warning(
                f'[trade] sb_insert_post_fail err={type(e).__name__}: {str(e)[:200]}'
            )
        except Exception:
            pass
        return False


def _sb_load_post_by_id(post_id):
    sb, available = _get_sb()
    if not available or sb is None:
        return None
    try:
        r = sb.table('trade_posts').select('*').eq('id', post_id).limit(1).execute()
        data = list(r.data or [])
        return data[0] if data else None
    except Exception:
        return None


def _sb_update_post(post_id, fields):
    sb, available = _get_sb()
    if not available or sb is None:
        return False
    try:
        fields = dict(fields or {})
        fields['updated_at'] = datetime.now(timezone.utc).isoformat()
        sb.table('trade_posts').update(fields).eq('id', post_id).execute()
        return True
    except Exception as e:
        try:
            current_app.logger.warning(
                f'[trade] sb_update_post_fail id={post_id} err={type(e).__name__}: {str(e)[:200]}'
            )
        except Exception:
            pass
        return False


def _sb_list_posts(airline=None, base=None, qualification=None, position=None,
                   swap_or_dump=None, aircraft=None, limit=50, offset=0):
    """Filtered list nur open + not-deleted. Sort: tour_start_date asc, created_at desc."""
    sb, available = _get_sb()
    if not available or sb is None:
        return None
    try:
        q = sb.table('trade_posts').select('*').eq('deleted', False).eq('status', 'open')
        if airline:
            q = q.eq('airline', airline)
        if base:
            q = q.eq('base', base)
        if position:
            q = q.eq('position', position)
        if aircraft:
            q = q.eq('aircraft', aircraft)
        if swap_or_dump:
            q = q.eq('swap_or_dump', swap_or_dump)
        if qualification:
            # Qualification ist Freitext — wir machen ilike-substring-match.
            q = q.ilike('qualification_required', f'%{qualification}%')
        q = q.order('tour_start_date', desc=False).order('created_at', desc=True)
        q = q.range(offset, offset + limit - 1)
        r = q.execute()
        return list(r.data or [])
    except Exception as e:
        try:
            current_app.logger.warning(
                f'[trade] sb_list_posts_fail err={type(e).__name__}: {str(e)[:200]}'
            )
        except Exception:
            pass
        return None


def _sb_list_my_posts(token):
    sb, available = _get_sb()
    if not available or sb is None:
        return None
    try:
        r = (sb.table('trade_posts').select('*')
             .eq('author_token', token)
             .eq('deleted', False)
             .order('created_at', desc=True)
             .execute())
        return list(r.data or [])
    except Exception:
        return None


# ─── Persistence-Layer: trade_interests ───────────────────────────────

def _sb_insert_interest(row):
    sb, available = _get_sb()
    if not available or sb is None:
        return False
    try:
        sb.table('trade_interests').insert(row).execute()
        return True
    except Exception as e:
        try:
            current_app.logger.warning(
                f'[trade] sb_insert_interest_fail err={type(e).__name__}: {str(e)[:200]}'
            )
        except Exception:
            pass
        return False


def _sb_list_interests_for_author(author_token):
    """Lädt alle Interessen auf Posts des author_token. Macht 2 Queries:
    1) post_ids von author, 2) interests für diese post_ids."""
    sb, available = _get_sb()
    if not available or sb is None:
        return None
    try:
        r1 = (sb.table('trade_posts').select('id')
              .eq('author_token', author_token)
              .execute())
        ids = [p['id'] for p in (r1.data or []) if isinstance(p, dict) and p.get('id')]
        if not ids:
            return []
        r2 = (sb.table('trade_interests').select('*')
              .in_('post_id', ids)
              .order('created_at', desc=True)
              .execute())
        return list(r2.data or [])
    except Exception as e:
        try:
            current_app.logger.warning(
                f'[trade] sb_list_incoming_fail err={type(e).__name__}: {str(e)[:200]}'
            )
        except Exception:
            pass
        return None


def _sb_count_interests_for_post(post_id):
    sb, available = _get_sb()
    if not available or sb is None:
        return None
    try:
        r = (sb.table('trade_interests').select('id', count='exact')
             .eq('post_id', post_id).execute())
        return int(r.count or 0)
    except Exception:
        return None


# ─── Unified Read-Helpers (SB primär, Disk-Fallback) ──────────────────

def _load_post_by_id(post_id):
    """Liest Post via SB primär; fallback Disk-Sweep."""
    p = _sb_load_post_by_id(post_id)
    if p is not None:
        return p
    for row in _load_disk_posts():
        if isinstance(row, dict) and row.get('id') == post_id:
            return row
    return None


def _persist_post(row):
    """Schreibt Post in SB + Disk (Disk als persistenter Fallback-Mirror).

    Gibt (sb_ok, disk_ok) zurück — der echte SB-Insert-Status wird NICHT mehr
    geschluckt. Wenn SB up ist aber den Insert ablehnt (Constraint/NOT-NULL/PGRST),
    landet der Post nur auf der ephemeren Disk; das wird dem Caller via Flag
    sichtbar gemacht und beim nächsten Read (_list_posts) nach SB reconciled."""
    sb_ok = _sb_insert_post(row)
    # Disk mirror auch bei SB-Erfolg — robuster bei späterer SB-Outage.
    posts = _load_disk_posts()
    posts.append(row)
    disk_ok = _save_disk_posts(posts)
    return sb_ok, disk_ok


def _update_post(post_id, fields):
    """SB-Update + Disk-Spiegel."""
    sb_ok = _sb_update_post(post_id, fields)
    posts = _load_disk_posts()
    changed = False
    for i, row in enumerate(posts):
        if isinstance(row, dict) and row.get('id') == post_id:
            posts[i] = {**row, **fields,
                        'updated_at': datetime.now(timezone.utc).isoformat()}
            changed = True
            break
    if changed:
        _save_disk_posts(posts)
    return sb_ok or changed


def _sb_post_exists(post_id):
    """True wenn der Post bereits in SB liegt. None bei SB-down (= unklar)."""
    if not post_id:
        return None
    sb, available = _get_sb()
    if not available or sb is None:
        return None
    try:
        r = sb.table('trade_posts').select('id').eq('id', post_id).limit(1).execute()
        return bool(list(r.data or []))
    except Exception:
        return None


def _reconcile_posts_disk_to_sb():
    """Read-Time-Reconcile: Disk-only-Posts (entstanden während SB-Outage ODER
    SB-up-aber-Insert-rejected) werden nach SB geheilt. Analog license_wallet
    lazy-migrate. Best-effort — bei SB-down passiert nichts, bei Insert-Fehler
    bleibt der Post auf Disk und wird beim nächsten Read erneut versucht.

    Idempotent: wir prüfen pro Post-ID ob er schon in SB liegt; nur fehlende
    werden inserted."""
    sb, available = _get_sb()
    if not available or sb is None:
        return
    disk = _load_disk_posts()
    if not disk:
        return
    healed = 0
    for row in disk:
        if not isinstance(row, dict):
            continue
        pid = row.get('id')
        if not pid:
            continue
        exists = _sb_post_exists(pid)
        if exists is None:
            return  # SB mittendrin weg → Reconcile abbrechen, nächster Read retry
        if exists:
            continue
        if _sb_insert_post(row):
            healed += 1
    if healed:
        try:
            current_app.logger.info(f'[trade] reconcile_posts_disk_to_sb healed={healed}')
        except Exception:
            pass


def _list_posts(airline=None, base=None, qualification=None, position=None,
                swap_or_dump=None, aircraft=None, limit=50, offset=0):
    """SB primär; bei SB-down filtert/sortiert wir die Disk-Liste.

    Vor dem SB-Read werden Disk-only-Posts nach SB reconciled (heal), damit
    während einer SB-Outage ODER eines SB-Insert-Rejects entstandene Posts
    nicht beim Cloud-Run-Restart verloren gehen."""
    _reconcile_posts_disk_to_sb()
    sb_rows = _sb_list_posts(airline=airline, base=base, qualification=qualification,
                             position=position, swap_or_dump=swap_or_dump,
                             aircraft=aircraft, limit=limit, offset=offset)
    if sb_rows is not None:
        return sb_rows
    # Disk-Fallback
    disk = _load_disk_posts()
    out = []
    for r in disk:
        if not isinstance(r, dict):
            continue
        if r.get('deleted'):
            continue
        if r.get('status') != 'open':
            continue
        if airline and r.get('airline') != airline:
            continue
        if base and r.get('base') != base:
            continue
        if position and r.get('position') != position:
            continue
        if aircraft and (r.get('aircraft') or '') != aircraft:
            continue
        if swap_or_dump and r.get('swap_or_dump') != swap_or_dump:
            continue
        if qualification:
            qr = (r.get('qualification_required') or '').lower()
            if qualification.lower() not in qr:
                continue
        out.append(r)
    out.sort(key=lambda x: (
        str(x.get('tour_start_date') or ''),
        # Neueste created_at zuerst → invertiert sortieren
        # Wir nutzen Negativ-Trick auf String nicht — stattdessen reverse sort
        # in einem zweiten Pass; einfacher Lösung: tuple key mit primary asc
        # + secondary desc via Negation des Timestamps wenn parsebar.
    ))
    # Secondary sort: created_at desc innerhalb des gleichen tour_start_date.
    # In-place stable sort — wir sortieren NACH dem Primary in einem
    # secondary stable pass.
    out.sort(key=lambda x: str(x.get('created_at') or ''), reverse=True)
    out.sort(key=lambda x: str(x.get('tour_start_date') or ''))
    return out[offset:offset + limit]


def _list_my_posts(token):
    sb_rows = _sb_list_my_posts(token)
    if sb_rows is not None:
        return sb_rows
    disk = _load_disk_posts()
    return [r for r in disk
            if isinstance(r, dict)
            and r.get('author_token') == token
            and not r.get('deleted')]


def _persist_interest(row, author_token):
    """SB-Insert + Disk-Spiegel an Author-Token-Datei.

    Gibt (sb_ok, disk_ok) zurück — das echte SB-Insert-Resultat wird NICHT mehr
    ignoriert. Bei SB-up-aber-rejected liegt das Interesse nur auf Disk; das wird
    sichtbar gemacht und beim nächsten Read (_list_incoming_interests) reconciled."""
    sb_ok = _sb_insert_interest(row)
    interests = _load_disk_interests(author_token)
    interests.append(row)
    disk_ok = _save_disk_interests(author_token, interests)
    return sb_ok, disk_ok


def _sb_interest_exists(interest_id):
    """True wenn das Interesse bereits in SB liegt. None bei SB-down."""
    if not interest_id:
        return None
    sb, available = _get_sb()
    if not available or sb is None:
        return None
    try:
        r = (sb.table('trade_interests').select('id')
             .eq('id', interest_id).limit(1).execute())
        return bool(list(r.data or []))
    except Exception:
        return None


def _reconcile_interests_disk_to_sb(author_token):
    """Read-Time-Reconcile für die incoming-Interessen eines Authors. Disk-only-
    Interessen (SB-Outage ODER SB-Insert-rejected) werden nach SB geheilt, analog
    _reconcile_posts_disk_to_sb. Best-effort + idempotent (Existenz-Check pro ID)."""
    sb, available = _get_sb()
    if not available or sb is None:
        return
    disk = _load_disk_interests(author_token)
    if not disk:
        return
    healed = 0
    for row in disk:
        if not isinstance(row, dict):
            continue
        iid = row.get('id')
        if not iid:
            continue
        exists = _sb_interest_exists(iid)
        if exists is None:
            return  # SB mittendrin weg → abbrechen, nächster Read retry
        if exists:
            continue
        if _sb_insert_interest(row):
            healed += 1
    if healed:
        try:
            current_app.logger.info(
                f'[trade] reconcile_interests_disk_to_sb healed={healed}'
            )
        except Exception:
            pass


def _list_incoming_interests(author_token):
    """Listet Interessen auf Posts dieses Authors. SB primär, Disk-Fallback.

    Vor dem SB-Read werden Disk-only-Interessen dieses Authors nach SB geheilt,
    damit ein SB-Insert-Reject nicht still beim Restart verschwindet."""
    _reconcile_interests_disk_to_sb(author_token)
    sb_rows = _sb_list_interests_for_author(author_token)
    if sb_rows is not None:
        return sb_rows
    return _load_disk_interests(author_token)


# ─── Input-Validation Helpers ─────────────────────────────────────────

_SWAP_VALUES = ('swap', 'dump', 'pickup')
_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def _valid_token(token):
    if not isinstance(token, str):
        return False
    if not token.strip():
        return False
    return bool(re.match(r'^[A-Za-z0-9_\-]{8,128}$', token.strip()))


def _truncate(value, n):
    if not isinstance(value, str):
        return None
    v = value.strip()
    if not v:
        return None
    return v[:n]


def _canonical_airline(value):
    """Normalisiert den Airline-String auf einen kanonischen Wert, damit das
    Board AIRLINE-WEIT konsistent matcht.

    Hintergrund (2026-06-29): Crew-Profile tragen die Airline mal als
    "Lufthansa", mal als "LH"/"DLH". Speichern wir den Roh-String und filtern
    per exaktem `==`, würde ein "LH"-Author NICHT in der "Lufthansa"-Sicht eines
    Kollegen auftauchen → die airline-weite Sichtbarkeit wäre löchrig. Wir
    normalisieren deshalb SOWOHL beim Speichern (create) ALS AUCH beim Filtern
    (board) auf denselben kanonischen Wert. Lufthansa-Varianten → "Lufthansa";
    alles andere bleibt unverändert (nur getrimmt), damit andere Airlines wie
    bisher exakt matchen."""
    if not isinstance(value, str):
        return value
    v = value.strip()
    if not v:
        return None
    low = v.lower()
    if 'lufthansa' in low or low in ('lh', 'dlh'):
        return 'Lufthansa'
    return v


def _public_post(row, viewer_token=None):
    """Serialisiert einen Post für Client-Responses OHNE author_token.

    SECURITY (2026-06 Audit): author_token ist das App-weite Bearer-Token des
    Autors — es darf das Backend nie verlassen (Board ist public-read; ein
    geleaktes Token = voller Account-Takeover über alle <token>-Routen).
    Statt des Tokens liefern wir `is_mine` (Server-Vergleich gegen das vom
    Viewer mitgesendete Token) — mehr braucht der Client nicht.
    """
    item = dict(row)
    author = item.pop('author_token', None)
    item['is_mine'] = bool(viewer_token) and author == viewer_token
    return item


# ─── Routes ───────────────────────────────────────────────────────────

@trip_trade_bp.route('/api/trade/<token>/post', methods=['POST'])
def create_trade_post(token):
    """Body: {tour_start_date, tour_end_date?, routing?, swap_or_dump (default swap),
             aircraft?, compensation_offered?, qualification_required?, position?,
             base?, airline?, message?, author_short_name?}.

    Rate-Limit: 3 Posts pro Tag pro Token.
    """
    if not _valid_token(token):
        return jsonify({'ok': False, 'error': 'Ungültiges Token.'}), 400

    if _rate_limited(token, 'trade_post', limit=3, window_sec=86400):
        return jsonify({
            'ok': False,
            'error': 'Tageslimit erreicht (max. 3 Trade-Posts pro Tag).'
        }), 429

    body = request.get_json(silent=True) or {}
    tour_start = _truncate(body.get('tour_start_date'), 10)
    if not tour_start or not _DATE_RE.match(tour_start):
        return jsonify({
            'ok': False,
            'error': 'tour_start_date fehlt oder ist ungültig (Format YYYY-MM-DD).'
        }), 400

    tour_end = _truncate(body.get('tour_end_date'), 10)
    if tour_end and not _DATE_RE.match(tour_end):
        return jsonify({
            'ok': False,
            'error': 'tour_end_date ungültig (Format YYYY-MM-DD).'
        }), 400
    if tour_end and tour_end < tour_start:
        return jsonify({
            'ok': False,
            'error': 'tour_end_date darf nicht vor tour_start_date liegen.'
        }), 400

    swap_or_dump = _truncate(body.get('swap_or_dump'), 16) or 'swap'
    swap_or_dump = swap_or_dump.lower()
    if swap_or_dump not in _SWAP_VALUES:
        return jsonify({
            'ok': False,
            'error': 'swap_or_dump muss swap, dump oder pickup sein.'
        }), 400

    now_iso = datetime.now(timezone.utc).isoformat()
    post_id = uuid.uuid4().hex
    row = {
        'id': post_id,
        'author_token': token,
        'author_short_name': _truncate(body.get('author_short_name'), 64),
        'position': _truncate(body.get('position'), 32),
        'base': _truncate(body.get('base'), 8),
        # Airline kanonisch speichern (LH-Varianten → "Lufthansa"), damit das
        # Board airline-weit konsistent matcht (s. _canonical_airline).
        'airline': _canonical_airline(_truncate(body.get('airline'), 32)),
        'tour_start_date': tour_start,
        'tour_end_date': tour_end or tour_start,
        'routing': _truncate(body.get('routing'), 256),
        'swap_or_dump': swap_or_dump,
        'aircraft': _truncate(body.get('aircraft'), 16),
        'compensation_offered': _truncate(body.get('compensation_offered'), 256),
        'qualification_required': _truncate(body.get('qualification_required'), 256),
        'message': _truncate(body.get('message'), 2000),
        'status': 'open',
        'deleted': False,
        'created_at': now_iso,
        'updated_at': now_iso,
    }
    sb_ok, disk_ok = _persist_post(row)
    if not sb_ok and not disk_ok:
        current_app.logger.error(
            f'[trade] post_persist_fail id={post_id} token={token[:8]} '
            f'sb={sb_ok} disk={disk_ok}'
        )
        return jsonify({
            'ok': False,
            'error': 'Speichern fehlgeschlagen. Bitte später erneut versuchen.'
        }), 500
    current_app.logger.info(
        f'[trade] post_created id={post_id} token={token[:8]} '
        f'kind={swap_or_dump} start={tour_start} sb={sb_ok} disk={disk_ok}'
    )
    return jsonify({
        'ok': True,
        'post': _public_post(row, viewer_token=token),
        'persisted_to': {'supabase': sb_ok, 'disk': disk_ok},
    }), 200


@trip_trade_bp.route('/api/trade/board', methods=['GET'])
def list_board():
    """Query: airline, base, qualification, position, aircraft, swap_or_dump,
    limit, offset, token (optional — nur für die is_mine-Berechnung, kein
    Auth-Gate). `airline` wird kanonisch normalisiert (LH-Varianten → "Lufthansa")
    → airline-weite Sichtbarkeit. Public-Read (kein Token erforderlich) —
    sortiert nach tour_start_date asc, dann created_at desc. Responses enthalten
    NIE author_token (s. _public_post)."""
    try:
        limit = int(request.args.get('limit', '50'))
    except (TypeError, ValueError):
        limit = 50
    try:
        offset = int(request.args.get('offset', '0'))
    except (TypeError, ValueError):
        offset = 0
    limit = max(1, min(200, limit))
    offset = max(0, offset)

    # Airline kanonisch normalisieren — identisch zum Speicher-Pfad, damit alle
    # LH-Crew (egal ob "Lufthansa"/"LH"/"DLH" im Profil) dieselbe LH-weite Liste
    # sehen (airline-weite Sichtbarkeit, kein self/friends-Scope).
    airline = _canonical_airline(_truncate(request.args.get('airline'), 32))
    base = _truncate(request.args.get('base'), 8)
    qualification = _truncate(request.args.get('qualification'), 64)
    position = _truncate(request.args.get('position'), 32)
    aircraft = _truncate(request.args.get('aircraft'), 16)
    if aircraft:
        aircraft = aircraft.upper()
    swap_or_dump = _truncate(request.args.get('swap_or_dump'), 16)
    if swap_or_dump:
        swap_or_dump = swap_or_dump.lower()
        if swap_or_dump not in _SWAP_VALUES:
            return jsonify({
                'ok': False,
                'error': 'swap_or_dump muss swap, dump oder pickup sein.'
            }), 400

    viewer_token = request.args.get('token')
    if viewer_token and not _valid_token(viewer_token):
        viewer_token = None

    posts = _list_posts(airline=airline, base=base, qualification=qualification,
                        position=position, swap_or_dump=swap_or_dump,
                        aircraft=aircraft, limit=limit, offset=offset)
    # interest_count anreichern (best effort — bei SB-down ist count None)
    out = []
    for p in posts:
        item = _public_post(p, viewer_token=viewer_token)
        cnt = _sb_count_interests_for_post(item.get('id') or '')
        item['interest_count'] = cnt if cnt is not None else 0
        out.append(item)
    return jsonify({'ok': True, 'posts': out, 'count': len(out)}), 200


@trip_trade_bp.route('/api/trade/<token>/my-posts', methods=['GET'])
@trip_trade_bp.route('/api/trade/<token>/my-offers', methods=['GET'])
def my_posts(token):
    """Listet alle Posts des Tokens (any status, exklusive soft-deleted).

    `/my-offers` ist ein Alias für `/my-posts` — der iOS-Client (TripTradeBoardView
    'Meine Angebote'-Tab) rief `/my-offers`, das Backend servte nur `/my-posts`
    → 404 (2026-06 Audit). Beide Pfade zeigen jetzt auf dieselbe Funktion.
    """
    if not _valid_token(token):
        return jsonify({'ok': False, 'error': 'Ungültiges Token.'}), 400
    posts = _list_my_posts(token)
    out = []
    for p in posts:
        item = _public_post(p, viewer_token=token)
        cnt = _sb_count_interests_for_post(item.get('id') or '')
        item['interest_count'] = cnt if cnt is not None else 0
        out.append(item)
    out.sort(key=lambda x: str(x.get('created_at') or ''), reverse=True)
    return jsonify({'ok': True, 'posts': out, 'count': len(out)}), 200


@trip_trade_bp.route('/api/trade/<token>/post/<post_id>', methods=['DELETE'])
def delete_trade_post(token, post_id):
    """Soft-Delete (deleted=true). Nur durch Author möglich — sonst 404
    (kein 403 um zu vermeiden, dass Existenz fremder Posts geleakt wird)."""
    if not _valid_token(token):
        return jsonify({'ok': False, 'error': 'Ungültiges Token.'}), 400
    post = _load_post_by_id(post_id)
    if post is None:
        return jsonify({'ok': False, 'error': 'Post nicht gefunden.'}), 404
    if post.get('author_token') != token:
        # Author-only — gleicher 404 wie "nicht da" um keine Info zu leaken
        current_app.logger.info(
            f'[trade] delete_denied id={post_id} token={token[:8]} '
            f'author={(post.get("author_token") or "")[:8]}'
        )
        return jsonify({'ok': False, 'error': 'Post nicht gefunden.'}), 404
    if post.get('deleted'):
        return jsonify({'ok': True, 'already_deleted': True}), 200
    _update_post(post_id, {'deleted': True, 'status': 'closed'})
    current_app.logger.info(
        f'[trade] post_deleted id={post_id} token={token[:8]}'
    )
    return jsonify({'ok': True}), 200


@trip_trade_bp.route('/api/trade/<token>/express-interest/<post_id>',
                     methods=['POST'])
def express_interest(token, post_id):
    """Body: {message?}.

    Self-Trade-Prevention: Author kann nicht auf eigenen Post Interesse zeigen → 400.
    Author erhält Push-Notification (best effort).
    """
    if not _valid_token(token):
        return jsonify({'ok': False, 'error': 'Ungültiges Token.'}), 400
    post = _load_post_by_id(post_id)
    if post is None or post.get('deleted'):
        return jsonify({'ok': False, 'error': 'Post nicht gefunden.'}), 404
    if post.get('status') == 'closed':
        return jsonify({
            'ok': False,
            'error': 'Post ist geschlossen — keine Interessenmeldung mehr möglich.'
        }), 400

    author_token = post.get('author_token')
    if not author_token:
        return jsonify({'ok': False, 'error': 'Post hat keinen Author.'}), 500
    if author_token == token:
        return jsonify({
            'ok': False,
            'error': 'Du kannst auf deinen eigenen Post kein Interesse anmelden.'
        }), 400

    # Rate-Limit gegen Interest-Spam (20/Stunde pro Token)
    if _rate_limited(token, 'trade_interest', limit=20, window_sec=3600):
        return jsonify({
            'ok': False,
            'error': 'Zu viele Interessenmeldungen — bitte später erneut versuchen.'
        }), 429

    body = request.get_json(silent=True) or {}
    msg = _truncate(body.get('message'), 1000)

    now_iso = datetime.now(timezone.utc).isoformat()
    interest_id = uuid.uuid4().hex
    row = {
        'id': interest_id,
        'post_id': post_id,
        'interested_token': token,
        'message': msg,
        'created_at': now_iso,
    }
    sb_ok, disk_ok = _persist_interest(row, author_token)
    if not sb_ok and not disk_ok:
        current_app.logger.error(
            f'[trade] interest_persist_fail id={interest_id} post={post_id} '
            f'sb={sb_ok} disk={disk_ok}'
        )
        return jsonify({
            'ok': False,
            'error': 'Speichern fehlgeschlagen. Bitte später erneut versuchen.'
        }), 500

    # Status auf in_negotiation hochstufen (erste Interesse-Meldung)
    if post.get('status') == 'open':
        _update_post(post_id, {'status': 'in_negotiation'})

    # Push best-effort an Author
    try:
        push_body = 'Jemand hat Interesse an deinem Trade-Post.'
        if msg:
            push_body = msg[:140]
        _push(author_token, 'Neues Trade-Interesse', push_body, data={
            'type': 'trade_interest',
            'post_id': post_id,
            'interest_id': interest_id,
        })
    except Exception:
        pass

    current_app.logger.info(
        f'[trade] interest_created id={interest_id} post={post_id} '
        f'token={token[:8]} author={author_token[:8]} sb={sb_ok} disk={disk_ok}'
    )
    return jsonify({
        'ok': True,
        'interest_id': interest_id,
        'persisted_to': {'supabase': sb_ok, 'disk': disk_ok},
    }), 200


@trip_trade_bp.route('/api/trade/<token>/incoming-interests', methods=['GET'])
def incoming_interests(token):
    """Listet alle Interesse-Events auf Posts dieses Tokens."""
    if not _valid_token(token):
        return jsonify({'ok': False, 'error': 'Ungültiges Token.'}), 400
    rows = _list_incoming_interests(token) or []
    # Anreichern mit Post-Snapshot (tour_start_date, routing) damit Client
    # nicht für jeden Interest einzeln den Post laden muss.
    post_cache = {}
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        pid = r.get('post_id')
        if pid and pid not in post_cache:
            post_cache[pid] = _load_post_by_id(pid)
        post = post_cache.get(pid) if pid else None
        item = dict(r)
        # SECURITY: das volle Token des Interessenten nie an den Author
        # ausliefern — Prefix reicht für Anzeige/Disambiguierung (gleiches
        # Muster wie crew-chat author_token-Truncation).
        it = item.pop('interested_token', None)
        if isinstance(it, str) and it:
            item['interested_token'] = it[:16] + '…'
        if post:
            item['post_snapshot'] = {
                'id': post.get('id'),
                'tour_start_date': post.get('tour_start_date'),
                'tour_end_date': post.get('tour_end_date'),
                'routing': post.get('routing'),
                'swap_or_dump': post.get('swap_or_dump'),
                'status': post.get('status'),
            }
        out.append(item)
    out.sort(key=lambda x: str(x.get('created_at') or ''), reverse=True)
    return jsonify({'ok': True, 'interests': out, 'count': len(out)}), 200


@trip_trade_bp.route('/api/trade/<token>/post/<post_id>/close',
                     methods=['POST'])
def close_trade_post(token, post_id):
    """Body: {accepted_interest_id?}. Setzt status=closed. Nur Author."""
    if not _valid_token(token):
        return jsonify({'ok': False, 'error': 'Ungültiges Token.'}), 400
    post = _load_post_by_id(post_id)
    if post is None or post.get('deleted'):
        return jsonify({'ok': False, 'error': 'Post nicht gefunden.'}), 404
    if post.get('author_token') != token:
        return jsonify({'ok': False, 'error': 'Post nicht gefunden.'}), 404
    if post.get('status') == 'closed':
        return jsonify({'ok': True, 'already_closed': True}), 200

    body = request.get_json(silent=True) or {}
    accepted = _truncate(body.get('accepted_interest_id'), 64)

    fields = {'status': 'closed'}
    if accepted:
        # Wir speichern accepted_interest_id NICHT als Spalte (Schema hat
        # die nicht). Stattdessen in message hängen als Audit-Suffix.
        existing = post.get('message') or ''
        suffix = f'\n[closed-accepted:{accepted}]'
        if suffix not in existing:
            fields['message'] = (existing + suffix)[:2000]

    _update_post(post_id, fields)

    # Push an alle Interessenten "Post wurde geschlossen"
    try:
        rows = _list_incoming_interests(token) or []
        notified = set()
        for r in rows:
            if not isinstance(r, dict):
                continue
            if r.get('post_id') != post_id:
                continue
            it = r.get('interested_token')
            if not it or it in notified:
                continue
            notified.add(it)
            try:
                _push(it, 'Trade-Post geschlossen',
                      'Ein Post auf den du Interesse hattest wurde geschlossen.',
                      data={'type': 'trade_closed', 'post_id': post_id})
            except Exception:
                pass
    except Exception:
        pass

    current_app.logger.info(
        f'[trade] post_closed id={post_id} token={token[:8]} '
        f'accepted={accepted or "-"}'
    )
    return jsonify({'ok': True}), 200
