# ═══════════════════════════════════════════════════════════════
#  License-Wallet Sync Blueprint  (Worker P4, 2026-06-01)
#
#  Cross-Device-Sync für das iOS LicenseWallet-Feature. Spiegelt die
#  iOS-SwiftData-Models (LicenseItem) in Supabase damit der User die
#  gleiche Wallet auf einem zweiten Gerät / nach Reinstall wiederfindet.
#
#  Architektur (analog wall_posts / layover_reviews):
#      SB-Primary  →  user_licenses-Tabelle (Whitelist-Cols + metadata jsonb)
#      Disk-Fallback → _USER_HISTORY_DIR/licenses_<token>.json
#      Lazy-Migrate bei SB-leerem Read (einmalig wenn SB nach Outage
#      wieder erreichbar ist und Disk-Daten existieren).
#
#  Wiring in app.py:
#      from blueprints.license_wallet_blueprint import license_wallet_bp
#      app.register_blueprint(license_wallet_bp)
#
#  Endpunkte (Token kommt aus URL — app.py's @before_request matcht <token>):
#      GET    /api/license-wallet/<token>/list
#      POST   /api/license-wallet/<token>/upsert       body = item-dict
#      DELETE /api/license-wallet/<token>/<item_id>
#      POST   /api/license-wallet/<token>/bulk-sync    body = {items: [...]}
#
#  Privacy:
#      Das Foto wird AES-GCM-verschlüsselt nur auf dem Device gehalten.
#      Der Server bekommt nur `photo_blob_id` (Referenz, derzeit
#      nicht vergeben) — KEIN Klartext-Bild und KEIN Cipher-Blob.
#      Spätere Server-Side-Photo-Storage würde verschlüsselt bleiben.
# ═══════════════════════════════════════════════════════════════

import os
import re
import json
import logging
import datetime as _dt
from flask import Blueprint, request, jsonify, current_app

license_wallet_bp = Blueprint('license_wallet', __name__)

# ─── Module-globals aus app.py importieren ─────────────────────
# Wrap in try/except, damit Unit-Tests ohne voll geladenes app.py
# importieren können (z.B. wenn tests/test_calculation.py den
# Blueprint per Import-Spy lädt).
try:
    from app import SB_AVAILABLE, sb, _USER_HISTORY_DIR, _atomic_write_json  # noqa: F401
except Exception:  # pragma: no cover
    SB_AVAILABLE = False
    sb = None
    _USER_HISTORY_DIR = '_user_history_state'

    def _atomic_write_json(path, data, max_items=None, **json_kwargs):
        """Notfall-Fallback wenn app.py noch nicht importiert ist (Unit-Test)."""
        json_kwargs.setdefault('ensure_ascii', False)
        target_dir = os.path.dirname(path) or '.'
        os.makedirs(target_dir, exist_ok=True)
        with open(path, 'w') as f:
            json.dump(data, f, **json_kwargs)


# ─── Schema / Whitelist ────────────────────────────────────────
# Spiegelt die Spalten in supabase_migrations/20260601_license_wallet.sql.
# Felder die ein Client schickt aber nicht in der Whitelist sind, landen
# in metadata jsonb (z.B. iOS-lokale Felder wie photoCipher-Hash etc.).
_LICENSE_KNOWN_COLS = {
    'id',
    'user_token',
    'category',
    'item_type',
    'label',
    'issue_date',
    'expiry_date',
    'issuing_authority',
    'document_number',
    'photo_blob_id',
    'custom_notes',
    'alert_window_days',
    'deleted',
}

_VALID_CATEGORIES = {'cockpit', 'cabin', 'general'}


# ─── Logger-Helper ─────────────────────────────────────────────
def _log():
    """Gibt den Flask-app.logger zurück wenn verfügbar, sonst Modul-Logger.
    Defensiv für den Unit-Test-Pfad wo `current_app` fehlen kann."""
    try:
        return current_app.logger
    except RuntimeError:
        return logging.getLogger('license_wallet')


# ─── Token-Sanitization ────────────────────────────────────────
def _safe_token(token):
    """Whitelist a-z A-Z 0-9 _ - · cap auf 64 Zeichen. Identisch zum
    Pattern in app.py's _user_history_path."""
    if not token or not isinstance(token, str):
        return None
    safe = re.sub(r'[^A-Za-z0-9_-]', '', token)[:64]
    return safe or None


def _disk_path(token):
    """Pfad zur Disk-Fallback-Datei für einen User-Token."""
    safe = _safe_token(token)
    if not safe:
        return None
    try:
        os.makedirs(_USER_HISTORY_DIR, exist_ok=True)
    except Exception:
        pass
    return os.path.join(_USER_HISTORY_DIR, f'licenses_{safe}.json')


# ─── Item-Normalisierung ───────────────────────────────────────
def _normalize_item(raw, token):
    """Bringt ein eingehendes Item-Dict (vom iOS-Client) auf das Server-
    Schema. Unbekannte Felder landen in metadata. Pflicht-Felder (id,
    category, item_type) werden geprüft — fehlende → None-Return.

    iOS schickt typischerweise camelCase ODER snake_case je nach JSON-
    Encoder-Config. Wir akzeptieren beide Schreibweisen.
    """
    if not isinstance(raw, dict):
        return None

    # Camel/Snake-Aliase (iOS encoder kann beide Stile)
    def _pick(*keys):
        for k in keys:
            if k in raw and raw[k] is not None:
                return raw[k]
        return None

    item_id = _pick('id')
    category = _pick('category')
    item_type = _pick('item_type', 'itemType')

    if not item_id or not category or not item_type:
        return None

    cat_lower = str(category).strip().lower()
    if cat_lower not in _VALID_CATEGORIES:
        return None

    alert_days = _pick('alert_window_days', 'alertWindowDays')
    if not isinstance(alert_days, list):
        alert_days = [90, 60, 30, 7]
    else:
        try:
            alert_days = [int(x) for x in alert_days if int(x) > 0]
        except (TypeError, ValueError):
            alert_days = [90, 60, 30, 7]
        if not alert_days:
            alert_days = [90, 60, 30, 7]

    deleted_flag = bool(_pick('deleted'))

    # ISO-8601 → date-string. iOS schickt "2027-08-15T00:00:00Z" oder
    # "2027-08-15". Wir extrahieren die Datums-Komponente und vertrauen
    # dem Postgres-Date-Parser.
    def _iso_date(v):
        if v is None or v == '':
            return None
        s = str(v).strip()
        if not s:
            return None
        # date-only erkennen
        if re.match(r'^\d{4}-\d{2}-\d{2}$', s):
            return s
        # ISO mit Zeit → split bei T
        if 'T' in s:
            head = s.split('T', 1)[0]
            if re.match(r'^\d{4}-\d{2}-\d{2}$', head):
                return head
        return None

    normalized = {
        'id': str(item_id),
        'user_token': token,
        'category': cat_lower,
        'item_type': str(item_type),
        'label': _pick('label'),
        'issue_date': _iso_date(_pick('issue_date', 'issueDate')),
        'expiry_date': _iso_date(_pick('expiry_date', 'expiryDate')),
        'issuing_authority': _pick('issuing_authority', 'issuingAuthority'),
        'document_number': _pick('document_number', 'documentNumber'),
        'photo_blob_id': _pick('photo_blob_id', 'photoBlobId'),
        'custom_notes': _pick('custom_notes', 'customNotes'),
        'alert_window_days': alert_days,
        'deleted': deleted_flag,
    }

    # Felder die NICHT in der Whitelist sind landen in metadata.
    # Wir blacklisten bekannte iOS-only Felder (photoCipher, photoData)
    # damit nie versehentlich Cipher-Bytes oder Klartext-Bilder im
    # Server-Storage landen.
    _NEVER_PERSIST = {
        'photoCipher', 'photo_cipher',
        'photoData', 'photo_data',
        'updatedAt', 'updated_at', 'createdAt', 'created_at',
    }
    metadata = {}
    for k, v in raw.items():
        if k in _NEVER_PERSIST:
            continue
        # Schon in der Hauptcolumn (in beiden Schreibweisen) abgehandelt?
        snake = re.sub(r'(?<!^)(?=[A-Z])', '_', k).lower()
        if snake in _LICENSE_KNOWN_COLS or k in _LICENSE_KNOWN_COLS:
            continue
        if snake in ('item_type', 'alert_window_days', 'issue_date',
                     'expiry_date', 'issuing_authority',
                     'document_number', 'photo_blob_id', 'custom_notes'):
            continue
        if v is None:
            continue
        metadata[k] = v
    normalized['metadata'] = metadata

    return normalized


def _denormalize_for_client(row):
    """SB- oder Disk-Row → iOS-Client-Dict mit camelCase-Aliasen.
    Wir liefern BEIDE Schreibweisen (snake + camel) zurück damit alte
    iOS-Build-Versionen die auf snake_case dekodieren weiter funktionieren."""
    if not isinstance(row, dict):
        return {}
    out = {}
    for k in _LICENSE_KNOWN_COLS:
        v = row.get(k)
        if v is not None:
            out[k] = v
    # Date-fields als ISO-strings sicherstellen (SB liefert je nach Adapter
    # entweder str oder datetime.date).
    for dk in ('issue_date', 'expiry_date'):
        v = out.get(dk)
        if isinstance(v, (_dt.date, _dt.datetime)):
            out[dk] = v.isoformat()[:10]
    # camelCase-Aliase fürs iOS-decoding-Bequemlichkeit
    if 'item_type' in out:
        out['itemType'] = out['item_type']
    if 'issue_date' in out:
        out['issueDate'] = out['issue_date']
    if 'expiry_date' in out:
        out['expiryDate'] = out['expiry_date']
    if 'issuing_authority' in out:
        out['issuingAuthority'] = out['issuing_authority']
    if 'document_number' in out:
        out['documentNumber'] = out['document_number']
    if 'photo_blob_id' in out:
        out['photoBlobId'] = out['photo_blob_id']
    if 'custom_notes' in out:
        out['customNotes'] = out['custom_notes']
    if 'alert_window_days' in out:
        out['alertWindowDays'] = out['alert_window_days']
    # metadata-Felder werden in den Top-Level zurückgefaltet (analog
    # wall_posts) — damit beliebige iOS-Side-Felder roundtrippen.
    md = row.get('metadata') or {}
    if isinstance(md, dict):
        for k, v in md.items():
            if k not in out:
                out[k] = v
    # updated_at / created_at als ISO-Strings durchreichen, falls vorhanden.
    for tk in ('created_at', 'updated_at'):
        if row.get(tk) is not None:
            v = row[tk]
            if isinstance(v, (_dt.date, _dt.datetime)):
                out[tk] = v.isoformat()
            else:
                out[tk] = str(v)
    return out


# ─── SB-Layer ──────────────────────────────────────────────────
def _sb_load_items(token):
    """Liest alle (nicht-gelöschten) Items eines Users aus SB.
    None bei SB-down/error — Caller fällt dann auf Disk zurück.
    Leere Liste wenn SB OK aber keine Daten."""
    if not SB_AVAILABLE or sb is None:
        return None
    try:
        out = []
        offset = 0
        page = 500
        while True:
            r = (sb.table('user_licenses')
                 .select('*')
                 .eq('user_token', token)
                 .eq('deleted', False)
                 .order('expiry_date', desc=False)
                 .range(offset, offset + page - 1)
                 .execute())
            rows = r.data or []
            for row in rows:
                out.append(_denormalize_for_client(row))
            if len(rows) < page:
                break
            offset += page
        return out
    except Exception as e:
        _log().warning(
            f'[license-wallet] sb_load_fail token={token[:8]}… '
            f'err={type(e).__name__}: {str(e)[:120]}'
        )
        return None


def _sb_upsert_item(normalized):
    """Schreibt 1 Item nach SB (Upsert auf id). True/False."""
    if not SB_AVAILABLE or sb is None or not normalized:
        return False
    row = dict(normalized)
    row['updated_at'] = _dt.datetime.now(_dt.timezone.utc).isoformat()
    try:
        sb.table('user_licenses').upsert(row, on_conflict='id').execute()
        return True
    except Exception as e:
        _log().error(
            f'[license-wallet] sb_upsert_fail id={row.get("id", "?")[:12]} '
            f'err={type(e).__name__}: {str(e)[:200]}'
        )
        return False


def _sb_bulk_upsert(items):
    """Bulk-upsert in 200er-Batches. True nur wenn ALLE Batches durchgehen."""
    if not SB_AVAILABLE or sb is None or not items:
        return False
    now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
    rows = []
    for it in items:
        if not it:
            continue
        r = dict(it)
        r['updated_at'] = now_iso
        rows.append(r)
    if not rows:
        return True
    try:
        for i in range(0, len(rows), 200):
            sb.table('user_licenses').upsert(rows[i:i + 200], on_conflict='id').execute()
        return True
    except Exception as e:
        _log().error(
            f'[license-wallet] sb_bulk_fail count={len(rows)} '
            f'err={type(e).__name__}: {str(e)[:200]}'
        )
        return False


def _sb_soft_delete(item_id, token):
    """Soft-Delete = deleted=true setzen. True/False.
    Wir matchen auf id+user_token damit ein Client nicht versehentlich
    fremde Item-IDs löschen kann (defense-in-depth zusätzlich zu RLS)."""
    if not SB_AVAILABLE or sb is None or not item_id:
        return False
    try:
        now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
        (sb.table('user_licenses')
         .update({'deleted': True, 'updated_at': now_iso})
         .eq('id', item_id)
         .eq('user_token', token)
         .execute())
        return True
    except Exception as e:
        _log().error(
            f'[license-wallet] sb_del_fail id={item_id[:12]} '
            f'err={type(e).__name__}: {str(e)[:200]}'
        )
        return False


# ─── Disk-Layer (Fallback) ─────────────────────────────────────
def _disk_load_items(token):
    """Liest die Disk-Fallback-Liste. Filtert deleted=true raus.
    Returns immer eine Liste (leer wenn File fehlt oder kaputt)."""
    p = _disk_path(token)
    if not p:
        return []
    try:
        with open(p) as f:
            data = json.load(f) or []
        if not isinstance(data, list):
            return []
        # Filter deleted, denormalize für client-shape
        out = []
        for row in data:
            if not isinstance(row, dict):
                continue
            if row.get('deleted'):
                continue
            out.append(_denormalize_for_client(row))
        return out
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    except Exception as e:
        _log().warning(
            f'[license-wallet] disk_load_fail err={type(e).__name__}: {str(e)[:120]}'
        )
        return []


def _disk_load_raw(token):
    """Wie _disk_load_items aber OHNE deleted-Filter und OHNE Denormalisierung
    — wird für Upsert-Merging gebraucht."""
    p = _disk_path(token)
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


def _disk_save_raw(token, rows):
    """Atomarer Replace-Write der gesamten Item-Liste eines Users."""
    p = _disk_path(token)
    if not p:
        return False
    try:
        _atomic_write_json(p, rows, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        _log().error(
            f'[license-wallet] disk_save_fail err={type(e).__name__}: {str(e)[:200]}'
        )
        return False


def _disk_upsert(token, normalized):
    """Single-Item-Upsert auf Disk — ersetzt die Row mit gleicher id."""
    rows = _disk_load_raw(token)
    now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
    new_row = dict(normalized)
    new_row['updated_at'] = now_iso
    # createdAt nur setzen wenn neuer Eintrag
    existing_idx = None
    for i, r in enumerate(rows):
        if isinstance(r, dict) and r.get('id') == new_row['id']:
            existing_idx = i
            break
    if existing_idx is None:
        new_row.setdefault('created_at', now_iso)
        rows.append(new_row)
    else:
        old = rows[existing_idx]
        new_row['created_at'] = old.get('created_at', now_iso) if isinstance(old, dict) else now_iso
        rows[existing_idx] = new_row
    return _disk_save_raw(token, rows)


def _disk_soft_delete(item_id, token):
    rows = _disk_load_raw(token)
    if not rows:
        return False
    changed = False
    now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
    for r in rows:
        if isinstance(r, dict) and r.get('id') == item_id and r.get('user_token') == token:
            r['deleted'] = True
            r['updated_at'] = now_iso
            changed = True
            break
    if not changed:
        return False
    return _disk_save_raw(token, rows)


# ─── Routes ────────────────────────────────────────────────────

@license_wallet_bp.route('/api/license-wallet/<token>/list', methods=['GET'])
def list_licenses(token):
    """Listet alle aktiven (nicht-gelöschten) Lizenz-Items des Users.
    SB primär, Disk-Fallback. Bei SB-leer + Disk vorhanden → lazy-migrate."""
    safe = _safe_token(token)
    if not safe:
        return jsonify({
            'ok': False,
            'error': 'Ungültiger Token. Bitte logge dich erneut ein.',
        }), 400

    sb_items = _sb_load_items(safe)
    if sb_items is not None:
        # SB ok. Wenn SB leer ist, prüfen wir die Disk auf alte Daten
        # und migrieren einmalig hoch.
        if not sb_items:
            disk_items_raw = _disk_load_raw(safe)
            if disk_items_raw:
                _log().info(
                    f'[license-wallet] lazy-migrate token={safe[:8]}… '
                    f'count={len(disk_items_raw)}'
                )
                ok = _sb_bulk_upsert([r for r in disk_items_raw if isinstance(r, dict)])
                if ok:
                    # Re-read damit der Client den kanonischen SB-State sieht
                    sb_items = _sb_load_items(safe) or []
                else:
                    # SB-Migrate fehlgeschlagen → wenigstens die Disk-Items
                    # ausliefern (deleted-gefiltert + denormalisiert)
                    sb_items = _disk_load_items(safe)
        return jsonify({'ok': True, 'items': sb_items, 'source': 'supabase'}), 200

    # SB down → Disk-only
    disk_items = _disk_load_items(safe)
    return jsonify({
        'ok': True,
        'items': disk_items,
        'source': 'disk',
        'warning': 'Synchronisierung derzeit nicht möglich — '
                   'lokale Kopie wird angezeigt.',
    }), 200


@license_wallet_bp.route('/api/license-wallet/<token>/upsert', methods=['POST'])
def upsert_license(token):
    """Upsert eines einzelnen Items. Body = item-dict.
    Strategie: SB-Schreibversuch zuerst, Disk-Mirror immer (damit ein
    späterer SB-Outage trotzdem Read-Fallback hat).
    """
    safe = _safe_token(token)
    if not safe:
        return jsonify({
            'ok': False,
            'error': 'Ungültiger Token. Bitte logge dich erneut ein.',
        }), 400

    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({
            'ok': False,
            'error': 'Ungültiger Request-Body. Erwartet wird ein JSON-Objekt.',
        }), 400

    normalized = _normalize_item(body, safe)
    if normalized is None:
        return jsonify({
            'ok': False,
            'error': 'Pflichtfelder fehlen oder Kategorie ist ungültig. '
                     'Bitte prüfe id, category (cockpit/cabin/general) und '
                     'item_type.',
        }), 400

    sb_ok = _sb_upsert_item(normalized)
    disk_ok = _disk_upsert(safe, normalized)

    if not sb_ok and not disk_ok:
        return jsonify({
            'ok': False,
            'error': 'Speichern fehlgeschlagen. Bitte später erneut '
                     'versuchen.',
        }), 500

    return jsonify({
        'ok': True,
        'id': normalized['id'],
        'persisted_to': {
            'supabase': sb_ok,
            'disk': disk_ok,
        },
    }), 200


@license_wallet_bp.route('/api/license-wallet/<token>/<item_id>', methods=['DELETE'])
def delete_license(token, item_id):
    """Soft-Delete: setzt deleted=true. Item bleibt physisch erhalten für
    Sync-Konflikt-Auflösung; spätere Cleanup-Jobs können hart purgen."""
    safe = _safe_token(token)
    if not safe:
        return jsonify({
            'ok': False,
            'error': 'Ungültiger Token. Bitte logge dich erneut ein.',
        }), 400

    if not item_id or not isinstance(item_id, str) or len(item_id) > 64:
        return jsonify({
            'ok': False,
            'error': 'Ungültige Item-ID.',
        }), 400

    sb_ok = _sb_soft_delete(item_id, safe)
    disk_ok = _disk_soft_delete(item_id, safe)

    if not sb_ok and not disk_ok:
        return jsonify({
            'ok': False,
            'error': 'Löschen fehlgeschlagen. Eintrag wurde nicht gefunden '
                     'oder Speicher ist derzeit nicht erreichbar.',
        }), 500

    return jsonify({
        'ok': True,
        'id': item_id,
        'deleted_in': {
            'supabase': sb_ok,
            'disk': disk_ok,
        },
    }), 200


@license_wallet_bp.route('/api/license-wallet/<token>/bulk-sync', methods=['POST'])
def bulk_sync(token):
    """Bulk-Upsert für den initialen Sync nach App-Install oder
    Multi-Device-Pairing. Body = {items: [item-dict, ...]}.

    Wir akzeptieren bis zu 500 Items pro Call. Wenn ein Item das Schema
    verletzt, wird es übersprungen — die Anzahl der akzeptierten/
    rejected items kommt in der Response.
    """
    safe = _safe_token(token)
    if not safe:
        return jsonify({
            'ok': False,
            'error': 'Ungültiger Token. Bitte logge dich erneut ein.',
        }), 400

    body = request.get_json(silent=True) or {}
    items_raw = body.get('items')
    if not isinstance(items_raw, list):
        return jsonify({
            'ok': False,
            'error': 'Ungültiger Request-Body. Erwartet wird {"items": [...]}.',
        }), 400

    if len(items_raw) > 500:
        return jsonify({
            'ok': False,
            'error': 'Zu viele Einträge auf einmal — bitte max. 500 Items '
                     'pro Bulk-Sync senden.',
        }), 413

    accepted = []
    rejected = 0
    for raw in items_raw:
        n = _normalize_item(raw, safe)
        if n is None:
            rejected += 1
            continue
        accepted.append(n)

    if not accepted:
        return jsonify({
            'ok': False,
            'error': 'Keine gültigen Einträge im Bulk-Sync. Bitte prüfe '
                     'das Datenformat (id, category, item_type erforderlich).',
            'rejected': rejected,
        }), 400

    sb_ok = _sb_bulk_upsert(accepted)

    # Disk-Mirror: existierende Disk-Daten mit Bulk mergen (per id ersetzen)
    disk_rows = _disk_load_raw(safe)
    disk_by_id = {r.get('id'): r for r in disk_rows if isinstance(r, dict) and r.get('id')}
    now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
    for n in accepted:
        merged = dict(n)
        merged['updated_at'] = now_iso
        old = disk_by_id.get(n['id'])
        if isinstance(old, dict) and old.get('created_at'):
            merged['created_at'] = old['created_at']
        else:
            merged['created_at'] = now_iso
        disk_by_id[n['id']] = merged
    disk_ok = _disk_save_raw(safe, list(disk_by_id.values()))

    if not sb_ok and not disk_ok:
        return jsonify({
            'ok': False,
            'error': 'Bulk-Sync fehlgeschlagen. Bitte später erneut '
                     'versuchen.',
            'rejected': rejected,
        }), 500

    _log().info(
        f'[license-wallet] bulk-sync token={safe[:8]}… '
        f'accepted={len(accepted)} rejected={rejected} '
        f'sb={sb_ok} disk={disk_ok}'
    )

    return jsonify({
        'ok': True,
        'accepted': len(accepted),
        'rejected': rejected,
        'persisted_to': {
            'supabase': sb_ok,
            'disk': disk_ok,
        },
    }), 200
